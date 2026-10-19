from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from bridge_core.api import create_app
from bridge_core.store import EnqueueResult
from bridge_core.workers import Wake, local_today

from app.main import ViberBridge
from app.viber_db import OutgoingEvent, SchemaNotReady
from app.workers import Watcher
from tests.conftest import SECRET, incoming

H = {"X-Bridge-Secret": SECRET}


def _enqueue(store, settings, message_id=1, phone="+380671234567"):
    res, _ = store.enqueue_outgoing(message_id, phone, "текст", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED
    return store.next_queued()


def _process(settings, store, ui, db, row):
    bridge = ViberBridge(settings, store, ui, db)
    asyncio.run(bridge.handle_outgoing(row, local_today(settings)))
    return bridge


def _drain(store):
    out = []
    while (cb := store.next_callback(time.time() + 1e6)) is not None:
        out.append(cb.payload)
        store.callback_done(cb.id)
    return out


def test_send_success(settings, store, fake_ui, fake_db):
    row = _enqueue(store, settings)
    _process(settings, store, fake_ui, fake_db, row)
    assert fake_ui.sent == [("+380671234567", "текст")]
    final = store.get_outgoing(1)
    assert final.state == "sent" and final.text is None and final.external_id == "ext101"
    assert _drain(store) == [{"message_id": 1, "state": "sent", "error": None, "external_id": "ext101"}]
    assert store.usage(local_today(settings)) == (1, 1)


def test_send_retry_once_then_error(settings, store, fake_ui, fake_db):
    fake_ui.deliver = False
    row = _enqueue(store, settings)
    _process(settings, store, fake_ui, fake_db, row)
    assert len(fake_ui.sent) == 2
    assert fake_ui.screenshots == ["msg1_try1", "msg1_try2"]
    final = store.get_outgoing(1)
    assert final.state == "error" and final.attempts == 2 and final.text is None
    assert store.usage(local_today(settings)) == (1, 1)  # counted once per message
    assert _drain(store)[0]["state"] == "error"


def test_target_not_verified_never_pastes(settings, store, fake_ui, fake_db):
    fake_ui.verify_ok = False
    row = _enqueue(store, settings)
    _process(settings, store, fake_ui, fake_db, row)
    assert fake_ui.sent == []
    assert store.get_outgoing(1).state == "error"


def test_late_confirmation_not_resent(settings, store, fake_ui, fake_db):
    row = _enqueue(store, settings)

    async def paste_without_event(text: str) -> None:
        fake_ui.sent.append((fake_ui.opened, text))

    fake_ui.paste_and_send = paste_without_event
    bridge = ViberBridge(settings, store, fake_ui, fake_db)
    orig = bridge.viber_sender._wait_confirmation

    async def wait_then_land(baseline, phone):
        res = await orig(baseline, phone)
        fake_db.outgoing.append(OutgoingEvent(fake_db.latest + 1, "late", phone))
        return res

    bridge.viber_sender._wait_confirmation = wait_then_land
    asyncio.run(bridge.handle_outgoing(row, local_today(settings)))
    assert len(fake_ui.sent) == 1
    assert store.get_outgoing(1).state == "sent"


def test_schema_not_ready_does_not_touch_ui_or_limits(settings, store, fake_ui, fake_db):
    def boom():
        raise SchemaNotReady("pending")

    fake_db.latest_event_id = boom
    row = _enqueue(store, settings)
    _process(settings, store, fake_ui, fake_db, row)
    assert fake_ui.opened is None and fake_ui.sent == []
    assert store.get_outgoing(1).state == "error"
    assert store.usage(local_today(settings)) == (0, 0)


def test_watcher_cursor_and_dedup(settings, store, fake_db):
    w = Watcher(settings, store, fake_db, Wake())
    fake_db.incoming = [incoming(50)]  # history before first start
    assert w.poll_once() == 0
    fake_db.incoming = [incoming(50), incoming(101), incoming(102)]
    assert w.poll_once() == 2
    assert w.poll_once() == 0
    store.kv_set(Watcher.CURSOR_KEY, "100")  # replay
    assert w.poll_once() == 0
    assert [p["external_id"] for p in _drain(store)] == ["in101", "in102"]


def test_health_and_logged_out(settings, store, fake_ui, fake_db):
    bridge = ViberBridge(settings, store, fake_ui, fake_db)
    with TestClient(create_app(bridge=bridge, start_workers=False)) as c:
        bridge.watcher.state = "ok"
        body = c.get("/health", headers=H).json()
        assert body["status"] == "connected" and body["details"]["viber_running"] is True
        bridge.watcher.state = "schema_pending"
        body = c.get("/health", headers=H).json()
        assert body["status"] == "degraded" and "watcher_schema_pending" in body["reasons"]
        fake_db.found = False
        assert c.get("/health", headers=H).json()["status"] == "logged_out"
        r = c.post("/send", json={"message_id": 1, "phone": "+380671234567", "text": "x"}, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "viber_logged_out"
