from __future__ import annotations

import asyncio
import json
import time

import httpx

from bridge_core.store import EnqueueResult
from bridge_core.workers import OdooDelivery, Wake, local_today
from tests.conftest import SECRET


def _enqueue(store, settings, message_id=1, phone="+380671234567"):
    res, _ = store.enqueue_outgoing(message_id, phone, "текст", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED


def drain(store):
    out = []
    while (cb := store.next_callback(time.time() + 1e6)) is not None:
        out.append(cb.payload)
        store.callback_done(cb.id)
    return out


def test_finish_wipes_text_and_enqueues_status(store, settings):
    _enqueue(store, settings)
    store.mark_sending(1, local_today(settings), count=True)
    store.finish_outgoing(1, "sent", external_id="x1")
    row = store.get_outgoing(1)
    assert row.state == "sent" and row.text is None
    assert drain(store) == [{"message_id": 1, "state": "sent", "error": None, "external_id": "x1"}]
    assert store.usage(local_today(settings)) == (1, 1)


def test_defer_respects_not_before(store, settings):
    _enqueue(store, settings)
    store.mark_sending(1, local_today(settings), count=True)
    store.defer(1, time.time() + 60, "flood wait")
    assert store.next_queued() is None
    row = store.next_queued(now=time.time() + 61)
    assert row is not None and row.attempts == 1 and row.text == "текст"


def test_recover_interrupted(store, settings):
    _enqueue(store, settings)
    store.mark_sending(1, local_today(settings), count=True)
    assert store.recover_interrupted() == [1]
    assert store.get_outgoing(1).state == "error"


def test_incoming_dedup_and_no_phone(store):
    p = {"external_id": "a", "phone": None, "user_id": "42", "text": "x"}
    assert store.enqueue_incoming(p) is True
    assert store.enqueue_incoming(p) is False
    assert [c["external_id"] for c in drain(store)] == ["a"]


def test_sender_loop_handles_and_survives_crash(bridge, store, settings):
    _enqueue(store, settings, 1)
    _enqueue(store, settings, 2, "+380671234568")

    async def crash_then_ok(row, today):
        if row.message_id == 1:
            store.mark_sending(1, today, count=True)
            raise RuntimeError("boom")
        bridge.sent(row.message_id, "ok")

    bridge.sender.handler = crash_then_ok

    async def scenario():
        task = asyncio.create_task(bridge.sender.run())
        for _ in range(100):
            if store.get_outgoing(2).state == "sent":
                break
            await asyncio.sleep(0.01)
        task.cancel()

    asyncio.run(scenario())
    assert store.get_outgoing(1).state == "error"
    assert store.get_outgoing(2).state == "sent"


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
    assert calls[0][0] == "/messenger/bridge/test/incoming"
    assert all(c[2] == SECRET for c in calls)
