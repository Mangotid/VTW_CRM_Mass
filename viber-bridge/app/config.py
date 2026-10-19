"""Viber bridge settings (core settings + Viber Desktop specifics)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from bridge_core.settings import ConfigError, CoreSettings, core_env, env_bool, env_float, env_int, env_str


@dataclass(frozen=True)
class Settings(CoreSettings):
    debug_dir: Path
    debug_retention_days: int
    display: str
    viber_home: Path
    viber_account: str | None
    viber_open_cmd: str
    viber_window_class: str
    # Deep link target that is never on Viber: opening it leaves no chat composer on screen.
    neutral_phone: str
    ui_timeout: float
    hover_delay: float
    # Require reading the pasted text back from the composer (AT-SPI) before pressing Enter.
    paste_check: bool

    @classmethod
    def from_env(cls) -> "Settings":
        account = os.environ.get("VIBER_ACCOUNT") or None
        neutral = env_str("VIBER_NEUTRAL_PHONE", "+380000000000")
        if not (neutral.startswith("+") and neutral[1:].isdigit()):
            raise ConfigError("VIBER_NEUTRAL_PHONE must be E.164 (+digits)")
        return cls(
            # Sending stays off until it has been verified on the live account (see README).
            **core_env("viber", send_enabled_default=False),
            debug_dir=Path(env_str("DEBUG_DIR", "/debug")),
            debug_retention_days=env_int("DEBUG_RETENTION_DAYS", 7),
            display=env_str("DISPLAY", ":0"),
            viber_home=Path(env_str("VIBER_HOME", "/home/viber/.ViberPC")),
            viber_account=account.lstrip("+") if account else None,
            viber_open_cmd=env_str("VIBER_OPEN_CMD", "xdg-open"),
            viber_window_class=env_str("VIBER_WINDOW_CLASS", "ViberPC"),
            neutral_phone=neutral,
            ui_timeout=env_float("UI_TIMEOUT", 10),
            hover_delay=env_float("UI_HOVER_DELAY", 1.5),
            paste_check=env_bool("PASTE_CHECK", True),
        )
