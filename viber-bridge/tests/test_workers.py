from __future__ import annotations

import asyncio
import json
import time

import httpx

from app.store import EnqueueResult
from app.workers import OdooDelivery, Sender, Wake, Watcher, local_today
from tests.conftest import SECRET, incoming


def _enqueue(store, settings, message_id=1, phone="+380671234567"):
    res, _ = store.enqueue_outgoing(message_id, phone, "текст", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED
    return store.next_queued()


def _sender(settings, store, ui, db):
    return Sender(settings, store, ui, db, Wake(), Wake())


def _statuses(store):
    out = []
    while (cb := store.next_callback(time.time() + 1e6)) is not None:
        out.append(cb.payload)
        store.callback_done(cb.id)
    return out


def test_send_success(settings, store, fake_ui, fake_db):
    row = _enqueue(store, settings)
    asyncio.run(_sender(settings, store, fake_ui, fake_db).process(row, local_today(settings)))
    assert fake_ui.sent == [("+380671234567", "текст")]
    final = store.get_outgoing(1)
    assert final.state == "sent" and final.text is None and final.external_id == "ext101"
    assert _statuses(store) == [{"message_id": 1, "state": "sent", "error": None, "external_id": "ext101"}]
    assert store.usage(local_today(settings)) == (1, 1)


def test_send_retry_once_then_error(settings, store, fake_ui, fake_db):
    fake_ui.deliver = False
    row = _enqueue(store, settings)
    asyncio.run(_sender(settings, store, fake_ui, fake_db).process(row, local_today(settings)))
    assert len(fake_ui.sent) == 2
    assert fake_ui.screenshots == ["msg1_try1", "msg1_try2"]
    final = store.get_outgoing(1)
    assert final.state == "error" and final.attempts == 2 and final.text is None
    assert store.usage(local_today(settings)) == (1, 1)  # counted once per message
    assert _statuses(store)[0]["state"] == "error"


def test_target_not_verified_never_pastes(settings, store, fake_ui, fake_db):
    fake_ui.verify_ok = False
    row = _enqueue(store, settings)
    asyncio.run(_sender(settings, store, fake_ui, fake_db).process(row, local_today(settings)))
    assert fake_ui.sent == []
    assert store.get_outgoing(1).state == "error"


def test_late_confirmation_not_resent(settings, store, fake_ui, fake_db):
    row = _enqueue(store, settings)

    async def slow_paste(text: str) -> None:
        fake_ui.sent.append((fake_ui.opened, text))

    fake_ui.paste_and_send = slow_paste  # event will appear only after the timeout

    async def scenario():
        sender = _sender(settings, store, fake_ui, fake_db)
        orig = sender._wait_confirmation

        async def wait_then_land(baseline, phone):
            res = await orig(baseline, phone)
            from app.viber_db import OutgoingEvent
            fake_db.outgoing.append(OutgoingEvent(fake_db.latest + 1, "late", phone))
            return res

        sender._wait_confirmation = wait_then_land
        await sender.process(row, local_today(settings))

    asyncio.run(scenario())
    assert len(fake_ui.sent) == 1
    assert store.get_outgoing(1).state == "sent"


def test_recover_interrupted(settings, store):
    _enqueue(store, settings)
    store.mark_sending(1, local_today(settings), count=True)
    assert store.recover_interrupted() == [1]
    assert store.get_outgoing(1).state == "error"


def test_watcher_cursor_and_dedup(settings, store, fake_db):
    w = Watcher(settings, store, fake_db, Wake())
    fake_db.incoming = [incoming(50)]  # history before first start
    assert w.poll_once() == 0
    fake_db.incoming = [incoming(50), incoming(101), incoming(102)]
    assert w.poll_once() == 2
    assert w.poll_once() == 0
    store.kv_set(Watcher.CURSOR_KEY, "100")  # replay
    assert w.poll_once() == 0
    payloads = _statuses(store)
    assert [p["external_id"] for p in payloads] == ["in101", "in102"]


def test_delivery_fifo_and_retry(settings, store):
    store.enqueue_incoming({"external_id": "a", "phone": "+380671234567", "text": "x"})
    store.enqueue_incoming({"external_id": "b", "phone": "+380671234567", "text": "y"})
    calls: list[tuple[str, dict, str | None]] = []
    fail = {"on": True}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.path, json.loads(request.content), request.headers.get("x-bridge-secret")))
        return httpx.Response(503 if fail["on"] else 200)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            d = OdooDelivery(settings, store, Wake(), client)
            assert await d.deliver_once(client) is False
            assert await d.deliver_once(client) is False  # backoff: not due yet
            fail["on"] = False
            with store._tx() as db:
                db.execute("UPDATE callbacks SET next_attempt_at=0")
            assert await d.deliver_once(client) is True
            assert await d.deliver_once(client) is True
            assert await d.deliver_once(client) is False

    asyncio.run(scenario())
    assert [c[1]["external_id"] for c in calls] == ["a", "a", "b"]
    assert calls[0][0] == "/messenger/bridge/viber/incoming"
    assert all(c[2] == SECRET for c in calls)


def test_schema_not_ready_does_not_touch_ui_or_limits(settings, store, fake_ui, fake_db):
    from app.viber_db import SchemaNotReady

    def boom():
        raise SchemaNotReady("pending")

    fake_db.latest_event_id = boom
    row = _enqueue(store, settings)
    asyncio.run(_sender(settings, store, fake_ui, fake_db).process(row, local_today(settings)))
    assert fake_ui.opened is None and fake_ui.sent == []
    assert store.get_outgoing(1).state == "error"
    assert store.usage(local_today(settings)) == (0, 0)
