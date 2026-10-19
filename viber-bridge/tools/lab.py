"""Phase 2 research tool for Viber Desktop: AT-SPI state, screenshots + OCR, notifications.

Run only via the host wrapper `sudo viber-lab <command> ...` (ops/viber-lab/), which copies
this directory into the container and runs it as user "viber". Results that are files
(screenshots) go to /tmp/lab-out and are copied by the wrapper to /var/lib/viber-lab/out.

Safety by construction:
- There is NO command that types, pastes or presses Enter: nothing can be sent from here.
- Text is printed only as its shape (length, phone, Cyrillic) unless it contains $LAB_REVEAL
  (the owner's own test number tail). Customer chats are synced to this account.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/dbus-session")
os.environ.setdefault("DISPLAY", ":0")

import gi  # noqa: E402

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi, Gio, GLib  # noqa: E402

from atspi_dump import node_text, shape, states  # noqa: E402

REVEAL = os.environ.get("LAB_REVEAL") or None
OUT = Path("/tmp/lab-out")
SAFE_KEYS = {"Escape", "Tab", "Up", "Down", "Home", "End"}


def viber_app() -> Atspi.Accessible:
    desktop = Atspi.get_desktop(0)
    for i in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(i)
        if app is not None and "viber" in (app.get_name() or "").lower():
            return app
    sys.exit("Viber is not on the a11y bus")


def extents(node: Atspi.Accessible) -> tuple[int, int, int, int] | None:
    try:
        r = node.get_extents(Atspi.CoordType.SCREEN)
        return (r.x, r.y, r.width, r.height) if r.width > 0 and r.height > 0 else None
    except Exception:  # noqa: BLE001
        return None


def iter_nodes(node: Atspi.Accessible, depth: int = 0, path: str = "0"):
    yield node, depth, path
    try:
        count = node.get_child_count()
    except Exception:  # noqa: BLE001
        return
    for i in range(count):
        child = node.get_child_at_index(i)
        if child is not None:
            yield from iter_nodes(child, depth + 1, f"{path}.{i}")


def describe(node: Atspi.Accessible) -> str:
    parts = [node.get_role_name()]
    if name := node.get_name():
        parts.append(f"name={shape(name, REVEAL)}")
    if desc := node.get_description():
        parts.append(f"desc={shape(desc, REVEAL)}")
    if (text := node_text(node)) and text != node.get_name():
        parts.append(f"text={shape(text, REVEAL)}")
    if st := states(node):
        parts.append(f"[{st}]")
    if ext := extents(node):
        parts.append("@{},{} {}x{}".format(*ext))
    return " ".join(parts)


# -- commands -----------------------------------------------------------------

def cmd_tree(args: argparse.Namespace) -> None:
    for node, depth, path in iter_nodes(viber_app()):
        if depth > args.depth:
            continue
        if args.showing and "showing" not in states(node) and depth > 1:
            continue
        print("  " * depth + f"{path} {describe(node)}")


def composer_state() -> dict[str, object]:
    """Editable text fields that are showing; the message composer is one of them."""
    fields = []
    for node, _, path in iter_nodes(viber_app()):
        if node.get_role_name() == "text" and "editable" in states(node) and "showing" in states(node):
            fields.append({"path": path, "states": states(node), "extents": extents(node),
                           "desc": shape(node.get_description() or "", REVEAL)})
    return {"editable_showing": fields}


def cmd_state(args: argparse.Namespace) -> None:
    for f in composer_state()["editable_showing"]:  # type: ignore[union-attr]
        print(f)


def cmd_key(args: argparse.Namespace) -> None:
    if args.key not in SAFE_KEYS:
        sys.exit(f"key not allowed: {args.key} (allowed: {sorted(SAFE_KEYS)})")
    subprocess.run(["xdotool", "key", "--clearmodifiers", args.key], check=True)
    time.sleep(args.wait)
    cmd_state(args)


def cmd_window(args: argparse.Namespace) -> None:
    """Minimize / activate the main Viber window (messengers do not notify while focused)."""
    ids = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", args.wm_class],
                         capture_output=True, text=True).stdout.split()
    if args.action == "list" or not ids:
        print(f"visible windows of class {args.wm_class}: {ids}")
        if not ids:
            return
    for wid in ids:
        if args.action == "minimize":
            subprocess.run(["xdotool", "windowminimize", wid], check=True)
        elif args.action == "activate":
            subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=False)
    time.sleep(1)
    active = subprocess.run(["xdotool", "getactivewindow"], capture_output=True, text=True).stdout.strip()
    print(f"{args.action}: windows={ids} active_now={active or None}")


def cmd_viber_restart(args: argparse.Namespace) -> None:
    """Restart only the Viber process (e.g. so it sees a notification server at startup)."""
    subprocess.run(["supervisorctl", "-c", "/etc/supervisor/viber-bridge.conf", "restart", "viber"], check=True)


def cmd_open(args: argparse.Namespace) -> None:
    phone = args.phone if args.phone.startswith("+") else "+" + args.phone
    if not phone[1:].isdigit():
        sys.exit("phone must be digits in E.164")
    url = f"viber://chat?number={quote(phone, safe='')}"
    subprocess.run(["xdg-open", url], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(args.wait)
    print(f"opened deep link for ...{phone[-4:]}")
    cmd_state(args)


def screenshot(name: str) -> Path:
    OUT.mkdir(mode=0o700, exist_ok=True)
    path = OUT / f"{name}.png"
    subprocess.run(["import", "-window", "root", str(path)], check=True)
    return path


def ocr(path: Path, psm: int) -> str:
    res = subprocess.run(["tesseract", str(path), "stdout", "-l", "ukr+eng", "--psm", str(psm)],
                         check=True, capture_output=True, text=True)
    return res.stdout


def cmd_shot(args: argparse.Namespace) -> None:
    full = screenshot("full")
    target = full
    if args.crop:
        x, y, w, h = (int(v) for v in args.crop.split(","))
        target = OUT / f"{args.name}.png"
        subprocess.run(["convert", str(full), "-crop", f"{w}x{h}+{x}+{y}", "+repage",
                        "-resize", f"{args.scale}%", str(target)], check=True)
    if target != full and not args.keep_full:
        full.unlink(missing_ok=True)
    print(f"saved {target.name}")
    if args.ocr:
        for line in ocr(target, args.psm).splitlines():
            if line.strip():
                print("ocr:", shape(line.strip(), REVEAL))


NOTIFY_XML = """
<node><interface name="org.freedesktop.Notifications">
  <method name="Notify">
    <arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/>
    <arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/>
    <arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/>
  </method>
  <method name="GetCapabilities"><arg type="as" direction="out"/></method>
  <method name="GetServerInformation">
    <arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/>
  </method>
  <method name="CloseNotification"><arg type="u" direction="in"/></method>
  <signal name="NotificationClosed"><arg type="u"/><arg type="u"/></signal>
  <signal name="ActionInvoked"><arg type="u"/><arg type="s"/></signal>
