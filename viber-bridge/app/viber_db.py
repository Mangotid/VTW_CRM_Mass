"""Read-only access to Viber Desktop's viber.db.

The SQL for reading events is intentionally NOT written yet: it must be based on the
actual schema studied on the live database (docs/viber-db-schema.md, phase 2 step 2).
Until then `SchemaPendingViberDb` raises `SchemaNotReady`, the watcher idles and
/health reports `degraded` with reason `viber_db_schema_pending`.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class SchemaNotReady(RuntimeError):
    pass


class ViberDbNotFound(RuntimeError):
    pass


@dataclass(frozen=True)
class IncomingEvent:
    event_id: int
    external_id: str
    phone: str
    name: str | None
    text: str
    timestamp: datetime


@dataclass(frozen=True)
class OutgoingEvent:
    event_id: int
    external_id: str
    phone: str


class ViberDb(Protocol):
    def locate(self) -> Path | None: ...

    def latest_event_id(self) -> int: ...

    def incoming_since(self, after_event_id: int, limit: int = 100) -> list[IncomingEvent]: ...

    def outgoing_since(self, after_event_id: int, phone: str) -> list[OutgoingEvent]: ...


def find_viber_db(viber_home: Path, account: str | None) -> Path | None:
    """~/.ViberPC/<account>/viber.db. Without VIBER_ACCOUNT, require exactly one profile."""
    if account:
        candidate = viber_home / account / "viber.db"
        return candidate if candidate.is_file() else None
    found = sorted(viber_home.glob("*/viber.db"))
    return found[0] if len(found) == 1 else None


def connect_ro(path: Path) -> sqlite3.Connection:
    """Open strictly read-only. No immutable=1: we need to see Viber's live writes (WAL)."""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5, check_same_thread=False)
    conn.execute("PRAGMA query_only=ON")
    return conn


class SchemaPendingViberDb:
    """Placeholder until the live schema is documented."""

    def __init__(self, viber_home: Path, account: str | None) -> None:
        self._home = viber_home
        self._account = account

    def locate(self) -> Path | None:
        return find_viber_db(self._home, self._account)

    def latest_event_id(self) -> int:
        raise SchemaNotReady("viber.db schema not studied yet")

    def incoming_since(self, after_event_id: int, limit: int = 100) -> list[IncomingEvent]:
        raise SchemaNotReady("viber.db schema not studied yet")

    def outgoing_since(self, after_event_id: int, phone: str) -> list[OutgoingEvent]:
        raise SchemaNotReady("viber.db schema not studied yet")
