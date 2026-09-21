"""
ultron/config.py — Secure configuration management.

Priority for secrets (e.g. YouTube API key):
  1. Windows Credential Manager (keyring)
  2. YOUTUBE_API_KEY environment variable
  3. ultron_config.json value (plain-text fallback, warn on use)

All other settings are loaded from ultron_config.json with defaults.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

try:
    import keyring
    import keyring.errors
    _KEYRING_AVAILABLE = True
except ImportError:
    keyring = None  # type: ignore
    _KEYRING_AVAILABLE = False

PROJECT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_DIR / "ultron_config.json"

_KEYRING_SERVICE = "Ultron"
_KEYRING_API_KEY = "youtube_api_key"

DEFAULT_CONFIG: dict[str, Any] = {
    # ── Activation ────────────────────────────────────────────────────────
    "wake_word": "activate ultron",
    "activation_phrases": ["activate ultron", "open ultron"],
    "deactivation_phrases": [
        "deactivate ultron", "ultron deactivate",
        "go to sleep", "ultron exit", "ultron exits", "ultron quit",
    ],
    "pin_phrase": "",                   # Optional spoken PIN after wake word

    # ── Audio capture ─────────────────────────────────────────────────────
    "idle_recording_seconds": 4,        # Max listen window for wake word
    "active_recording_seconds": 6,      # Max listen window for commands
    "speech_sample_rate": 16000,
    "speech_silence_rms": 150,          # Set to 0 to auto-calibrate on startup
    "speech_noise_floor_rms": 0,        # Auto-set by noise calibration
    "speech_end_silence_seconds": 0.8,
    "speech_preroll_seconds": 0.3,
    "microphone_device": None,          # None = Windows default input

    # ── Recognition ───────────────────────────────────────────────────────
    "speech_recognition_language": "en-IN",
    "vosk_model_path": "",              # Path to Vosk model directory
    "vosk_full_commands": False,        # Use Vosk for commands too (not just wake)

    # ── TTS ───────────────────────────────────────────────────────────────
    "tts_rate": 185,

    # ── Timeouts ──────────────────────────────────────────────────────────
    "active_timeout_seconds": 300,      # 5 min idle → return to IDLE state

    # ── Browser / YouTube ─────────────────────────────────────────────────
    "youtube_api_key": "",              # Prefer keyring; this is last resort
    "youtube_cache_max": 100,           # Max video ID cache entries

    # ── Push-to-talk ──────────────────────────────────────────────────────
    "ptt_hotkey": "ctrl+space",         # Hold to record, release to process

    # ── Reliability ───────────────────────────────────────────────────────
    "max_restarts": 3,                  # Crash-recovery restart limit (VBS)

    # ── Queue ─────────────────────────────────────────────────────────────
    "video_queue_max": 50,

    # ── History ───────────────────────────────────────────────────────────
    "command_history_max": 500,
}


def load() -> dict[str, Any]:
    """Load config from disk, filling missing keys with defaults."""
    config: dict[str, Any] = DEFAULT_CONFIG.copy()
    try:
        if CONFIG_PATH.is_file():
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                config.update({k: raw[k] for k in config if k in raw})
    except (OSError, json.JSONDecodeError) as exc:
        logging.error("Could not load %s: %s", CONFIG_PATH.name, exc)
    return config


def get_api_key(config: dict[str, Any]) -> str:
    """Retrieve YouTube API key with secure priority chain."""
    # 1. Windows Credential Manager
    if _KEYRING_AVAILABLE and keyring is not None:
        try:
            stored = keyring.get_password(_KEYRING_SERVICE, _KEYRING_API_KEY)
            if stored:
                return stored
        except keyring.errors.KeyringError as exc:
            logging.warning("Keyring read failed: %s", exc)

    # 2. Environment variable
    env_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if env_key:
        return env_key

    # 3. Plain-text config (warn)
    cfg_key = str(config.get("youtube_api_key", "")).strip()
    if cfg_key:
        logging.warning(
            "YouTube API key loaded from plain-text config. "
            "Run: python ultron.py --set-api-key  to store it securely."
        )
        return cfg_key

    return ""


def save_api_key(key: str) -> bool:
    """Store API key in Windows Credential Manager via keyring."""
    if not _KEYRING_AVAILABLE or keyring is None:
        print("keyring is not installed. Install it with: pip install keyring")
        return False
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_API_KEY, key)
        print("YouTube API key saved securely in Windows Credential Manager.")
        return True
    except keyring.errors.KeyringError as exc:
        print(f"Could not save to keyring: {exc}")
        return False


def save(config: dict[str, Any]) -> None:
    """Write config back to disk (never writes the API key to file)."""
    safe = {k: v for k, v in config.items() if k != "youtube_api_key"}
    safe["youtube_api_key"] = ""        # Always blank out key on save
    try:
        CONFIG_PATH.write_text(
            json.dumps(safe, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logging.error("Could not save config: %s", exc)
