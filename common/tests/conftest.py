from __future__ import annotations

from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bridge_core.api import BridgeBase, ChannelHealth
from bridge_core.settings import CoreSettings
from bridge_core.store import OutboxRow, Store

SECRET = "s" * 40


@pytest.fixture
def settings(tmp_path: Path) -> CoreSettings:
    return CoreSettings(
        channel="test", bridge_secret=SECRET, odoo_url="http://odoo.test", odoo_timeout=5,
        data_dir=tmp_path / "data", timezone=ZoneInfo("Europe/Kyiv"), sending_enabled=True,
        send_delay_min=0, send_delay_max=0, daily_limit_total=3, daily_limit_new_contacts=2,
        callback_stale_after=600,
    )


@pytest.fixture
def store(settings: CoreSettings) -> Store:
    s = Store(settings.state_db_path)
    yield s
    s.close()


class DummyBridge(BridgeBase):
    def __init__(self, settings: CoreSettings, store: Store) -> None:
        super().__init__(settings, store)
        self.logged_in = True
        self.unavailable: str | None = None
        self.handled: list[int] = []

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        self.handled.append(row.message_id)
        self.store.mark_sending(row.message_id, today, count=True)
        self.sent(row.message_id, f"ext{row.message_id}")

    def send_unavailable(self) -> str | None:
        return self.unavailable

    def channel_health(self) -> ChannelHealth:
        return ChannelHealth(logged_in=self.logged_in, details={"dummy": True})


@pytest.fixture
def bridge(settings: CoreSettings, store: Store) -> DummyBridge:
    return DummyBridge(settings, store)
