from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from telethon import errors, types

from bridge_core.store import EnqueueResult
from bridge_core.workers import local_today

from app.gateway import SERVICE_USER_ID, Peer, incoming_payload, should_forward
from app.main import PEER_CACHE_PREFIX, TelegramBridge

PHONE = "+380671234567"


def _enqueue(store, settings, message_id=1, phone=PHONE):
    res, _ = store.enqueue_outgoing(message_id, phone, "Привіт *світ*_", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED
    return store.next_queued()


def _run(bridge, row, settings):
    asyncio.run(bridge.handle_outgoing(row, local_today(settings)))


def _statuses(store):
    out = []
    while (cb := store.next_callback(time.time() + 1e6)) is not None:
        out.append(cb.payload)
        store.callback_done(cb.id)
    return out


def test_send_imports_once_then_uses_cache(settings, store, gw):
    gw.users[PHONE] = Peer(42, 777)
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings, 1), settings)
    _run(bridge, _enqueue(store, settings, 2), settings)
    assert gw.imports == [PHONE]
    assert [t for _, t in gw.sent] == ["Привіт *світ*_", "Привіт *світ*_"]  # verbatim
    st = _statuses(store)
    assert [s["state"] for s in st] == ["sent", "sent"]
    assert st[0]["external_id"] == "42:1001"


def test_not_found(settings, store, gw):
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings), settings)
    assert gw.sent == []
    assert _statuses(store)[0]["state"] == "not_found"


def test_flood_wait_defers_and_pauses(settings, store, gw):
    gw.users[PHONE] = Peer(42, 777)
    gw.send_error = errors.FloodWaitError(request=None, capture=120)
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings), settings)
    row = store.get_outgoing(1)
    assert row.state == "queued" and row.text is not None
    assert store.next_queued() is None  # not before the flood wait ends
    assert bridge.sender.paused_until > time.time() + 100
    assert _statuses(store) == []
    # after the wait: sent, counted once
    retry = store.next_queued(now=time.time() + 200)
    _run(bridge, retry, settings)
    assert store.get_outgoing(1).state == "sent"
    assert store.usage(local_today(settings))[0] == 1


def test_import_retry_defers(settings, store, gw):
    gw.retry = True
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings), settings)
    assert store.get_outgoing(1).state == "queued"


def test_peer_flood_errors_and_pauses(settings, store, gw):
    gw.users[PHONE] = Peer(42, 777)
    gw.send_error = errors.PeerFloodError(request=None)
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings), settings)
    assert store.get_outgoing(1).state == "error"
    assert bridge.sender.paused_until > time.time()


def test_session_revoked_marks_logged_out(settings, store, gw):
    gw.users[PHONE] = Peer(42, 777)
    gw.send_error = errors.AuthKeyUnregisteredError(request=None)
    bridge = TelegramBridge(settings, store, gw)
    bridge.authorized = True
    _run(bridge, _enqueue(store, settings), settings)
    assert store.get_outgoing(1).state == "error"
    assert bridge.authorized is False
    assert bridge.send_unavailable() == "telegram_logged_out"


def test_stale_peer_cache_retried_once(settings, store, gw):
    store.kv_set(PEER_CACHE_PREFIX + PHONE, '{"user_id": 1, "access_hash": 2}')
    gw.users[PHONE] = Peer(42, 777)
    gw.send_error = errors.PeerIdInvalidError(request=None)
    bridge = TelegramBridge(settings, store, gw)
    _run(bridge, _enqueue(store, settings), settings)
    assert store.get_outgoing(1).state == "queued"
    _run(bridge, store.next_queued(now=time.time() + 60), settings)
    assert store.get_outgoing(1).state == "sent"
    assert gw.sent[0][0] == Peer(42, 777)


def test_network_error_on_resolve_retry_once_then_error(settings, store, gw):
    bridge = TelegramBridge(settings, store, gw)
    gw.import_error = ConnectionError("down")
    _run(bridge, _enqueue(store, settings), settings)
    assert store.get_outgoing(1).state == "queued"
    _run(bridge, store.next_queued(now=time.time() + 60), settings)
    assert store.get_outgoing(1).state == "error"
    assert gw.sent == []


