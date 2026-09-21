"""
ultron/hotkey.py — Push-to-talk (PTT) global hotkey.

Hold the configured hotkey (default: ctrl+space) to start recording.
Release to stop and process the command.

Uses the `keyboard` library which works at the system level on Windows.
Falls back gracefully when keyboard is unavailable.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

try:
    import keyboard
    _KEYBOARD = True
except ImportError:
    keyboard = None  # type: ignore
    _KEYBOARD = False


class PTTHotkey:
    """Hold-to-talk global hotkey manager."""

    def __init__(
        self,
        hotkey: str,
        on_press: Callable[[], None],
        on_release: Callable[[], None],
    ) -> None:
        self._hotkey = hotkey
        self._on_press = on_press
        self._on_release = on_release
        self._held = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not _KEYBOARD:
            logging.warning("keyboard library not installed. PTT disabled.")
            print("PTT disabled: install 'keyboard' with pip install keyboard")
            return
        if not self._hotkey:
            return
        try:
            # add_hotkey supports combo keys like ctrl+space
            keyboard.add_hotkey(self._hotkey, self._handle_press, suppress=False)
            print(f"PTT hotkey active: hold [{self._hotkey.upper()}] to speak.")
        except Exception as exc:
            logging.warning("PTT hotkey setup failed: %s", exc)
            print(f"PTT hotkey setup failed: {exc}")

    def _handle_press(self, _event) -> None:
        if not self._held:
            self._held = True
            t = threading.Thread(target=self._on_press, daemon=True)
            t.start()

    def _handle_release(self, _event) -> None:
        if self._held:
            self._held = False
            t = threading.Thread(target=self._on_release, daemon=True)
            t.start()

    def stop(self) -> None:
        if _KEYBOARD and keyboard is not None and self._hotkey:
            try:
                keyboard.unhook_all()
            except Exception:
                pass


class NoPTT:
    """Fallback when PTT is unavailable."""
    def start(self) -> None:
        pass
    def stop(self) -> None:
        pass


def create_ptt(
    hotkey: str,
    on_press: Callable[[], None],
    on_release: Callable[[], None],
) -> PTTHotkey | NoPTT:
    if _KEYBOARD and hotkey:
        return PTTHotkey(hotkey, on_press, on_release)
    return NoPTT()
