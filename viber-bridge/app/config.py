"""Viber bridge settings (core settings + Viber Desktop specifics)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bridge_core.settings import CoreSettings, core_env, env_float, env_int, env_str


@dataclass(frozen=True)
class Settings(CoreSettings):
    debug_dir: Path
    debug_retention_days: int
    send_confirm_timeout: float
    display: str
    viber_home: Path
    viber_account: str | None
    viber_open_cmd: str
    viber_window_class: str
    ui_step_delay: float
    watch_interval: float

    @classmethod
    def from_env(cls) -> "Settings":
        account = os.environ.get("VIBER_ACCOUNT") or None
        return cls(
            # Sending stays off until target-chat verification exists (see README).
            **core_env("viber", send_enabled_default=False),
            debug_dir=Path(env_str("DEBUG_DIR", "/debug")),
            debug_retention_days=env_int("DEBUG_RETENTION_DAYS", 7),
            send_confirm_timeout=env_float("SEND_CONFIRM_TIMEOUT", 30),
            display=env_str("DISPLAY", ":0"),
            viber_home=Path(env_str("VIBER_HOME", "/home/viber/.ViberPC")),
            viber_account=account.lstrip("+") if account else None,
            viber_open_cmd=env_str("VIBER_OPEN_CMD", "xdg-open"),
            viber_window_class=env_str("VIBER_WINDOW_CLASS", "ViberPC"),
            ui_step_delay=env_float("UI_STEP_DELAY", 2),
            watch_interval=env_float("WATCH_INTERVAL", 3),
        )
