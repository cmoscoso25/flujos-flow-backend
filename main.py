import os
import re
import uuid
import hmac
import hashlib
import sqlite3
from datetime import datetime
from urllib.parse import urlencode
from email.message import EmailMessage
import smtplib

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, Request, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, FileResponse

load_dotenv()

app = FastAPI(title="flow-backend")

# =========================
# Config
# =========================
FLOW_API_URL = os.getenv("FLOW_API_URL", "https://sandbox.flow.cl/api").rstrip("/")
FLOW_API_KEY = os.getenv("FLOW_API_KEY", "")
FLOW_SECRET_KEY = os.getenv("FLOW_SECRET_KEY", "")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DOWNLOAD_BASE_URL = os.getenv("DOWNLOAD_BASE_URL", PUBLIC_BASE_URL).rstrip("/")

PRODUCT_FILE = os.getenv("PRODUCT_FILE", "products/pack_ia_pymes_2026.zip")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", SMTP_USER)

DB_PATH = "orders.db"


# =========================
# Utils
# =========================
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(email: str) -> bool:
    return bool(email and EMAIL_REGEX.match(email))


# =========================
# DB (simple)
# =========================
def db_init():
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                email TEXT NOT NULL,
                commerce_order TEXT NOT NULL,
                flow_token TEXT NOT NULL,
                status INTEGER NOT NULL,
                download_token TEXT,
                paid_at TEXT
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_flow_token ON orders(flow_token)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_download_token ON orders(download_token)")
        con.commit()


db_init()


def db_create_order(order_id: str, email: str, commerce_order: str, flow_token: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            INSERT INTO orders (id, created_at, email, commerce_order, flow_token, status)
            VALUES (?,?,?,?,?,?)
            """,
            (order_id, datetime.utcnow().isoformat(), email, commerce_order, flow_token, 1)
        )
        con.commit()


def db_get_by_flow_token(flow_token: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, email, status, download_token FROM orders WHERE flow_token=?",
            (flow_token,)
        )
        return cur.fetchone()


def db_mark_paid(flow_token: str, download_token: str):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            UPDATE orders
            SET status=2, download_token=?, paid_at=?
            WHERE flow_token=? AND (status IS NULL OR status != 2 OR download_token IS NULL)
            """,
            (download_token, datetime.utcnow().isoformat(), flow_token)
        )
        con.commit()


def db_get_by_download_token(download_token: str):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT email, status, flow_token FROM orders WHERE download_token=?",
            (download_token,)
        )
        return cur.fetchone()


# =========================
# Flow signing (official)
# sort keys asc, concat key+value, HMAC SHA256 hex
# =========================
def flow_sign(params: dict, secret_key: str) -> str:
    items = sorted(params.items(), key=lambda x: x[0])
    to_sign = "".join([f"{k}{v}" for k, v in items])
    return hmac.new(secret_key.encode(), to_sign.encode(), hashlib.sha256).hexdigest()


def flow_post(endpoint: str, params: dict) -> dict:
    if not FLOW_API_KEY or not FLOW_SECRET_KEY:
        raise HTTPException(status_code=500, detail="FLOW_API_KEY / FLOW_SECRET_KEY no configuradas en .env")

    params = dict(params)
    params["apiKey"] = FLOW_API_KEY
    params["s"] = flow_sign(params, FLOW_SECRET_KEY)

    encoded = urlencode(params)
    url = f"{FLOW_API_URL}{endpoint}"
    resp = requests.post(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Flow error: {resp.text}")

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=400, detail=f"Flow response no-JSON: {resp.text}")


def flow_get_status(token: str) -> dict:
    params = {"apiKey": FLOW_API_KEY, "token": token}
    params["s"] = flow_sign(params, FLOW_SECRET_KEY)

    url = f"{FLOW_API_URL}/payment/getStatus"
    resp = requests.get(url, params=params, timeout=20)
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Flow status error: {resp.text}")

    try:
        return resp.json()
    except Exception:
        raise HTTPException(status_code=400, detail=f"Flow status response no-JSON: {resp.text}")


# =========================
# Email (optional)
# =========================
def send_email(to_email: str, subject: str, body: str):
    if not to_email:
        print("[EMAIL SKIPPED] to_email vacío. Body:", body)
        return

    # Si no hay SMTP configurado, solo loguea
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        print("[EMAIL SKIPPED] SMTP no configurado. Para:", to_email, "Link:", body)
        return

    msg = EmailMessage()
    msg["From"] = FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)


# =========================
# Endpoints
# =========================
@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "flow-backend",
        "flow_api_url": FLOW_API_URL,
        "public_base_url": PUBLIC_BASE_URL,
        "download_base_url": DOWNLOAD_BASE_URL
    }


