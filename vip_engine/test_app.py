"""Isolated QA tests; do not treat these as a production payment integration."""
import threading

import pytest
from fastapi.testclient import TestClient

from vip_engine import app as vip


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(vip, "DB", tmp_path / "vip.sqlite")
    monkeypatch.setattr(vip, "_db_ready", False)
    monkeypatch.setattr(vip, "DEMO_ENABLED", True)
    monkeypatch.setattr(vip, "ADMIN_KEY", "test-admin-only")
    monkeypatch.setattr(vip, "SCANNER_KEY", "test-scanner-only")
    return TestClient(vip.app)


def req_hold(client, table="T12", guest="guest-very-long-A", action="unique-action-123456"):
    return client.post("/api/v1/hold", json={
      "event_id": "event_demo_001", "table_id": table, "client_action_id": action
    }, headers={"X-Guest-ID": guest})


def test_table_inventory_and_prices(client):
    response = client.get("/api/v1/demo/event")
    assert response.status_code == 200
    tables = {t["table_id"]: t for t in response.json()["tables"]}
    assert tables["T12"]["price_kobo"] == 20000000
    assert tables["T12"]["capacity"] == 6
    assert all(t["status"] == "available" for t in tables.values())


def test_idempotency_and_no_double_hold(client):
    a = req_hold(client)
    assert a.status_code == 200
    assert a.json()["status"] == "held"
    b = req_hold(client)
    assert b.json()["replayed"] is True
    assert a.json()["hold_id"] == b.json()["hold_id"]
    assert req_hold(client, guest="guest-very-long-B", action="another-action-987").status_code == 409


def test_release_is_authenticated_and_reusable(client):
    h = req_hold(client).json()
    body = {"hold_id": h["hold_id"]}
    assert client.post("/api/v1/release", json=body,
                       headers={"X-Guest-ID": "guest-very-long-B"}).status_code == 403
    assert client.post("/api/v1/release", json=body,
                       headers={"X-Guest-ID": "guest-very-long-A"}).json()["status"] == "released"
    assert client.post("/api/v1/release", json=body,
                       headers={"X-Guest-ID": "guest-very-long-A"}).json()["status"] == "released"
    assert req_hold(client, guest="guest-very-long-B", action="another-action-987").status_code == 200


def test_ticket_issue_six_and_single_use(client):
    h = req_hold(client).json()
    assert client.post("/api/v1/demo/pay", json={"hold_id": h["hold_id"]},
                       headers={"X-Guest-ID": "guest-very-long-A"}).status_code == 404
    paid = client.post("/api/v1/demo/pay", json={"hold_id": h["hold_id"]},
       headers={"X-Guest-ID": "guest-very-long-A",
                "X-Demo-Admin-Key": "test-admin-only"})
    assert paid.status_code == 200
    assert paid.json()["status"] == "confirmed"
    passes = paid.json()["passes"]
    assert len(passes) == 6
    assert len(set(p["token"] for p in passes)) == 6
    headers = {"X-Scanner-Key": "test-scanner-only"}
    first = client.post("/api/v1/tickets/check-in",
                        json={"event_id": "event_demo_001", "token": passes[0]["token"]},
                        headers=headers)
    assert first.json()["result"] == "ADMITTED"
    assert first.json()["admitted"] == 1
    second = client.post("/api/v1/tickets/check-in",
                         json={"event_id": "event_demo_001", "token": passes[0]["token"]},
                         headers=headers)
    assert second.json()["result"] == "ALREADY_USED"
    qr = client.get("/api/v1/tickets/qr", params={"token": passes[0]["token"]})
    assert qr.status_code == 200
    assert "image/svg+xml" in qr.headers["content-type"]


def test_late_payment_safe_if_no_new_owner(client, monkeypatch):
    h = req_hold(client).json()
    with vip.transaction() as db:
        db.execute("UPDATE holds SET status='expired' WHERE hold_id=?", (h["hold_id"],))
        result = vip.confirm_paid_hold(db, h["hold_id"], h["expires_at"] - 5)
        assert result["status"] == "confirmed"


def test_never_confirm_after_table_resold(client):
    h = req_hold(client).json()
    with vip.transaction() as db:
        db.execute("UPDATE holds SET status='expired' WHERE hold_id=?", (h["hold_id"],))
    next_h = req_hold(client, guest="guest-very-long-B", action="new-action-456789").json()
    with vip.transaction() as db:
        result = vip.confirm_paid_hold(db, h["hold_id"], h["expires_at"] - 2)
        assert result["status"] == "payment_exception"
        assert not db.execute("SELECT 1 FROM orders WHERE hold_id=?", (h["hold_id"],)).fetchone()
    assert next_h["status"] == "held"


def test_two_parallel_holds_only_one_succeeds(client):
    gate = threading.Barrier(2)
    outcome = []
    def reserve(guest, action):
        gate.wait()
        outcome.append(req_hold(client, guest=guest, action=action).status_code)
    threads = [threading.Thread(target=reserve, args=("guest-very-long-A", "action-parallel-A")),
               threading.Thread(target=reserve, args=("guest-very-long-B", "action-parallel-B"))]
    for thread in threads: thread.start()
    for thread in threads: thread.join(timeout=15)
    assert sorted(outcome) == [200, 409]
