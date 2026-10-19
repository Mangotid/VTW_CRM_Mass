"""Viber bridge entrypoint (uvicorn app.main:app)."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Awaitable, Callable

from bridge_core.api import BridgeBase, ChannelHealth, create_app
from bridge_core.store import OutboxRow, Store

from .config import Settings
from .viber_db import find_viber_db
from .viber_ui import ViberUiDriver, X11ViberUi
from .workers import IncomingResolver, ViberSender, debug_cleanup_loop


class ViberBridge(BridgeBase):
    settings: Settings

    def __init__(self, settings: Settings, store: Store, ui: ViberUiDriver, notify_factory: Any = None) -> None:
        super().__init__(settings, store)
        self.ui = ui
        self.ui_lock = asyncio.Lock()
        self.notify_factory = notify_factory  # NotificationServer in production, None in tests
        self.notify: Any = None
        self.viber_sender = ViberSender(settings, store, ui, self.ui_lock, self.sent, self.failed)
        self.resolver = IncomingResolver(settings, store, ui, self.ui_lock, self._click, self.delivery_wake)

    def _click(self, notification_id: int) -> None:
        if self.notify is not None:
            self.notify.click(notification_id)

    async def startup(self) -> None:
        if self.notify_factory is None:
            return
        loop = asyncio.get_running_loop()
        self.notify = self.notify_factory(lambda n: loop.call_soon_threadsafe(self.resolver.submit, n))
        await asyncio.to_thread(self.notify.start)

    async def shutdown(self) -> None:
        if self.notify is not None:
            self.notify.stop()

    async def handle_outgoing(self, row: OutboxRow, today: date) -> None:
        await self.viber_sender.process(row, today)

    def logged_in(self) -> bool:
        return find_viber_db(self.settings.viber_home, self.settings.viber_account) is not None

    def send_unavailable(self) -> str | None:
        return None if self.logged_in() else "viber_logged_out"

    def extra_tasks(self) -> dict[str, Callable[[], Awaitable[None]]]:
        return {"incoming": self.resolver.run, "debug_cleanup": lambda: debug_cleanup_loop(self.settings)}

    def channel_health(self) -> ChannelHealth:
        running = self.ui.viber_running()
        notify_ok = self.notify is not None and self.notify.owned
        reasons = []
        if not running:
            reasons.append("viber_not_running")
        if self.notify_factory is not None and not notify_ok:
            reasons.append("notify_server_down")
        return ChannelHealth(
            logged_in=self.logged_in(),
            reasons=reasons,
            details={
                "viber_running": running,
                "notify_server": notify_ok,
                "incoming_pending": self.resolver.queue.qsize(),
                "incoming_unresolved": self.resolver.unresolved,
            },
            last_activity=self.resolver.last_event,
        )


def build() -> ViberBridge:
    from .notify import NotificationServer  # needs gi; not imported by tests

    s = Settings.from_env()
    ui = X11ViberUi(
        s.display, s.viber_open_cmd, s.viber_window_class, s.ui_timeout, s.hover_delay,
        s.paste_check, s.neutral_phone, s.debug_dir,
    )
    return ViberBridge(s, Store(s.state_db_path), ui, notify_factory=NotificationServer)


app = create_app(factory=build, title="PDM Viber bridge")
