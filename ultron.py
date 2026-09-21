"""Ultron MVP: a deliberately safe, voice-enabled Windows assistant.

It only performs an allowlisted set of low-risk actions. It never executes
arbitrary commands, changes security settings, or handles credentials.
"""

from __future__ import annotations

import argparse
import sys
import json
import io
import logging
import os
from pathlib import Path
import random
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

import numpy as np

try:
    import pyttsx3
except ImportError:
    pyttsx3 = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None

try:
    import sounddevice as sd
    import soundfile as sf
except ImportError:
    sd = None
    sf = None

try:
    import vosk
except ImportError:
    vosk = None

try:
    from selenium import webdriver
    from selenium.common.exceptions import WebDriverException
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
except ImportError:
    webdriver = None
    WebDriverException = Exception
    ChromeOptions = None
    By = None
    WebDriverWait = None


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "ultron_config.json"
LOG_DIR = PROJECT_DIR / "logs"
APP_VERSION = "2026.09.19-startup-audio"

DEFAULT_CONFIG = {
    "active_timeout_seconds": 300,
    "idle_recording_seconds": 2,
    "active_recording_seconds": 4,
    "speech_silence_rms": 150,
    "speech_end_silence_seconds": 0.8,
    "speech_preroll_seconds": 0.3,
    "speech_sample_rate": 16000,
    "microphone_device": None,
    "wake_word": "activate ultron",
    "activation_phrases": ["activate ultron", "open ultron"],
    "deactivation_phrases": [
        "deactivate ultron",
        "ultron deactivate",
        "go to sleep",
        "ultron exit",
        "ultron exits",
        "ultron quit",
    ],
    "vosk_model_path": "",
    "speech_recognition_language": "en-US",
    "tts_rate": 185,
    "youtube_api_key": "",
}

SUCCESS_TEMPLATES = (
    "Done — {message}.",
    "All set — {message}.",
    "Nice, {message}.",
)
FAILURE_TEMPLATES = (
    "That did not work — {message}.",
    "I hit a snag — {message}.",
    "I could not finish that — {message}.",
)

HELP = """Commands:
  open youtube
  search youtube <topic>
  search google <topic>
  search <topic>
  play <song or artist>
  pause / resume / next
  volume up / volume down / set volume <0-100>
  fullscreen / go back
  open google
  open spotify
  help
  exit

Voice mode:
  Type voice once to start the always-ready idle mode.
  Say "Activate Ultron" to enter active mode.
  Say "Deactivate Ultron", "go to sleep", or "Ultron, exit" to return to idle mode.

Commands outside this list are refused on purpose.
"""

WAKE_WORD = re.compile(r"^\s*(?:hey\s+)?ultron\b[\s,.:;!-]*(.*)$", re.IGNORECASE)
VOICE_SILENCE_RMS = 150


