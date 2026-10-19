"""WhatsApp adapter entrypoint (uvicorn app.main:app): contract API + Evolution webhook receiver."""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from datetime import date
from typing import Any, Awaitable, Callable

import httpx
from fastapi import HTTPException, Request

from bridge_core.api import BridgeBase, ChannelHealth, create_app
from bridge_core.logs import mask_phone
from bridge_core.store import OutboxRow, Store

from .config import Settings
from .evolution import EvolutionClient, EvolutionError
from .mapping import parse_upsert

log = logging.getLogger("whatsapp.adapter")

RETRY_DELAY = 30.0
MAX_ATTEMPTS = 2  # first try + one retry, only before the send request was made


class WhatsAppBridge(BridgeBase):
    settings: Settings

    def __init__(self, settings: Settings, store: Store, evo: EvolutionClient) -> None:
        super().__init__(settings, store)
        self.evo = evo
        self.state = "unknown"          # Evolution connection state: open | connecting | close | unknown
        self.evolution_reachable = False
        self.webhook_ready = False
        self.skipped: dict[str, int] = {}
        self.last_event: float | None = None

    # -- outgoing -------------------------------------------------------------

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        mid = row.message_id
        if not row.text:
            self.failed(mid, "error", "empty text")
            return
        self.store.mark_sending(mid, today, count=row.attempts == 0)
        digits = row.phone.lstrip("+")
        log.info("send_attempt", extra={"message_id": mid, "attempt": row.attempts + 1, "phone": mask_phone(row.phone)})
        stage = "check"
        try:
            if not await self.evo.number_exists(digits):
                self.failed(mid, "not_found", "номер не зареєстрований у WhatsApp")
                return
            stage = "send"
            ext = await self.evo.send_text(digits, row.text)
            self.sent(mid, f"wa:{ext}" if ext else None)
            self.last_event = time.time()
        except EvolutionError as exc:
            if exc.not_on_whatsapp():
                self.failed(mid, "not_found", "номер не зареєстрований у WhatsApp")
            elif stage == "check" and exc.status >= 500:
                self._retry_or_fail(row, f"evolution {exc.status}")
            elif stage == "send" and exc.status >= 500:
                self.failed(mid, "error", f"evolution {exc.status} під час відправки: доставка невідома")
            else:
                self.failed(mid, "error", f"evolution {exc.status}")
        except (httpx.TransportError, asyncio.TimeoutError) as exc:
            if stage == "send":
                # The request may have reached Evolution: a resend could duplicate the message.
                self.failed(mid, "error", f"зв'язок з Evolution обірвався під час відправки ({type(exc).__name__}): "
                                          "доставка невідома, перевірте чат перед повтором")
            else:
                self._retry_or_fail(row, f"network: {type(exc).__name__}")

    def _retry_or_fail(self, row: OutboxRow, reason: str) -> None:
        if row.attempts + 1 < MAX_ATTEMPTS:
            self.store.defer(row.message_id, time.time() + RETRY_DELAY, reason)
            log.warning("send_deferred", extra={"message_id": row.message_id, "retry_in": RETRY_DELAY, "reason": reason})
        else:
            self.failed(row.message_id, "error", reason)

    # -- incoming -------------------------------------------------------------

    def on_webhook(self, event: dict[str, Any]) -> str:
        name = (event.get("event") or "").lower()
        if name == "connection.update":
            state = (event.get("data") or {}).get("state")
            if state:
                self.state = str(state)
                log.info("whatsapp_connection", extra={"state": self.state})
            return "connection"
        parsed = parse_upsert(event, time.time(), self.settings.incoming_max_age)
        if parsed.payload is None:
            self.skipped[parsed.reason] = self.skipped.get(parsed.reason, 0) + 1
            return parsed.reason
        if self.store.enqueue_incoming(parsed.payload):
            self.last_event = time.time()
            self.delivery_wake.set()
            log.info("incoming_queued", extra={
                "external_id": parsed.payload["external_id"], "phone": mask_phone(parsed.payload["phone"]),
            })
        return "ok"

    # -- lifecycle / health -----------------------------------------------------

    def extra_tasks(self) -> dict[str, Callable[[], Awaitable[None]]]:
        return {"evolution_monitor": self._monitor}

    async def _monitor(self) -> None:
        while True:
            await self.check_evolution()
            await asyncio.sleep(self.settings.state_interval)

    async def check_evolution(self) -> None:
        evo = self.settings.evo
        try:
            if not self.webhook_ready:
                created = await self.evo.ensure_instance(evo.webhook_url, evo.webhook_secret)
                self.webhook_ready = True
                log.info("evolution_instance_ready", extra={"instance": evo.instance, "created": created})
            self.state = await self.evo.connection_state()
            self.evolution_reachable = True
        except (httpx.HTTPError, EvolutionError) as exc:
            self.evolution_reachable = False
            log.warning("evolution_unreachable", extra={"error": type(exc).__name__})

    async def shutdown(self) -> None:
        await self.evo.aclose()

    def send_unavailable(self) -> str | None:
        return None if self.state == "open" else "whatsapp_logged_out"

    def channel_health(self) -> ChannelHealth:
        reasons = []
        if not self.evolution_reachable:
            reasons.append("evolution_unreachable")
        elif not self.webhook_ready:
            reasons.append("webhook_not_set")
        return ChannelHealth(
            logged_in=self.state == "open",
            reasons=reasons,
            details={"state": self.state, "instance": self.settings.evo.instance,
                     "webhook_ready": self.webhook_ready, "skipped": dict(self.skipped)},
            last_activity=self.last_event,
        )


def build() -> WhatsAppBridge:
    s = Settings.from_env()
    return WhatsAppBridge(s, Store(s.state_db_path), EvolutionClient(s.evo.url, s.evo.api_key, s.evo.instance))


def add_webhook_route(app: Any) -> None:
    @app.post("/evolution/webhook")
    async def evolution_webhook(request: Request) -> dict[str, Any]:
        bridge: WhatsAppBridge = request.app.state.bridge
        got = request.headers.get("x-webhook-secret") or ""
        if not hmac.compare_digest(got.encode(), bridge.settings.evo.webhook_secret.encode()):
            raise HTTPException(status_code=401, detail="invalid webhook secret")
        try:
            event = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid json")
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="invalid payload")
        # Always 200 for an authentic event: Evolution must not retry skipped (group, old) messages.
        return {"ok": True, "result": bridge.on_webhook(event)}


app = create_app(factory=build, title="PDM WhatsApp adapter")
add_webhook_route(app)
