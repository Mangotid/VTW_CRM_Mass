"""Thin wrapper over Telethon: everything the bridge needs from Telegram, nothing more."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from telethon import TelegramClient, events, functions, types

from .config import TelegramApi

SERVICE_USER_ID = 777000  # Telegram service notifications (login codes!) — never forward.


@dataclass(frozen=True)
class Peer:
    user_id: int
    access_hash: int


@dataclass(frozen=True)
class ImportResult:
    peer: Peer | None
    retry: bool  # Telegram asked to retry this contact later (rate limited)


class Gateway(Protocol):
    async def import_contact(self, phone: str, first_name: str) -> ImportResult: ...

    async def send_text(self, peer: Peer, text: str) -> int: ...


def build_client(api: TelegramApi, catch_up: bool, flood_sleep_threshold: int = 30) -> TelegramClient:
    api.session_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    client = TelegramClient(
        str(api.session_path),
        api.api_id,
        api.api_hash,
        device_model=api.device_model,
        system_version=api.system_version,
        app_version=api.app_version,
        lang_code="uk",
        system_lang_code="uk",
        catch_up=catch_up,
        flood_sleep_threshold=flood_sleep_threshold,
    )
    protect_session_file(api)  # the session file is created by the constructor
    return client


def protect_session_file(api: TelegramApi) -> None:
    for suffix in (".session", ".session-journal"):
        path = api.session_path.with_name(api.session_path.name + suffix)
        if path.exists():
            os.chmod(path, 0o600)


class TelethonGateway:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    async def import_contact(self, phone: str, first_name: str) -> ImportResult:
        contact = types.InputPhoneContact(
            client_id=random.randrange(1, 2**62), phone=phone, first_name=first_name, last_name="",
        )
        res = await self.client(functions.contacts.ImportContactsRequest([contact]))
        if res.retry_contacts:
            return ImportResult(None, retry=True)
        imported_ids = {ic.user_id for ic in res.imported}
        for user in res.users:
            if isinstance(user, types.User) and user.id in imported_ids and user.access_hash is not None:
                return ImportResult(Peer(user.id, user.access_hash), retry=False)
        # Not on Telegram, or the user hides their number from people who are not their contacts.
        return ImportResult(None, retry=False)

    async def send_text(self, peer: Peer, text: str) -> int:
        # parse_mode=None: operator text is sent verbatim (no Markdown interpretation).
        msg = await self.client.send_message(
            types.InputPeerUser(peer.user_id, peer.access_hash), text, parse_mode=None, link_preview=False,
        )
        return msg.id

    def on_private_message(self, callback: Callable[[Any], Awaitable[None]]) -> None:
        self.client.add_event_handler(callback, events.NewMessage(incoming=True))


# -- incoming mapping ---------------------------------------------------------

# Order matters: stickers, GIFs, voice and round videos are also documents/videos.
_MEDIA_LABELS = (
    ("sticker", "стікер"),
    ("gif", "GIF"),
    ("voice", "голосове повідомлення"),
    ("video_note", "відеоповідомлення"),
    ("photo", "фото"),
    ("video", "відео"),
    ("audio", "аудіо"),
    ("contact", "контакт"),
    ("geo", "геолокація"),
    ("poll", "опитування"),
    ("document", "файл"),
)


def media_label(message: Any) -> str | None:
    for attr, label in _MEDIA_LABELS:
        if getattr(message, attr, None):
            return label
    return None


def should_forward(message: Any, sender: Any) -> bool:
    """Only private chats with real people. Groups, channels, bots, service and self are ignored."""
    if not getattr(message, "is_private", False) or getattr(message, "out", False):
        return False
    if not isinstance(sender, types.User):
        return False
    if sender.bot or sender.is_self or sender.support or sender.deleted:
        return False
    return sender.id != SERVICE_USER_ID


def incoming_payload(message: Any, sender: types.User) -> dict[str, Any]:
    text = message.raw_text or ""
    label = media_label(message)
    if label:
        # Attachments are not transferred yet; tell the operator something was sent.
        text = f"{text}\n[вкладення: {label}]" if text else f"[вкладення: {label}]"
    name = " ".join(p for p in (sender.first_name, sender.last_name) if p) or None
    return {
        "external_id": f"{sender.id}:{message.id}",
        "phone": f"+{sender.phone}" if sender.phone else None,
        "name": name,
        "text": text,
        "attachments": [],
        "timestamp": message.date.isoformat(),
        "user_id": str(sender.id),
        "username": sender.username,
    }
