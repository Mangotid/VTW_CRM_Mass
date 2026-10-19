"""Bridge base class and FastAPI app: POST /send, GET /health (contract in CLAUDE.md)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .logs import mask_phone, setup_logging
from .models import DailyUsage, HealthResponse, SendRequest
from .settings import CoreSettings
from .store import EnqueueResult, OutboxRow, Store
from .workers import OdooDelivery, SenderLoop, Wake, local_today, supervise

log = logging.getLogger("bridge.api")

# Reasons that are reported but do not make the bridge "degraded".
SOFT_REASONS = frozenset({"daily_limit_reached", "sending_disabled"})


@dataclass
class ChannelHealth:
    logged_in: bool
    reasons: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    last_activity: float | None = None


class BridgeBase:
    """Channel bridges subclass this and implement `handle_outgoing` and `channel_health`."""

    def __init__(self, settings: CoreSettings, store: Store) -> None:
        self.settings = settings
        self.store = store
        self.send_wake = Wake()
        self.delivery_wake = Wake()
        self.sender = SenderLoop(settings, store, self.handle_outgoing, self.send_wake)
        self.delivery = OdooDelivery(settings, store, self.delivery_wake)

    # -- to implement ------------------------------------------------------

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        raise NotImplementedError

    def channel_health(self) -> ChannelHealth:
        raise NotImplementedError

    def send_unavailable(self) -> str | None:
        """Return a 503 detail if /send must be refused right now."""
        return None

    def extra_tasks(self) -> dict[str, Callable[[], Awaitable[None]]]:
        """Background loops by name; each factory is called again if its loop crashes."""
        return {}

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    # -- common ------------------------------------------------------------

    def start(self) -> list[asyncio.Task[None]]:
        interrupted = self.store.recover_interrupted()
        if interrupted:
            log.warning("interrupted_messages_marked_error", extra={"message_ids": interrupted})
            self.delivery_wake.set()
        loops = {"sender": self.sender.run, "odoo_delivery": self.delivery.run, **self.extra_tasks()}
        return [asyncio.create_task(supervise(name, f), name=name) for name, f in loops.items()]

    def sent(self, message_id: int, external_id: str | None) -> None:
        self.store.finish_outgoing(message_id, "sent", external_id=external_id)
        log.info("send_ok", extra={"message_id": message_id, "external_id": external_id})
        self.delivery_wake.set()

    def failed(self, message_id: int, state: str, error: str) -> None:
        self.store.finish_outgoing(message_id, state, error=error)
        log.warning("send_failed", extra={"message_id": message_id, "state": state, "error": error})
        self.delivery_wake.set()


def get_bridge(request: Request) -> BridgeBase:
    return request.app.state.bridge


def require_secret(
    request: Request,
    x_bridge_secret: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_bridge(request).settings.bridge_secret.encode()
    if not x_bridge_secret or not hmac.compare_digest(x_bridge_secret.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid bridge secret")


BridgeDep = Annotated[BridgeBase, Depends(get_bridge)]


def create_app(
    factory: Callable[[], BridgeBase] | None = None,
    bridge: BridgeBase | None = None,
    start_workers: bool = True,
    title: str = "PDM bridge",
) -> FastAPI:
    """Pass `bridge` (tests) or `factory` (production; called inside lifespan)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal bridge
        if bridge is None:
            assert factory is not None
            setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
            bridge = factory()
        app.state.bridge = bridge
        if start_workers:
            await bridge.startup()
        tasks = bridge.start() if start_workers else []
        log.info("bridge_started", extra={
            "channel": bridge.settings.channel, "version": __version__,
            "sending_enabled": bridge.settings.sending_enabled,
        })
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if start_workers:
                await bridge.shutdown()
            bridge.store.close()

    app = FastAPI(title=title, version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/send", status_code=202, dependencies=[Depends(require_secret)])
    def send(req: SendRequest, b: BridgeDep) -> JSONResponse:
        s = b.settings
        if req.attachments:
            raise HTTPException(status_code=422, detail="attachments_not_supported")
        if not s.sending_enabled:
            raise HTTPException(status_code=503, detail="sending_disabled")
        if (unavailable := b.send_unavailable()) is not None:
            raise HTTPException(status_code=503, detail=unavailable)
        result, existing = b.store.enqueue_outgoing(
            req.message_id, req.phone, req.text, local_today(s),
            s.daily_limit_total, s.daily_limit_new_contacts,
        )
        extra = {"message_id": req.message_id, "phone": mask_phone(req.phone), "result": result}
        if result == EnqueueResult.CONFLICT:
            log.warning("send_conflict", extra=extra)
            raise HTTPException(status_code=409, detail="message_id_reused_with_other_phone")
        if result in (EnqueueResult.LIMIT_TOTAL, EnqueueResult.LIMIT_NEW):
            log.warning("send_rejected_limit", extra=extra)
            raise HTTPException(status_code=429, detail=result)
        if result == EnqueueResult.DUPLICATE and existing is not None:
            b.store.requeue_status(existing)
            b.delivery_wake.set()
            log.info("send_duplicate", extra=extra)
            return JSONResponse({"accepted": True, "duplicate": True, "state": existing.state}, status_code=202)
        log.info("send_queued", extra=extra)
        b.send_wake.set()
        return JSONResponse({"accepted": True}, status_code=202)

    @app.get("/health", dependencies=[Depends(require_secret)])
    def health(b: BridgeDep) -> HealthResponse:
        return build_health(b)

    return app


def build_health(b: BridgeBase) -> HealthResponse:
    s = b.settings
    ch = b.channel_health()
    reasons = list(ch.reasons)
    callbacks_pending, oldest_cb = b.store.callbacks_stats()
    now = time.time()
    if oldest_cb is not None and now - oldest_cb > s.callback_stale_after:
        reasons.append("odoo_delivery_stalled")
    if b.sender.paused_until > now:
        reasons.append("sender_paused")
    if b.sender.limit_reached:
        reasons.append("daily_limit_reached")
    if not s.sending_enabled:
        reasons.append("sending_disabled")

    if not ch.logged_in:
        status = "logged_out"
    elif any(r not in SOFT_REASONS for r in reasons):
        status = "degraded"
    else:
        status = "connected"

    activity = [t for t in (b.store.last_activity(), ch.last_activity) if t]
    details = dict(ch.details)
    if b.sender.paused_until > now:
        details["sender_paused_until"] = datetime.fromtimestamp(b.sender.paused_until, timezone.utc).isoformat()
    sent, new_contacts = b.store.usage(local_today(s))
    return HealthResponse(
        status=status,
        channel=s.channel,
        reasons=reasons,
        queue_length=b.store.queue_length(),
        callbacks_pending=callbacks_pending,
        last_activity=datetime.fromtimestamp(max(activity), timezone.utc) if activity else None,
        sending_enabled=s.sending_enabled,
        daily=DailyUsage(
            sent=sent, limit=s.daily_limit_total,
            new_contacts=new_contacts, new_contacts_limit=s.daily_limit_new_contacts,
        ),
        details=details,
    )
