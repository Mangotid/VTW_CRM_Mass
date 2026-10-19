from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

from bridge_core.store import Store  # noqa: E402

from app.config import EvolutionApi, Settings  # noqa: E402

SECRET = "s" * 40
WEBHOOK_SECRET = "w" * 40


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        channel="whatsapp", bridge_secret=SECRET, odoo_url="http://odoo.test", odoo_timeout=5,
        data_dir=tmp_path / "data", timezone=ZoneInfo("Europe/Kyiv"), sending_enabled=True,
        send_delay_min=0, send_delay_max=0, daily_limit_total=100, daily_limit_new_contacts=100,
        callback_stale_after=600,
        evo=EvolutionApi(url="http://evo.test", api_key="k", instance="pdm",
                         webhook_url="http://adapter.test/evolution/webhook", webhook_secret=WEBHOOK_SECRET),
        state_interval=30, incoming_max_age=24 * 3600,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.state_db_path)
    yield s
    s.close()
