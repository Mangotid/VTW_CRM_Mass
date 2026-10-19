"""Viber-specific loops: send attempt via UI, viber.db watcher, debug cleanup."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date
from typing import Callable

from bridge_core.logs import mask_phone
from bridge_core.store import OutboxRow, Store
from bridge_core.workers import Wake

from .config import Settings
from .viber_db import SchemaNotReady, ViberDb
from .viber_ui import UiError, ViberUiDriver, cleanup_debug_dir

log = logging.getLogger("viber.workers")


class ViberSender:
    MAX_ATTEMPTS = 2  # first try + one retry

    def __init__(
        self, settings: Settings, store: Store, ui: ViberUiDriver, db: ViberDb,
        sent: Callable[[int, str | None], None], failed: Callable[[int, str, str], None],
    ) -> None:
        self.s = settings
        self.store = store
        self.ui = ui
        self.db = db
        self._sent = sent
        self._failed = failed

    async def process(self, row: OutboxRow, today: date) -> None:
        mid = row.message_id
        if not row.text:
            self._failed(mid, "error", "empty text")
            return
        try:
            # Without a baseline we cannot confirm delivery: fail before touching the UI or limits.
            baseline = await asyncio.to_thread(self.db.latest_event_id)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) if isinstance(exc, SchemaNotReady) else type(exc).__name__
            self._failed(mid, "error", f"viber.db not ready: {reason}")
            return
        error = "unknown"
        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            self.store.mark_sending(mid, today, count=row.attempts == 0 and attempt == 1)
            log.info("send_attempt", extra={"message_id": mid, "attempt": attempt, "phone": mask_phone(row.phone)})
            try:
                if attempt > 1 and (ext := await self._confirmed(baseline, row.phone)) is not None:
                    # Previous attempt landed late; do not resend.
                    self._sent(mid, ext)
                    return
                await self.ui.open_chat(row.phone)
                await self.ui.verify_target(row.phone)
                await self.ui.paste_and_send(row.text)
                ext = await self._wait_confirmation(baseline, row.phone)
                if ext is not None:
                    self._sent(mid, ext)
                    return
                error = "confirmation timeout"
            except UiError as exc:
                error = f"ui: {exc}"
            except Exception as exc:  # noqa: BLE001 - must never kill the loop
                log.exception("send_unexpected_error", extra={"message_id": mid})
                error = f"unexpected: {type(exc).__name__}"
            shot = await self.ui.screenshot(f"msg{mid}_try{attempt}")
            log.warning(
                "send_attempt_failed",
                extra={"message_id": mid, "attempt": attempt, "error": error, "screenshot": str(shot) if shot else None},
            )
        self._failed(mid, "error", error)

    async def _confirmed(self, baseline: int, phone: str) -> str | None:
        events = await asyncio.to_thread(self.db.outgoing_since, baseline, phone)
        return events[0].external_id if events else None

    async def _wait_confirmation(self, baseline: int, phone: str) -> str | None:
        deadline = time.monotonic() + self.s.send_confirm_timeout
        while time.monotonic() < deadline:
            if (ext := await self._confirmed(baseline, phone)) is not None:
                return ext
            await asyncio.sleep(1)
        return None


class Watcher:
    CURSOR_KEY = "watcher.last_event_id"

    def __init__(self, settings: Settings, store: Store, db: ViberDb, delivery_wake: Wake) -> None:
        self.s = settings
        self.store = store
        self.db = db
        self.delivery_wake = delivery_wake
        self.state = "starting"

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.poll_once)
            except SchemaNotReady:
                self.state = "schema_pending"
            except Exception:  # noqa: BLE001
                if self.state != "error":
                    log.exception("watcher_error")
                self.state = "error"
            await asyncio.sleep(self.s.watch_interval)

    def poll_once(self) -> int:
        if self.db.locate() is None:
            self.state = "db_not_found"
            return 0
        cursor = self.store.kv_get(self.CURSOR_KEY)
        if cursor is None:
            # First start: do not replay history into Odoo.
            self.store.kv_set(self.CURSOR_KEY, str(self.db.latest_event_id()))
            self.state = "ok"
            return 0
        events = self.db.incoming_since(int(cursor))
        new = 0
        for ev in events:
            payload = {
                "external_id": ev.external_id,
                "phone": ev.phone,
                "name": ev.name,
                "text": ev.text,
                "attachments": [],
                "timestamp": ev.timestamp.isoformat(),
            }
            if self.store.enqueue_incoming(payload):
                new += 1
                log.info("incoming_queued", extra={"external_id": ev.external_id, "phone": mask_phone(ev.phone)})
            self.store.kv_set(self.CURSOR_KEY, str(ev.event_id))
        self.state = "ok"
        if new:
            self.delivery_wake.set()
        return new


async def debug_cleanup_loop(settings: Settings) -> None:
    while True:
        removed = await asyncio.to_thread(cleanup_debug_dir, settings.debug_dir, settings.debug_retention_days)
        if removed:
            log.info("debug_screenshots_removed", extra={"count": removed})
        await asyncio.sleep(3600)
