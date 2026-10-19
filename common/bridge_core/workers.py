"""Generic background loops: sequential sender with limits, Odoo callback delivery."""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import date, datetime, timedelta
from typing import Awaitable, Callable

import httpx

from .settings import CoreSettings
from .store import OutboxRow, Store

log = logging.getLogger("bridge.workers")

OutgoingHandler = Callable[[OutboxRow, date], Awaitable[None]]


class Wake:
    """Lets the API nudge a sleeping worker. `set()` is safe from any thread (sync endpoints run in a pool)."""

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    def set(self) -> None:
        loop = self._loop
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if loop is None or loop is current:
            self._event.set()
        elif not loop.is_closed():
            loop.call_soon_threadsafe(self._event.set)

    async def wait(self, timeout: float) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        self._event.clear()


def backoff(base: float, cap: float, attempts: int) -> float:
    # Exponent capped: 2 ** 1024 no longer fits a float (OverflowError after weeks of retries).
    return min(cap, base * 2 ** min(attempts, 20))


async def supervise(name: str, factory: Callable[[], Awaitable[None]], restart_delay: float = 5.0) -> None:
    """Run a background loop forever: a crash is logged and the loop restarted, never lost silently."""
    while True:
        try:
            await factory()
            log.error("task_exited", extra={"task": name, "restart_in": restart_delay})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("task_crashed", extra={"task": name, "restart_in": restart_delay})
        await asyncio.sleep(restart_delay)


def local_today(settings: CoreSettings) -> date:
    return datetime.now(settings.timezone).date()


def seconds_until_midnight(settings: CoreSettings) -> float:
    now = datetime.now(settings.timezone)
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), settings.timezone)
    return max(1.0, (midnight - now).total_seconds())


class SenderLoop:
    """Strictly sequential: one message at a time, random pause, daily limits.

    The channel handler owns the attempt: mark_sending / finish_outgoing / defer.
    It may call `pause()` to stop all sending for a while (e.g. account-level flood wait).
    """

    def __init__(self, settings: CoreSettings, store: Store, handler: OutgoingHandler, wake: Wake) -> None:
        self.s = settings
        self.store = store
        self.handler = handler
        self.wake = wake
        self.limit_reached = False
        self.paused_until = 0.0

    def pause(self, seconds: float) -> None:
        self.paused_until = max(self.paused_until, time.time() + seconds)

    async def run(self) -> None:
        while True:
            if (wait := self.paused_until - time.time()) > 0:
                await asyncio.sleep(min(wait, 60))
                continue
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
            try:
                await self.handler(row, today)
            except Exception:  # noqa: BLE001 - must never kill the loop
                log.exception("send_handler_crashed", extra={"message_id": row.message_id})
                current = self.store.get_outgoing(row.message_id)
                if current is not None and current.state in ("queued", "sending"):
                    self.store.finish_outgoing(row.message_id, "error", error="internal error")
            await asyncio.sleep(random.uniform(self.s.send_delay_min, self.s.send_delay_max))


class OdooDelivery:
    """Strict FIFO delivery of incoming messages and statuses to Odoo, with backoff."""

    def __init__(
        self, settings: CoreSettings, store: Store, wake: Wake, client: httpx.AsyncClient | None = None,
    ) -> None:
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
            retry = backoff(5.0, 300.0, cb.attempts)
            self.store.callback_failed(cb.id, f"network: {type(exc).__name__}", retry)
            log.warning("odoo_unreachable", extra={"callback_id": cb.id, "kind": cb.kind, "retry_in": retry})
            return False
        if 200 <= resp.status_code < 300:
            self.store.callback_done(cb.id)
            log.info("odoo_delivered", extra={"callback_id": cb.id, "kind": cb.kind})
            return True
        # Never drop data: 4xx usually means config error (secret/route) and needs a human.
        base, cap = (60.0, 3600.0) if 400 <= resp.status_code < 500 else (5.0, 300.0)
        retry = backoff(base, cap, cb.attempts)
        self.store.callback_failed(cb.id, f"http {resp.status_code}", retry)
        log.error(
            "odoo_rejected",
            extra={"callback_id": cb.id, "kind": cb.kind, "status_code": resp.status_code, "retry_in": retry},
        )
        return False
