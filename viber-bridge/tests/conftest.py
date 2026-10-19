from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "common"))

from bridge_core.store import Store  # noqa: E402

from app.config import Settings  # noqa: E402
from app.viber_ui import ChatOpen, UiError  # noqa: E402

SECRET = "s" * 40
NEUTRAL = "+380000000000"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        channel="viber", bridge_secret=SECRET, odoo_url="http://odoo.test", odoo_timeout=5,
        data_dir=tmp_path / "data", debug_dir=tmp_path / "debug", debug_retention_days=7,
        timezone=ZoneInfo("Europe/Kyiv"), sending_enabled=True,
        send_delay_min=0, send_delay_max=0, daily_limit_total=3, daily_limit_new_contacts=2,
        display=":0", viber_home=tmp_path / "viber", viber_account=None, viber_open_cmd="true",
        viber_window_class="ViberPC", neutral_phone=NEUTRAL, ui_timeout=0.2, hover_delay=0,
        paste_check=True, callback_stale_after=600,
    )


@pytest.fixture
def store(settings: Settings) -> Store:
    s = Store(settings.state_db_path)
    yield s
    s.close()


class FakeUi:
    """Scriptable UI. `calls` records the order of actions; `on_viber` lists numbers that exist."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.on_viber = {"+380671234567"}
        self.open_chat_phone: str | None = None
        self.header_override: str | None = None  # what peer_phone() reads instead of the real chat
        self.neutral_error: Exception | None = None
        self.paste_ok = True
        self.enter_ok = True
        self.chat_opens = True
        self.screenshots: list[str] = []
        self.pasted: list[tuple[str | None, str]] = []

    async def prepare(self) -> None:
        self.calls.append("prepare")

    async def neutral(self) -> None:
        self.calls.append("neutral")
        if self.neutral_error:
            raise self.neutral_error
        self.open_chat_phone = NEUTRAL

    async def open_chat(self, phone: str) -> ChatOpen:
        self.calls.append(f"open:{phone}")
        if not self.chat_opens:
            return ChatOpen.TIMEOUT
        self.open_chat_phone = phone
        return ChatOpen.OPEN if phone in self.on_viber else ChatOpen.NOT_ON_VIBER

    async def wait_chat_open(self) -> bool:
        self.calls.append("wait_chat_open")
        return self.chat_opens

    async def peer_phone(self) -> str | None:
        self.calls.append("peer_phone")
        if self.header_override is not None:
            return self.header_override or None
        return self.open_chat_phone

    async def paste(self, text: str) -> bool:
        self.calls.append("paste")
        self.pasted.append((self.open_chat_phone, text))
        return self.paste_ok

    async def clear_composer(self) -> None:
        self.calls.append("clear")

    async def press_enter(self) -> bool:
        self.calls.append("enter")
        return self.enter_ok

    async def screenshot(self, name: str) -> Path | None:
        self.screenshots.append(name)
        return None

    def viber_running(self) -> bool:
        return True


@pytest.fixture
def fake_ui() -> FakeUi:
    return FakeUi()


__all__ = ["FakeUi", "NEUTRAL", "SECRET", "UiError"]
