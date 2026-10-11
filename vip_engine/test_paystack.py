"""Mocked Paystack TEST flow; no real credentials or network requests."""
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from vip_engine import app as vip
from vip_engine import paystack_integration as payment


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(vip, "DB", tmp_path / "vip-test.sqlite")
    monkeypatch.setattr(vip, "_db_ready", False)
    monkeypatch.setattr(vip, "DEMO_ENABLED", True)
    monkeypatch.setattr(payment, "DEMO_ENABLED", True)
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_fake_unit_test_only_secret_key")
    monkeypatch.setenv("VIP_TICKET_FERNET_KEY", Fernet.generate_key().decode())
    def mock_api(method, path, key, payload=None):
        if path == "/transaction/initialize":
            return {
                "authorization_url": "https://checkout.paystack.com/test-session",
                "reference": payload["reference"]
            }
        if path.startswith("/transaction/verify/"):
            return {
                "status": "success", "domain": "test",
                "reference": path.removeprefix("/transaction/verify/"),
                "amount": 20000000, "currency": "NGN",
                "paid_at": datetime.now(timezone.utc).isoformat()
            }
        raise AssertionError("Unexpected Paystack request")
    monkeypatch.setattr(payment, "paystack_http", mock_api)
    return TestClient(vip.app)


GUEST = {"X-Guest-ID": "guest-very-long-A"}


def hold(client):
    response = client.post("/api/v1/hold", json={
        "event_id": "event_demo_001",
        "table_id": "T12",
        "client_action_id": "test-checkout-action-id"
    }, headers=GUEST)
    assert response.status_code == 200
    return response.json()["hold_id"]


def checkout(client, hold_id):
    return client.post("/api/v1/checkout", headers=GUEST,
                       json={"hold_id": hold_id, "email": "test@example.org"})


def signed_event(reference, key="sk_test_fake_unit_test_only_secret_key"):
    body = json.dumps({"event": "charge.success",
                       "data": {"reference": reference}}).encode()
    signature = hmac.new(key.encode(), body, hashlib.sha512).hexdigest()
    return body, {"x-paystack-signature": signature,
                  "content-type": "application/json"}


def test_signed_webhook_and_order_passes(client):
    h = hold(client)
    initiated = checkout(client, h)
    assert initiated.status_code == 200
    info = initiated.json()
    assert info["checkout_url"] == "https://checkout.paystack.com/test-session"
    assert checkout(client, h).json()["replayed"] is True
    reference = info["reference"]

    forged_body, signed_headers = signed_event(reference)
    invalid = client.post("/api/v1/paystack/webhook",
        content=forged_body, headers={**signed_headers, "x-paystack-signature": "0"*128})
    assert invalid.status_code == 401
    assert client.get("/api/v1/payments/" + reference, headers=GUEST).json()["status"] == "initialized"

    result = client.post("/api/v1/paystack/webhook", content=forged_body, headers=signed_headers)
    assert result.status_code == 200
    assert result.json()["status"] == "confirmed"
    assert "passes" not in result.json()
    repeated = client.post("/api/v1/paystack/webhook", content=forged_body, headers=signed_headers)
    assert repeated.status_code == 200
    assert repeated.json()["replayed"] is True

    status = client.get("/api/v1/payments/" + reference, headers=GUEST).json()
    assert status["status"] == "confirmed"
    assert client.get("/api/v1/payments/" + reference,
                      headers={"X-Guest-ID": "guest-very-long-B"}).status_code == 404
    order_id = status["order_id"]
    passes = client.get("/api/v1/orders/" + order_id + "/passes", headers=GUEST).json()["passes"]
    assert len(passes) == 6
    assert len(set(p["token"] for p in passes)) == 6
    assert client.get("/api/v1/orders/" + order_id + "/passes",
                      headers={"X-Guest-ID": "guest-very-long-B"}).status_code == 404
    vip.SCANNER_KEY = "local-scanner-key"
    first = client.post("/api/v1/tickets/check-in", headers={"X-Scanner-Key": "local-scanner-key"},
                        json={"token": passes[0]["token"], "event_id": "event_demo_001"})
    second = client.post("/api/v1/tickets/check-in", headers={"X-Scanner-Key": "local-scanner-key"},
                         json={"token": passes[0]["token"], "event_id": "event_demo_001"})
    assert first.json()["result"] == "ADMITTED"
    assert second.json()["result"] == "ALREADY_USED"


def test_no_confirmation_when_provider_mismatches_amount(client, monkeypatch):
    h = hold(client)
    ref = checkout(client, h).json()["reference"]
    monkeypatch.setattr(payment, "paystack_http", lambda *args, **kwargs: {
        "status": "success", "domain": "test", "reference": ref, "amount": 100,
        "currency": "NGN", "paid_at": datetime.now(timezone.utc).isoformat()
    })
    body, headers = signed_event(ref)
    assert client.post("/api/v1/paystack/webhook", content=body, headers=headers).status_code == 503
    assert client.get("/api/v1/payments/" + ref, headers=GUEST).json()["status"] == "initialized"


def test_live_key_rejected_and_missing_signature(client, monkeypatch):
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_live_must_not_be_accepted_12345")
    assert checkout(client, hold(client)).status_code == 503
    monkeypatch.setenv("PAYSTACK_SECRET_KEY", "sk_test_fake_unit_test_only_secret_key")
    assert client.post("/api/v1/paystack/webhook",
                       json={"event": "charge.success", "data": {"reference": "anything"}}).status_code == 401
