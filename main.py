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
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

app = FastAPI(title="flow-backend")

# =========================
# CORS (OBLIGATORIO PARA HTML)
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # puedes restringir luego a tu dominio
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Config
# =========================
FLOW_API_URL = os.getenv("FLOW_API_URL", "https://www.flow.cl/api").rstrip("/")
FLOW_API_KEY = os.getenv("FLOW_API_KEY", "")
FLOW_SECRET_KEY = os.getenv("FLOW_SECRET_KEY", "")

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
DOWNLOAD_BASE_URL = os.getenv("DOWNLOAD_BASE_URL", PUBLIC_BASE_URL).rstrip("/")

PRODUCT_FILE = os.getenv("PRODUCT_FILE", "products/pack_ia_pymes_2026.zip")
PRODUCT_DRIVE_URL = os.getenv("PRODUCT_DRIVE_URL", "").strip()

EMAIL_PROVIDER = (os.getenv("EMAIL_PROVIDER", "smtp") or "smtp").strip().lower()

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
FROM_EMAIL = os.getenv("FROM_EMAIL", "").strip()

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASS = os.getenv("SMTP_PASS", "").strip()

DB_PATH = "orders.db"

# =========================
# Utils
# =========================
EMAIL_REGEX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

def is_valid_email(email: str) -> bool:
    return bool(email and EMAIL_REGEX.match(email))

# =========================
# DB
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

def db_create_order(order_id, email, commerce_order, flow_token):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)",
            (order_id, datetime.utcnow().isoformat(), email, commerce_order, flow_token, 1, None, None)
        )
        con.commit()

def db_get_by_flow_token(flow_token):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT id, email, status, download_token FROM orders WHERE flow_token=?",
            (flow_token,)
        )
        return cur.fetchone()

def db_mark_paid(flow_token, download_token):
    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            "UPDATE orders SET status=2, download_token=?, paid_at=? WHERE flow_token=?",
            (download_token, datetime.utcnow().isoformat(), flow_token)
        )
        con.commit()

def db_get_by_download_token(download_token):
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute(
            "SELECT email, status FROM orders WHERE download_token=?",
            (download_token,)
        )
        return cur.fetchone()

# =========================
# Flow helpers
# =========================
def flow_sign(params, secret):
    items = sorted(params.items())
    raw = "".join(f"{k}{v}" for k, v in items)
    return hmac.new(secret.encode(), raw.encode(), hashlib.sha256).hexdigest()

def flow_post(endpoint, params):
    params["apiKey"] = FLOW_API_KEY
    params["s"] = flow_sign(params, FLOW_SECRET_KEY)
    resp = requests.post(
        f"{FLOW_API_URL}{endpoint}",
        data=urlencode(params),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20
    )
    return resp.json()

def flow_get_status(token):
    params = {"apiKey": FLOW_API_KEY, "token": token}
    params["s"] = flow_sign(params, FLOW_SECRET_KEY)
    resp = requests.get(f"{FLOW_API_URL}/payment/getStatus", params=params, timeout=20)
    return resp.json()

# =========================
# Email
# =========================
def send_email_via_resend(to_email, subject, body):
    r = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "text": body,
        },
        timeout=20
    )
    if r.status_code >= 300:
        raise RuntimeError(r.text)

def send_email(to_email, subject, body):
    try:
        if EMAIL_PROVIDER == "resend":
            send_email_via_resend(to_email, subject, body)
            print(f"[EMAIL] Enviado a {to_email}")
    except Exception as e:
        print("[EMAIL ERROR]", e)

# =========================
# Endpoints
# =========================
@app.get("/health")
def health():
    return {
        "ok": True,
        "email_provider": EMAIL_PROVIDER,
        "resend_configured": bool(RESEND_API_KEY and FROM_EMAIL),
        "has_product_drive_url": bool(PRODUCT_DRIVE_URL),
    }

@app.post("/pay/create")
async def pay_create(payload: dict):
    email = (payload.get("email") or "").strip().lower()
    if not is_valid_email(email):
        raise HTTPException(400, "Email inválido")

    order_id = str(uuid.uuid4())
    commerce_order = str(uuid.uuid4())

    params = {
        "commerceOrder": commerce_order,
        "subject": "Pack IA para PYMES 2026",
        "currency": "CLP",
        "amount": 350,
        "email": email,
        "urlConfirmation": f"{PUBLIC_BASE_URL}/flow/confirmation",
        "urlReturn": f"{PUBLIC_BASE_URL}/flow/return",
    }

    data = flow_post("/payment/create", params)
    token = data["token"]
    checkout_url = f"{data['url']}?token={token}"

    db_create_order(order_id, email, commerce_order, token)

    return {"ok": True, "checkoutUrl": checkout_url, "token": token}

@app.post("/flow/confirmation")
async def flow_confirmation(request: Request, background_tasks: BackgroundTasks):
    form = await request.form()
    token = form.get("token")

    status = flow_get_status(token)
    if int(status.get("status", 0)) == 2:
        order = db_get_by_flow_token(token)
        if order:
            _, email, _, existing_token = order
            download_token = existing_token or uuid.uuid4().hex
            db_mark_paid(token, download_token)

            link = f"{DOWNLOAD_BASE_URL}/download/{download_token}"
            subject = "Tu Pack IA para PYMES 2026 — Descarga"
            body = f"Gracias por tu compra.\n\nDescarga aquí:\n{link}"

            background_tasks.add_task(send_email, email, subject, body)

    return JSONResponse({"ok": True})

@app.post("/flow/return")
async def flow_return(request: Request):
    return {"ok": True, "message": "Pago confirmado. Revisa tu correo."}

@app.get("/download/{download_token}")
def download(download_token: str):
    row = db_get_by_download_token(download_token)
    if not row or int(row[1]) != 2:
        raise HTTPException(403, "Link inválido")

    if PRODUCT_DRIVE_URL:
        return RedirectResponse(PRODUCT_DRIVE_URL)

    return FileResponse(PRODUCT_FILE, media_type="application/zip")
