"""Settings shared by all bridges, loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


def env_str(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"environment variable {name} is required")
    return value


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclass(frozen=True)
class CoreSettings:
    channel: str
    bridge_secret: str
    odoo_url: str
    odoo_timeout: float
    data_dir: Path
    timezone: ZoneInfo
    sending_enabled: bool
    send_delay_min: float
    send_delay_max: float
    daily_limit_total: int
    daily_limit_new_contacts: int
    callback_stale_after: float

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "bridge.sqlite3"


def core_env(channel: str, send_enabled_default: bool) -> dict[str, Any]:
    """Keyword arguments for CoreSettings fields; service settings add their own."""
    secret = env_str("BRIDGE_SECRET")
    if len(secret) < 32:
        raise ConfigError("BRIDGE_SECRET must be at least 32 characters")
    delay_min = env_float("SEND_DELAY_MIN", 4)
    delay_max = env_float("SEND_DELAY_MAX", 10)
    if not 0 <= delay_min <= delay_max:
        raise ConfigError("SEND_DELAY_MIN must be <= SEND_DELAY_MAX and >= 0")
    return dict(
        channel=channel,
        bridge_secret=secret,
        odoo_url=env_str("ODOO_URL").rstrip("/"),
        odoo_timeout=env_float("ODOO_TIMEOUT", 15),
        data_dir=Path(env_str("DATA_DIR", "/data")),
        timezone=ZoneInfo(env_str("TZ", "Europe/Kyiv")),
        sending_enabled=env_bool("SEND_ENABLED", send_enabled_default),
        send_delay_min=delay_min,
        send_delay_max=delay_max,
        daily_limit_total=env_int("DAILY_LIMIT_TOTAL", 150),
        daily_limit_new_contacts=env_int("DAILY_LIMIT_NEW_CONTACTS", 20),
        callback_stale_after=env_float("CALLBACK_STALE_AFTER", 600),
    )
