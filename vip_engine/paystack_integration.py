"""Paystack TEST-MODE integration for Partywin's ISOLATED VIP QA demo.

Do not mount these routes in production. No sk_live_ keys are accepted.
Webhook HMAC: SHA-512 HMAC(raw request bytes, Paystack secret API key).
Every successful event independently re-verifies transaction/verify/{reference}.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .app import (
    DEMO_ENABLED, available, confirm_paid_hold, expire, initialize,
    now, require_guest, transaction
)

router = APIRouter(prefix="/api/v1", tags=["Paystack isolated TEST integration"])
PAYSTACK_URL = "https://api.paystack.co"
EMAIL = re.compile(r"^[^@\s]{1,128}@[^@\s]{1,190}\.[^@\s]{2,63}$")
REF = re.compile(r"^[a-zA-Z0-9._=-]{8,90}$")


def require_config():
    key = os.environ.get("PAYSTACK_SECRET_KEY", "")
    if not DEMO_ENABLED or not key.startswith("sk_test_") or len(key) < 20:
        raise HTTPException(503, "Paystack test mode not configured")
    # An encryption key ensures confirmed guest passes can be retrieved later.
    encryption_key = os.environ.get("VIP_TICKET_FERNET_KEY", "")
    try:
        cipher = Fernet(encryption_key.encode("ascii"))
    except Exception:
        raise HTTPException(503, "VIP ticket encryption not configured")
    return key, cipher


def verify_signature(raw_body: bytes, supplied: str | None, secret: str):
    if not supplied or not re.fullmatch(r"[0-9a-fA-F]{128}", supplied):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, supplied.lower())


def paystack_http(method: str, path: str, key: str, payload: dict | None = None):
    """One fixed HTTPS origin and strict timeout. Never pass untrusted URLs."""
    with httpx.Client(timeout=httpx.Timeout(12.0)) as client:
        response = client.request(
            method, PAYSTACK_URL + path,
            headers={"Authorization": "Bearer " + key,
                     "Content-Type": "application/json"},
            json=payload if method == "POST" else None
        )
        response.raise_for_status()
        result = response.json()
    if result.get("status") is not True or not isinstance(result.get("data"), dict):
        raise ValueError("Paystack returned an unsuccessful response")
    return result["data"]


def setup_paystack_tables(db):
    db.execute("""
      CREATE TABLE IF NOT EXISTS paystack_checkouts (
        reference TEXT PRIMARY KEY,
        hold_id TEXT NOT NULL UNIQUE,
        guest_id TEXT NOT NULL,
        email TEXT NOT NULL,
        amount_kobo INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'NGN',
        status TEXT NOT NULL CHECK(status IN (
          'initializing','initialized','confirmed','payment_exception'
        )),
        authorization_url TEXT,
        created_at INTEGER NOT NULL,
        last_verified_at INTEGER,
        FOREIGN KEY (hold_id) REFERENCES holds(hold_id)
      )
    """)
    db.execute("""
      CREATE TABLE IF NOT EXISTS ticket_secrets (
        ticket_id TEXT PRIMARY KEY,
        encrypted_token TEXT NOT NULL,
        FOREIGN KEY(ticket_id) REFERENCES tickets(id)
      )
    """)
    db.execute("""
      CREATE TABLE IF NOT EXISTS webhook_audit (
        id TEXT PRIMARY KEY,
        reference TEXT,
        result TEXT NOT NULL,
        created_at INTEGER NOT NULL
      )
    """)


class CheckoutIn(BaseModel):
    hold_id: str = Field(min_length=20, max_length=160)
    email: str = Field(min_length=5, max_length=254)


def get_checkout(db, reference):
    return db.execute("SELECT * FROM paystack_checkouts WHERE reference=?", (reference,)).fetchone()


@router.post("/checkout")
def paystack_checkout(body: CheckoutIn, x_guest_id: str | None = Header(default=None)):
    key, _ = require_config()
    guest = require_guest(x_guest_id)
    if not EMAIL.fullmatch(body.email):
        raise HTTPException(422, "Valid checkout email required by payment provider")
    initialize()
    with transaction() as db:
        setup_paystack_tables(db)
        current = now()
        expire(db, current)
        h = db.execute("SELECT * FROM holds WHERE hold_id=? AND guest_id=?",
                       (body.hold_id, guest)).fetchone()
        if not h:
            raise HTTPException(404, "Hold not found")
        if h["status"] != "held" or h["expires_at"] <= current:
            raise HTTPException(409, "Hold expired or unavailable")
        attempt = db.execute("SELECT * FROM paystack_checkouts WHERE hold_id=?",
                             (body.hold_id,)).fetchone()
        if attempt:
            if attempt["status"] == "initialized":
                return {"reference": attempt["reference"], "checkout_url":
                        attempt["authorization_url"], "expires_at": h["expires_at"],
                        "replayed": True}
            raise HTTPException(409, "Checkout already pending or finalized; use status endpoint")
        t = db.execute("SELECT price_kobo FROM tables WHERE event_id=? AND table_id=?",
                       (h["event_id"], h["table_id"])).fetchone()
        if not t:
            raise HTTPException(404, "Table not found")
        reference = "PWTEST-" + secrets.token_hex(20)
        amount = int(t["price_kobo"])
        db.execute("""
          INSERT INTO paystack_checkouts
          (reference,hold_id,guest_id,email,amount_kobo,currency,status,created_at)
          VALUES (?,?,?,?,?,'NGN','initializing',?)
        """, (reference, body.hold_id, guest, body.email, amount, current))

    try:
        provider = paystack_http("POST", "/transaction/initialize", key, {
            "email": body.email,
            "amount": str(amount),
            "currency": "NGN",
            "reference": reference,
            "metadata": {"hold_id": body.hold_id, "source": "partywin-vip-qa"}
        })
    except (httpx.HTTPError, ValueError) as err:
        # Deliberately retain the reference in 'initializing': network uncertainty
        # must not create duplicate charges. Operator can reconcile via reference.
        raise HTTPException(503, "Provider initialization uncertain; contact support with your hold ID") from err
    checkout_url = provider.get("authorization_url", "")
    host = urlsplit(checkout_url)
    if (provider.get("reference") != reference or host.scheme != "https" or
            host.hostname != "checkout.paystack.com"):
        raise HTTPException(502, "Unexpected provider checkout response")
    with transaction() as db:
        setup_paystack_tables(db)
        db.execute("""
          UPDATE paystack_checkouts
          SET status='initialized',authorization_url=?
          WHERE reference=? AND status='initializing'
        """, (checkout_url, reference))
    return {"reference": reference, "checkout_url": checkout_url,
            "expires_at": h["expires_at"], "replayed": False}


def parse_paid_at(value):
    if not isinstance(value, str):
        raise ValueError("Missing provider payment timestamp")
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Payment timestamp missing time zone")
    return dt.timestamp()


def validate_verified(data, expected_reference, amount):
    """Provider verification, not webhook/browser fields, is authoritative."""
    if (data.get("status") != "success" or
            data.get("domain") != "test" or
            data.get("reference") != expected_reference or
            data.get("currency") != "NGN" or
            type(data.get("amount")) is not int or data["amount"] != amount):
        raise ValueError("Provider verification does not match booking")
    paid = parse_paid_at(data.get("paid_at"))
    if paid <= 0 or paid > now() + 300:
        raise ValueError("Invalid payment timestamp")
    return paid


def settle_verified(reference, key, cipher):
    """Safe for duplicate Paystack webhooks. Side effects occur in one DB transaction."""
    if not REF.fullmatch(reference):
        raise HTTPException(400, "Invalid reference")
    initialize()
    with transaction() as db:
        setup_paystack_tables(db)
        checkout = get_checkout(db, reference)
        if not checkout:
            # Authenticated but unrelated merchant event: acknowledge, don't issue.
            return {"status": "ignored"}
        expected_amount = checkout["amount_kobo"]
        if checkout["status"] in ("confirmed", "payment_exception"):
            return {"status": checkout["status"], "replayed": True}
    try:
        verified = paystack_http("GET", "/transaction/verify/" + reference, key)
        paid_at = validate_verified(verified, reference, expected_amount)
    except (httpx.HTTPError, ValueError) as err:
        # Transient verification failure must not mint tickets.
        raise HTTPException(503, "Provider transaction verification incomplete") from err

    with transaction() as db:
        setup_paystack_tables(db)
        checkout = get_checkout(db, reference)
        if not checkout:
            raise HTTPException(404, "Checkout missing during settlement")
        if checkout["status"] in ("confirmed", "payment_exception"):
            return {"status": checkout["status"], "replayed": True}
        # Check again under lock; do not issue a pass if amount/booking differs.
        result = confirm_paid_hold(db, checkout["hold_id"], paid_at)
        if result["status"] == "confirmed" and not result.get("replayed"):
            tickets = db.execute("""
              SELECT id, guest_number FROM tickets
              WHERE order_id=? ORDER BY guest_number
            """, (result["order_id"],)).fetchall()
            for t, p in zip(tickets, result["passes"]):
                db.execute("INSERT INTO ticket_secrets VALUES (?,?)",
                           (t["id"], cipher.encrypt(p["token"].encode()).decode()))
        db.execute("""
          UPDATE paystack_checkouts SET status=?,last_verified_at=?
          WHERE reference=?
        """, (result["status"], now(), reference))
        db.execute("INSERT INTO webhook_audit VALUES (?,?,?,?)",
                   (secrets.token_hex(12), reference, result["status"], now()))
        # Do NOT return bearer tokens from webhook; guest retrieves them privately.
        return {"status": result["status"], "replayed": False}


@router.post("/paystack/webhook")
async def paystack_webhook(request: Request,
                           x_paystack_signature: str | None = Header(default=None)):
    key, cipher = require_config()
    raw_body = await request.body()
    if len(raw_body) > 200_000:
        raise HTTPException(413, "Webhook too large")
    if not verify_signature(raw_body, x_paystack_signature, key):
        raise HTTPException(401, "Webhook signature invalid")
    try:
        event = __import__("json").loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON")
    if event.get("event") != "charge.success":
        return {"status": "ignored"}
    data = event.get("data") or {}
    reference = data.get("reference", "")
    if not isinstance(reference, str):
        raise HTTPException(400, "Invalid reference")
    result = settle_verified(reference, key, cipher)
    return {"acknowledged": True, **result}


@router.get("/payments/{reference}")
def payment_status(reference: str, x_guest_id: str | None = Header(default=None)):
    require_config()
    guest = require_guest(x_guest_id)
    initialize()
    with transaction() as db:
        setup_paystack_tables(db)
        checkout = get_checkout(db, reference)
        if not checkout or checkout["guest_id"] != guest:
            raise HTTPException(404, "Payment not found")
        row = db.execute("SELECT order_id FROM orders WHERE hold_id=?",
                         (checkout["hold_id"],)).fetchone()
        return {"reference": reference, "status": checkout["status"],
                "order_id": row["order_id"] if row else None}


@router.get("/orders/{order_id}/passes")
def order_passes(order_id: str, x_guest_id: str | None = Header(default=None)):
    """Demo only! X-Guest-ID is not secure authentication for a real checkout."""
    _, cipher = require_config()
    guest = require_guest(x_guest_id)
    initialize()
    with transaction() as db:
        setup_paystack_tables(db)
        owner = db.execute("""
          SELECT 1 FROM orders o JOIN holds h ON h.hold_id=o.hold_id
          JOIN paystack_checkouts c ON c.hold_id=h.hold_id
          WHERE o.order_id=? AND c.guest_id=? AND c.status='confirmed'
        """, (order_id, guest)).fetchone()
        if not owner:
            raise HTTPException(404, "Order not found")
        rows = db.execute("""
          SELECT t.guest_number,s.encrypted_token FROM tickets t
          JOIN ticket_secrets s ON s.ticket_id=t.id
          WHERE t.order_id=? ORDER BY t.guest_number
        """, (order_id,)).fetchall()
        return {"order_id": order_id, "passes": [
            {"guest_number": r["guest_number"],
             "token": cipher.decrypt(r["encrypted_token"].encode()).decode()}
            for r in rows
        ]}
