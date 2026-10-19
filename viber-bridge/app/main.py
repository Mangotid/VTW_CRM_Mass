"""Viber bridge entrypoint (uvicorn app.main:app)."""

from __future__ import annotations

from datetime import date
from typing import Any, Coroutine

from bridge_core.api import BridgeBase, ChannelHealth, create_app
from bridge_core.store import OutboxRow, Store

from .config import Settings
from .viber_db import SchemaPendingViberDb, ViberDb
from .viber_ui import ViberUiDriver, X11ViberUi
from .workers import ViberSender, Watcher, debug_cleanup_loop


class ViberBridge(BridgeBase):
    settings: Settings

    def __init__(self, settings: Settings, store: Store, ui: ViberUiDriver, db: ViberDb) -> None:
        super().__init__(settings, store)
        self.ui = ui
        self.db = db
        self.viber_sender = ViberSender(settings, store, ui, db, self.sent, self.failed)
        self.watcher = Watcher(settings, store, db, self.delivery_wake)

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        await self.viber_sender.process(row, today)

    def send_unavailable(self) -> str | None:
        return "viber_logged_out" if self.db.locate() is None else None

    def extra_tasks(self) -> list[Coroutine[Any, Any, None]]:
        return [self.watcher.run(), debug_cleanup_loop(self.settings)]

    def channel_health(self) -> ChannelHealth:
        db_path = self.db.locate()
        running = self.ui.viber_running()
        reasons = []
        if not running:
            reasons.append("viber_not_running")
        if db_path is None:
            reasons.append("viber_db_not_found")
        if self.watcher.state != "ok":
            reasons.append(f"watcher_{self.watcher.state}")
        last = None
        if db_path is not None:
            try:
                last = db_path.stat().st_mtime
            except OSError:
                pass
        return ChannelHealth(
            logged_in=db_path is not None,
            reasons=reasons,
            details={"viber_running": running, "viber_db_found": db_path is not None, "watcher": self.watcher.state},
            last_activity=last,
        )


def build() -> ViberBridge:
    s = Settings.from_env()
    return ViberBridge(
        s,
        Store(s.state_db_path),
        X11ViberUi(s.display, s.viber_open_cmd, s.viber_window_class, s.ui_step_delay, s.debug_dir),
        SchemaPendingViberDb(s.viber_home, s.viber_account),
    )


app = create_app(factory=build, title="PDM Viber bridge")
