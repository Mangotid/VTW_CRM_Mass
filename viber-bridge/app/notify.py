"""Desktop notification server (org.freedesktop.Notifications) on the container's session bus.

Incoming Viber messages are read from the notifications Viber Desktop sends: viber.db is
encrypted and the AT-SPI tree has no messages (docs/viber-ui-research.md). Viber only sends
them if this server already owns the name when Viber starts (supervisord orders that).

Runs a GLib main loop in its own thread; notifications are handed to asyncio thread-safely.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("viber.notify")

BUS_NAME = "org.freedesktop.Notifications"
OBJECT_PATH = "/org/freedesktop/Notifications"

INTROSPECTION = """
<node><interface name="org.freedesktop.Notifications">
  <method name="Notify">
    <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
  </method>
  <method name="GetCapabilities"><arg type="as" direction="out"/></method>
  <method name="GetServerInformation">
    <arg type="s" direction="out"/><arg type="s" direction="out"/>
    <arg type="s" direction="out"/><arg type="s" direction="out"/>
  </method>
  <method name="CloseNotification"><arg type="u" direction="in"/></method>
  <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
  <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
</interface></node>
"""


@dataclass(frozen=True)
class Notification:
    id: int
    app_name: str
    summary: str  # contact name as Viber shows it (profile name for unsaved contacts), not the number
    body: str
    actions: tuple[str, ...]
    received_at: float


def _gi() -> tuple[Any, Any]:
    from gi.repository import Gio, GLib  # imported lazily: tests run without PyGObject

    return Gio, GLib


class NotificationServer:
    def __init__(self, on_notify: Callable[[Notification], None]) -> None:
        self._on_notify = on_notify
        self._loop: Any = None      # GLib.MainLoop
        self._context: Any = None   # GLib.MainContext
        self._conn: Any = None      # Gio.DBusConnection
        self._next_id = 0
        self._ready = threading.Event()
        self.owned = False

    def start(self, timeout: float = 5) -> bool:
        threading.Thread(target=self._run, name="notify-server", daemon=True).start()
        self._ready.wait(timeout)
        return self.owned

    def stop(self) -> None:
        if self._loop is not None and self._context is not None:
            self._context.invoke_full(_gi()[1].PRIORITY_DEFAULT, self._quit)

    def _quit(self) -> bool:
        if self._loop is not None:
            self._loop.quit()
        return False

    def click(self, notification_id: int, action: str = "default") -> None:
        """Emulate a click: Viber opens the sender's chat."""
        if self._context is None:
            return
        self._context.invoke_full(_gi()[1].PRIORITY_DEFAULT, self._emit_action, notification_id, action)

    # -- GLib thread -----------------------------------------------------------

    def _run(self) -> None:
        Gio, GLib = _gi()
        self._context = GLib.MainContext.new()
        self._context.push_thread_default()
        self._loop = GLib.MainLoop.new(self._context, False)
        info = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION).interfaces[0]

        def on_bus(conn: Any, name: str) -> None:
            self._conn = conn
            conn.register_object(OBJECT_PATH, info, self._on_call, None, None)

        def on_acquired(conn: Any, name: str) -> None:
            self.owned = True
            log.info("notify_server_ready")
            self._ready.set()

        def on_lost(conn: Any, name: str) -> None:
            self.owned = False
            log.error("notify_server_name_lost")
            self._ready.set()

        # REPLACE without ALLOW_REPLACEMENT: research tools cannot take incoming messages away from us.
        Gio.bus_own_name(Gio.BusType.SESSION, BUS_NAME, Gio.BusNameOwnerFlags.REPLACE, on_bus, on_acquired, on_lost)
        self._loop.run()

    def _on_call(self, conn, sender, path, iface, method, params, invocation) -> None:  # noqa: ANN001
        GLib = _gi()[1]
        if method == "Notify":
            app_name, _replaces, _icon, summary, body, actions, _hints, _timeout = params.unpack()
            self._next_id += 1
            invocation.return_value(GLib.Variant("(u)", (self._next_id,)))
            self._on_notify(Notification(self._next_id, app_name, summary, body, tuple(actions), time.time()))
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "actions"],)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant("(ssss)", ("pdm-viber-bridge", "pdm", "1", "1.2")))
        else:  # CloseNotification
            invocation.return_value(None)

    def _emit_action(self, notification_id: int, action: str) -> bool:
        if self._conn is not None:
            GLib = _gi()[1]
            self._conn.emit_signal(None, OBJECT_PATH, BUS_NAME, "ActionInvoked",
                                   GLib.Variant("(us)", (notification_id, action)))
        return False
