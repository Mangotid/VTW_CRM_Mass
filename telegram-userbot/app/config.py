"""Telegram userbot settings (core settings + Telethon specifics)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bridge_core.settings import CoreSettings, ConfigError, core_env, env_float, env_str


@dataclass(frozen=True)
class TelegramApi:
    """What both the service and login.py need."""

    api_id: int
    api_hash: str
    session_path: Path
    device_model: str
    system_version: str
    app_version: str

    @classmethod
    def from_env(cls) -> "TelegramApi":
        raw_id = env_str("TG_API_ID")
        if not raw_id.isdigit():
            raise ConfigError("TG_API_ID must be numeric (my.telegram.org)")
        return cls(
            api_id=int(raw_id),
            api_hash=env_str("TG_API_HASH"),
            # Telethon appends ".session" to this path.
            session_path=Path(env_str("TG_SESSION_PATH", "/session/userbot")),
            device_model=env_str("TG_DEVICE_MODEL", "PDM Messenger Hub"),
            system_version=env_str("TG_SYSTEM_VERSION", "Linux"),
            app_version=env_str("TG_APP_VERSION", "pdm-telegram-userbot 0.1"),
        )


@dataclass(frozen=True)
class Settings(CoreSettings):
    tg: TelegramApi
    contact_label: str
    health_interval: float
    flood_sleep_threshold: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            **core_env("telegram", send_enabled_default=True),
            tg=TelegramApi.from_env(),
            contact_label=env_str("TG_CONTACT_LABEL", "PDM"),
            health_interval=env_float("TG_HEALTH_INTERVAL", 60),
            # Flood waits up to this many seconds are slept through by Telethon itself.
            flood_sleep_threshold=int(env_float("TG_FLOOD_SLEEP_THRESHOLD", 30)),
        )
