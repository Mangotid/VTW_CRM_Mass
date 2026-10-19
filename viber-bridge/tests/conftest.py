from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

from bridge_core.store import Store  # noqa: E402

from app.config import Settings  # noqa: E402
from app.viber_db import IncomingEvent, OutgoingEvent  # noqa: E402
from app.viber_ui import TargetNotVerified  # noqa: E402

SECRET = "s" * 40


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        channel="viber", bridge_secret=SECRET, odoo_url="http://odoo.test", odoo_timeout=5,
        data_dir=tmp_path / "data", debug_dir=tmp_path / "debug", debug_retention_days=7,
        timezone=ZoneInfo("Europe/Kyiv"), sending_enabled=True,
        send_delay_min=0, send_delay_max=0, daily_limit_total=3, daily_limit_new_contacts=2,
        send_confirm_timeout=0.2, display=":0", viber_home=tmp_path / "viber",
        viber_account=None, viber_open_cmd="true", viber_window_class="ViberPC",
        ui_step_delay=0, watch_interval=0.01, callback_stale_after=600,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.state_db_path)
    yield s
    s.close()


class FakeDb:
    def __init__(self) -> None:
        self.found = True
        self.latest = 100
        self.incoming: list[IncomingEvent] = []
        self.outgoing: list[OutgoingEvent] = []

    def locate(self) -> Path | None:
        return Path("/fake/viber.db") if self.found else None

    def latest_event_id(self) -> int:
        return self.latest

    def incoming_since(self, after_event_id: int, limit: int = 100) -> list[IncomingEvent]:
        return [e for e in self.incoming if e.event_id > after_event_id][:limit]

    def outgoing_since(self, after_event_id: int, phone: str) -> list[OutgoingEvent]:
        return [e for e in self.outgoing if e.event_id > after_event_id and e.phone == phone]


class FakeUi:
    def __init__(self, db: FakeDb) -> None:
        self.db = db
        self.sent: list[tuple[str, str]] = []
        self.opened: str | None = None
        self.verify_ok = True
        self.deliver = True  # whether a paste results in an outgoing event
        self.screenshots: list[str] = []

    async def open_chat(self, phone: str) -> None:
        self.opened = phone

    async def verify_target(self, phone: str) -> None:
        if not self.verify_ok:
            raise TargetNotVerified("nope")

    async def paste_and_send(self, text: str) -> None:
        assert self.opened is not None
        self.sent.append((self.opened, text))
        if self.deliver:
            self.db.latest += 1
            self.db.outgoing.append(OutgoingEvent(self.db.latest, f"ext{self.db.latest}", self.opened))

    async def screenshot(self, name: str) -> Path | None:
        self.screenshots.append(name)
        return None

    def viber_running(self) -> bool:
        return True


@pytest.fixture
def fake_db() -> FakeDb:
    return FakeDb()


@pytest.fixture
def fake_ui(fake_db: FakeDb) -> FakeUi:
    return FakeUi(fake_db)


def incoming(event_id: int, phone: str = "+380671234567") -> IncomingEvent:
    return IncomingEvent(
        event_id=event_id, external_id=f"in{event_id}", phone=phone, name="Client",
        text="Привіт", timestamp=datetime(2026, 10, 19, tzinfo=timezone.utc),
    )

