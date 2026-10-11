"""Partywin VIP Engine V1 isolated demo.

This module is NOT mounted in the existing bundled Stage 8 application.
Its SQLite data store and demo payment endpoint are for QA ONLY.
Do not deploy it to accept real customer payments.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
import qrcode
from qrcode.image.svg import SvgImage
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DB = Path(os.environ.get("VIP_DEMO_DB", "/tmp/partywin-vip-v1.sqlite3"))
DEMO_ENABLED = (
    os.environ.get("APP_ENV", "development") != "production"
    and os.environ.get("VIP_DEMO_MODE", "false").lower() == "true"
)
ADMIN_KEY = os.environ.get("VIP_DEMO_ADMIN_KEY", "")
SCANNER_KEY = os.environ.get("VIP_SCANNER_KEY", "")
app = FastAPI(title="Partywin VIP Engine V1 - Isolated QA Demo")
_db_setup_lock = threading.Lock()
_db_ready = False


def now() -> int:
    return int(time.time())


@contextmanager
def transaction():
    """SQLite writes are serialized across processes by BEGIN IMMEDIATE.

    Replace SQLite with PostgreSQL row locking before any production integration.
    """
    DB.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(DB), timeout=15, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=15000")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def initialize():
    global _db_ready
    if _db_ready:
        return
    with _db_setup_lock:
        if _db_ready:
            return
        DB.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(DB), timeout=15)
        db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS tables (
          event_id TEXT NOT NULL,
          table_id TEXT NOT NULL,
          zone TEXT NOT NULL,
          capacity INTEGER NOT NULL,
          price_kobo INTEGER NOT NULL,
          x INTEGER NOT NULL,
          y INTEGER NOT NULL,
          PRIMARY KEY(event_id, table_id)
        );
        CREATE TABLE IF NOT EXISTS holds (
          hold_id TEXT PRIMARY KEY,
          event_id TEXT NOT NULL,
          table_id TEXT NOT NULL,
          guest_id TEXT NOT NULL,
          action_id TEXT NOT NULL UNIQUE,
          created_at INTEGER NOT NULL,
          expires_at INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('held','released','expired','confirmed'))
        );
        CREATE INDEX IF NOT EXISTS holds_table_idx ON holds(event_id,table_id,status,expires_at);
        CREATE TABLE IF NOT EXISTS orders (
          order_id TEXT PRIMARY KEY,
          hold_id TEXT NOT NULL UNIQUE REFERENCES holds(hold_id),
          paid_at INTEGER NOT NULL,
          created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tickets (
          id TEXT PRIMARY KEY,
          order_id TEXT NOT NULL REFERENCES orders(order_id),
          token_hash TEXT NOT NULL UNIQUE,
          admitted_at INTEGER,
          guest_number INTEGER NOT NULL,
          UNIQUE(order_id,guest_number)
        );
        CREATE TABLE IF NOT EXISTS payment_exceptions (
          id TEXT PRIMARY KEY, hold_id TEXT, reason TEXT NOT NULL, created_at INTEGER NOT NULL
        );
        """)
        db.executemany(
          "INSERT OR IGNORE INTO tables VALUES (?,?,?,?,?,?,?)",
          [
            ("event_demo_001", "T12", "VIP", 6, 20000000, 330, 335),
            ("event_demo_001", "T13", "Premium VIP", 8, 50000000, 680, 335),
          ]
        )
        db.commit()
        db.close()
        _db_ready = True


def row_json(row):
    return dict(row) if row else None


def expire(db, current):
    db.execute(
      "UPDATE holds SET status='expired' WHERE status='held' AND expires_at <= ?",
      (current,)
    )


def available(db, event_id, table_id, current):
    return not db.execute("""
      SELECT 1 FROM holds WHERE event_id=? AND table_id=?
      AND (status='confirmed' OR (status='held' AND expires_at>?)) LIMIT 1
    """, (event_id, table_id, current)).fetchone()


def require_guest(guest_id: str | None):
    if not guest_id or len(guest_id) < 12 or len(guest_id) > 128:
        raise HTTPException(401, "Demo requires a generated X-Guest-ID session identifier")
    return guest_id


class HoldIn(BaseModel):
    event_id: str = Field(min_length=1, max_length=100)
    table_id: str = Field(min_length=1, max_length=100)
    client_action_id: str = Field(min_length=12, max_length=200)


