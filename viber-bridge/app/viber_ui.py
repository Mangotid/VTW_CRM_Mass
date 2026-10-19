"""Viber Desktop UI automation (X11 + AT-SPI + OCR). Findings: docs/viber-ui-research.md.

- Chat state from AT-SPI: the message composer is the only editable field at the bottom of the
  chat pane; "not on Viber" chats have no composer and a "Viber Out" button.
- Peer number: hovering the header status line replaces it with the number; OCR (digits only).
- Text goes through the clipboard (xclip + Ctrl+V); `xdotool type` breaks Cyrillic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol, TypeVar
from urllib.parse import quote

log = logging.getLogger("viber.ui")

T = TypeVar("T")

PHONE_RE = re.compile(r"\+?(\d{10,15})")


class UiError(RuntimeError):
    pass


class ChatOpen(str, Enum):
    OPEN = "open"                  # composer appeared: a chat with this number is open
    NOT_ON_VIBER = "not_on_viber"  # "isn't on Viber" screen for this number
    TIMEOUT = "timeout"            # nothing recognisable happened


class ViberUiDriver(Protocol):
    async def prepare(self) -> None: ...

    async def neutral(self) -> None: ...

    async def open_chat(self, phone: str) -> ChatOpen: ...

    async def wait_chat_open(self) -> bool: ...

    async def peer_phone(self) -> str | None: ...

    async def paste(self, text: str) -> bool: ...

    async def clear_composer(self) -> None: ...

    async def press_enter(self) -> bool: ...

    async def screenshot(self, name: str) -> Path | None: ...

    def viber_running(self) -> bool: ...


def deep_link(phone: str) -> str:
    return f"viber://chat?number={quote(phone, safe='')}"


def parse_phone(ocr_text: str) -> str | None:
    """OCR output of the hovered status line -> E.164, or None (group chat, garbage)."""
    compact = re.sub(r"[\s.,]", "", ocr_text)
    m = PHONE_RE.search(compact)
    return f"+{m.group(1)}" if m else None


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int


class X11ViberUi:
    # Offsets inside the chat pane, measured on the maximized window (1280x800).
    HEADER_H = 72
    STATUS = (70, 36, 260, 22)        # crop of the status line relative to the pane origin
    STATUS_HOVER = (97, 47)           # where to put the mouse to reveal the number
    COMPOSER_BAND = 80                # composer lies within this many px of the pane bottom

    def __init__(
        self, display: str, open_cmd: str, window_class: str, timeout: float, hover_delay: float,
        paste_check: bool, neutral_phone: str, debug_dir: Path,
    ) -> None:
        self._env = {**os.environ, "DISPLAY": display}
        self._open_cmd = open_cmd
        self._window_class = window_class
        self._timeout = timeout
        self._hover_delay = hover_delay
        self._paste_check = paste_check
        self._neutral_phone = neutral_phone
        self._debug_dir = debug_dir
        # libatspi is not thread-safe: every AT-SPI call runs on this single thread.
        self._a11y = ThreadPoolExecutor(max_workers=1, thread_name_prefix="atspi")

    # -- process helpers -------------------------------------------------------

    async def _run(self, *args: str, stdin: bytes | None = None, timeout: float = 15) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise UiError(f"{args[0]} timed out")
        if proc.returncode != 0:
            raise UiError(f"{args[0]} exited {proc.returncode}: {err.decode(errors='replace')[:200]}")
        return out

    async def _a11y_call(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(self._a11y, fn, *args)

    async def _wait(self, predicate: Callable[[], Any], timeout: float | None = None) -> Any:
        deadline = time.monotonic() + (timeout or self._timeout)
        while True:
            if result := await predicate():
                return result
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.3)

    # -- window ----------------------------------------------------------------

    async def prepare(self) -> None:
        """Activate and maximize Viber so the measured layout applies."""
        out = await self._run("xdotool", "search", "--onlyvisible", "--class", self._window_class)
        ids = out.decode().split()
        if not ids:
            raise UiError("viber window not found")
        for wid in ids:
            await self._run("xdotool", "windowactivate", "--sync", wid)
            await self._run("xdotool", "windowmove", wid, "0", "0", "windowsize", wid, "100%", "100%")
        await asyncio.sleep(0.5)

    # -- AT-SPI state (runs on the a11y thread) ----------------------------------

    @staticmethod
    def _snapshot() -> dict[str, Any]:
        from gi.repository import Atspi  # imported lazily: tests run without libatspi

        app = None
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            child = desktop.get_child_at_index(i)
            if child is not None and "viber" in (child.get_name() or "").lower():
                app = child
                break
        if app is None:
            return {"pane": None, "composer": None, "viber_out": False}

        def walk(node: Any):
            yield node
            for i in range(node.get_child_count()):
                c = node.get_child_at_index(i)
                if c is not None:
                    yield from walk(c)

        def showing(node: Any) -> bool:
            return node.get_state_set().contains(Atspi.StateType.SHOWING)

        def rect(node: Any) -> Rect | None:
            try:
                r = node.get_extents(Atspi.CoordType.SCREEN)
            except Exception:  # noqa: BLE001
                return None
            return Rect(r.x, r.y, r.width, r.height) if r.width > 0 and r.height > 0 else None

        nodes = [n for n in walk(app) if showing(n)]
        panes = [(r, n) for n in nodes if n.get_role_name() == "layered pane" and (r := rect(n))]
        if not panes:
            return {"pane": None, "composer": None, "viber_out": False}
        pane = min(panes, key=lambda p: p[0].x)[0]  # chat pane; the info panel is to its right
        composer = None
        viber_out = False
        for n in nodes:
            r = rect(n)
            if r is None or not (pane.x <= r.x < pane.x + pane.w):
                continue
            role = n.get_role_name()
            if role == "text" and n.get_state_set().contains(Atspi.StateType.EDITABLE) \
                    and r.y >= pane.y + pane.h - X11ViberUi.COMPOSER_BAND:
                text = ""
                iface = n.get_text_iface()
                if iface is not None:
                    text = Atspi.Text.get_text(iface, 0, Atspi.Text.get_character_count(iface)) or ""
                composer = {"rect": r, "text": text}
            elif role == "push button" and (n.get_name() or "").strip().lower() == "viber out":
                viber_out = True
        return {"pane": pane, "composer": composer, "viber_out": viber_out}

    async def _state(self) -> dict[str, Any]:
        return await self._a11y_call(self._snapshot)

    # -- chat navigation ---------------------------------------------------------

    async def _open_link(self, phone: str) -> None:
        await self._run(self._open_cmd, deep_link(phone))

    async def neutral(self) -> None:
        """Open the never-on-Viber number: afterwards no composer is on screen (proved, not assumed)."""
        await self._open_link(self._neutral_phone)

        async def is_neutral() -> bool:
            st = await self._state()
            return st["composer"] is None and st["viber_out"]

        if not await self._wait(is_neutral):
            raise UiError("neutral state not reached")
        if await self.peer_phone() != self._neutral_phone:
            raise UiError("neutral state: header does not show the neutral number")

    async def open_chat(self, phone: str) -> ChatOpen:
        await self._open_link(phone)

        async def outcome() -> ChatOpen | None:
            st = await self._state()
            if st["composer"] is not None:
                return ChatOpen.OPEN
            if st["viber_out"] and await self.peer_phone() == phone:
                return ChatOpen.NOT_ON_VIBER
            return None

        return await self._wait(outcome) or ChatOpen.TIMEOUT

    async def wait_chat_open(self) -> bool:
        async def has_composer() -> bool:
            return (await self._state())["composer"] is not None

        return bool(await self._wait(has_composer))

    async def peer_phone(self) -> str | None:
        st = await self._state()
        pane: Rect | None = st["pane"]
        if pane is None:
            return None
        hx, hy = self.STATUS_HOVER
        await self._run("xdotool", "mousemove", str(pane.x + hx), str(pane.y + hy))
        await asyncio.sleep(self._hover_delay)
        try:
            text = await self._ocr_digits(Rect(pane.x + self.STATUS[0], pane.y + self.STATUS[1],
                                               self.STATUS[2], self.STATUS[3]))
        finally:
            await self._run("xdotool", "mousemove", str(pane.x + pane.w - 10), str(pane.y + pane.h // 2))
        return parse_phone(text)

    async def _ocr_digits(self, r: Rect) -> str:
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            full, crop = Path(tmp) / "full.png", Path(tmp) / "crop.png"
            await self._run("import", "-window", "root", str(full), timeout=20)
            await self._run("convert", str(full), "-crop", f"{r.w}x{r.h}+{r.x}+{r.y}", "+repage",
                            "-resize", "200%", str(crop))
            out = await self._run("tesseract", str(crop), "stdout", "--psm", "7", "-l", "eng",
                                  "-c", "tessedit_char_whitelist=+0123456789", timeout=20)
        return out.decode(errors="replace")

    # -- composer ----------------------------------------------------------------

    async def paste(self, text: str) -> bool:
        """Paste into the composer; True only if the composer then holds exactly this text."""
        await self._run("xclip", "-selection", "clipboard", "-i", stdin=text.encode("utf-8"))
        try:
            await self._run("xdotool", "key", "--clearmodifiers", "ctrl+v")
            await asyncio.sleep(0.5)
        finally:
            # Do not leave customer text in the clipboard.
            await self._run("xclip", "-selection", "clipboard", "-i", stdin=b"")
        composer = (await self._state())["composer"]
        if composer is None:
            return False
        if not self._paste_check:
            return True
        return _same_text(composer["text"], text)

    async def clear_composer(self) -> None:
        await self._run("xdotool", "key", "--clearmodifiers", "ctrl+a")
        await self._run("xdotool", "key", "--clearmodifiers", "Delete")

    async def press_enter(self) -> bool:
        """Send; True if the composer is empty afterwards (the message left the composer)."""
        await self._run("xdotool", "key", "--clearmodifiers", "Return")

        async def emptied() -> bool:
            composer = (await self._state())["composer"]
            return composer is not None and not composer["text"].strip()

        return bool(await self._wait(emptied, timeout=5))

    # -- misc ----------------------------------------------------------------------

    async def screenshot(self, name: str) -> Path | None:
        self._debug_dir.mkdir(parents=True, exist_ok=True)
        path = self._debug_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.png"
        try:
            await self._run("import", "-window", "root", str(path), timeout=20)
            os.chmod(path, 0o600)
            return path
        except (UiError, OSError):
            log.warning("screenshot_failed", extra={"name": name})
            return None

    def viber_running(self) -> bool:
        return any_process_matches("/opt/viber/Viber")


def _same_text(actual: str, expected: str) -> bool:
    # Qt may normalise line endings and trailing whitespace in the composer.
    def norm(s: str) -> str:
        return "\n".join(line.rstrip() for line in s.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()

    return norm(actual) == norm(expected)


def any_process_matches(needle: str) -> bool:
    proc = Path("/proc")
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if needle.encode() in cmdline:
            return True
    return False


def cleanup_debug_dir(debug_dir: Path, retention_days: int) -> int:
    """Screenshots may show customer chats: keep them only for a short time."""
    if not debug_dir.is_dir():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in debug_dir.glob("*.png"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed
