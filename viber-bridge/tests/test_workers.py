from __future__ import annotations

import asyncio
import time

from fastapi.testclient import TestClient

from bridge_core.api import create_app
from bridge_core.store import EnqueueResult
from bridge_core.workers import local_today

from app.main import ViberBridge
from app.notify import Notification
from app.viber_ui import _same_text, parse_phone
from app.workers import incoming_text
from tests.conftest import NEUTRAL, SECRET, UiError

H = {"X-Bridge-Secret": SECRET}
PHONE = "+380671234567"


def _enqueue(store, settings, message_id=1, phone=PHONE):
    res, _ = store.enqueue_outgoing(message_id, phone, "текст", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED
    return store.next_queued()


def _process(settings, store, ui, row):
    bridge = ViberBridge(settings, store, ui)
    asyncio.run(bridge.handle_outgoing(row, local_today(settings)))
    return bridge


def _drain(store):
    out = []
    while (cb := store.next_callback(time.time() + 1e6)) is not None:
        out.append(cb.payload)
        store.callback_done(cb.id)
    return out


# -- sending ---------------------------------------------------------------------

def test_send_success_order(settings, store, fake_ui):
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert fake_ui.calls == ["prepare", "neutral", f"open:{PHONE}", "peer_phone", "paste", "enter", "neutral"]
    assert fake_ui.pasted == [(PHONE, "текст")]
    final = store.get_outgoing(1)
    assert final.state == "sent" and final.text is None
    assert _drain(store) == [{"message_id": 1, "state": "sent", "error": None, "external_id": "viber:1"}]
    assert store.usage(local_today(settings)) == (1, 1)


def test_not_on_viber_is_not_found_without_paste(settings, store, fake_ui):
    fake_ui.on_viber = set()
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert "paste" not in fake_ui.calls
    assert store.get_outgoing(1).state == "not_found"
    assert _drain(store)[0]["state"] == "not_found"


def test_wrong_header_number_never_pastes(settings, store, fake_ui):
    fake_ui.header_override = "+380999999999"
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert fake_ui.pasted == []
    assert fake_ui.calls.count(f"open:{PHONE}") == 2  # retried once: nothing was pasted
    assert fake_ui.screenshots == ["msg1_try1", "msg1_try2"]
    final = store.get_outgoing(1)
    assert final.state == "error" and "target not verified" in final.error
    assert store.usage(local_today(settings)) == (1, 1)  # counted once per message


def test_unreadable_header_never_pastes(settings, store, fake_ui):
    fake_ui.header_override = ""  # OCR found no number
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert fake_ui.pasted == [] and store.get_outgoing(1).state == "error"


def test_neutral_failure_retries_then_errors_without_open(settings, store, fake_ui):
    fake_ui.neutral_error = UiError("neutral state not reached")
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert not any(c.startswith("open:") for c in fake_ui.calls)
    assert "paste" not in fake_ui.calls
    assert store.get_outgoing(1).state == "error"


def test_paste_mismatch_clears_and_does_not_send_or_retry(settings, store, fake_ui):
    fake_ui.paste_ok = False
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert "enter" not in fake_ui.calls
    assert fake_ui.calls.count("paste") == 1 and "clear" in fake_ui.calls
    assert store.get_outgoing(1).state == "error"


def test_ui_error_after_paste_started_is_final(settings, store, fake_ui):
    async def broken_paste(text):
        fake_ui.calls.append("paste")
        raise UiError("xclip timed out")

    fake_ui.paste = broken_paste
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert fake_ui.calls.count("paste") == 1  # no second attempt: text may already be in the composer
    final = store.get_outgoing(1)
    assert final.state == "error" and "delivery unknown" in final.error


def test_enter_unconfirmed_is_error_without_resend(settings, store, fake_ui):
    fake_ui.enter_ok = False
    _process(settings, store, fake_ui, _enqueue(store, settings))
    assert fake_ui.calls.count("paste") == 1 and fake_ui.calls.count("enter") == 1
    final = store.get_outgoing(1)
    assert final.state == "error" and "delivery unknown" in final.error
    assert fake_ui.calls[-1] == "neutral"


# -- incoming ----------------------------------------------------------------------

def _notification(nid=1, app="ViberPC", summary="Олег", body="Добрий день"):
    return Notification(nid, app, summary, body, ("default", "Open"), 1_760_000_000.0)


def test_incoming_resolved_and_queued(settings, store, fake_ui):
    bridge = ViberBridge(settings, store, fake_ui)
    clicks = []
    bridge.resolver.click = clicks.append
    fake_ui.open_chat_phone = PHONE  # the clicked notification opens the sender's chat
    assert asyncio.run(bridge.resolver.handle(_notification(7))) is True
    assert clicks == [7]
    assert fake_ui.calls == ["prepare", "wait_chat_open", "prepare", "peer_phone", "neutral"]
    [payload] = _drain(store)
    assert payload["phone"] == PHONE and payload["name"] == "Олег" and payload["text"] == "Добрий день"
    assert payload["external_id"].startswith("vn:") and payload["attachments"] == []


def test_incoming_without_number_is_not_forwarded(settings, store, fake_ui):
    bridge = ViberBridge(settings, store, fake_ui)
    bridge.resolver.click = lambda nid: None
    fake_ui.header_override = ""  # group chat / hidden number
    assert asyncio.run(bridge.resolver.handle(_notification())) is False
    assert _drain(store) == [] and bridge.resolver.unresolved == 1
    assert fake_ui.calls[-1] == "neutral"


def test_incoming_chat_not_opened_still_returns_to_neutral(settings, store, fake_ui):
    bridge = ViberBridge(settings, store, fake_ui)
    bridge.resolver.click = lambda nid: None
    fake_ui.chat_opens = False
    assert asyncio.run(bridge.resolver.handle(_notification())) is False
    assert "peer_phone" not in fake_ui.calls and fake_ui.calls[-1] == "neutral"


def test_only_viber_notifications_are_taken(settings, store, fake_ui):
    bridge = ViberBridge(settings, store, fake_ui)
    bridge.resolver.submit(_notification(app="other"))
    bridge.resolver.submit(_notification(app="ViberPC"))
    assert bridge.resolver.queue.qsize() == 1


def test_incoming_text_media_labels():
    assert incoming_text("Photo message") == "[вкладення: фото]"
    assert incoming_text("Sticker") == "[вкладення: стікер]"
    assert incoming_text("Photo of the sofa, please") == "Photo of the sofa, please"


# -- parsing -----------------------------------------------------------------------

def test_parse_phone():
    assert parse_phone("+380637631837\n") == "+380637631837"
    assert parse_phone("+3 8 0 6 3 7 6 3 1 8 3 7") == "+380637631837"
    assert parse_phone(NEUTRAL) == NEUTRAL
    assert parse_phone("12") is None  # e.g. "12 participants" of a group
    assert parse_phone("") is None


def test_same_text_normalises_line_endings_and_trailing_space():
    assert _same_text("Привіт \r\nсвіт\n", "Привіт\nсвіт")
    assert not _same_text("Привіт", "Привіт світ")


# -- API / health ------------------------------------------------------------------

def test_health_and_logged_out(settings, store, fake_ui):
    bridge = ViberBridge(settings, store, fake_ui)
    profile = settings.viber_home / "380671234567"
    profile.mkdir(parents=True)
    (profile / "viber.db").write_bytes(b"encrypted")
    with TestClient(create_app(bridge=bridge, start_workers=False)) as c:
        body = c.get("/health", headers=H).json()
        assert body["status"] == "connected" and body["details"]["viber_running"] is True
        (profile / "viber.db").unlink()
        assert c.get("/health", headers=H).json()["status"] == "logged_out"
        r = c.post("/send", json={"message_id": 1, "phone": PHONE, "text": "x"}, headers=H)
        assert r.status_code == 503 and r.json()["detail"] == "viber_logged_out"


def test_notify_server_down_degrades(settings, store, fake_ui):
    class DeadNotify:
        owned = False

    bridge = ViberBridge(settings, store, fake_ui, notify_factory=lambda cb: DeadNotify())
    bridge.notify = DeadNotify()
    (settings.viber_home / "1").mkdir(parents=True)
    (settings.viber_home / "1" / "viber.db").write_bytes(b"x")
    with TestClient(create_app(bridge=bridge, start_workers=False)) as c:
        body = c.get("/health", headers=H).json()
        assert body["status"] == "degraded" and "notify_server_down" in body["reasons"]
