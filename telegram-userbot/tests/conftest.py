from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

from bridge_core.store import Store  # noqa: E402

from app.config import Settings, TelegramApi  # noqa: E402
from app.gateway import ImportResult, Peer  # noqa: E402

SECRET = "s" * 40


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        channel="telegram", bridge_secret=SECRET, odoo_url="http://odoo.test", odoo_timeout=5,
        data_dir=tmp_path / "data", timezone=ZoneInfo("Europe/Kyiv"), sending_enabled=True,
        send_delay_min=0, send_delay_max=0, daily_limit_total=100, daily_limit_new_contacts=100,
        callback_stale_after=600,
        tg=TelegramApi(api_id=1, api_hash="x", session_path=tmp_path / "s" / "userbot",
                       device_model="t", system_version="t", app_version="t"),
        contact_label="PDM", health_interval=60, flood_sleep_threshold=0,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.state_db_path)
    yield s
    s.close()


class FakeGateway:
    def __init__(self) -> None:
        self.users: dict[str, Peer] = {}
        self.imports: list[str] = []
        self.sent: list[tuple[Peer, str]] = []
        self.retry = False
        self.send_error: Exception | None = None
        self.import_error: Exception | None = None

    async def import_contact(self, phone: str, first_name: str) -> ImportResult:
        self.imports.append(phone)
        if self.import_error:
            raise self.import_error
        if self.retry:
            return ImportResult(None, retry=True)
        return ImportResult(self.users.get(phone), retry=False)

    async def send_text(self, peer: Peer, text: str) -> int:
        if self.send_error:
            err, self.send_error = self.send_error, None
            raise err
        self.sent.append((peer, text))
        return 1000 + len(self.sent)


@pytest.fixture
def gw() -> FakeGateway:
    return FakeGateway()
