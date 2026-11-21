import os
import hmac
import json
import time
import hashlib
import asyncio
from contextlib import asynccontextmanager
from typing import Dict, Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

try:
    from dotenv import load_dotenv
    load_dotenv()  # بيقرأ ملف .env الموجود بنفس مجلد main.py تلقائياً (للتجربة المحلية فقط)
except ImportError:
    pass  # إذا ما كانت المكتبة مثبتة، بيكمل الكود عادي ويقرأ من environment variables الحقيقية

# ================== الإعدادات (حطها كـ Environment Variables على السيرفر) ==================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0"))
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# بيانات NOWPayments (من الداشبورد بتاعك على nowpayments.io)
NOWPAYMENTS_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_BASE_URL = "https://api.nowpayments.io/v1"

# رابط موقعك (لازم يكون https ومتاح من برا، تيجي NOWPayments تحطه كـ IPN callback)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://your-domain.com")

ORDER_TTL_SECONDS = 3600 * 6  # الاحتفاظ بالطلب بالذاكرة 6 ساعات

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


# ================== Keep-Alive (لمنع نوم خطة Render المجانية) ==================
# ⚠️ ملاحظة: هاد بيمنع "الكولد ستارت" بس، وما بيلغي سقف الساعات الشهري لخطة Render المجانية.
# لو بدك توقفه، خلي KEEP_ALIVE_ENABLED=false بمتغيرات البيئة.
KEEP_ALIVE_ENABLED = os.environ.get("KEEP_ALIVE_ENABLED", "true").lower() == "true"
KEEP_ALIVE_INTERVAL_SECONDS = int(os.environ.get("KEEP_ALIVE_INTERVAL_SECONDS", "600"))  # كل 10 دقايق


