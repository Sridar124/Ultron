"""
ultron/tray.py — System tray icon using pystray.

States:
  ● idle      — grey dot, waiting for wake word
  ● active    — green dot, listening for commands
  ● processing — yellow dot, executing a command
  ● error     — red dot, something went wrong

Right-click menu: Status | Activate | Deactivate | Exit
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

try:
    import pystray
    from PIL import Image, ImageDraw
    _TRAY = True
except ImportError:
    pystray = None  # type: ignore
    Image = None  # type: ignore
    ImageDraw = None  # type: ignore
    _TRAY = False

# RGBA colours for each state
_COLOURS = {
    "idle":       (120, 120, 140, 255),
    "active":     (80,  200, 100, 255),
    "processing": (255, 200,  50, 255),
    "error":      (220,  60,  60, 255),
}
_STATE_LABELS = {
    "idle":       "ULTRON — Idle (say 'Activate ULTRON')",
    "active":     "ULTRON — Active (listening)",
    "processing": "ULTRON — Processing command…",
    "error":      "ULTRON — Error (check logs)",
}


def _make_icon(state: str) -> "Image.Image":
    """Create a small circle icon for the given state."""
    size = 64
    colour = _COLOURS.get(state, _COLOURS["idle"])
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse(
        (margin, margin, size - margin, size - margin),
        fill=colour,
    )
    return img


class TrayManager:
    """Manages the system tray icon lifecycle."""

    def __init__(
        self,
        on_activate: Callable[[], None],
        on_deactivate: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate
        self._on_exit = on_exit
        self._icon: "pystray.Icon | None" = None
        self._state = "idle"
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the tray icon in a daemon thread."""
        if not _TRAY:
            logging.warning("pystray or Pillow not installed. Tray icon disabled.")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="UltronTray")
        self._thread.start()

    def _run(self) -> None:
        try:
            menu = pystray.Menu(
                pystray.MenuItem("Status", self._status_action),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Activate", lambda: self._on_activate()),
                pystray.MenuItem("Deactivate", lambda: self._on_deactivate()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda: self._on_exit()),
            )
            self._icon = pystray.Icon(
                name="Ultron",
                icon=_make_icon("idle"),
                title=_STATE_LABELS["idle"],
                menu=menu,
            )
            self._icon.run()
        except Exception as exc:
            logging.warning("Tray icon failed: %s", exc)

    def _status_action(self) -> None:
        import subprocess
        subprocess.Popen(
            ["powershell", "-NoProfile", "-Command",
             f"[System.Windows.MessageBox]::Show('ULTRON state: {self._state.upper()}', 'Ultron Status')"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def set_state(self, state: str) -> None:
        """Update tray icon and tooltip. Thread-safe."""
        self._state = state
        if self._icon is not None:
            try:
                self._icon.icon = _make_icon(state)
                self._icon.title = _STATE_LABELS.get(state, f"ULTRON — {state}")
            except Exception as exc:
                logging.debug("Tray update failed: %s", exc)

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass


class NoOpTray:
    """Fallback when pystray is unavailable."""
    def start(self) -> None:
        pass
    def set_state(self, state: str) -> None:
        pass
    def stop(self) -> None:
        pass


def create_tray(
    on_activate: Callable[[], None],
    on_deactivate: Callable[[], None],
    on_exit: Callable[[], None],
) -> TrayManager | NoOpTray:
    if _TRAY:
        return TrayManager(on_activate, on_deactivate, on_exit)
    return NoOpTray()