class HoldRef(BaseModel):
    hold_id: str


class DemoPayIn(BaseModel):
    hold_id: str


class CheckInIn(BaseModel):
    token: str = Field(min_length=20, max_length=256)
    event_id: str = "event_demo_001"


@app.get("/")
def home():
    return FileResponse(ROOT / "index.html")


@app.get("/api/v1/demo/event")
def demo_event():
    initialize()
    with transaction() as db:
        expire(db, now())
        rows = db.execute("SELECT * FROM tables WHERE event_id=?", ("event_demo_001",)).fetchall()
        result = [dict(r) | {"status": "available" if available(db, r["event_id"], r["table_id"], now()) else "unavailable"} for r in rows]
    return {"event_id": "event_demo_001", "name": "Demo Lounge Night (TEST ONLY)",
            "location": "Port Harcourt (fictional venue)", "currency": "NGN",
            "price_unit": "kobo", "tables": result, "demo_mode": DEMO_ENABLED}


@app.post("/api/v1/hold")
def hold(body: HoldIn, x_guest_id: str | None = Header(default=None)):
    initialize()
    guest = require_guest(x_guest_id)
    action_key = hashlib.sha256(
      json.dumps([body.event_id, body.table_id, guest, body.client_action_id]).encode()
    ).hexdigest()
    current = now()
    with transaction() as db:
        expire(db, current)
        previous = db.execute("SELECT * FROM holds WHERE action_id=?", (action_key,)).fetchone()
        if previous:
            return {"hold_id": previous["hold_id"], "status": previous["status"],
                    "expires_at": previous["expires_at"], "replayed": True}
        table = db.execute(
          "SELECT * FROM tables WHERE event_id=? AND table_id=?",
          (body.event_id, body.table_id)
        ).fetchone()
        if not table:
            raise HTTPException(404, "Table not found")
        hold_count = db.execute(
          "SELECT COUNT(*) FROM holds WHERE guest_id=? AND created_at>? AND status <> 'confirmed'",
          (guest, current - 900)
        ).fetchone()[0]
        if hold_count >= 3:
            raise HTTPException(429, "Hold limit reached. Try again later.")
        if not available(db, body.event_id, body.table_id, current):
            raise HTTPException(409, "Table unavailable")
        hold_id = secrets.token_urlsafe(24)
        expires = current + 600
        db.execute("""
          INSERT INTO holds VALUES (?,?,?,?,?,?,?,?)
        """, (hold_id, body.event_id, body.table_id, guest, action_key, current, expires, "held"))
        return {"hold_id": hold_id, "table_id": body.table_id, "status": "held",
                "expires_at": expires, "amount_kobo": table["price_kobo"],
                "capacity": table["capacity"], "currency": "NGN", "replayed": False}


@app.post("/api/v1/release")
def release(body: HoldRef, x_guest_id: str | None = Header(default=None)):
    initialize()
    guest = require_guest(x_guest_id)
    with transaction() as db:
        expire(db, now())
        hold_row = db.execute("SELECT * FROM holds WHERE hold_id=?", (body.hold_id,)).fetchone()
        if not hold_row:
            raise HTTPException(404, "Hold not found")
        if hold_row["guest_id"] != guest:
            raise HTTPException(403, "Not your hold")
        if hold_row["status"] == "confirmed":
            raise HTTPException(409, "Paid booking cannot be released")
        if hold_row["status"] == "held":
            db.execute("UPDATE holds SET status='released' WHERE hold_id=?", (body.hold_id,))
            return {"status": "released"}
        return {"status": hold_row["status"]}


