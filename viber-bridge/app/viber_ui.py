"""Viber Desktop UI automation (X11): deep link + clipboard paste + Enter.

Text goes through the clipboard (xclip + Ctrl+V); `xdotool type` breaks Cyrillic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

log = logging.getLogger("viber.ui")


class UiError(RuntimeError):
    pass


class TargetNotVerified(UiError):
    """The focused chat could not be proven to belong to the target phone."""


class ViberUiDriver(Protocol):
    async def open_chat(self, phone: str) -> None: ...

    async def verify_target(self, phone: str) -> None: ...

    async def paste_and_send(self, text: str) -> None: ...

    async def screenshot(self, name: str) -> Path | None: ...

    def viber_running(self) -> bool: ...


def deep_link(phone: str) -> str:
    return f"viber://chat?number={quote(phone, safe='')}"


class X11ViberUi:
    def __init__(
        self, display: str, open_cmd: str, window_class: str, step_delay: float, debug_dir: Path,
    ) -> None:
        self._env = {**os.environ, "DISPLAY": display}
        self._open_cmd = open_cmd
        self._window_class = window_class
        self._step_delay = step_delay
        self._debug_dir = debug_dir

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

    async def _activate_window(self) -> None:
        await self._run(
            "xdotool", "search", "--limit", "1", "--onlyvisible", "--class", self._window_class,
            "windowactivate", "--sync",
        )

    async def open_chat(self, phone: str) -> None:
        await self._run(self._open_cmd, deep_link(phone))
        await asyncio.sleep(self._step_delay)
        await self._activate_window()

    async def verify_target(self, phone: str) -> None:
        # TODO(phase 2): design on the live Viber instance how to prove the focused chat
        # is `phone` (window title, viber.db state, ...). Until then refuse to send:
        # pasting into the previously open chat would leak text to another customer.
        raise TargetNotVerified("target verification not implemented yet")

    async def paste_and_send(self, text: str) -> None:
        await self._run("xclip", "-selection", "clipboard", "-i", stdin=text.encode("utf-8"))
        try:
            await self._activate_window()
            await self._run("xdotool", "key", "--clearmodifiers", "ctrl+v")
            await asyncio.sleep(0.5)
            await self._run("xdotool", "key", "--clearmodifiers", "Return")
        finally:
            # Do not leave customer text in the clipboard.
            await self._run("xclip", "-selection", "clipboard", "-i", stdin=b"")

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
