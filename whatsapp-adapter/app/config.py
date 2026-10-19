"""WhatsApp adapter settings (core settings + Evolution API specifics)."""

from __future__ import annotations

from dataclasses import dataclass

from bridge_core.settings import ConfigError, CoreSettings, core_env, env_float, env_str


@dataclass(frozen=True)
class EvolutionApi:
    """What both the service and login.py need."""

    url: str
    api_key: str
    instance: str
    webhook_url: str
    webhook_secret: str

    @classmethod
    def from_env(cls) -> "EvolutionApi":
        secret = env_str("WEBHOOK_SECRET")
        if len(secret) < 32:
            raise ConfigError("WEBHOOK_SECRET must be at least 32 characters")
        return cls(
            url=env_str("EVOLUTION_URL", "http://evolution-api:8080").rstrip("/"),
            api_key=env_str("EVOLUTION_API_KEY"),
            instance=env_str("EVOLUTION_INSTANCE", "pdm"),
            webhook_url=env_str("ADAPTER_WEBHOOK_URL", "http://whatsapp-adapter:8103/evolution/webhook"),
            webhook_secret=secret,
        )


@dataclass(frozen=True)
class Settings(CoreSettings):
    evo: EvolutionApi
    state_interval: float
    # Incoming older than this is dropped (offline catch-up after linking must not flood Odoo).
    incoming_max_age: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            **core_env("whatsapp", send_enabled_default=True),
            evo=EvolutionApi.from_env(),
            state_interval=env_float("WA_STATE_INTERVAL", 30),
            incoming_max_age=env_float("WA_INCOMING_MAX_AGE_HOURS", 24) * 3600,
        )
