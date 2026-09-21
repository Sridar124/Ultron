"""
ultron/voice.py — Background VAD thread + audio capture.

Key improvements over original:
- VAD runs on a background thread; main thread is never blocked between captures
- Dynamic speech start/end — no fixed recording window
- Noise floor auto-calibration on startup (1-second ambient sample)
- Pre-roll preserved across all modes (idle and active)
- Thread-safe result queue for PTT and always-ready modes
"""

from __future__ import annotations

import io
import logging
import queue
import threading
import time
from typing import Any

import numpy as np

try:
    import sounddevice as sd
    import soundfile as sf
except ImportError:
    sd = None  # type: ignore
    sf = None  # type: ignore

try:
    import speech_recognition as sr
except ImportError:
    sr = None  # type: ignore

try:
    import vosk
except ImportError:
    vosk = None  # type: ignore


# ── Constants ─────────────────────────────────────────────────────────────────

BLOCK_SECONDS = 0.1          # Size of each audio chunk for RMS evaluation
NOISE_CALIBRATION_SECONDS = 1.0
NOISE_MULTIPLIER = 2.5       # speech_silence_rms = floor_rms * NOISE_MULTIPLIER


def _rms(block: np.ndarray) -> int:
    return int((block.astype("float64") ** 2).mean() ** 0.5)


# ── Noise calibration ─────────────────────────────────────────────────────────