def confirm_paid_hold(db, hold_id: str, paid_at: int):
    """Must be invoked ONLY after trusted provider verification or demo-admin gate.

    Race policy: never double book a table even if payment is verified after expiry.
    """
    t = now()
    row = db.execute("SELECT * FROM holds WHERE hold_id=?", (hold_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Hold not found")
    previous = db.execute("SELECT * FROM orders WHERE hold_id=?", (hold_id,)).fetchone()
    if previous:
        return {"status": "confirmed", "order_id": previous["order_id"],
                "passes": [], "replayed": True}
    another = db.execute("""
      SELECT 1 FROM holds WHERE event_id=? AND table_id=? AND hold_id<>?
      AND (status='confirmed' OR (status='held' AND expires_at>?)) LIMIT 1
    """, (row["event_id"], row["table_id"], hold_id, t)).fetchone()
    if row["status"] not in ("held", "expired") or paid_at > row["expires_at"] or another:
        reason = "late_or_conflicting_payment"
        db.execute("INSERT INTO payment_exceptions VALUES (?,?,?,?)",
                   (secrets.token_urlsafe(16), hold_id, reason, t))
        return {"status": "payment_exception", "reason": reason, "passes": []}
    table = db.execute("SELECT capacity FROM tables WHERE event_id=? AND table_id=?",
                       (row["event_id"], row["table_id"])).fetchone()
    order_id = secrets.token_urlsafe(16)
    db.execute("UPDATE holds SET status='confirmed' WHERE hold_id=?", (hold_id,))
    db.execute("INSERT INTO orders VALUES (?,?,?,?)", (order_id, hold_id, paid_at, t))
    passes = []
    for number in range(1, table["capacity"] + 1):
        token = secrets.token_urlsafe(32)
        db.execute("INSERT INTO tickets VALUES (?,?,?,?,?)",
                   (secrets.token_urlsafe(12), order_id,
                    hashlib.sha256(token.encode()).hexdigest(), None, number))
        passes.append({"guest_number": number, "token": token})
    return {"status": "confirmed", "order_id": order_id, "passes": passes, "replayed": False}


@app.post("/api/v1/demo/pay")
def demo_pay(body: DemoPayIn, x_demo_admin_key: str | None = Header(default=None),
             x_guest_id: str | None = Header(default=None)):
    initialize()
    if not DEMO_ENABLED or not ADMIN_KEY or not x_demo_admin_key or not hmac.compare_digest(x_demo_admin_key, ADMIN_KEY):
        raise HTTPException(404, "Demo payments disabled")
    guest = require_guest(x_guest_id)
    with transaction() as db:
        hold_row = db.execute("SELECT * FROM holds WHERE hold_id=?", (body.hold_id,)).fetchone()
        if not hold_row or hold_row["guest_id"] != guest:
            raise HTTPException(403, "Not your hold")
        return confirm_paid_hold(db, body.hold_id, now())


@app.post("/api/v1/tickets/check-in")
def check_in(body: CheckInIn, x_scanner_key: str | None = Header(default=None)):
    initialize()
    if not SCANNER_KEY or not x_scanner_key or not hmac.compare_digest(SCANNER_KEY, x_scanner_key):
        raise HTTPException(401, "Scanner authentication required")
    digest = hashlib.sha256(body.token.encode()).hexdigest()
    with transaction() as db:
        ticket = db.execute("""
          SELECT t.*, h.event_id,h.table_id,v.capacity FROM tickets t
          JOIN orders o ON o.order_id=t.order_id
          JOIN holds h ON h.hold_id=o.hold_id
          JOIN tables v ON v.event_id=h.event_id AND v.table_id=h.table_id
          WHERE t.token_hash=?
        """, (digest,)).fetchone()
        if not ticket or ticket["event_id"] != body.event_id:
            return {"result": "INVALID"}
        if ticket["admitted_at"] is not None:
            return {"result": "ALREADY_USED", "table_id": ticket["table_id"]}
        db.execute("UPDATE tickets SET admitted_at=? WHERE id=? AND admitted_at IS NULL",
                   (now(), ticket["id"]))
        admitted = db.execute("""
          SELECT COUNT(*) FROM tickets WHERE order_id=? AND admitted_at IS NOT NULL
        """, (ticket["order_id"],)).fetchone()[0]
        return {"result": "ADMITTED", "table_id": ticket["table_id"],
                "admitted": admitted, "total": ticket["capacity"]}


@app.get("/api/v1/health")
def health():
    initialize()
    return {"status": "ok", "mode": "isolated_demo", "real_payments_enabled": False}


@app.get("/api/v1/tickets/qr")
def ticket_qr(token: str):
    """Generate a test QR. The token is bearer-like; do not log ticket URLs."""
    if not DEMO_ENABLED:
        raise HTTPException(404, "Demo QR disabled")
    if len(token) < 20 or len(token) > 256:
        raise HTTPException(400, "Invalid token format")
    img = qrcode.make("partywin-v1:" + token, image_factory=SvgImage)
    return Response(
        content=img.to_string(), media_type="image/svg+xml",
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"}
    )


# Keep the Paystack integration in a separately isolated QA module.
from .paystack_integration import router as paystack_test_router
app.include_router(paystack_test_router)
