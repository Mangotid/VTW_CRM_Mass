"""Local state in SQLite (data volume): outbox, Odoo callback queue, dedup, limits.

Message text is kept only while it is needed: outbox text is wiped once the
message reaches a final state, callback payloads are deleted once Odoo accepts them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    message_id     INTEGER PRIMARY KEY,
    phone          TEXT NOT NULL,
    text           TEXT,
    state          TEXT NOT NULL,
    attempts       INTEGER NOT NULL DEFAULT 0,
    is_new_contact INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    external_id    TEXT,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_state ON outbox(state, message_id);

CREATE TABLE IF NOT EXISTS callbacks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,           -- 'incoming' | 'status'
    dedup_key       TEXT NOT NULL UNIQUE,
    payload         TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL,
    last_error      TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_incoming (
    external_id TEXT PRIMARY KEY,
    seen_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS contacts (
    phone        TEXT PRIMARY KEY,
    first_out_at REAL,
    last_in_at   REAL
);

CREATE TABLE IF NOT EXISTS daily_counters (
    day   TEXT NOT NULL,
    kind  TEXT NOT NULL,                     -- 'total' | 'new_contact'
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind)
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

FINAL_STATES = ("sent", "error", "not_found")
ACTIVE_STATES = ("queued", "sending")


@dataclass(frozen=True)
class OutboxRow:
    message_id: int
    phone: str
    text: str | None
    state: str
    attempts: int
    is_new_contact: bool
    error: str | None
    external_id: str | None


@dataclass(frozen=True)
class Callback:
    id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    created_at: float


class EnqueueResult:
    CREATED = "created"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"
    LIMIT_TOTAL = "daily_limit"
    LIMIT_NEW = "daily_new_contact_limit"


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- helpers ---------------------------------------------------------

    def _tx(self) -> "_Tx":
        return _Tx(self)

    def _q(self, sql: str, args: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    # -- outbox ----------------------------------------------------------

    def enqueue_outgoing(
        self, message_id: int, phone: str, text: str, today: date,
        limit_total: int, limit_new: int,
    ) -> tuple[str, OutboxRow | None]:
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT * FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            if row is not None:
                existing = _outbox(row)
                if existing.phone != phone:
                    return EnqueueResult.CONFLICT, existing
                return EnqueueResult.DUPLICATE, existing

            day = today.isoformat()
            used_total = _counter(db, day, "total")
            used_new = _counter(db, day, "new_contact")
            queued_total, queued_new = db.execute(
                "SELECT COUNT(*), COALESCE(SUM(is_new_contact),0) FROM outbox WHERE state IN (?,?)",
                ACTIVE_STATES,
            ).fetchone()
            if used_total + queued_total >= limit_total:
                return EnqueueResult.LIMIT_TOTAL, None

            known = db.execute("SELECT 1 FROM contacts WHERE phone=?", (phone,)).fetchone()
            pending_same = db.execute(
                "SELECT 1 FROM outbox WHERE phone=? AND state IN ('queued','sending','sent') LIMIT 1",
                (phone,),
            ).fetchone()
            is_new = known is None and pending_same is None
            if is_new and used_new + queued_new >= limit_new:
                return EnqueueResult.LIMIT_NEW, None

            db.execute(
                "INSERT INTO outbox(message_id, phone, text, state, is_new_contact, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (message_id, phone, text, "queued", int(is_new), now, now),
            )
        return EnqueueResult.CREATED, None

    def next_queued(self) -> OutboxRow | None:
        rows = self._q("SELECT * FROM outbox WHERE state='queued' ORDER BY message_id LIMIT 1")
        return _outbox(rows[0]) if rows else None

    def get_outgoing(self, message_id: int) -> OutboxRow | None:
        rows = self._q("SELECT * FROM outbox WHERE message_id=?", (message_id,))
        return _outbox(rows[0]) if rows else None

    def limits_allow(self, row: OutboxRow, today: date, limit_total: int, limit_new: int) -> bool:
        day = today.isoformat()
        with self._tx() as db:
            if _counter(db, day, "total") >= limit_total:
                return False
            if row.is_new_contact and _counter(db, day, "new_contact") >= limit_new:
                return False
        return True

    def mark_sending(self, message_id: int, today: date, count: bool) -> None:
        """Mark attempt start. Counters are bumped once per message (first attempt)."""
        with self._tx() as db:
            row = db.execute("SELECT is_new_contact FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            db.execute(
                "UPDATE outbox SET state='sending', attempts=attempts+1, updated_at=? WHERE message_id=?",
                (time.time(), message_id),
            )
            if count:
                _bump(db, today.isoformat(), "total")
                if row and row["is_new_contact"]:
                    _bump(db, today.isoformat(), "new_contact")

    def finish_outgoing(
        self, message_id: int, state: str, error: str | None = None, external_id: str | None = None,
    ) -> None:
        """Set final state, wipe text, enqueue status callback for Odoo."""
        assert state in FINAL_STATES
        now = time.time()
        with self._tx() as db:
            row = db.execute("SELECT phone FROM outbox WHERE message_id=?", (message_id,)).fetchone()
            db.execute(
                "UPDATE outbox SET state=?, error=?, external_id=?, text=NULL, updated_at=? WHERE message_id=?",
                (state, error, external_id, now, message_id),
            )
            if state == "sent" and row is not None:
                db.execute(
                    "INSERT INTO contacts(phone, first_out_at) VALUES (?,?)"
                    " ON CONFLICT(phone) DO UPDATE SET first_out_at=COALESCE(first_out_at, excluded.first_out_at)",
                    (row["phone"], now),
                )
            _enqueue_status(db, message_id, state, error, external_id, now)

    def requeue_status(self, row: OutboxRow) -> None:
        """Resend the final status for a duplicate /send (Odoo may have missed it)."""
        if row.state not in FINAL_STATES:
            return
        with self._tx() as db:
            _enqueue_status(db, row.message_id, row.state, row.error, row.external_id, time.time())

    def recover_interrupted(self) -> list[int]:
        """Messages left in 'sending' after a crash: delivery unknown, never resend blindly."""
        ids = [r["message_id"] for r in self._q("SELECT message_id FROM outbox WHERE state='sending'")]
        for message_id in ids:
            self.finish_outgoing(message_id, "error", error="interrupted: delivery unknown")
        return ids

    def queue_length(self) -> int:
        return self._q("SELECT COUNT(*) FROM outbox WHERE state IN (?,?)", ACTIVE_STATES)[0][0]

    def usage(self, today: date) -> tuple[int, int]:
        with self._tx() as db:
            return _counter(db, today.isoformat(), "total"), _counter(db, today.isoformat(), "new_contact")

    # -- incoming --------------------------------------------------------

    def enqueue_incoming(self, payload: dict[str, Any]) -> bool:
        """Returns False if this external_id was already seen."""
        now = time.time()
        ext = payload["external_id"]
        with self._tx() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO seen_incoming(external_id, seen_at) VALUES (?,?)", (ext, now)
            )
            if cur.rowcount == 0:
                return False
            db.execute(
                "INSERT INTO contacts(phone, last_in_at) VALUES (?,?)"
                " ON CONFLICT(phone) DO UPDATE SET last_in_at=excluded.last_in_at",
                (payload["phone"], now),
            )
            db.execute(
                "INSERT OR IGNORE INTO callbacks(kind, dedup_key, payload, next_attempt_at, created_at)"
                " VALUES ('incoming', ?, ?, ?, ?)",
                (f"in:{ext}", json.dumps(payload, ensure_ascii=False, default=str), now, now),
            )
        return True

    # -- callbacks to Odoo ----------------------------------------------

    def next_callback(self, now: float) -> Callback | None:
        """Oldest pending callback, only if it is due (strict FIFO)."""
        rows = self._q("SELECT * FROM callbacks ORDER BY id LIMIT 1")
        if not rows or rows[0]["next_attempt_at"] > now:
            return None
        r = rows[0]
        return Callback(r["id"], r["kind"], json.loads(r["payload"]), r["attempts"], r["created_at"])

    def callback_done(self, callback_id: int) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM callbacks WHERE id=?", (callback_id,))

    def callback_failed(self, callback_id: int, error: str, retry_in: float) -> None:
        with self._tx() as db:
            db.execute(
                "UPDATE callbacks SET attempts=attempts+1, last_error=?, next_attempt_at=? WHERE id=?",
                (error[:500], time.time() + retry_in, callback_id),
            )

    def callbacks_stats(self) -> tuple[int, float | None]:
        count, oldest = self._q("SELECT COUNT(*), MIN(created_at) FROM callbacks")[0]
        return count, oldest

    # -- kv --------------------------------------------------------------

    def kv_get(self, key: str) -> str | None:
        rows = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return rows[0][0] if rows else None

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO kv(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    def last_activity(self) -> float | None:
        rows = self._q(
            "SELECT MAX(t) FROM (SELECT MAX(updated_at) t FROM outbox WHERE state='sent'"
            " UNION ALL SELECT MAX(seen_at) FROM seen_incoming)"
        )
        return rows[0][0]


class _Tx:
    def __init__(self, store: Store) -> None:
        self._store = store

    def __enter__(self) -> sqlite3.Connection:
        self._store._lock.acquire()
        self._store._db.execute("BEGIN IMMEDIATE")
        return self._store._db

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        try:
            self._store._db.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self._store._lock.release()


def _outbox(r: sqlite3.Row) -> OutboxRow:
    return OutboxRow(
        message_id=r["message_id"], phone=r["phone"], text=r["text"], state=r["state"],
        attempts=r["attempts"], is_new_contact=bool(r["is_new_contact"]),
        error=r["error"], external_id=r["external_id"],
    )


def _counter(db: sqlite3.Connection, day: str, kind: str) -> int:
    row = db.execute("SELECT count FROM daily_counters WHERE day=? AND kind=?", (day, kind)).fetchone()
    return row[0] if row else 0


def _bump(db: sqlite3.Connection, day: str, kind: str) -> None:
    db.execute(
        "INSERT INTO daily_counters(day, kind, count) VALUES (?,?,1)"
        " ON CONFLICT(day, kind) DO UPDATE SET count=count+1",
        (day, kind),
    )


def _enqueue_status(
    db: sqlite3.Connection, message_id: int, state: str, error: str | None,
    external_id: str | None, now: float,
) -> None:
    payload = {"message_id": message_id, "state": state, "error": error, "external_id": external_id}
    db.execute(
        "INSERT OR IGNORE INTO callbacks(kind, dedup_key, payload, next_attempt_at, created_at)"
        " VALUES ('status', ?, ?, ?, ?)",
        (f"st:{message_id}:{state}", json.dumps(payload), now, now),
    )

