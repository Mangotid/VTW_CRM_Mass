"""Evolution API webhook (messages.upsert) -> contract incoming payload. Pure functions, no I/O.

Shape (Evolution 2.3.7, prepareMessage): {"event": "messages.upsert", "instance": ..., "data": {
  "key": {"remoteJid", "fromMe", "id", "remoteJidAlt"?, "addressingMode"?}, "pushName", "message": {...},
  "messageType", "messageTimestamp" (seconds)}}.
Evolution already swaps an @lid remoteJid for remoteJidAlt (the phone JID) when it knows it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

USER_JID_RE = re.compile(r"^(\d{8,15})(?::\d+)?@s\.whatsapp\.net$")

# messageType -> label for the operator (attachments themselves are not transferred).
_MEDIA_LABELS = {
    "imageMessage": "фото",
    "videoMessage": "відео",
    "ptvMessage": "відеоповідомлення",
    "audioMessage": "аудіо",
    "documentMessage": "файл",
    "stickerMessage": "стікер",
    "locationMessage": "геолокація",
    "liveLocationMessage": "геолокація",
    "contactMessage": "контакт",
    "contactsArrayMessage": "контакти",
    "pollCreationMessage": "опитування",
    "pollCreationMessageV3": "опитування",
}
# Not customer messages: reactions, deletes/edits, poll votes, receipts, key distribution.
_IGNORED_TYPES = {
    "reactionMessage", "protocolMessage", "pollUpdateMessage", "senderKeyDistributionMessage",
    "messageContextInfo", "editedMessage", "keepInChatMessage", "unknown",
}


@dataclass(frozen=True)
class Parsed:
    payload: dict[str, Any] | None   # contract incoming payload, or None
    reason: str                      # "ok" or why it was skipped (for logs/metrics, no content)


def phone_from_jid(jid: str | None) -> str | None:
    m = USER_JID_RE.match(jid or "")
    return f"+{m.group(1)}" if m else None


def _text_of(message: dict[str, Any], mtype: str) -> str:
    if message.get("conversation"):
        return str(message["conversation"])
    ext = message.get("extendedTextMessage") or {}
    if ext.get("text"):
        return str(ext["text"])
    body = message.get(mtype) or {}
    return str(body.get("caption") or "") if isinstance(body, dict) else ""


def parse_upsert(event: dict[str, Any], now: float, max_age_seconds: float) -> Parsed:
    if (event.get("event") or "").lower() != "messages.upsert":
        return Parsed(None, "not_upsert")
    data = event.get("data") or {}
    key = data.get("key") or {}
    if key.get("fromMe"):
        return Parsed(None, "from_me")
    jid = key.get("remoteJid") or ""
    if jid.endswith("@g.us") or jid.endswith("@broadcast") or jid.endswith("@newsletter"):
        return Parsed(None, "not_private")
    phone = phone_from_jid(jid) or phone_from_jid(key.get("remoteJidAlt"))
    if phone is None:
        return Parsed(None, "no_phone")  # e.g. @lid without a known phone JID
    mtype = data.get("messageType") or "unknown"
    if mtype in _IGNORED_TYPES:
        return Parsed(None, "ignored_type")
    message = data.get("message") or {}
    text = _text_of(message, mtype)
    label = _MEDIA_LABELS.get(mtype)
    if label:
        text = f"{text}\n[вкладення: {label}]" if text else f"[вкладення: {label}]"
    if not text:
        return Parsed(None, "empty")
    ts = data.get("messageTimestamp")
    try:
        ts_f = float(ts)
    except (TypeError, ValueError):
        ts_f = now
    if now - ts_f > max_age_seconds:
        # Offline catch-up after linking / long downtime: do not flood Odoo with old messages.
        return Parsed(None, "too_old")
    msg_id = key.get("id")
    if not msg_id:
        return Parsed(None, "no_id")
    push = (data.get("pushName") or "").strip()
    return Parsed({
        "external_id": f"wa:{msg_id}",
        "phone": phone,
        "name": push if push and push != "Você" else None,
        "text": text,
        "attachments": [],
        "timestamp": datetime.fromtimestamp(ts_f, timezone.utc).isoformat(),
    }, "ok")