@app.post("/pay/create")
async def pay_create(payload: dict):
    """
    payload esperado: {"email":"cliente@correo.cl"}
    Retorna: {"ok":true, "checkoutUrl":"...","token":"..."}
    """
    email = (payload.get("email") or "").strip().lower()
    if not is_valid_email(email):
        raise HTTPException(status_code=400, detail="Email inválido o faltante")

    if not PUBLIC_BASE_URL:
        raise HTTPException(
            status_code=500,
            detail="PUBLIC_BASE_URL no está configurado (necesario para urlConfirmation/urlReturn públicos)."
        )

    commerce_order = str(uuid.uuid4())
    order_id = str(uuid.uuid4())

    params = {
        "commerceOrder": commerce_order,
        "subject": "Pack IA para PYMES 2026",
        "currency": "CLP",
        "amount": 350,
        "email": email,
        "urlConfirmation": f"{PUBLIC_BASE_URL}/flow/confirmation",
        "urlReturn": f"{PUBLIC_BASE_URL}/flow/return",
        "optional": f'{{"orderId":"{order_id}"}}'
    }

    data = flow_post("/payment/create", params)
    flow_token = data["token"]
    checkout_url = f"{data['url']}?token={flow_token}"

    db_create_order(order_id, email, commerce_order, flow_token)

    return {"ok": True, "checkoutUrl": checkout_url, "token": flow_token}


@app.post("/flow/confirmation")
async def flow_confirmation(request: Request, background_tasks: BackgroundTasks):
    """
    Flow POST: token=XXXX (application/x-www-form-urlencoded)
    Debe responder 200 rápido.
    """
    form = await request.form()
    token = str(form.get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "missing token"}, status_code=200)

    # Consultar estado real
    status = flow_get_status(token)
    st = int(status.get("status", 0))  # 1 pendiente, 2 pagada, 3 rechazada, 4 anulada

    # Buscar la orden local
    order = db_get_by_flow_token(token)
    if not order:
        # Igual responde 200 para que Flow no reintente eternamente
        return JSONResponse({"ok": True, "warn": "order_not_found"}, status_code=200)

    _order_id, order_email, order_status, existing_download_token = order

    # Idempotencia: si Flow dice pagado, aseguramos download_token UNA sola vez
    if st == 2:
        if existing_download_token:
            download_token = existing_download_token
        else:
            download_token = uuid.uuid4().hex  # 32 chars
            db_mark_paid(token, download_token)

        download_link = f"{DOWNLOAD_BASE_URL}/download/{download_token}"
        mail_subject = "Tu Pack IA para PYMES 2026 — Link de descarga"
        mail_body = (
            "¡Gracias por tu compra!\n\n"
            f"Aquí está tu link de descarga:\n{download_link}\n\n"
            "Si tienes problemas, responde a este correo.\n"
            "— Flujos Digitales"
        )

        # IMPORTANTE: enviamos al email guardado en la DB (el que viene del formulario)
        background_tasks.add_task(send_email, order_email, mail_subject, mail_body)

    return JSONResponse({"ok": True}, status_code=200)


@app.post("/flow/return")
async def flow_return(request: Request):
    """
    Flow POST via browser a urlReturn con token=...
    Aquí mostramos estado al cliente (simple).
    """
    form = await request.form()
    token = str(form.get("token") or "").strip()
    if not token:
        return JSONResponse({"ok": False, "error": "missing token"}, status_code=200)

    status = flow_get_status(token)
    st = int(status.get("status", 0))

    if st == 2:
        return JSONResponse({"ok": True, "message": "Pago confirmado. Revisa tu correo para el link de descarga."})
    elif st == 1:
        return JSONResponse({"ok": True, "message": "Pago pendiente. Si fue transferencia/cupón, puede tardar."})
    else:
        return JSONResponse({"ok": False, "message": "Pago no completado (rechazado/anulado)."})


@app.get("/download/{download_token}")
def download(download_token: str):
    row = db_get_by_download_token(download_token)
    if not row:
        raise HTTPException(status_code=404, detail="Link inválido o expirado")

    email, status, flow_token = row
    if int(status) != 2:
        raise HTTPException(status_code=403, detail="Pago no confirmado")

    if not os.path.exists(PRODUCT_FILE):
        raise HTTPException(status_code=500, detail=f"No existe el archivo del producto: {PRODUCT_FILE}")

    return FileResponse(
        PRODUCT_FILE,
        media_type="application/zip",
        filename=os.path.basename(PRODUCT_FILE)
    )
