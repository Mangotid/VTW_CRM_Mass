"""Viber Desktop profile on disk.

viber.db is encrypted in Viber Desktop 27.x (docs/viber-db-schema.md), so its content is not
read. Its presence still tells whether the account is logged in.
"""

from __future__ import annotations

from pathlib import Path


def find_viber_db(viber_home: Path, account: str | None) -> Path | None:
    """~/.ViberPC/<account>/viber.db. Without VIBER_ACCOUNT, require exactly one profile."""
    if account:
        candidate = viber_home / account / "viber.db"
        return candidate if candidate.is_file() else None
    found = sorted(viber_home.glob("*/viber.db"))
    return found[0] if len(found) == 1 else None
