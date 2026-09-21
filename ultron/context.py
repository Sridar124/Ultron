"""
ultron/context.py — Persistent state and command history.

All state is written to context.json on disk so it survives crashes
and Windows restarts. Command history is capped at command_history_max.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONTEXT_FILE = PROJECT_DIR / "context.json"
HISTORY_FILE = PROJECT_DIR / "command_history.json"


class Context:
    """Persistent browser/session context."""

    def __init__(self, history_max: int = 500) -> None:
        self._history_max = history_max
        self._state: dict[str, Any] = self._load_context()
        self._history: list[dict[str, Any]] = self._load_history()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load_context(self) -> dict[str, Any]:
        defaults: dict[str, Any] = {
            "site": "",
            "last_search": "",
            "last_video": "",
            "volume": 100,
            "last_result_urls": [],
        }
        try:
            if CONTEXT_FILE.is_file():
                raw = json.loads(CONTEXT_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    defaults.update({k: raw[k] for k in defaults if k in raw})
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Could not load context: %s", exc)
        return defaults

    def _save_context(self) -> None:
        try:
            CONTEXT_FILE.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logging.warning("Could not save context: %s", exc)

    def _load_history(self) -> list[dict[str, Any]]:
        try:
            if HISTORY_FILE.is_file():
                raw = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    return raw
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("Could not load history: %s", exc)
        return []

    def _save_history(self) -> None:
        try:
            HISTORY_FILE.write_text(
                json.dumps(self._history[-self._history_max:], indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            logging.warning("Could not save history: %s", exc)

    # ── Context getters / setters ─────────────────────────────────────────

    @property
    def site(self) -> str:
        return str(self._state.get("site", ""))

    @site.setter
    def site(self, value: str) -> None:
        self._state["site"] = value
        self._save_context()

    @property
    def last_search(self) -> str:
        return str(self._state.get("last_search", ""))

    @last_search.setter
    def last_search(self, value: str) -> None:
        self._state["last_search"] = value
        self._save_context()

    @property
    def last_video(self) -> str:
        return str(self._state.get("last_video", ""))

    @last_video.setter
    def last_video(self, value: str) -> None:
        self._state["last_video"] = value
        self._save_context()

    @property
    def volume(self) -> int:
        return int(self._state.get("volume", 100))

    @volume.setter
    def volume(self, value: int) -> None:
        self._state["volume"] = max(0, min(100, value))
        self._save_context()

    @property
    def last_result_urls(self) -> list[str]:
        return list(self._state.get("last_result_urls", []))

    @last_result_urls.setter
    def last_result_urls(self, value: list[str]) -> None:
        self._state["last_result_urls"] = value
        self._save_context()

    def update_site(self, site: str, search: str = "") -> None:
        self._state["site"] = site
        if search:
            self._state["last_search"] = search
        self._save_context()

    # ── History ───────────────────────────────────────────────────────────

    def add_history(self, command: str, success: bool = True) -> None:
        entry = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "cmd": command,
            "ok": success,
        }
        self._history.append(entry)
        if len(self._history) > self._history_max:
            self._history = self._history[-self._history_max:]
        self._save_history()

    def get_history(self, n: int = 10) -> list[dict[str, Any]]:
        return self._history[-n:]

    def format_history(self, n: int = 10) -> str:
        entries = self.get_history(n)
        if not entries:
            return "No command history yet."
        lines = [f"Last {min(n, len(entries))} commands:"]
        for i, e in enumerate(reversed(entries), 1):
            status = "✓" if e.get("ok") else "✗"
            lines.append(f"  {i:2}. [{e['ts']}] {status} {e['cmd']}")
        return "\n".join(lines)

    def reset_site(self) -> None:
        self._state["site"] = ""
        self._save_context()