def test_network_error_on_send_is_not_resent(settings, store, gw):
    # Delivery unknown: a retry would be a new message (new random_id) -> possible duplicate.
    gw.users[PHONE] = Peer(42, 777)
    bridge = TelegramBridge(settings, store, gw)
    gw.send_error = asyncio.TimeoutError()
    _run(bridge, _enqueue(store, settings), settings)
    row = store.get_outgoing(1)
    assert row.state == "error"
    assert "доставка невідома" in row.error
    assert store.next_queued(now=time.time() + 1e6) is None
    assert _statuses(store)[-1]["state"] == "error"


def test_monitor_survives_rpc_error(settings, store, gw):
    class Client:
        def is_connected(self):
            return True

        async def is_user_authorized(self):
            return True

        async def __call__(self, request):
            raise errors.FloodWaitError(request=None, capture=30)

    bridge = TelegramBridge(settings, store, gw, client=Client())
    bridge.authorized = True
    asyncio.run(bridge._check_session(connect=False))
    assert bridge.authorized is True  # last known state kept, no exception escaped


# -- incoming -------------------------------------------------------------

def _user(**kw):
    base = dict(id=42, is_self=False, bot=False, phone="380671234567", first_name="Олена",
                last_name="Ш.", username="olena", access_hash=777)
    base.update(kw)
    return types.User(**base)


def _msg(**kw):
    base = dict(id=5, is_private=True, out=False, raw_text="Добрий день",
                date=datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc))
    base.update(kw)
    return SimpleNamespace(**base)


def test_should_forward_filters():
    assert should_forward(_msg(), _user())
    assert not should_forward(_msg(is_private=False), _user())          # group / channel
    assert not should_forward(_msg(out=True), _user())
    assert not should_forward(_msg(), _user(bot=True))
    assert not should_forward(_msg(), _user(id=SERVICE_USER_ID))        # login codes
    assert not should_forward(_msg(), _user(is_self=True))
    assert not should_forward(_msg(), types.Channel(id=1, title="c", photo=None, date=None))


def test_incoming_payload():
    p = incoming_payload(_msg(), _user())
    assert p == {
        "external_id": "42:5", "phone": "+380671234567", "name": "Олена Ш.", "text": "Добрий день",
        "attachments": [], "timestamp": "2026-10-19T10:00:00+00:00", "user_id": "42", "username": "olena",
    }
    hidden = incoming_payload(_msg(raw_text="", photo=object()), _user(phone=None, last_name=None))
    assert hidden["phone"] is None and hidden["name"] == "Олена"
    assert hidden["text"] == "[вкладення: фото]"
    voice = incoming_payload(_msg(raw_text="", voice=object(), document=object()), _user())
    assert voice["text"] == "[вкладення: голосове повідомлення]"


def test_on_new_message_queues_and_caches_peer(settings, store, gw):
    bridge = TelegramBridge(settings, store, gw)
    user = _user()

    async def get_sender():
        return user

    event = SimpleNamespace(message=_msg(), get_sender=get_sender)
    asyncio.run(bridge.on_new_message(event))
    asyncio.run(bridge.on_new_message(event))  # duplicate
    payloads = _statuses(store)
    assert [p["external_id"] for p in payloads] == ["42:5"]
    assert store.kv_get(PEER_CACHE_PREFIX + PHONE) is not None


def test_telethon_gateway_import_contact():
    from app.gateway import TelethonGateway

    class FakeClient:
        def __init__(self, result):
            self.result = result
            self.requests = []

        async def __call__(self, request):
            self.requests.append(request)
            return self.result

    found = types.contacts.ImportedContacts(
        imported=[types.ImportedContact(user_id=42, client_id=1)], popular_invites=[], retry_contacts=[],
        users=[_user()],
    )
    client = FakeClient(found)
    res = asyncio.run(TelethonGateway(client).import_contact(PHONE, "PDM +380671234567"))
    assert res.peer == Peer(42, 777) and res.retry is False
    sent = client.requests[0].contacts[0]
    assert sent.phone == PHONE and sent.first_name == "PDM +380671234567"

    empty = types.contacts.ImportedContacts(imported=[], popular_invites=[], retry_contacts=[], users=[])
    assert asyncio.run(TelethonGateway(FakeClient(empty)).import_contact(PHONE, "x")).peer is None
    retry = types.contacts.ImportedContacts(imported=[], popular_invites=[], retry_contacts=[1], users=[])
    assert asyncio.run(TelethonGateway(FakeClient(retry)).import_contact(PHONE, "x")).retry is True
