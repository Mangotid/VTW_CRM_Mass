"""Dump Viber Desktop's accessibility (AT-SPI) tree — research tool for phase 2.

Privacy: by default no text is printed, only roles, structure and text *shape*
(length, looks like a phone, has Cyrillic). Customer chats may be synced to this
account, so raw text is shown only for nodes whose text contains --reveal (e.g. the
owner's own test number), never wholesale.

    docker compose exec viber-bridge python3 /opt/bridge/tools/atspi_dump.py
    docker compose exec viber-bridge python3 /opt/bridge/tools/atspi_dump.py --depth 40 --reveal 4567
"""

from __future__ import annotations

import argparse
import os
import re
import sys

os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/dbus-session")
os.environ.setdefault("DISPLAY", ":0")

import gi  # noqa: E402

gi.require_version("Atspi", "2.0")
from gi.repository import Atspi  # noqa: E402

PHONE_RE = re.compile(r"\+?\d[\d\s()-]{8,}\d")
CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


def shape(value: str, reveal: str | None) -> str:
    if not value:
        return ""
    if reveal and reveal in value:
        return repr(value)
    tags = [f"len={len(value)}"]
    if PHONE_RE.search(value):
        tags.append("phone")
    if CYRILLIC_RE.search(value):
        tags.append("cyr")
    return "<" + ",".join(tags) + ">"


def node_text(node: Atspi.Accessible) -> str:
    try:
        iface = node.get_text_iface()
        if iface is None:
            return ""
        return Atspi.Text.get_text(iface, 0, Atspi.Text.get_character_count(iface)) or ""
    except Exception:  # noqa: BLE001 - research tool, keep walking
        return ""


def states(node: Atspi.Accessible) -> str:
    try:
        wanted = {"focused", "selected", "active", "showing", "editable", "checked"}
        names = [Atspi.StateType(s).value_nick for s in node.get_state_set().get_states()]
        return ",".join(n for n in names if n in wanted)
    except Exception:  # noqa: BLE001
        return ""


def walk(node: Atspi.Accessible, depth: int, max_depth: int, budget: list[int], reveal: str | None) -> None:
    if budget[0] <= 0:
        return
    budget[0] -= 1
    try:
        role = node.get_role_name()
        name = node.get_name() or ""
        desc = node.get_description() or ""
        count = node.get_child_count()
    except Exception as exc:  # noqa: BLE001
        print("  " * depth + f"<error {type(exc).__name__}>")
        return
    parts = [role]
    if name:
        parts.append(f"name={shape(name, reveal)}")
    if desc:
        parts.append(f"desc={shape(desc, reveal)}")
    if text := node_text(node):
        if text != name:
            parts.append(f"text={shape(text, reveal)}")
    if st := states(node):
        parts.append(f"[{st}]")
    if count:
        parts.append(f"children={count}")
    print("  " * depth + " ".join(parts))
    if depth >= max_depth:
        return
    for i in range(count):
        child = node.get_child_at_index(i)
        if child is not None:
            walk(child, depth + 1, max_depth, budget, reveal)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="viber", help="substring of the application name (case-insensitive)")
    ap.add_argument("--depth", type=int, default=25)
    ap.add_argument("--max-nodes", type=int, default=3000)
    ap.add_argument("--reveal", help="print raw text only for nodes containing this substring")
    args = ap.parse_args()

    desktop = Atspi.get_desktop(0)
    apps = [desktop.get_child_at_index(i) for i in range(desktop.get_child_count())]
    print(f"apps on a11y bus: {[a.get_name() for a in apps if a is not None]}")
    targets = [a for a in apps if a is not None and args.app.lower() in (a.get_name() or "").lower()]
    if not targets:
        print("target app not found: is Viber started with QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1?", file=sys.stderr)
        return 1
    budget = [args.max_nodes]
    for app in targets:
        walk(app, 0, args.depth, budget, args.reveal)
    if budget[0] <= 0:
        print(f"... stopped after {args.max_nodes} nodes (--max-nodes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
