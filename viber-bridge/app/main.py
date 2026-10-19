"""FastAPI entrypoint: POST /send, GET /health (contract in CLAUDE.md)."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated, AsyncIterator

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .logs import mask_phone, setup_logging
from .models import DailyUsage, HealthResponse, SendRequest
from .store import EnqueueResult, Store
from .viber_db import SchemaPendingViberDb, ViberDb
from .viber_ui import ViberUiDriver, X11ViberUi
from .workers import OdooDelivery, Sender, Wake, Watcher, debug_cleanup_loop, local_today

log = logging.getLogger("viber.api")


class Bridge:
    """Wires settings, storage, drivers and workers together."""

    def __init__(self, settings: Settings, store: Store, ui: ViberUiDriver, db: ViberDb) -> None:
        self.settings = settings
        self.store = store
        self.ui = ui
        self.db = db
        self.send_wake = Wake()
        self.delivery_wake = Wake()
        self.sender = Sender(settings, store, ui, db, self.send_wake, self.delivery_wake)
        self.watcher = Watcher(settings, store, db, self.delivery_wake)
        self.delivery = OdooDelivery(settings, store, self.delivery_wake)

    def start(self) -> list[asyncio.Task[None]]:
        interrupted = self.store.recover_interrupted()
        if interrupted:
            log.warning("interrupted_messages_marked_error", extra={"message_ids": interrupted})
        return [
            asyncio.create_task(self.sender.run(), name="sender"),
            asyncio.create_task(self.watcher.run(), name="watcher"),
            asyncio.create_task(self.delivery.run(), name="delivery"),
            asyncio.create_task(debug_cleanup_loop(self.settings), name="debug_cleanup"),
        ]


def get_bridge(request: Request) -> Bridge:
    return request.app.state.bridge


def require_secret(
    request: Request,
    x_bridge_secret: Annotated[str | None, Header()] = None,
) -> None:
    expected = get_bridge(request).settings.bridge_secret.encode()
    if not x_bridge_secret or not hmac.compare_digest(x_bridge_secret.encode(), expected):
        raise HTTPException(status_code=401, detail="invalid bridge secret")


BridgeDep = Annotated[Bridge, Depends(get_bridge)]


def create_app(bridge: Bridge | None = None, start_workers: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal bridge
        if bridge is None:
            setup_logging(os.environ.get("LOG_LEVEL", "INFO"))
            settings = Settings.from_env()
            bridge = Bridge(
                settings,
                Store(settings.state_db_path),
                X11ViberUi(
                    settings.display, settings.viber_open_cmd, settings.viber_window_class,
                    settings.ui_step_delay, settings.debug_dir,
                ),
                SchemaPendingViberDb(settings.viber_home, settings.viber_account),
            )
        app.state.bridge = bridge
        tasks = bridge.start() if start_workers else []
        log.info("bridge_started", extra={"version": __version__, "sending_enabled": bridge.settings.sending_enabled})
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            bridge.store.close()

    app = FastAPI(title="PDM Viber bridge", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.post("/send", status_code=202, dependencies=[Depends(require_secret)])
    def send(req: SendRequest, b: BridgeDep) -> JSONResponse:
        s = b.settings
        if req.attachments:
            raise HTTPException(status_code=422, detail="attachments_not_supported")
        if not s.sending_enabled:
            raise HTTPException(status_code=503, detail="sending_disabled")
        if b.db.locate() is None:
            raise HTTPException(status_code=503, detail="viber_logged_out")
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


def build_health(b: Bridge) -> HealthResponse:
    s = b.settings
    reasons: list[str] = []
    db_path = b.db.locate()
    running = b.ui.viber_running()
    callbacks_pending, oldest_cb = b.store.callbacks_stats()

    if not running:
        reasons.append("viber_not_running")
    if db_path is None:
        reasons.append("viber_db_not_found")
    if b.watcher.state != "ok":
        reasons.append(f"watcher_{b.watcher.state}")
    if oldest_cb is not None and datetime.now(timezone.utc).timestamp() - oldest_cb > s.callback_stale_after:
        reasons.append("odoo_delivery_stalled")
    if b.sender.limit_reached:
        reasons.append("daily_limit_reached")
    if not s.sending_enabled:
        reasons.append("sending_disabled")

    if db_path is None:
        status = "logged_out"
    elif [r for r in reasons if r not in ("daily_limit_reached", "sending_disabled")]:
        status = "degraded"
    else:
        status = "connected"

    activity = [t for t in (b.store.last_activity(),) if t]
    if db_path is not None:
        try:
            activity.append(db_path.stat().st_mtime)
        except OSError:
            pass
    sent, new_contacts = b.store.usage(local_today(s))
    return HealthResponse(
        status=status,
        channel=s.channel,
        reasons=reasons,
        queue_length=b.store.queue_length(),
        callbacks_pending=callbacks_pending,
        last_activity=datetime.fromtimestamp(max(activity), timezone.utc) if activity else None,
        sending_enabled=s.sending_enabled,
        viber_running=running,
        viber_db_found=db_path is not None,
        watcher=b.watcher.state,
        daily=DailyUsage(
            sent=sent, limit=s.daily_limit_total,
            new_contacts=new_contacts, new_contacts_limit=s.daily_limit_new_contacts,
        ),
    )


app = create_app()
