"""Viber-specific work: one send attempt via the UI, incoming messages from notifications.

Every UI action runs under one lock: sending and resolving an incoming message both drive the
same window. After each of them the UI is returned to the neutral state, so no customer chat
stays open (Viber does not notify about messages in the open chat).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import date, datetime, timezone
from typing import Callable

from bridge_core.logs import mask_phone
from bridge_core.store import OutboxRow, Store
from bridge_core.workers import Wake

from .config import Settings
from .notify import Notification
from .viber_ui import ChatOpen, UiError, ViberUiDriver, cleanup_debug_dir

log = logging.getLogger("viber.workers")

VIBER_APP = "ViberPC"

# Notification bodies for media (English UI). Only "Photo message" is confirmed on the live app.
_MEDIA_BODIES = {
    "photo": "фото", "video": "відео", "voice": "голосове повідомлення", "sticker": "стікер",
    "gif": "GIF", "file": "файл", "location": "геолокація", "contact": "контакт",
}
_MEDIA_RE = re.compile(r"^(photo|video|voice|sticker|gif|file|location|contact)( message)?$", re.IGNORECASE)


def incoming_text(body: str) -> str:
    m = _MEDIA_RE.match(body.strip())
    if m:
        # Attachments are not transferred; the caption is not in the notification either.
        return f"[вкладення: {_MEDIA_BODIES[m.group(1).lower()]}]"
    return body


class ViberSender:
    MAX_ATTEMPTS = 2  # first try + one retry, only for failures before anything was pasted

    def __init__(
        self, settings: Settings, store: Store, ui: ViberUiDriver, ui_lock: asyncio.Lock,
        sent: Callable[[int, str | None], None], failed: Callable[[int, str, str], None],
    ) -> None:
        self.s = settings
        self.store = store
        self.ui = ui
        self.ui_lock = ui_lock
        self._sent = sent
        self._failed = failed

    async def process(self, row: OutboxRow, today: date) -> None:
        mid = row.message_id
        if not row.text:
            self._failed(mid, "error", "empty text")
            return
        error = "unknown"
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self.store.mark_sending(mid, today, count=row.attempts == 0 and attempt == 1)
            log.info("send_attempt", extra={"message_id": mid, "attempt": attempt, "phone": mask_phone(row.phone)})
            async with self.ui_lock:
                try:
                    outcome = await self._attempt(row.phone, row.text)
                except UiError as exc:
                    outcome = ("retry", f"ui: {exc}")
                except Exception as exc:  # noqa: BLE001 - must never kill the loop
                    log.exception("send_unexpected_error", extra={"message_id": mid})
                    outcome = ("final_error", f"unexpected: {type(exc).__name__}")
                if outcome[0] != "sent":
                    shot = await self.ui.screenshot(f"msg{mid}_try{attempt}")
                    log.warning("send_attempt_failed", extra={
                        "message_id": mid, "attempt": attempt, "error": outcome[1],
                        "screenshot": str(shot) if shot else None,
                    })
                await self._back_to_neutral()
            kind, detail = outcome
            if kind == "sent":
                self._sent(mid, f"viber:{mid}")
                return
            if kind == "not_found":
                self._failed(mid, "not_found", detail)
                return
            if kind == "final_error":
                self._failed(mid, "error", detail)
                return
            error = detail
        self._failed(mid, "error", error)

    async def _attempt(self, phone: str, text: str) -> tuple[str, str]:
        """(sent|not_found|retry|final_error, detail). 'retry' only if nothing was pasted."""
        await self.ui.prepare()
        # Neutral first: then a composer can only appear in the chat our deep link opens.
        await self.ui.neutral()
        opened = await self.ui.open_chat(phone)
        if opened is ChatOpen.NOT_ON_VIBER:
            return "not_found", "номер не зареєстрований у Viber"
        if opened is not ChatOpen.OPEN:
            return "retry", "chat did not open"
        peer = await self.ui.peer_phone()
        if peer != phone:
            # Never paste unless the open chat is proven to be this number.
            return "retry", f"target not verified (header shows {mask_phone(peer) if peer else 'no number'})"
        # From here on text may be in the composer or already sent: any failure is final, never a retry.
        try:
            if not await self.ui.paste(text):
                await self.ui.clear_composer()
                return "final_error", "pasted text could not be verified in the composer, not sent"
            if not await self.ui.press_enter():
                return "final_error", "composer not empty after Enter: delivery unknown"
        except UiError as exc:
            return "final_error", f"ui after paste started: {exc}; delivery unknown"
        return "sent", ""

    async def _back_to_neutral(self) -> None:
        try:
            await self.ui.neutral()
        except Exception as exc:  # noqa: BLE001
            log.warning("neutral_failed", extra={"error": f"{type(exc).__name__}: {exc}"})


class IncomingResolver:
    """Notification -> click it (Viber opens the sender's chat) -> read the number -> queue for Odoo."""

    def __init__(
        self, settings: Settings, store: Store, ui: ViberUiDriver, ui_lock: asyncio.Lock,
        click: Callable[[int], None], delivery_wake: Wake,
    ) -> None:
        self.s = settings
        self.store = store
        self.ui = ui
        self.ui_lock = ui_lock
        self.click = click
        self.delivery_wake = delivery_wake
        self.queue: asyncio.Queue[Notification] = asyncio.Queue()
        self.unresolved = 0
        self.last_event: float | None = None

    def submit(self, n: Notification) -> None:
        """Called on the event loop (the notify thread hands over via call_soon_threadsafe)."""
        if n.app_name != VIBER_APP:
            return
        self.queue.put_nowait(n)

    async def run(self) -> None:
        while True:
            n = await self.queue.get()
            try:
                await self.handle(n)
            except Exception:  # noqa: BLE001 - one bad notification must not stop the rest
                log.exception("incoming_failed", extra={"notification_id": n.id})

    async def handle(self, n: Notification) -> bool:
        async with self.ui_lock:
            phone = None
            try:
                await self.ui.prepare()
                self.click(n.id)
                if await self.ui.wait_chat_open():
                    await self.ui.prepare()  # clicking restores the window un-maximized
                    phone = await self.ui.peer_phone()
            finally:
                try:
                    await self.ui.neutral()
                except Exception as exc:  # noqa: BLE001
                    log.warning("neutral_failed", extra={"error": f"{type(exc).__name__}: {exc}"})
        if phone is None:
            # Group chat, hidden number or OCR failure: Odoo needs the number, so it is not forwarded.
            self.unresolved += 1
            shot = await self.ui.screenshot(f"incoming{n.id}_unresolved")
            log.error("incoming_unresolved", extra={"notification_id": n.id, "screenshot": str(shot) if shot else None})
            return False
        ts = datetime.fromtimestamp(n.received_at, timezone.utc)
        digest = hashlib.sha256(f"{phone}|{n.received_at:.3f}|{n.body}".encode()).hexdigest()[:20]
        payload = {
            "external_id": f"vn:{digest}",
            "phone": phone,
            "name": n.summary or None,
            "text": incoming_text(n.body),
            "attachments": [],
            "timestamp": ts.isoformat(),
        }
        if self.store.enqueue_incoming(payload):
            self.last_event = n.received_at
            self.delivery_wake.set()
            log.info("incoming_queued", extra={"external_id": payload["external_id"], "phone": mask_phone(phone)})
        return True


async def debug_cleanup_loop(settings: Settings) -> None:
    while True:
        removed = await asyncio.to_thread(cleanup_debug_dir, settings.debug_dir, settings.debug_retention_days)
        if removed:
            log.info("debug_screenshots_removed", extra={"count": removed})
        await asyncio.sleep(3600)