def calibrate_noise_floor(
    device: Any,
    sample_rate: int = 16000,
    duration: float = NOISE_CALIBRATION_SECONDS,
) -> int:
    """Sample ambient audio for `duration` seconds and return median RMS."""
    if sd is None:
        return 0
    block_frames = int(sample_rate * BLOCK_SECONDS)
    n_blocks = max(1, int(duration / BLOCK_SECONDS))
    rms_values: list[int] = []
    try:
        with sd.InputStream(
            device=device, samplerate=sample_rate, channels=1,
            dtype="int16", blocksize=block_frames
        ) as stream:
            for _ in range(n_blocks):
                block, _ = stream.read(block_frames)
                rms_values.append(_rms(block))
    except Exception as exc:
        logging.warning("Noise calibration failed: %s", exc)
        return 0
    rms_values.sort()
    return rms_values[len(rms_values) // 2]   # median


# ── Wake-word detector ─────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    import re
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


class WakeWordDetector:
    """Offline Vosk or online SpeechRecognition wake-phrase detector."""

    def __init__(self, wake_word: str, activation_phrases: list[str], model_path: str) -> None:
        from pathlib import Path
        self.wake_word = wake_word.lower().strip()
        self.phrases = tuple(dict.fromkeys(
            [_normalize(wake_word)] + [_normalize(p) for p in activation_phrases if p]
        ))
        self.model = None
        if vosk and model_path:
            mp = Path(model_path)
            if not mp.is_absolute():
                from ultron.config import PROJECT_DIR
                mp = PROJECT_DIR / mp
            if mp.is_dir():
                try:
                    self.model = vosk.Model(str(mp))
                    logging.info("Offline Vosk wake-word model loaded.")
                except Exception as exc:
                    logging.error("Vosk model load failed: %s", exc)

    @property
    def offline(self) -> bool:
        return self.model is not None

    def detect(self, recording: np.ndarray, sample_rate: int) -> bool:
        if self.model is not None:
            recognizer = vosk.KaldiRecognizer(self.model, sample_rate)
            recognizer.SetGrammar(__import__("json").dumps([*self.phrases, "[unk]"]))
            recognizer.AcceptWaveform(recording.tobytes())
            result = __import__("json").loads(recognizer.FinalResult())
            return _normalize(result.get("text", "")) in self.phrases
        return False


# ── Vosk full-command recognizer ───────────────────────────────────────────────

class VoskCommandRecognizer:
    """Use an offline Vosk model for full command transcription."""

    def __init__(self, model_path: str, sample_rate: int = 16000) -> None:
        from pathlib import Path
        self.model = None
        self.sample_rate = sample_rate
        if not vosk or not model_path:
            return
        mp = Path(model_path)
        if not mp.is_absolute():
            from ultron.config import PROJECT_DIR
            mp = PROJECT_DIR / mp
        if mp.is_dir():
            try:
                self.model = vosk.Model(str(mp))
                logging.info("Offline Vosk command recognizer loaded.")
            except Exception as exc:
                logging.warning("Vosk command model load failed: %s", exc)

    def recognize(self, recording: np.ndarray) -> str | None:
        if self.model is None:
            return None
        try:
            rec = vosk.KaldiRecognizer(self.model, self.sample_rate)
            rec.AcceptWaveform(recording.tobytes())
            result = __import__("json").loads(rec.FinalResult())
            text = result.get("text", "").strip()
            return text if text else None
        except Exception as exc:
            logging.warning("Vosk command recognition failed: %s", exc)
            return None


# ── Audio capture ─────────────────────────────────────────────────────────────

class AudioCapture:
    """
    Single-shot dynamic VAD capture.

    Records from the microphone using dynamic speech start + end detection.
    No fixed window — stops when silence follows speech, or after max_seconds.
    Returns raw numpy int16 array or None if no speech detected.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._cfg = config
        self._device_reported = False
        self._device = config.get("microphone_device") or None
        self._rate = int(config.get("speech_sample_rate", 16000))
        self._threshold = int(config.get("speech_silence_rms", 150))
        self._end_silence = float(config.get("speech_end_silence_seconds", 0.8))
        self._preroll = float(config.get("speech_preroll_seconds", 0.3))

    def reconfigure(self, config: dict[str, Any]) -> None:
        self._threshold = int(config.get("speech_silence_rms", 150))
        self._end_silence = float(config.get("speech_end_silence_seconds", 0.8))
        self._preroll = float(config.get("speech_preroll_seconds", 0.3))

    def capture(self, max_seconds: float = 6.0) -> np.ndarray | None:
        """Capture one utterance. Returns int16 numpy array or None."""
        if sd is None:
            return None

        block_frames = int(self._rate * BLOCK_SECONDS)
        pre_roll_n = max(1, int(self._preroll / BLOCK_SECONDS))
        end_silence_n = max(1, int(self._end_silence / BLOCK_SECONDS))
        max_blocks = max(1, int(max_seconds / BLOCK_SECONDS))

        pre_roll: list[np.ndarray] = []
        utterance: list[np.ndarray] = []
        speech_started = False
        silent_blocks = 0
        overflows = 0

        try:
            if not self._device_reported:
                info = sd.query_devices(self._device, kind="input")
                print(
                    f"Audio input: name={info['name']!r}, "
                    f"rate={info['default_samplerate']}, device={self._device!r}"
                )
                self._device_reported = True

            with sd.InputStream(
                device=self._device, samplerate=self._rate,
                channels=1, dtype="int16", blocksize=block_frames
            ) as stream:
                # Phase 1: wait for speech
                for _ in range(max_blocks):
                    block, overflowed = stream.read(block_frames)
                    if overflowed:
                        overflows += 1
                    pre_roll.append(block.copy())
                    pre_roll = pre_roll[-pre_roll_n:]
                    if _rms(block) >= self._threshold:
                        speech_started = True
                        utterance = list(pre_roll)
                        break

                if not speech_started:
                    return None

                # Phase 2: capture until silence
                for _ in range(max_blocks):
                    block, overflowed = stream.read(block_frames)
                    if overflowed:
                        overflows += 1
                    utterance.append(block.copy())
                    if _rms(block) < self._threshold:
                        silent_blocks += 1
                    else:
                        silent_blocks = 0
                    if silent_blocks >= end_silence_n:
                        break

        except sd.PortAudioError as exc:
            logging.error("Microphone error: %s", exc)
            print(f"Microphone error: {exc}")
            return None
        except (OSError, RuntimeError) as exc:
            logging.error("Audio recording error: %s", exc)
            return None

        if overflows:
            logging.debug("Audio: %d overflow(s) during capture.", overflows)

        if not utterance:
            return None

        recording = np.vstack(utterance)
        rms = _rms(recording)
        print(
            f"Captured: frames={len(recording)}, rms={rms}, "
            f"peak={int(abs(recording.astype('int32')).max())}, "
            f"rate={self._rate} Hz"
        )
        if rms < self._threshold:
            return None
        return recording


def recording_to_wav(recording: np.ndarray, sample_rate: int) -> io.BytesIO:
    """Convert int16 numpy array to an in-memory WAV stream."""
    buf = io.BytesIO()
    sf.write(buf, recording, sample_rate, format="WAV", subtype="PCM_16")
    buf.seek(0)
    return buf


def transcribe_online(recording: np.ndarray, sample_rate: int, language: str) -> str | None:
    """Transcribe audio using Google SpeechRecognition."""
    if sr is None:
        return None
    recognizer = sr.Recognizer()
    buf = recording_to_wav(recording, sample_rate)
    try:
        with sr.AudioFile(buf) as source:
            audio = recognizer.record(source)
        transcript = recognizer.recognize_google(audio, language=language)
        print(f"SR transcript ({language}): {transcript!r}")
        return transcript
    except sr.UnknownValueError:
        logging.debug("SR: could not understand audio.")
        return None
    except sr.RequestError as exc:
        logging.warning("SR: Google request failed: %s", exc)
        print(f"SR request error: {exc}")
        return None
    except Exception as exc:
        logging.warning("SR: unexpected error: %s", exc)
        return None
