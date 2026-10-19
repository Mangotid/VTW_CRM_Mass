"""Bridge <-> Odoo contract (see CLAUDE.md, section "Контракт")."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

E164_RE = re.compile(r"^\+[1-9]\d{9,14}$")

OutState = Literal["queued", "sending", "sent", "error", "not_found"]
FinalState = Literal["sent", "error", "not_found"]


def normalize_phone(value: str) -> str:
    """Strip formatting characters; require E.164 with leading '+'."""
    cleaned = re.sub(r"[\s\-()]", "", value or "")
    if not E164_RE.match(cleaned):
        raise ValueError("phone must be in E.164 format, e.g. +380671234567")
    return cleaned


class Attachment(BaseModel):
    url: str
    filename: str
    mimetype: str


class SendRequest(BaseModel):
    message_id: int = Field(gt=0)
    phone: str
    text: str = Field(min_length=1, max_length=7000)
    attachments: list[Attachment] = Field(default_factory=list)

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        return normalize_phone(v)


class IncomingPayload(BaseModel):
    external_id: str
    phone: str
    name: str | None
    text: str
    attachments: list[Attachment] = Field(default_factory=list)
    timestamp: datetime


class StatusPayload(BaseModel):
    message_id: int
    state: FinalState
    error: str | None = None
    external_id: str | None = None


class DailyUsage(BaseModel):
    sent: int
    limit: int
    new_contacts: int
    new_contacts_limit: int


class HealthResponse(BaseModel):
    status: Literal["connected", "logged_out", "degraded"]
    channel: str
    reasons: list[str]
    queue_length: int
    callbacks_pending: int
    last_activity: datetime | None
    sending_enabled: bool
    viber_running: bool
    viber_db_found: bool
    watcher: str
    daily: DailyUsage
