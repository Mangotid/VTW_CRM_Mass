from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from app.main import Bridge, create_app
from tests.conftest import SECRET

H = {"X-Bridge-Secret": SECRET}
MSG = {"message_id": 1, "phone": "+380671234567", "text": "Добрий день"}


def client(settings, store, ui, db) -> tuple[TestClient, Bridge]:
    bridge = Bridge(settings, store, ui, db)
    return TestClient(create_app(bridge, start_workers=False)), bridge


def test_auth_required(settings, store, fake_ui, fake_db):
    c, _ = client(settings, store, fake_ui, fake_db)
    with c:
        assert c.post("/send", json=MSG).status_code == 401
        assert c.post("/send", json=MSG, headers={"X-Bridge-Secret": "wrong"}).status_code == 401
        assert c.get("/health").status_code == 401


def test_send_idempotent(settings, store, fake_ui, fake_db):
    c, _ = client(settings, store, fake_ui, fake_db)
    with c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 202 and r.json() == {"accepted": True}
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 202 and r.json()["duplicate"] is True
        assert store.queue_length() == 1
        r = c.post("/send", json={**MSG, "phone": "+380501112233"}, headers=H)
        assert r.status_code == 409


def test_send_validation(settings, store, fake_ui, fake_db):
    c, _ = client(settings, store, fake_ui, fake_db)
    with c:
        assert c.post("/send", json={**MSG, "phone": "0671234567"}, headers=H).status_code == 422
        att = {**MSG, "attachments": [{"url": "http://x/a.jpg", "filename": "a.jpg", "mimetype": "image/jpeg"}]}
        r = c.post("/send", json=att, headers=H)
        assert r.status_code == 422 and r.json()["detail"] == "attachments_not_supported"
        r = c.post("/send", json={**MSG, "phone": "+38 (067) 123-45-67"}, headers=H)
        assert r.status_code == 202


def test_sending_disabled(settings, store, fake_ui, fake_db):
    c, _ = client(replace(settings, sending_enabled=False), store, fake_ui, fake_db)
    with c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "sending_disabled"


def test_daily_limits(settings, store, fake_ui, fake_db):
    c, _ = client(settings, store, fake_ui, fake_db)  # total 3, new contacts 2
    with c:
        assert c.post("/send", json={**MSG, "message_id": 1, "phone": "+380670000001"}, headers=H).status_code == 202
        assert c.post("/send", json={**MSG, "message_id": 2, "phone": "+380670000002"}, headers=H).status_code == 202
        r = c.post("/send", json={**MSG, "message_id": 3, "phone": "+380670000003"}, headers=H)
        assert r.status_code == 429 and r.json()["detail"] == "daily_new_contact_limit"
        # same (already queued) contact is not new
        assert c.post("/send", json={**MSG, "message_id": 4, "phone": "+380670000001"}, headers=H).status_code == 202
        r = c.post("/send", json={**MSG, "message_id": 5, "phone": "+380670000001"}, headers=H)
        assert r.status_code == 429 and r.json()["detail"] == "daily_limit"


def test_health(settings, store, fake_ui, fake_db):
    c, bridge = client(settings, store, fake_ui, fake_db)
    with c:
        bridge.watcher.state = "ok"
        body = c.get("/health", headers=H).json()
        assert body["status"] == "connected"
        assert body["daily"]["limit"] == 3
        fake_db.found = False
        assert c.get("/health", headers=H).json()["status"] == "logged_out"
        fake_db.found = True
        bridge.watcher.state = "schema_pending"
        body = c.get("/health", headers=H).json()
        assert body["status"] == "degraded" and "watcher_schema_pending" in body["reasons"]


def test_send_rejected_when_logged_out(settings, store, fake_ui, fake_db):
    fake_db.found = False
    c, _ = client(settings, store, fake_ui, fake_db)
    with c:
        r = c.post("/send", json=MSG, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "viber_logged_out"
