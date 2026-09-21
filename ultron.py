"""
Ultron — Modular Voice Assistant  v2026.09.21
=============================================

A safe, always-on Windows voice assistant.

Usage
-----
  python ultron.py                     # Interactive mode
  python ultron.py --background        # Background always-ready mode
  python ultron.py --set-api-key       # Store YouTube API key securely
  python ultron.py --calibrate         # Calibrate noise floor and exit
  python ultron.py --list-devices      # List audio input devices and exit

Safety contract
---------------
Ultron only performs an explicit allowlist of low-risk browser actions.
It never executes arbitrary shell commands, modifies files, changes system
settings, accesses credentials, sends messages, or makes purchases.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import random
import sys
import time
from pathlib import Path

# ── Bootstrap path so `ultron` package is always importable ──────────────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ultron import APP_VERSION
from ultron import config as cfg_mod
from ultron.browser import YouTubeBrowser
from ultron.commands import HELP_TEXT, CommandExecutor, plan_instruction
from ultron.context import Context
from ultron.hotkey import create_ptt
from ultron.tray import create_tray
from ultron.tts import TTSWorker
from ultron.voice import (
    AudioCapture,
    VoskCommandRecognizer,
    WakeWordDetector,
    calibrate_noise_floor,
    transcribe_online,
)

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_DIR = _HERE / "logs"

ACK_TEMPLATES = ("Done — {msg}.", "All set — {msg}.", "Got it — {msg}.")
FAIL_TEMPLATES = (
    "That didn't work — {msg}.",
    "I hit a snag — {msg}.",
    "I couldn't finish that — {msg}.",
)


def _normalize(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _configure_background_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        filename=LOG_DIR / "ultron.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    class _LogStream:
        def write(self, t: str) -> int:
            t = t.strip()
            if t:
                logging.info(t)
            return len(t)
        def flush(self) -> None:
            pass

    sys.stdout = _LogStream()  # type: ignore
    sys.stderr = _LogStream()  # type: ignore


# ── Main application ──────────────────────────────────────────────────────────

class Ultron:
    """Top-level orchestrator wiring all modules together."""

    def __init__(self, config: dict) -> None:
        self._cfg = config
        self._start_time = time.time()

        # Core modules
        self._tts = TTSWorker(rate=int(config.get("tts_rate", 185)))
        self._ctx = Context(history_max=int(config.get("command_history_max", 500)))
        self._browser = YouTubeBrowser(cache_max=int(config.get("youtube_cache_max", 100)))
        self._executor = CommandExecutor(
            browser=self._browser,
            context=self._ctx,
            tts=self._tts,
            config=config,
            api_key_getter=lambda: cfg_mod.get_api_key(self._cfg),
            start_time=self._start_time,
        )

        # Wake-word detector
        self._wake = WakeWordDetector(
            wake_word=str(config.get("wake_word", "activate ultron")),
            activation_phrases=list(config.get("activation_phrases", [])),
            model_path=str(config.get("vosk_model_path", "")),
        )

        # Vosk full-command recognizer (offline)
        self._vosk_cmd: VoskCommandRecognizer | None = None
        if config.get("vosk_full_commands") and config.get("vosk_model_path"):
            self._vosk_cmd = VoskCommandRecognizer(
                str(config["vosk_model_path"]),
                int(config.get("speech_sample_rate", 16000)),
            )

        # Audio capture
        self._audio = AudioCapture(config)

        # Noise calibration
        silence_rms = int(config.get("speech_silence_rms", 150))
        if silence_rms == 0:
            self._tts.say("Calibrating noise floor. Please stay quiet for one second.")
            floor = calibrate_noise_floor(
                config.get("microphone_device"),
                int(config.get("speech_sample_rate", 16000)),
            )
            silence_rms = max(80, int(floor * 2.5))
            self._cfg["speech_silence_rms"] = silence_rms
            self._audio.reconfigure(self._cfg)
            print(f"Noise calibration: floor={floor}, threshold set to {silence_rms}")
            self._tts.say(f"Threshold set to {silence_rms}.")

        # Active state
        self._active = False
        self._last_cmd_at = 0.0
        self._pin_phrase = _normalize(str(config.get("pin_phrase", "")))
        self._pin_waiting = False

        # Tray + PTT
        self._tray = create_tray(
            on_activate=self._tray_activate,
            on_deactivate=self._tray_deactivate,
            on_exit=self._tray_exit,
        )
        ptt_key = str(config.get("ptt_hotkey", ""))
        self._ptt_recording = False
        self._ptt = create_ptt(
            hotkey=ptt_key,
            on_press=self._ptt_press,
            on_release=self._ptt_release,
        )

        logging.info("Ultron v%s initialized.", APP_VERSION)

    # ── Tray callbacks ────────────────────────────────────────────────────

    def _tray_activate(self) -> None:
        if not self._active:
            self._activate()

    def _tray_deactivate(self) -> None:
        if self._active:
            self._deactivate()

    def _tray_exit(self) -> None:
        self._tts.say("Goodbye.")
        self.shutdown()
        sys.exit(0)

    # ── PTT callbacks ─────────────────────────────────────────────────────

    def _ptt_press(self) -> None:
        """Called when PTT key is pressed — capture audio."""
        self._ptt_recording = True
        print("[PTT] Recording…")
        self._tray.set_state("active")
        recording = self._audio.capture(
            max_seconds=float(self._cfg.get("active_recording_seconds", 6))
        )
        self._ptt_recording = False
        if recording is None:
            print("[PTT] No speech detected.")
            return
        transcript = self._transcribe(recording, wake_only=False)
        if transcript:
            print(f"[PTT] You said: {transcript}")
            self._handle_command(transcript)

    def _ptt_release(self) -> None:
        pass  # capture is driven by hold time; release is handled in _ptt_press

    # ── Activation state ──────────────────────────────────────────────────

    def _activate(self) -> None:
        self._active = True
        self._last_cmd_at = time.monotonic()
        self._tray.set_state("active")
        print("[ULTRON ACTIVE]")
        self._tts.say("ULTRON activated. Online, I'm listening.")

    def _deactivate(self, reason: str = "deactivated") -> None:
        self._active = False
        self._tray.set_state("idle")
        print(f"[ULTRON IDLE — {reason.upper()}]")
        self._tts.say("ULTRON deactivated.")

    # ── Transcription ─────────────────────────────────────────────────────

    def _transcribe(self, recording, wake_only: bool = False) -> str | None:
        rate = int(self._cfg.get("speech_sample_rate", 16000))
        lang = str(self._cfg.get("speech_recognition_language", "en-IN"))

        if wake_only:
            # Offline Vosk if available
            if self._wake.offline:
                detected = self._wake.detect(recording, rate)
                return self._wake.wake_word if detected else None
            # Online fallback for wake word
            text = transcribe_online(recording, rate, lang)
            if not text:
                return None
            return self._wake.wake_word if _normalize(text) in self._wake.phrases else None

        # Full command — try Vosk offline first
        if self._vosk_cmd:
            text = self._vosk_cmd.recognize(recording)
            if text:
                return text

        # Online Google SR
        return transcribe_online(recording, rate, lang)

    # ── Command handling ──────────────────────────────────────────────────

    def _is_deactivation(self, text: str) -> bool:
        norm = _normalize(text)
        phrases = [
            _normalize(p)
            for p in self._cfg.get("deactivation_phrases", [])
            if isinstance(p, str)
        ]
        return norm in phrases

    def _is_activation(self, text: str) -> bool:
        return _normalize(text) in self._wake.phrases

    def _handle_command(self, raw: str) -> bool:
        """
        Process one command. Returns False only when Ultron should stop.
        """
        command = raw.strip()
        norm = _normalize(command)

        if self._is_deactivation(command):
            self._deactivate("deactivated by voice")
            return True

        plan = plan_instruction(command, self._ctx.site)
        if not plan:
            self._tts.say_error(
                "That request is not approved or I couldn't understand it. "
                "Say help for the command list."
            )
            self._ctx.add_history(command, success=False)
            return True

        self._tray.set_state("processing")
        keep = True
        for action, value in plan:
            ok, msg = self._executor.run_step(action, value)
            if msg == "__exit__":
                keep = False
                break
            if not ok:
                self._tts.say_error(
                    random.choice(FAIL_TEMPLATES).format(msg=msg) +
                    " I stopped the remaining steps."
                )
                self._ctx.add_history(command, success=False)
                self._tray.set_state("active" if self._active else "idle")
                return keep
            # Only speak ack if not weather/status/history (they speak themselves)
            if action not in {"weather", "status", "history", "help",
                               "queue_list", "queue_clear", "timer"}:
                self._tts.say_ack(random.choice(ACK_TEMPLATES).format(msg=msg))

        self._ctx.add_history(command, success=True)
        self._last_cmd_at = time.monotonic()
        self._tray.set_state("active" if self._active else "idle")
        return keep

    # ── Always-ready voice loop ───────────────────────────────────────────

    def run_always_ready(self) -> None:
        """Idle ↔ Active state machine with voice capture."""
        idle_secs = float(self._cfg.get("idle_recording_seconds", 4))
        active_secs = float(self._cfg.get("active_recording_seconds", 6))
        timeout = self._cfg.get("active_timeout_seconds", 300)

        self._tray.start()
        self._ptt.start()
        self._tray.set_state("idle")
        print("[ULTRON IDLE — waiting for 'Activate ULTRON']")

        try:
            while True:
                if not self._active:
                    # ── IDLE: listen for wake word ─────────────────────────
                    recording = self._audio.capture(max_seconds=idle_secs)
                    if recording is None:
                        continue
                    detected = self._transcribe(recording, wake_only=True)
                    if not detected:
                        continue
                    # Optional PIN phrase
                    if self._pin_phrase:
                        self._tts.say("Please say your PIN phrase.")
                        pin_rec = self._audio.capture(max_seconds=active_secs)
                        if pin_rec is None:
                            self._tts.say("PIN not heard. Staying idle.")
                            continue
                        pin_text = self._transcribe(pin_rec, wake_only=False)
                        if _normalize(pin_text or "") != self._pin_phrase:
                            self._tts.say("Wrong PIN. Staying idle.")
                            continue
                    self._activate()

                else:
                    # ── Timeout check ──────────────────────────────────────
                    if (
                        isinstance(timeout, (int, float))
                        and timeout > 0
                        and time.monotonic() - self._last_cmd_at >= timeout
                    ):
                        self._deactivate("timeout")
                        continue

                    # ── ACTIVE: listen for command ─────────────────────────
                    print("[ULTRON ACTIVE — listening]")
                    recording = self._audio.capture(max_seconds=active_secs)
                    if recording is None:
                        continue

                    # Update timeout on any speech, not just approved commands
                    self._last_cmd_at = time.monotonic()

                    transcript = self._transcribe(recording, wake_only=False)
                    if not transcript:
                        self._tts.say("I couldn't understand that.")
                        continue

                    print(f"You said: {transcript}")
                    print(f"Normalized: {_normalize(transcript)!r}")

                    keep = self._handle_command(transcript)
                    if not keep:
                        # "exit" in voice mode → return to idle, never kill process
                        self._deactivate("exit requested")

        except KeyboardInterrupt:
            print("\n[ULTRON STOPPED]")

    # ── Interactive typed mode ────────────────────────────────────────────

    def run_interactive(self) -> None:
        """Interactive console + optional voice mode."""
        self._tray.start()
        self._ptt.start()
        self._tts.say("Ultron is ready. Type help for commands, or type voice for always-ready mode.")

        while True:
            try:
                raw = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not raw:
                continue

            if raw.lower() == "voice":
                self.run_always_ready()
                continue

            if not self._handle_command(raw):
                break

        self.shutdown()

    # ── Cleanup ───────────────────────────────────────────────────────────

    def shutdown(self) -> None:
        self._executor.cancel_timers()
        self._browser.close()
        self._ptt.stop()
        self._tray.stop()
        logging.info("Ultron stopped.")


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Ultron voice assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--background", action="store_true",
                        help="Run always-ready voice mode, log to file")
    parser.add_argument("--set-api-key", action="store_true",
                        help="Securely store YouTube API key in Windows Credential Manager")
    parser.add_argument("--calibrate", action="store_true",
                        help="Sample ambient noise, print recommended threshold, then exit")
    parser.add_argument("--list-devices", action="store_true",
                        help="List audio input devices and exit")
    args = parser.parse_args()

    # ── List devices ──────────────────────────────────────────────────────
    if args.list_devices:
        try:
            import sounddevice as sd
            print("Available audio input devices:")
            for i, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    print(f"  [{i:2d}] {dev['name']}  (rate={dev['default_samplerate']:.0f} Hz)")
        except ImportError:
            print("sounddevice not installed.")
        return

    # ── Secure API key setup ──────────────────────────────────────────────
    if args.set_api_key:
        key = getpass.getpass("Paste your YouTube Data API key (hidden): ").strip()
        if key:
            cfg_mod.save_api_key(key)
        else:
            print("No key entered. Cancelled.")
        return

    # ── Load config ───────────────────────────────────────────────────────
    config = cfg_mod.load()

    # ── Calibrate ─────────────────────────────────────────────────────────
    if args.calibrate:
        try:
            from ultron.voice import calibrate_noise_floor
            print("Sampling ambient noise for 1 second. Stay quiet…")
            floor = calibrate_noise_floor(
                config.get("microphone_device"),
                int(config.get("speech_sample_rate", 16000)),
            )
            recommended = max(80, int(floor * 2.5))
            print(f"Ambient RMS floor : {floor}")
            print(f"Recommended threshold: {recommended}")
            print(f"Set  \"speech_silence_rms\": {recommended}  in ultron_config.json")
        except ImportError:
            print("sounddevice not installed.")
        return

    # ── Background mode ───────────────────────────────────────────────────
    if args.background:
        _configure_background_logging()
        logging.info("Ultron background process starting (version=%s).", APP_VERSION)

    ultron = Ultron(config)
    try:
        if args.background:
            ultron.run_always_ready()
        else:
            ultron.run_interactive()
    except Exception:
        logging.exception("Ultron terminated unexpectedly.")
        raise
    finally:
        ultron.shutdown()


if __name__ == "__main__":
    _cli()