</interface></node>
"""


def cmd_notify_listen(args: argparse.Namespace) -> None:
    """Act as the desktop notification server for N seconds and print what Viber sends."""
    loop = GLib.MainLoop()
    counter = [0]
    info = Gio.DBusNodeInfo.new_for_xml(NOTIFY_XML).interfaces[0]

    def on_call(conn, sender, path, iface, method, params, invocation):  # noqa: ANN001
        if method == "Notify":
            app, replaces, icon, summary, body, actions, hints, timeout = params.unpack()
            counter[0] += 1
            print(f"notify #{counter[0]} app={app!r} replaces={replaces} summary={shape(summary, REVEAL)} "
                  f"body={shape(body, REVEAL)} actions={actions[::2]} hints={sorted(hints)} timeout={timeout}",
                  flush=True)
            invocation.return_value(GLib.Variant("(u)", (counter[0],)))
        elif method == "GetCapabilities":
            invocation.return_value(GLib.Variant("(as)", (["body", "actions", "persistence"],)))
        elif method == "GetServerInformation":
            invocation.return_value(GLib.Variant("(ssss)", ("pdm-lab", "pdm", "0.1", "1.2")))
        else:
            invocation.return_value(None)

    def on_bus(conn, name):  # noqa: ANN001
        conn.register_object("/org/freedesktop/Notifications", info, on_call, None, None)

    def on_acquired(conn, name):  # noqa: ANN001
        print(f"listening as {name} for {args.seconds}s", flush=True)

    def on_lost(conn, name):  # noqa: ANN001
        print("could not own org.freedesktop.Notifications (another server running?)", flush=True)
        loop.quit()

    Gio.bus_own_name(Gio.BusType.SESSION, "org.freedesktop.Notifications",
                     Gio.BusNameOwnerFlags.REPLACE, on_bus, on_acquired, on_lost)
    GLib.timeout_add_seconds(args.seconds, loop.quit)
    loop.run()
    print(f"done, {counter[0]} notification(s)")


def main() -> int:
    ap = argparse.ArgumentParser(prog="viber-lab")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("tree", help="AT-SPI tree with screen coordinates")
    p.add_argument("--depth", type=int, default=60)
    p.add_argument("--showing", action="store_true", help="only nodes currently on screen")
    p.set_defaults(fn=cmd_tree)

    p = sub.add_parser("state", help="showing editable fields (is a chat composer open?)")
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser("key", help=f"press a navigation key: {sorted(SAFE_KEYS)}")
    p.add_argument("key")
    p.add_argument("--wait", type=float, default=1.5)
    p.set_defaults(fn=cmd_key)

    p = sub.add_parser("window", help="list / minimize / activate the Viber window")
    p.add_argument("action", choices=["list", "minimize", "activate"])
    p.add_argument("--wm-class", default="ViberPC")
    p.set_defaults(fn=cmd_window)

    p = sub.add_parser("viber-restart", help="restart the Viber process via supervisorctl")
    p.set_defaults(fn=cmd_viber_restart)

    p = sub.add_parser("open", help="open viber://chat deep link (no typing)")
    p.add_argument("phone")
    p.add_argument("--wait", type=float, default=4)
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser("shot", help="screenshot, optional crop x,y,w,h and OCR")
    p.add_argument("--crop")
    p.add_argument("--name", default="crop")
    p.add_argument("--scale", type=int, default=100)
    p.add_argument("--ocr", action="store_true")
    p.add_argument("--psm", type=int, default=6)
    p.add_argument("--keep-full", action="store_true", help="keep full-screen PNG (contains chat list!)")
    p.set_defaults(fn=cmd_shot)

    p = sub.add_parser("notify-listen", help="be the notification server for N seconds")
    p.add_argument("--seconds", type=int, default=120)
    p.set_defaults(fn=cmd_notify_listen)

    args = ap.parse_args()
    args.fn(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
