"""Background loops: sequential sender, viber.db watcher, Odoo callback delivery."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date, datetime, timedelta

import httpx

from .config import Settings
from .logs import mask_phone
from .store import OutboxRow, Store
from .viber_db import SchemaNotReady, ViberDb
from .viber_ui import UiError, ViberUiDriver, cleanup_debug_dir

log = logging.getLogger("viber.workers")


class Wake:
    """Lets the API nudge a sleeping worker."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    async def wait(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()


def local_today(settings: Settings) -> date:
    return datetime.now(settings.timezone).date()


def seconds_until_midnight(settings: Settings) -> float:
    now = datetime.now(settings.timezone)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), settings.timezone)
    return max(1.0, (midnight - now).total_seconds())


class Sender:
    MAX_ATTEMPTS = 2  # first try + one retry

    def __init__(
        self, settings: Settings, store: Store, ui: ViberUiDriver, db: ViberDb,
        wake: Wake, delivery_wake: Wake,
    ) -> None:
        self.s = settings
        self.store = store
        self.ui = ui
        self.db = db
        self.wake = wake
        self.delivery_wake = delivery_wake
        self.limit_reached = False

    async def run(self) -> None:
        while True:
            row = self.store.next_queued()
            if row is None:
                await self.wake.wait(5)
                continue
            today = local_today(self.s)
            if not self.store.limits_allow(row, today, self.s.daily_limit_total, self.s.daily_limit_new_contacts):
                self.limit_reached = True
                log.warning("daily_limit_reached", extra={"message_id": row.message_id})
                await asyncio.sleep(min(600.0, seconds_until_midnight(self.s)))
                continue
            self.limit_reached = False
            await self.process(row, today)
            await asyncio.sleep(random.uniform(self.s.send_delay_min, self.s.send_delay_max))

    async def process(self, row: OutboxRow, today: date) -> None:
        mid = row.message_id
        if not row.text:
            self.store.finish_outgoing(mid, "error", error="empty text")
            return
        try:
            # Without a baseline we cannot confirm delivery: fail before touching the UI or limits.
            baseline = await asyncio.to_thread(self.db.latest_event_id)
        except Exception as exc:  # noqa: BLE001
            reason = str(exc) if isinstance(exc, SchemaNotReady) else type(exc).__name__
            self.store.finish_outgoing(mid, "error", error=f"viber.db not ready: {reason}")
            log.warning("send_viber_db_not_ready", extra={"message_id": mid})
            self.delivery_wake.set()
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
        self.store.finish_outgoing(mid, "error", error=error)
        self.delivery_wake.set()

    def _sent(self, message_id: int, external_id: str) -> None:
        self.store.finish_outgoing(message_id, "sent", external_id=external_id)
        log.info("send_ok", extra={"message_id": message_id, "external_id": external_id})
        self.delivery_wake.set()

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


class OdooDelivery:
    """Strict FIFO delivery of incoming messages and statuses to Odoo, with backoff."""

    def __init__(self, settings: Settings, store: Store, wake: Wake, client: httpx.AsyncClient | None = None) -> None:
        self.s = settings
        self.store = store
        self.wake = wake
        self._client = client

    async def run(self) -> None:
        client = self._client or httpx.AsyncClient(timeout=self.s.odoo_timeout)
        try:
            while True:
                if not await self.deliver_once(client):
                    await self.wake.wait(2)
        finally:
            if self._client is None:
                await client.aclose()

    async def deliver_once(self, client: httpx.AsyncClient) -> bool:
        cb = self.store.next_callback(time.time())
        if cb is None:
            return False
        url = f"{self.s.odoo_url}/messenger/bridge/{self.s.channel}/{cb.kind}"
        try:
            resp = await client.post(url, json=cb.payload, headers={"X-Bridge-Secret": self.s.bridge_secret})
        except httpx.HTTPError as exc:
            retry = min(300.0, 5.0 * 2 ** cb.attempts)
            self.store.callback_failed(cb.id, f"network: {type(exc).__name__}", retry)
            log.warning("odoo_unreachable", extra={"callback_id": cb.id, "kind": cb.kind, "retry_in": retry})
            return False
        if 200 <= resp.status_code < 300:
            self.store.callback_done(cb.id)
            log.info("odoo_delivered", extra={"callback_id": cb.id, "kind": cb.kind})
            return True
        # Never drop data: 4xx usually means config error (secret/route) and needs a human.
        base, cap = (60.0, 3600.0) if 400 <= resp.status_code < 500 else (5.0, 300.0)
        retry = min(cap, base * 2 ** cb.attempts)
        self.store.callback_failed(cb.id, f"http {resp.status_code}", retry)
        log.error(
            "odoo_rejected",
            extra={"callback_id": cb.id, "kind": cb.kind, "status_code": resp.status_code, "retry_in": retry},
        )
        return False


async def debug_cleanup_loop(settings: Settings) -> None:
    while True:
        removed = await asyncio.to_thread(cleanup_debug_dir, settings.debug_dir, settings.debug_retention_days)
        if removed:
            log.info("debug_screenshots_removed", extra={"count": removed})
        await asyncio.sleep(3600)
