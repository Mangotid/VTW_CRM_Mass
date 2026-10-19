"""Telegram userbot bridge entrypoint (uvicorn app.main:app)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from typing import Any, Coroutine

from telethon import errors, functions

from bridge_core.api import BridgeBase, ChannelHealth, create_app
from bridge_core.logs import mask_phone
from bridge_core.store import OutboxRow, Store

from .config import Settings
from .gateway import (
    Gateway, Peer, TelethonGateway, build_client, incoming_payload, protect_session_file, should_forward,
)

log = logging.getLogger("telegram.bridge")

PEER_CACHE_PREFIX = "tg.peer:"
FLOOD_MARGIN = 5.0
PEER_FLOOD_PAUSE = 1800.0
IMPORT_RETRY_DELAY = 600.0
RETRY_DELAY = 30.0
MAX_ATTEMPTS = 2  # first try + one retry

SESSION_ERRORS = (errors.UnauthorizedError, errors.AuthKeyDuplicatedError)
NOT_FOUND_ERRORS = (errors.PhoneNumberInvalidError, errors.InputUserDeactivatedError)


class TelegramBridge(BridgeBase):
    settings: Settings

    def __init__(self, settings: Settings, store: Store, gateway: Gateway, client: Any = None) -> None:
        super().__init__(settings, store)
        self.gateway = gateway
        self.client = client  # TelegramClient in production, None in tests
        self.authorized = False
        self.connected = False
        self.last_event: float | None = None

    # -- outgoing -------------------------------------------------------------

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        mid = row.message_id
        if not row.text:
            self.failed(mid, "error", "empty text")
            return
        self.store.mark_sending(mid, today, count=row.attempts == 0)
        log.info("send_attempt", extra={"message_id": mid, "attempt": row.attempts + 1, "phone": mask_phone(row.phone)})
        try:
            peer = await self._resolve(row.phone)
            if peer is None:
                self.failed(mid, "not_found", "номер не знайдено в Telegram або користувач приховує номер")
                return
            if peer == "retry":
                self._defer(mid, IMPORT_RETRY_DELAY, "contact import rate limited")
                return
            msg_id = await self.gateway.send_text(peer, row.text)
            self.sent(mid, f"{peer.user_id}:{msg_id}")
            self.last_event = time.time()
        except errors.FloodWaitError as exc:
            wait = float(exc.seconds) + FLOOD_MARGIN
            self.sender.pause(wait)
            self._defer(mid, wait, f"flood wait {exc.seconds}s")
        except errors.PeerFloodError:
            # Account is limited for writing to non-contacts; stop for a while, needs a human look.
            self.sender.pause(PEER_FLOOD_PAUSE)
            self.failed(mid, "error", "Telegram обмежив акаунт (PeerFlood): забагато повідомлень новим контактам")
        except NOT_FOUND_ERRORS:
            self._forget(row.phone)
            self.failed(mid, "not_found", "номер недійсний або акаунт Telegram видалено")
        except (errors.UserPrivacyRestrictedError, errors.UserIsBlockedError, errors.YouBlockedUserError) as exc:
            self.failed(mid, "error", f"користувач обмежив повідомлення ({type(exc).__name__})")
        except errors.PeerIdInvalidError:
            # Cached access_hash no longer valid: drop it and resolve again on the next attempt.
            self._forget(row.phone)
            self._retry_or_fail(row, "stale contact cache")
        except SESSION_ERRORS as exc:
            self.authorized = False
            self.failed(mid, "error", f"сесія Telegram недійсна ({type(exc).__name__}), потрібен повторний вхід")
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            self._retry_or_fail(row, f"network: {type(exc).__name__}")
        except errors.RPCError as exc:
            self.failed(mid, "error", f"telegram: {type(exc).__name__}")

    def _retry_or_fail(self, row: OutboxRow, reason: str) -> None:
        """One more attempt later, then error (row.attempts is the value before this attempt)."""
        if row.attempts + 1 < MAX_ATTEMPTS:
            self._defer(row.message_id, RETRY_DELAY, reason)
        else:
            self.failed(row.message_id, "error", reason)

    def _defer(self, message_id: int, seconds: float, reason: str) -> None:
        self.store.defer(message_id, time.time() + seconds, reason)
        log.warning("send_deferred", extra={"message_id": message_id, "retry_in": seconds, "reason": reason})

    async def _resolve(self, phone: str) -> Peer | str | None:
        cached = self.store.kv_get(PEER_CACHE_PREFIX + phone)
        if cached:
            data = json.loads(cached)
            return Peer(int(data["user_id"]), int(data["access_hash"]))
        result = await self.gateway.import_contact(phone, f"{self.settings.contact_label} {phone}")
        if result.retry:
            return "retry"
        if result.peer is not None:
            self._remember(phone, result.peer)
        return result.peer

    def _remember(self, phone: str, peer: Peer) -> None:
        self.store.kv_set(
            PEER_CACHE_PREFIX + phone, json.dumps({"user_id": peer.user_id, "access_hash": peer.access_hash}),
        )

    def _forget(self, phone: str) -> None:
        self.store.kv_delete(PEER_CACHE_PREFIX + phone)

    # -- incoming -------------------------------------------------------------

    async def on_new_message(self, event: Any) -> None:
        try:
            message = event.message
            sender = await event.get_sender()
            if not should_forward(message, sender):
                return
            payload = incoming_payload(message, sender)
            if payload["phone"] and sender.access_hash is not None:
                self._remember(payload["phone"], Peer(sender.id, sender.access_hash))
            if self.store.enqueue_incoming(payload):
                self.last_event = time.time()
                self.delivery_wake.set()
                log.info("incoming_queued", extra={
                    "external_id": payload["external_id"],
                    "phone": mask_phone(payload["phone"]) if payload["phone"] else None,
                })
        except Exception:  # noqa: BLE001 - one bad update must not break the handler
            log.exception("incoming_failed")

    # -- lifecycle / health ---------------------------------------------------

    async def startup(self) -> None:
        # Never block here: the API (and /health) must come up even if Telegram is unreachable.
        logging.getLogger("telethon").setLevel(logging.WARNING)
        if self.client is not None:
            self.gateway.on_private_message(self.on_new_message)  # type: ignore[attr-defined]

    async def shutdown(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            protect_session_file(self.settings.tg)

    def extra_tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self._monitor()] if self.client is not None else []

    async def _monitor(self) -> None:
        while True:
            await self._check_session(connect=not self.client.is_connected())
            await asyncio.sleep(self.settings.health_interval)

    async def _check_session(self, connect: bool) -> None:
        try:
            if connect:
                await self.client.connect()
                protect_session_file(self.settings.tg)
            self.connected = self.client.is_connected()
            if not await self.client.is_user_authorized():
                if self.authorized or connect:
                    log.error("telegram_not_authorized", extra={"hint": "run login.py"})
                self.authorized = False
                return
            # Cheap request that fails if the session was revoked from another device.
            await self.client(functions.updates.GetStateRequest())
            if not self.authorized:
                log.info("telegram_authorized")
            self.authorized = True
        except SESSION_ERRORS as exc:
            self.authorized = False
            log.error("telegram_session_invalid", extra={"error": type(exc).__name__})
        except (ConnectionError, OSError, asyncio.TimeoutError) as exc:
            self.connected = False
            log.warning("telegram_connect_failed", extra={"error": type(exc).__name__})

    def send_unavailable(self) -> str | None:
        return None if self.authorized else "telegram_logged_out"

    def channel_health(self) -> ChannelHealth:
        reasons = []
        if self.authorized and not self.connected:
            reasons.append("telegram_disconnected")
        return ChannelHealth(
            logged_in=self.authorized,
            reasons=reasons,
            details={"connected": self.connected, "authorized": self.authorized},
            last_activity=self.last_event,
        )


def build() -> TelegramBridge:
    s = Settings.from_env()
    client = build_client(s.tg, catch_up=True, flood_sleep_threshold=s.flood_sleep_threshold)
    return TelegramBridge(s, Store(s.state_db_path), TelethonGateway(client), client)


app = create_app(factory=build, title="PDM Telegram userbot bridge")
