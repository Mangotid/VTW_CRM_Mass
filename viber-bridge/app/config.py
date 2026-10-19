"""Settings loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


class ConfigError(RuntimeError):
    pass


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise ConfigError(f"environment variable {name} is required")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name) or default)


@dataclass(frozen=True)
class Settings:
    channel: str
    bridge_secret: str
    odoo_url: str
    odoo_timeout: float
    data_dir: Path
    debug_dir: Path
    debug_retention_days: int
    timezone: ZoneInfo
    # Sending
    sending_enabled: bool
    send_delay_min: float
    send_delay_max: float
    daily_limit_total: int
    daily_limit_new_contacts: int
    send_confirm_timeout: float
    # Viber Desktop
    display: str
    viber_home: Path
    viber_account: str | None
    viber_open_cmd: str
    viber_window_class: str
    ui_step_delay: float
    watch_interval: float
    callback_stale_after: float

    @property
    def state_db_path(self) -> Path:
        return self.data_dir / "bridge.sqlite3"

    @classmethod
    def from_env(cls) -> "Settings":
        secret = _env("BRIDGE_SECRET")
        if len(secret) < 32:
            raise ConfigError("BRIDGE_SECRET must be at least 32 characters")
        delay_min = _env_float("SEND_DELAY_MIN", 4)
        delay_max = _env_float("SEND_DELAY_MAX", 10)
        if not 0 <= delay_min <= delay_max:
            raise ConfigError("SEND_DELAY_MIN must be <= SEND_DELAY_MAX and >= 0")
        account = os.environ.get("VIBER_ACCOUNT") or None
        return cls(
            channel="viber",
            bridge_secret=secret,
            odoo_url=_env("ODOO_URL").rstrip("/"),
            odoo_timeout=_env_float("ODOO_TIMEOUT", 15),
            data_dir=Path(_env("DATA_DIR", "/data")),
            debug_dir=Path(_env("DEBUG_DIR", "/debug")),
            debug_retention_days=_env_int("DEBUG_RETENTION_DAYS", 7),
            timezone=ZoneInfo(_env("TZ", "Europe/Kyiv")),
            sending_enabled=_env_bool("VIBER_SEND_ENABLED", False),
            send_delay_min=delay_min,
            send_delay_max=delay_max,
            daily_limit_total=_env_int("DAILY_LIMIT_TOTAL", 150),
            daily_limit_new_contacts=_env_int("DAILY_LIMIT_NEW_CONTACTS", 20),
            send_confirm_timeout=_env_float("SEND_CONFIRM_TIMEOUT", 30),
            display=_env("DISPLAY", ":0"),
            viber_home=Path(_env("VIBER_HOME", "/home/viber/.ViberPC")),
            viber_account=account.lstrip("+") if account else None,
            viber_open_cmd=_env("VIBER_OPEN_CMD", "xdg-open"),
            viber_window_class=_env("VIBER_WINDOW_CLASS", "ViberPC"),
            ui_step_delay=_env_float("UI_STEP_DELAY", 2),
            watch_interval=_env_float("WATCH_INTERVAL", 3),
            callback_stale_after=_env_float("CALLBACK_STALE_AFTER", 600),
        )