def normalize_voice_phrase(text: str) -> str:
    """Normalize recognizer punctuation and spacing before phrase matching."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def normalized_phrases(value: object, fallback: list[str]) -> tuple[str, ...]:
    """Return a safe, normalized phrase list from JSON configuration."""
    if not isinstance(value, list):
        value = fallback
    phrases = tuple(
        normalized for item in value if isinstance(item, str)
        if (normalized := normalize_voice_phrase(item))
    )
    return phrases or tuple(normalize_voice_phrase(item) for item in fallback)


def is_deactivation_phrase(text: str, configured_phrases: object) -> bool:
    """Accept safe, narrowly scoped stop-listening phrases while active."""
    return normalize_voice_phrase(text) in normalized_phrases(
        configured_phrases, DEFAULT_CONFIG["deactivation_phrases"]
    )


def load_config() -> dict[str, object]:
    """Load a user-editable configuration without accepting arbitrary code."""
    config = DEFAULT_CONFIG.copy()
    try:
        if CONFIG_PATH.is_file():
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config.update({key: loaded[key] for key in config if key in loaded})
    except (OSError, json.JSONDecodeError) as error:
        logging.error("Could not load %s: %s", CONFIG_PATH.name, error)
    return config


def configure_background_logging() -> None:
    """Send hidden-startup output and uncaught exceptions to a durable log."""
    LOG_DIR.mkdir(exist_ok=True)
    log_path = LOG_DIR / "ultron.log"
    logging.basicConfig(
        filename=log_path,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    class LogStream:
        def write(self, text: str) -> int:
            text = text.strip()
            if text:
                logging.info(text)
            return len(text)

        def flush(self) -> None:
            return None

    sys.stdout = LogStream()
    sys.stderr = LogStream()


class WakeWordDetector:
    """Detect only the configured phrase while Ultron is idle.

    With a local Vosk model this is offline, grammar-restricted recognition;
    normal command transcription is never run while idle. SpeechRecognition
    is a compatibility fallback when no Vosk model has been configured.
    """

    def __init__(self, wake_word: str, activation_phrases: object, model_path: str) -> None:
        self.wake_word = wake_word.lower().strip()
        configured_phrases = normalized_phrases(
            activation_phrases, DEFAULT_CONFIG["activation_phrases"]
        )
        # Keep the legacy single wake_word setting authoritative as well, so
        # changing it in the JSON cannot accidentally leave an old phrase
        # active in the separate convenience list.
        self.activation_phrases = tuple(
            dict.fromkeys((normalize_voice_phrase(self.wake_word), *configured_phrases))
        )
        self.normalized_activation_phrases = self.activation_phrases
        self.model = None
        model_directory = Path(model_path)
        if model_path and not model_directory.is_absolute():
            model_directory = PROJECT_DIR / model_directory
        if vosk and model_path and model_directory.is_dir():
            try:
                self.model = vosk.Model(str(model_directory))
                logging.info("Offline Vosk wake-word detector enabled.")
            except Exception as error:
                logging.error("Could not load Vosk wake-word model: %s", error)

    @property
    def uses_offline_model(self) -> bool:
        return self.model is not None

    def detect(self, recording, sample_rate: int) -> bool:
        if self.model is not None:
            recognizer = vosk.KaldiRecognizer(self.model, sample_rate)
            recognizer.SetGrammar(json.dumps([*self.activation_phrases, "[unk]"]))
            recognizer.AcceptWaveform(recording.tobytes())
            result = json.loads(recognizer.FinalResult())
            return normalize_voice_phrase(result.get("text", "")) in self.normalized_activation_phrases
        return False


class YouTubeBrowser:
    """Own the controlled Chrome session and verify browser/media actions."""

    def __init__(self) -> None:
        self.driver = None

    def open(self, url: str) -> bool:
        """Navigate the Selenium-controlled Chrome session to a YouTube URL."""
        if webdriver is None or ChromeOptions is None:
            print("Controlled YouTube browser unavailable: Selenium is not installed.")
            return False

        try:
            if self.driver is None:
                options = ChromeOptions()
                self.driver = webdriver.Chrome(options=options)
                self.driver.set_page_load_timeout(30)
                print("Controlled YouTube Chrome session started.")
            self.driver.get(url)
            actual_url = self.driver.current_url
            if not actual_url.startswith(("http://", "https://")):
                print(f"Browser navigation verification failed: {actual_url}")
                return False
            print(f"Controlled browser session navigated to: {actual_url}")
            return True
        except WebDriverException as error:
            print(f"Controlled YouTube browser error: {type(error).__name__}: {error}")
            self.close()
            return False

    def is_youtube_page(self) -> bool:
        return self.driver is not None and "youtube.com" in self.driver.current_url

    def youtube_search(self, query: str) -> bool:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        if not self.open(url):
            return False
        try:
            WebDriverWait(self.driver, 10).until(
                lambda driver: len(driver.find_elements(By.CSS_SELECTOR, "a#video-title")) > 0
            )
            return True
        except WebDriverException as error:
            print(f"YouTube search verification failed: {type(error).__name__}: {error}")
            return False

    def open_first_youtube_result(self) -> bool:
        """Open the first visible YouTube search result and verify navigation."""
        if not self.is_youtube_page():
            print("Cannot choose a YouTube result: YouTube is not the active page.")
            return False
        try:
            before_url = self.driver.current_url
            results = WebDriverWait(self.driver, 10).until(
                lambda driver: [item for item in driver.find_elements(By.CSS_SELECTOR, "a#video-title") if item.is_displayed()]
            )
            results[0].click()
            WebDriverWait(self.driver, 10).until(
                lambda driver: driver.current_url != before_url and "youtube.com" in driver.current_url
            )
            print(f"YouTube result opened: {self.driver.current_url}")
            return True
        except WebDriverException as error:
            print(f"YouTube result navigation failed: {type(error).__name__}: {error}")
            return False

    def youtube_search_and_open_first(self, query: str) -> bool:
        return self.youtube_search(query) and self.open_first_youtube_result()

    def media_action(self, action: str, amount: int | None = None) -> bool:
        """Act on the current YouTube video and verify the player state changed."""
        if not self.is_youtube_page():
            print("Media action failed: the controlled browser is not on YouTube.")
            return False
        try:
            if action == "play":
                result = self.driver.execute_async_script(
                    """
                    const done = arguments[arguments.length - 1];
                    const video = document.querySelector('video');
                    if (!video) return done(false);
                    video.play().then(() => done(!video.paused)).catch(() => done(false));
                    """
                )
            elif action == "pause":
                result = self.driver.execute_script(
                    "const v=document.querySelector('video'); if (!v) return false; v.pause(); return v.paused;"
                )
            elif action == "next":
                before_url = self.driver.current_url
                result = self.driver.execute_script(
                    "const b=document.querySelector('button.ytp-next-button'); if (!b || b.disabled) return false; b.click(); return true;"
                )
                if result:
                    WebDriverWait(self.driver, 10).until(lambda driver: driver.current_url != before_url)
            elif action == "volume":
                result = self.driver.execute_script(
                    """
                    const v=document.querySelector('video');
                    if (!v) return false;
                    v.muted = false;
                    v.volume = Math.max(0, Math.min(1, arguments[0] / 100));
                    return Math.round(v.volume * 100) === arguments[0];
                    """,
                    amount,
                )
            elif action == "fullscreen":
                result = self.driver.execute_async_script(
                    """
                    const done = arguments[arguments.length - 1];
                    const target = document.querySelector('.html5-video-player') || document.querySelector('video');
                    if (!target || !target.requestFullscreen) return done(false);
                    target.requestFullscreen().then(() => done(document.fullscreenElement === target)).catch(() => done(false));
                    """
                )
            else:
                return False
            print(f"Media verification for {action}: {bool(result)}")
            return bool(result)
        except WebDriverException as error:
            print(f"YouTube media action failed: {type(error).__name__}: {error}")
            return False

    def go_back(self) -> bool:
        if self.driver is None:
            print("Go back failed: no controlled browser session is open.")
            return False
        try:
            before_url = self.driver.current_url
            self.driver.back()
            WebDriverWait(self.driver, 10).until(lambda driver: driver.current_url != before_url)
            print(f"Browser back navigation verified: {self.driver.current_url}")
            return True
        except WebDriverException as error:
            print(f"Browser back navigation failed: {type(error).__name__}: {error}")
            return False

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            finally:
                self.driver = None


class Ultron:
    def __init__(self) -> None:
        self.config = load_config()
        self.engine = self.create_speech_engine(int(self.config["tts_rate"]))
        self.last_recording_had_speech = False
        self._audio_device_reported = False
        self.youtube_browser = YouTubeBrowser()
        self.context: dict[str, str] = {"site": "", "last_search": ""}
        self.wake_detector = WakeWordDetector(
            str(self.config["wake_word"]),
            self.config["activation_phrases"],
            str(self.config["vosk_model_path"]),
        )

    @staticmethod
    def create_speech_engine(speech_rate: int):
        """Create Windows speech output without letting TTS break listening."""
        if pyttsx3 is None:
            print("Speech output disabled: pyttsx3 is not installed.")
            return None
        try:
            engine = pyttsx3.init(driverName="sapi5" if os.name == "nt" else None)
            engine.setProperty("rate", speech_rate)
            print(f"Speech output ready (SAPI5, rate={speech_rate}).")
            return engine
        except Exception as error:
            print(f"Speech output disabled: {type(error).__name__}: {error}")
            return None

    @staticmethod
    def report_state(state: str) -> None:
        """Visual/log state indicator; it is intentionally not spoken verbatim."""
        print(f"[ULTRON {state.upper()}]")

    def acknowledge(self, message: str) -> None:
        self.say(random.choice(SUCCESS_TEMPLATES).format(message=message))

    def report_failure(self, message: str) -> None:
        self.say(random.choice(FAILURE_TEMPLATES).format(message=message))

    def say(self, message: str) -> None:
        print(f"Ultron: {message}")
        if self.engine:
            try:
                self.engine.say(message)
                self.engine.runAndWait()
            except Exception as error:
                print(f"Speech output error: {type(error).__name__}: {error}")
                try:
                    self.engine.stop()
                except Exception:
                    pass
                self.engine = None

    @staticmethod
    def open_url(url: str) -> None:
        """Open a URL in Chrome when it is installed, else use the default browser."""
        chrome_paths = [
            os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
        ]
        for chrome_path in chrome_paths:
            if os.path.isfile(chrome_path):
                subprocess.Popen([chrome_path, url])
                return
        webbrowser.open(url)

    def youtube_search(self, query: str) -> bool:
        if self.youtube_browser.youtube_search(query):
            self.context["site"] = "youtube"
            self.context["last_search"] = query
            return True
        return False

    def google_search(self, query: str) -> bool:
        url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(query)
        if self.youtube_browser.open(url) and "google." in self.youtube_browser.driver.current_url:
            self.context["site"] = "google"
            self.context["last_search"] = query
            return True
        return False

    def top_youtube_video_id(self, query: str) -> tuple[str | None, str | None]:
        """Look up a video ID using the supported YouTube Data API.

        An explicitly supplied environment variable takes precedence; the
        persisted local configuration is the automatic fallback.
        """
        api_key = os.environ.get("YOUTUBE_API_KEY") or str(
            self.config.get("youtube_api_key", "")
        ).strip()
        if not api_key:
            return None, "A YouTube Data API key is not configured."

        parameters = urllib.parse.urlencode(
            {
                "part": "snippet",
                "q": query,
                "type": "video",
                "maxResults": "1",
                "key": api_key,
            }
        )
        request = urllib.request.Request(
            "https://www.googleapis.com/youtube/v3/search?" + parameters,
            headers={"User-Agent": "Ultron-MVP/1.0"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            # Google returns useful JSON here for quota, API-enable, key, and
            # restriction failures. Never print the request URL or API key.
            body = error.read().decode("utf-8", errors="replace")
            print(f"YouTube API HTTP {error.code} response:\n{body.replace(api_key, '[redacted]')}")
            return None, f"YouTube API returned HTTP {error.code}. See the terminal for details."
        except urllib.error.URLError as error:
            print(f"YouTube API network error: {error.reason}")
            return None, "YouTube API network request failed. See the terminal for details."
        except (json.JSONDecodeError, OSError, ValueError) as error:
            print(f"YouTube API response error: {error}")
            return None, "YouTube API returned an unreadable response. See the terminal for details."

        try:
            video_id = result["items"][0]["id"]["videoId"]
        except (KeyError, IndexError, TypeError) as error:
            print(
                "YouTube API response had no playable video ID "
                f"({error}): {json.dumps(result, ensure_ascii=False)}"
            )
            return None, "YouTube returned no playable video. See the terminal for details."

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return None, "YouTube returned an invalid video ID."
        return video_id, None

    @staticmethod
    def plan_instruction(raw_command: str) -> list[tuple[str, str | int | None]]:
        """Parse a small, explicit low-risk natural-language command plan."""
        text = raw_command.strip().lower()
        if not text:
            return []
        if match := re.fullmatch(r"open youtube(?:\s+and)?\s+play\s+(.+)", text):
            return [("open_youtube", None), ("play_search", match.group(1))]

        parts = re.split(
            r"\s+(?:and then|then|and)\s+(?=(?:open|search|play|pause|resume|next|volume|set volume|fullscreen|go back)\b)",
            text,
        )
        plan: list[tuple[str, str | int | None]] = []
        for part in parts:
            if part in {"open youtube", "youtube"}:
                plan.append(("open_youtube", None))
            elif part in {"open google", "google", "open chrome", "open google chrome"}:
                plan.append(("open_google", None))
            elif part in {"open spotify", "spotify"}:
                plan.append(("open_spotify", None))
            elif part.startswith("search youtube "):
                plan.append(("search_youtube", part.removeprefix("search youtube ").strip()))
            elif part.startswith("search google "):
                plan.append(("search_google", part.removeprefix("search google ").strip()))
            elif part.startswith("search for "):
                plan.append(("search_google", part.removeprefix("search for ").strip()))
            elif part.startswith("search "):
                plan.append(("search_google", part.removeprefix("search ").strip()))
            elif part.startswith("play "):
                plan.append(("play_search", part.removeprefix("play ").strip()))
            elif part in {"open first result", "select first result"}:
                plan.append(("open_first_result", None))
            elif part in {"play", "resume", "continue"}:
                plan.append(("media_play", None))
            elif part == "pause":
                plan.append(("media_pause", None))
            elif part == "next":
                plan.append(("media_next", None))
            elif part in {"volume up", "increase volume"}:
                plan.append(("volume_change", 10))
            elif part in {"volume down", "decrease volume"}:
                plan.append(("volume_change", -10))
            elif match := re.fullmatch(r"(?:set )?volume (\d{1,3})(?: percent)?", part):
                plan.append(("volume_set", min(100, int(match.group(1)))))
            elif part in {"fullscreen", "go fullscreen"}:
                plan.append(("fullscreen", None))
            elif part in {"go back", "back"}:
                plan.append(("go_back", None))
            else:
                return []
        return plan

    def run_step(self, action: str, value: str | int | None) -> tuple[bool, str]:
        """Execute one low-risk plan step and return a verified outcome."""
        if action == "open_youtube":
            success = self.youtube_browser.open("https://www.youtube.com")
            if success:
                self.context["site"] = "youtube"
            return success, "YouTube opened" if success else "YouTube did not open in the controlled browser"
        if action == "open_google":
            success = self.youtube_browser.open("https://www.google.com")
            if success:
                self.context["site"] = "google"
            return success, "Google opened" if success else "Google did not open in the controlled browser"
        if action == "open_spotify":
            success = self.youtube_browser.open("https://open.spotify.com")
            if success:
                self.context["site"] = "spotify"
            return success, "Spotify opened" if success else "Spotify did not open in the controlled browser"
        if action == "search_youtube":
            success = self.youtube_search(str(value))
            return success, f"YouTube search completed for {value}" if success else f"YouTube search failed for {value}"
        if action == "search_google":
            success = self.google_search(str(value))
            return success, f"Google search completed for {value}" if success else f"Google search failed for {value}"
        if action == "play_search":
            query = str(value)
            video_id, api_error = self.top_youtube_video_id(query)
            if video_id:
                success = self.youtube_browser.open(f"https://www.youtube.com/watch?v={video_id}&autoplay=1")
                if success:
                    self.context["site"] = "youtube"
                    success = self.youtube_browser.media_action("play")
                return success, f"Playing {query}" if success else f"I opened {query}, but playback could not be verified"
            success = self.youtube_browser.youtube_search_and_open_first(query)
            if success:
                self.context["site"] = "youtube"
                success = self.youtube_browser.media_action("play")
            detail = "using the first YouTube search result" if api_error else ""
            return success, f"Playing {query} {detail}" if success else f"Could not play {query}"
        if action == "open_first_result":
            success = self.youtube_browser.open_first_youtube_result()
            return success, "First YouTube result opened" if success else "Could not open the first YouTube result"
        if action == "media_play":
            success = self.youtube_browser.media_action("play")
            return success, "Playback resumed" if success else "Playback could not be started"
        if action == "media_pause":
            success = self.youtube_browser.media_action("pause")
            return success, "Playback paused" if success else "Playback could not be paused"
        if action == "media_next":
            success = self.youtube_browser.media_action("next")
            return success, "Next video opened" if success else "Could not open the next video"
        if action in {"volume_change", "volume_set"}:
            try:
                current = self.youtube_browser.driver.execute_script(
                    "const v=document.querySelector('video'); return v ? Math.round(v.volume * 100) : null;"
                ) if self.youtube_browser.driver else None
            except WebDriverException as error:
                print(f"Volume read failed: {type(error).__name__}: {error}")
                return False, "Volume could not be read from the current player"
            target = int(value) if action == "volume_set" else max(0, min(100, (current or 50) + int(value)))
            success = self.youtube_browser.media_action("volume", target)
            return success, f"Volume set to {target} percent" if success else "Volume could not be changed"
        if action == "fullscreen":
            success = self.youtube_browser.media_action("fullscreen")
            return success, "Fullscreen enabled" if success else "Fullscreen could not be enabled"
        if action == "go_back":
            success = self.youtube_browser.go_back()
            return success, "Went back" if success else "Could not go back"
        return False, "Unsupported action"

    def handle(self, raw_command: str) -> bool:
        command = raw_command.strip().lower()
        if command in {"exit", "quit", "goodbye"}:
            self.say("Goodbye.")
            return False
        if command in {"help", "what can you do"}:
            print(HELP)
            self.say("I printed the approved commands.")
            return True

        # Preserve short-term context: an unqualified search after a YouTube
        # action means search YouTube, not a separate Google search.
        contextual_command = raw_command
        if (
            self.context["site"] == "youtube"
            and command.startswith("search ")
            and not command.startswith(("search youtube ", "search google ", "search for "))
        ):
            contextual_command = "search youtube " + raw_command.strip()[len("search "):]

        plan = self.plan_instruction(contextual_command)
        if not plan:
            self.report_failure("that request is not approved or I could not understand it; try help for the low-risk commands")
            return True
        for action, value in plan:
            success, message = self.run_step(action, value)
            if not success:
                self.report_failure(f"{message}; I stopped the remaining steps")
                return True
            self.acknowledge(message)
        return True

    def listen_once(
        self, recording_seconds: int = 4, quiet: bool = False, wake_only: bool = False
    ) -> str | None:
        """Record one command with sounddevice, then transcribe it with SpeechRecognition.

        SpeechRecognition's ``Microphone`` class requires PyAudio, so this
        intentionally records PCM audio with sounddevice instead. soundfile
        writes an in-memory WAV that SpeechRecognition reads as ``AudioData``.
        """
        if sr is None or sd is None or sf is None:
            self.say(
                "Voice mode needs SpeechRecognition, sounddevice, and soundfile. "
                "Using typed commands instead."
            )
            return None

        recognizer = sr.Recognizer()
        sample_rate = int(self.config["speech_sample_rate"])
        configured_device = self.config.get("microphone_device")
        input_device = configured_device if configured_device not in (None, "") else None
        block_seconds = 0.1
        block_frames = int(sample_rate * block_seconds)
        speech_threshold = int(self.config.get("speech_silence_rms", VOICE_SILENCE_RMS))
        pre_roll_blocks = max(
            1, int(float(self.config.get("speech_preroll_seconds", 0.3)) / block_seconds)
        )
        end_silence_blocks = max(
            1,
            int(float(self.config.get("speech_end_silence_seconds", 0.8)) / block_seconds),
        )
        try:
            if not self._audio_device_reported:
                device_info = sd.query_devices(input_device, kind="input")
                default_device = sd.default.device
                print(
                    "Audio input device: "
                    f"name={device_info['name']!r}, "
                    f"host_api={device_info['hostapi']}, "
                    f"default_input_index={default_device[0]}, "
                    f"configured_device={input_device!r}, "
                    f"default_sample_rate={device_info['default_samplerate']}"
                )
                self._audio_device_reported = True
            print(
                f"Listening for speech (up to {recording_seconds} seconds after it starts)…"
            )

            # A fixed recording window can split a phrase across two clips: a
            # common cause of valid PCM audio producing UnknownValueError.
            # Capture starts at speech level, retains a short pre-roll, then
            # stops only after a brief silence. No audio is saved to disk.
            waiting_blocks = max(1, int(recording_seconds / block_seconds))
            max_utterance_blocks = max(waiting_blocks, int(recording_seconds / block_seconds))
            pre_roll = []
            utterance = []
            speech_started = False
            silent_blocks = 0
            overflow_count = 0
            with sd.InputStream(
                device=input_device,
                samplerate=sample_rate,
                channels=1,
                dtype="int16",
                blocksize=block_frames,
            ) as stream:
                for _ in range(waiting_blocks):
                    block, overflowed = stream.read(block_frames)
                    if overflowed:
                        overflow_count += 1
                    block_rms = int(
                        (block.astype("float64") ** 2).mean() ** 0.5
                    )
                    pre_roll.append(block.copy())
                    pre_roll = pre_roll[-pre_roll_blocks:]
                    if block_rms >= speech_threshold:
                        speech_started = True
                        utterance = pre_roll.copy()
                        break

                if not speech_started:
                    self.last_recording_had_speech = False
                    print(
                        "Voice activity: no speech-level audio detected "
                        f"(RMS is below {speech_threshold})."
                    )
                    return None

                for _ in range(max_utterance_blocks):
                    block, overflowed = stream.read(block_frames)
                    if overflowed:
                        overflow_count += 1
                    utterance.append(block.copy())
                    block_rms = int(
                        (block.astype("float64") ** 2).mean() ** 0.5
                    )
                    silent_blocks = silent_blocks + 1 if block_rms < speech_threshold else 0
                    if silent_blocks >= end_silence_blocks:
                        break

            recording = np.vstack(utterance)

            # Report capture properties, but never print or save the spoken
            # audio itself. SpeechRecognition expects PCM samples plus their
            # sample rate and byte width; 16 kHz / signed 16-bit / mono is a
            # standard format for its Google recognizer.
            samples = recording.astype("int32")
            peak = int(abs(samples).max())
            rms = int((samples.astype("float64") ** 2).mean() ** 0.5)
            print(
                "Audio capture: "
                f"frames={len(recording)}, channels={recording.shape[1]}, "
                f"sample_rate={sample_rate} Hz, dtype={recording.dtype}, "
                f"peak={peak}, rms={rms}"
            )
            self.last_recording_had_speech = rms >= speech_threshold
            if not self.last_recording_had_speech:
                print(
                    "Voice activity: no speech-level audio detected "
                    f"(RMS is below {speech_threshold})."
                )
                return None
            if overflow_count:
                print(f"Audio capture warning: {overflow_count} input buffer overflow(s).")

            if wake_only and self.wake_detector.uses_offline_model:
                detected = self.wake_detector.detect(recording, sample_rate)
                print(f"Offline wake-word detection: {detected}")
                return self.wake_detector.wake_word if detected else None

            # AudioFile accepts a seekable WAV stream. This keeps the capture
            # in memory and avoids creating a temporary recording on disk.
            wav_stream = io.BytesIO()
            sf.write(wav_stream, recording, sample_rate, format="WAV", subtype="PCM_16")
            wav_stream.seek(0)
            wav_info = sf.info(wav_stream)
            print(
                "WAV passed to SpeechRecognition: "
                f"format={wav_info.format}, subtype={wav_info.subtype}, "
                f"sample_rate={wav_info.samplerate} Hz, channels={wav_info.channels}, "
                f"frames={wav_info.frames}"
            )
            wav_stream.seek(0)
            with sr.AudioFile(wav_stream) as source:
                audio = recognizer.record(source)
            print(
                "SpeechRecognition AudioData: "
                f"sample_rate={audio.sample_rate} Hz, sample_width={audio.sample_width} bytes, "
                f"raw_bytes={len(audio.frame_data)}"
            )
            language = str(self.config.get("speech_recognition_language", "en-US"))
            transcript = recognizer.recognize_google(audio, language=language)
            print(
                f"SpeechRecognition transcript ({language}): {transcript!r}"
            )
            if wake_only:
                detected = (
                    normalize_voice_phrase(transcript)
                    in self.wake_detector.normalized_activation_phrases
                )
                print(f"Fallback wake-word detection: {detected}")
                return self.wake_detector.wake_word if detected else None
            return transcript
        except sd.PortAudioError as error:
            print(f"Microphone error: {error}")
            self.say("I could not access the microphone. Check Windows microphone permissions and try again.")
        except (OSError, RuntimeError) as error:
            print(f"Audio recording error: {error}")
            self.say("Audio recording failed. Check that a microphone is connected and try again.")
        except sr.UnknownValueError as error:
            print(f"SpeechRecognition {type(error).__name__}: {error!r}")
            if not quiet:
                self.say("I could not understand that.")
        except sr.RequestError as error:
            print(f"SpeechRecognition {type(error).__name__}: {error!r}")
            if not quiet:
                self.say("Speech recognition is unavailable. Check your internet connection.")
        except Exception as error:
            print(f"Unexpected voice-mode {type(error).__name__}: {error!r}")
            if not quiet:
                self.say("Voice recognition failed. See the terminal for the exact error.")
        return None

    @staticmethod
    def wake_word_command(transcript: str) -> str | None:
        """Return the command after the wake word, or None when not woken."""
        match = WAKE_WORD.match(transcript)
        if not match:
            return None
        return match.group(1).strip()

    @staticmethod
    def is_approved_command(raw_command: str) -> bool:
        """Only let recognized allowlisted phrases advance a voice session."""
        command = raw_command.strip().lower()
        return command in {"exit", "quit", "goodbye", "help", "what can you do"} or bool(Ultron.plan_instruction(raw_command))

    def run_always_ready(self) -> None:
        """Run an idle/active assistant where only active audio can act.

        Idle mode uses a narrow wake phrase and does not pass any transcript to
        command handling. Active mode retains existing command context until a
        spoken deactivation or optional inactivity timeout returns it to idle.
        """
        idle_seconds = int(self.config["idle_recording_seconds"])
        active_seconds = int(self.config["active_recording_seconds"])
        timeout = self.config["active_timeout_seconds"]
        active = False
        last_command_at = 0.0
        self.report_state("idle — waiting for Activate ULTRON")

        try:
            while True:
                if not active:
                    detected = self.listen_once(
                        recording_seconds=idle_seconds, quiet=True, wake_only=True
                    )
                    if detected:
                        active = True
                        last_command_at = time.monotonic()
                        self.report_state("active")
                        self.say("ULTRON activated.")
                    continue

                if isinstance(timeout, (int, float)) and timeout > 0:
                    if time.monotonic() - last_command_at >= timeout:
                        active = False
                        self.report_state("idle — active timeout")
                        continue

                self.report_state("active — listening")
                transcript = self.listen_once(recording_seconds=active_seconds, quiet=True)
                if not transcript:
                    continue
                command = transcript.strip()
                print(f"You said: {command}")
                lowered = normalize_voice_phrase(command)
                print(f"Normalized active phrase: {lowered!r}")

                if is_deactivation_phrase(command, self.config["deactivation_phrases"]):
                    active = False
                    self.report_state("idle — deactivated")
                    self.say("ULTRON deactivated.")
                    continue

                if not self.is_approved_command(command):
                    print("Ignored: not an approved active-mode command.")
                    continue

                self.report_state("processing")
                keep_running = self.handle(command)
                if not keep_running:
                    # Spoken exit is deliberately not a process-exit command
                    # in background mode; it returns to protected idle mode.
                    active = False
                    self.report_state("idle — exit requested")
                    self.say("ULTRON deactivated.")
                    continue
                last_command_at = time.monotonic()
                self.report_state("active")
        except KeyboardInterrupt:
            print("\n[ULTRON STOPPED]")

    def run(self) -> None:
        self.say("Ultron is ready. Type a command, or type voice for always-ready mode.")
        while True:
            try:
                command = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if command.lower() == "voice":
                self.run_always_ready()
                continue
            if not self.handle(command):
                break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ultron voice assistant")
    parser.add_argument(
        "--background",
        action="store_true",
        help="run always-ready voice mode with logs instead of a console",
    )
    arguments = parser.parse_args()
    if arguments.background:
        configure_background_logging()
        logging.info("Ultron background process starting (version=%s).", APP_VERSION)
    ultron = Ultron()
    try:
        if arguments.background:
            ultron.run_always_ready()
        else:
            ultron.run()
    except Exception:
        logging.exception("Ultron terminated unexpectedly.")
        raise
    finally:
        ultron.youtube_browser.close()
        logging.info("Ultron process stopped.")