async def keep_alive_ping() -> None:
    assert http_client is not None
    # ننتظر شوي أول تشغيل حتى السيرفر يخلص يقوم كامل
    await asyncio.sleep(30)
    while True:
        try:
            if PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith("http"):
                await http_client.get(f"{PUBLIC_BASE_URL}/", timeout=10.0)
        except Exception as e:
            print(f"Keep-alive ping failed: {e}")
        await asyncio.sleep(KEEP_ALIVE_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    cleanup_task = asyncio.create_task(cleanup_expired_orders())
    keep_alive_task = asyncio.create_task(keep_alive_ping()) if KEEP_ALIVE_ENABLED else None
    try:
        yield
    finally:
        if keep_alive_task:
            keep_alive_task.cancel()
        cleanup_task.cancel()
        if http_client:
            await http_client.aclose()


app = FastAPI(title="Jawaker Store - NOWPayments Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # الأفضل تحددها بدومين موقعك بالإنتاج
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================== Telegram ==================
async def telegram_post(method: str, json_data: dict) -> None:
    assert http_client is not None
    resp = await http_client.post(f"{TELEGRAM_API}/{method}", json=json_data)
    resp.raise_for_status()


def build_admin_message(order_id: str, order: dict, status_emoji: str, status_text: str) -> str:
    return (
        f"{status_emoji} طلب شحن - {status_text}\n"
        "------------------------------------\n"
        f"رقم الطلب: {order_id}\n"
        f"الفئة: {order.get('category_name', '-')}\n"
        f"الكمية: {order.get('amount', '-')}\n"
        f"السعر: {order.get('price_amount', '-')} {order.get('price_currency', '-')}\n"
        "------------------------------------\n"
        f"🆔 ID اللاعب: {order.get('player_id', '-')}\n"
        f"💰 عملة الدفع: {order.get('pay_currency', '-')}\n"
    )


# ================== NOWPayments helpers ==================
async def nowpayments_create_invoice(order_id: str, price_amount: float, price_currency: str,
                                      order_description: str) -> dict:
    assert http_client is not None
    headers = {
        "x-api-key": NOWPAYMENTS_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "price_amount": price_amount,
        "price_currency": price_currency,          # مثلاً "usd"
        "order_id": order_id,
        "order_description": order_description,
        "ipn_callback_url": f"{PUBLIC_BASE_URL}/webhook/nowpayments",
        "success_url": f"{PUBLIC_BASE_URL}/payment/success",
        "cancel_url": f"{PUBLIC_BASE_URL}/payment/cancel",
        # is_fixed_rate / is_fee_paid_by_user اختياري حسب كيف بدك تحسب العمولة
    }
    resp = await http_client.post(f"{NOWPAYMENTS_BASE_URL}/invoice", json=payload, headers=headers)
    resp.raise_for_status()
    return resp.json()


def verify_nowpayments_signature(raw_body: bytes, signature: str) -> bool:
    """
    NOWPayments بتوقع الـ IPN بتوقيع HMAC-SHA512 على الـ JSON بعد ترتيب الـ keys أبجدياً.
    لازم تتحقق منه قبل ما تثق بأي بيانات جايّة بالـ webhook.
    """
    if not signature or not NOWPAYMENTS_IPN_SECRET:
        return False
    try:
        data = json.loads(raw_body)
    except Exception:
        return False

    sorted_payload = json.dumps(data, separators=(",", ":"), sort_keys=True)
    expected_sig = hmac.new(
        NOWPAYMENTS_IPN_SECRET.encode("utf-8"),
        sorted_payload.encode("utf-8"),
        hashlib.sha512,
    ).hexdigest()

    return hmac.compare_digest(expected_sig, signature)


# ================== Endpoints ==================
@app.get("/")
async def health():
    return {"status": "running"}


@app.post("/api/order")
async def create_order(request: Request):
    """
    بيستقبل من موقعك: الفئة، الكمية، السعر، ومعرف اللاعب (الـID)
    وبيرجع رابط دفع (invoice_url) تحول المستخدم عليه ليدفع بفيزا أو كريبتو مباشرة عند NOWPayments.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    player_id = payload.get("player_id") or payload.get("id")
    category_name = payload.get("category_name") or payload.get("name")
    amount = payload.get("amount")
    price_amount = payload.get("price_amount") or payload.get("price")
    price_currency = (payload.get("price_currency") or "usd").lower()

    if not player_id or not category_name or price_amount is None:
        raise HTTPException(status_code=400, detail="missing_required_fields")

    try:
        price_amount = float(price_amount)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="invalid_price")

    # مرّر order_id خاص فينا نربطه بالطلب لما يوصل الـ webhook
    order_id = f"ord_{int(time.time())}_{player_id}"

    async with orders_lock:
        orders[order_id] = {
            "status": "pending",
            "created_at": time.time(),
            "player_id": player_id,
            "category_name": category_name,
            "amount": amount,
            "price_amount": price_amount,
            "price_currency": price_currency,
            "pay_currency": None,
        }

    try:
        invoice = await nowpayments_create_invoice(
            order_id=order_id,
            price_amount=price_amount,
            price_currency=price_currency,
            order_description=f"{category_name} x{amount} - player {player_id}",
        )
    except httpx.HTTPError as e:
        async with orders_lock:
            orders.pop(order_id, None)
        print(f"NOWPayments create_invoice error: {e}")
        return JSONResponse(status_code=502, content={"error": "payment_gateway_error"})

    invoice_url = invoice.get("invoice_url")
    if not invoice_url:
        async with orders_lock:
            orders.pop(order_id, None)
        return JSONResponse(status_code=502, content={"error": "no_invoice_url_returned"})

    return {"order_id": order_id, "payment_url": invoice_url}


@app.get("/api/order/{order_id}")
async def get_order_status(order_id: str):
    async with orders_lock:
        order = orders.get(order_id)
    if not order:
        raise HTTPException(status_code=404, detail="not_found")
    return {"status": order["status"]}


@app.post("/webhook/nowpayments")
async def nowpayments_webhook(request: Request, x_nowpayments_sig: str = Header(default=None)):
    """
    هاد الـ endpoint هو يلي بتستدعيه NOWPayments لما تتغير حالة الدفع.
    لازم نتحقق من التوقيع قبل أي شي، وإلا أي حدا فيه يبعتلنا طلبات وهمية.
    """
    raw_body = await request.body()

    if not verify_nowpayments_signature(raw_body, x_nowpayments_sig or ""):
        raise HTTPException(status_code=401, detail="invalid_signature")

    try:
        data = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_payload")

    order_id = data.get("order_id")
    payment_status = data.get("payment_status")  # waiting / confirming / confirmed / finished / failed / expired / refunded
    pay_currency = data.get("pay_currency")

    if not order_id:
        return {"ok": True}

    async with orders_lock:
        order = orders.get(order_id)
        if order:
            order["pay_currency"] = pay_currency

    if payment_status == "finished" and order:
        async with orders_lock:
            already_notified = order["status"] == "approved"
            order["status"] = "approved"

        if not already_notified:
            text = build_admin_message(order_id, order, "✅", "تم الدفع بنجاح")
            try:
                await telegram_post("sendMessage", {"chat_id": ADMIN_CHAT_ID, "text": text})
            except httpx.HTTPError as e:
                print(f"Telegram notify error: {e}")

    elif payment_status in ("failed", "expired", "refunded") and order:
        async with orders_lock:
            order["status"] = "rejected"

    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        reload=False,
    )
