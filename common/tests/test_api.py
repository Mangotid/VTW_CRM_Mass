from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from bridge_core.api import create_app
from tests.conftest import SECRET, DummyBridge

H = {"X-Bridge-Secret": SECRET}
MSG = {"message_id": 1, "phone": "+380671234567", "text": "Добрий день"}


def client(bridge) -> TestClient:
    return TestClient(create_app(bridge=bridge, start_workers=False))


def test_auth_required(bridge):
    with client(bridge) as c:
        assert c.post("/send", json=MSG).status_code == 401
        assert c.post("/send", json=MSG, headers={"X-Bridge-Secret": "wrong"}).status_code == 401
        assert c.get("/health").status_code == 401


def test_send_idempotent(bridge, store):
    with client(bridge) as c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 202 and r.json() == {"accepted": True}
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 202 and r.json()["duplicate"] is True
        assert store.queue_length() == 1
        r = c.post("/send", json={**MSG, "phone": "+380501112233"}, headers=H)
        assert r.status_code == 409


def test_send_validation(bridge):
    with client(bridge) as c:
        assert c.post("/send", json={**MSG, "phone": "0671234567"}, headers=H).status_code == 422
        att = {**MSG, "attachments": [{"url": "http://x/a.jpg", "filename": "a.jpg", "mimetype": "image/jpeg"}]}
        r = c.post("/send", json=att, headers=H)
        assert r.status_code == 422 and r.json()["detail"] == "attachments_not_supported"
        r = c.post("/send", json={**MSG, "phone": "+38 (067) 123-45-67"}, headers=H)
        assert r.status_code == 202


def test_sending_disabled(settings, store):
    b = DummyBridge(replace(settings, sending_enabled=False), store)
    with client(b) as c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "sending_disabled"


def test_send_unavailable(bridge):
    bridge.unavailable = "logged_out"
    with client(bridge) as c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "logged_out"


def test_daily_limits(bridge):
    with client(bridge) as c:  # total 3, new contacts 2
        assert c.post("/send", json={**MSG, "message_id": 1, "phone": "+380670000001"}, headers=H).status_code == 202
        assert c.post("/send", json={**MSG, "message_id": 2, "phone": "+380670000002"}, headers=H).status_code == 202
        r = c.post("/send", json={**MSG, "message_id": 3, "phone": "+380670000003"}, headers=H)
        assert r.status_code == 429 and r.json()["detail"] == "daily_new_contact_limit"
        # same (already queued) contact is not new
        assert c.post("/send", json={**MSG, "message_id": 4, "phone": "+380670000001"}, headers=H).status_code == 202
        r = c.post("/send", json={**MSG, "message_id": 5, "phone": "+380670000001"}, headers=H)
        assert r.status_code == 429 and r.json()["detail"] == "daily_limit"


def test_health(bridge):
    with client(bridge) as c:
        body = c.get("/health", headers=H).json()
        assert body["status"] == "connected"
        assert body["daily"]["limit"] == 3 and body["details"] == {"dummy": True}
        bridge.logged_in = False
        assert c.get("/health", headers=H).json()["status"] == "logged_out"
        bridge.logged_in = True
        bridge.sender.pause(100)
        body = c.get("/health", headers=H).json()
        assert body["status"] == "degraded" and "sender_paused" in body["reasons"]
        assert "sender_paused_until" in body["details"]
