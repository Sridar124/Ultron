"""
ultron/tts.py — Non-blocking, cross-platform text-to-speech.

TTS runs on a dedicated daemon thread so Ultron can never be
blocked while speaking. say() enqueues immediately and returns.
Supports per-message rate multipliers (errors = slower, acks = faster).
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import threading
from typing import Any

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None  # type: ignore


class TTSWorker:
    """Thread-safe TTS engine wrapper with non-blocking enqueue."""

    def __init__(self, rate: int = 185) -> None:
        self._rate = rate
        self._queue: queue.Queue[tuple[str, int] | None] = queue.Queue()
        self._engine = self._init_engine(rate)
        self._thread = threading.Thread(target=self._worker, daemon=True, name="UltronTTS")
        self._thread.start()

    # ── Engine init ───────────────────────────────────────────────────────

    @staticmethod
    def _init_engine(rate: int):
        """Create the platform-appropriate TTS engine."""
        if pyttsx3 is not None:
            try:
                engine = pyttsx3.init(driverName="sapi5" if os.name == "nt" else None)
                engine.setProperty("rate", rate)
                logging.info("TTS: SAPI5 engine ready (rate=%d).", rate)
                print(f"Speech output ready (SAPI5, rate={rate}).")
                return engine
            except Exception as exc:
                logging.warning("TTS: pyttsx3 init failed: %s", exc)
                print(f"Speech output disabled: {type(exc).__name__}: {exc}")
        else:
            logging.warning("TTS: pyttsx3 not installed.")
            print("Speech output disabled: pyttsx3 is not installed.")
        return None

    # ── Background worker ─────────────────────────────────────────────────

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                if hasattr(self._queue, "task_done"):
                    self._queue.task_done()
                break
            text, rate_override = item
            self._speak_sync(text, rate_override)
            if hasattr(self._queue, "task_done"):
                self._queue.task_done()

    def _speak_sync(self, text: str, rate_override: int) -> None:
        """Actually speak — only called from the TTS thread."""
        print(f"Ultron: {text}")
        if self._engine is None:
            self._fallback_speak(text)
            return
        try:
            self._engine.setProperty("rate", rate_override)
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as exc:
            logging.warning("TTS speak error: %s", exc)
            try:
                self._engine.stop()
            except Exception:
                pass
            self._engine = None
            self._fallback_speak(text)

    @staticmethod
    def _fallback_speak(text: str) -> None:
        """Last-resort: use OS speech command when pyttsx3 is unavailable."""
        if os.name == "nt":
            # PowerShell SAPI fallback
            ps = (
                f'Add-Type -AssemblyName System.Speech; '
                f'$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; '
                f'$s.Speak("{text.replace(chr(34), chr(39))}")'
            )
            try:
                subprocess.Popen(
                    ["powershell", "-NoProfile", "-Command", ps],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
            except OSError:
                pass
        elif os.name == "posix":
            for cmd in (["say", text], ["espeak", text], ["spd-say", text]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    break
                except FileNotFoundError:
                    continue

    # ── Public API ────────────────────────────────────────────────────────

    def say(self, text: str, rate_multiplier: float = 1.0) -> None:
        """Enqueue a message for speaking. Returns immediately."""
        rate = max(50, int(self._rate * rate_multiplier))
        self._queue.put((text, rate))

    def say_error(self, text: str) -> None:
        """Speak an error message — slightly slower for clarity."""
        self.say(text, rate_multiplier=0.85)

    def say_ack(self, text: str) -> None:
        """Speak a quick acknowledgement — slightly faster."""
        self.say(text, rate_multiplier=1.1)

    def wait_until_done(self, timeout: float = 10.0) -> None:
        """Block until the TTS queue is empty (used before exit)."""
        self._queue.join() if hasattr(self._queue, "join") else None

    def shutdown(self) -> None:
        """Stop the TTS thread cleanly."""
        self._queue.put(None)
        self._thread.join(timeout=5.0)
