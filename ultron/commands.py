"""
ultron/commands.py — Fuzzy NLP command parser and executor.

Improvements over original:
- rapidfuzz fuzzy matching (80% threshold) for natural language variation
- Synonym/alias expansion table
- New commands: weather, timer, status, history, queue, queue add/next/list/clear
- Multi-step chaining with single retry on failure
- Context-aware search routing
- Disabled-next detection with clear message
"""

from __future__ import annotations

import datetime
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from ultron.desktop import DesktopExecutor

try:
    from rapidfuzz import fuzz, process as rf_process
    _FUZZY = True
except ImportError:
    _FUZZY = False

if TYPE_CHECKING:
    from ultron.browser import YouTubeBrowser
    from ultron.context import Context
    from ultron.tts import TTSWorker

# ── Synonym expansion ─────────────────────────────────────────────────────────

SYNONYMS: dict[str, str] = {
    # Search
    "find":            "search",
    "look for":        "search",
    "look up":         "search",
    "google":          "search google",
    "youtube":         "search youtube",
    # Play
    "put on":          "play",
    "start playing":   "play",
    "listen to":       "play",
    # Pause
    "stop music":      "pause",
    "stop video":      "pause",
    "hold on":         "pause",
    # Resume
    "continue":        "resume",
    "keep playing":    "resume",
    "unpause":         "resume",
    # Volume
    "louder":          "volume up",
    "quieter":         "volume down",
    "mute":            "set volume 0",
    "unmute":          "volume up",
    # Navigation
    "back":            "go back",
    "next song":       "next",
    "next video":      "next",
    "skip":            "next",
    # Sites
    "open chrome":     "open google",
    "chrome":          "open google",
    # Fullscreen
    "full screen":     "fullscreen",
    "maximize":        "fullscreen",
    # Exit
    "goodbye":         "exit",
    "bye":             "exit",
    "quit":            "exit",
    # Desktop / Apps
    "launch":          "open",
    "run":             "open",
    "start":           "open",
    "kill":            "close app",
    "shut down":       "shutdown",
    "turn off":        "shutdown",
    "reboot":          "restart",
    "grab screenshot": "take screenshot",
    "capture screen":  "take screenshot",
    "system volume up":   "system volume up",
    "system volume down": "system volume down",
}

APPROVED_FIXED = {
    "open youtube", "youtube", "open google", "google",
    "open chrome", "open google chrome", "open spotify", "spotify",
    "pause", "resume", "continue", "next", "go back", "back",
    "fullscreen", "go fullscreen", "full screen",
    "open first result", "select first result",
    "help", "what can you do", "status", "history",
    "queue list", "queue clear", "queue next",
    "exit", "quit", "goodbye", "bye",
    "spotify liked songs", "spotify new releases",
    # Desktop
    "take screenshot", "screenshot",
    "lock screen", "lock my screen",
    "shutdown", "restart", "cancel shutdown",
    "system volume up", "system volume down",
}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", text.lower())).strip()


def _expand_synonyms(text: str) -> str:
    """Replace synonyms in the normalized text."""
    for alias, canonical in SYNONYMS.items():
        pattern = r"\b" + re.escape(alias) + r"\b"
        text = re.sub(pattern, canonical, text, flags=re.IGNORECASE)
    return text


def _preprocess(raw: str) -> str:
    """Normalize + synonym-expand a raw command."""
    return _expand_synonyms(_normalize(raw))


# ── Plan builder ──────────────────────────────────────────────────────────────

CHAIN_RE = re.compile(
    r"\s+(?:and then|then|and)\s+"
    r"(?=(?:open|search|play|pause|resume|next|volume|set volume|fullscreen|go back|"
    r"queue|weather|status|history|go fullscreen|full screen)\b)",
    re.IGNORECASE,
)


def plan_instruction(raw: str, context_site: str = "") -> list[tuple[str, Any]]:
    """
    Parse a command into an ordered list of (action, value) steps.
    Returns [] if the command is not recognized.
    """
    text = _preprocess(raw)
    if not text:
        return []

    # ── Special macros ────────────────────────────────────────────────────
    if m := re.fullmatch(r"open youtube(?:\s+and)?\s+play\s+(.+)", text):
        return [("open_youtube", None), ("play_search", m.group(1).strip())]

    if m := re.fullmatch(r"open spotify(?:\s+and)?\s+search\s+(.+)", text):
        return [("open_spotify", None), ("spotify_search", m.group(1).strip())]

    # ── Split multi-step chain ─────────────────────────────────────────────
    parts = CHAIN_RE.split(text)
    plan: list[tuple[str, Any]] = []

    for part in parts:
        step = _parse_single(part.strip(), context_site)
        if step is None:
            step = _fuzzy_match(part.strip(), context_site)
        if step is None:
            # If any part of the chain is unknown, pass the entire original text to AI
            return [("ask_ai", text)]
        plan.extend(step if isinstance(step, list) else [step])

    return plan


def _parse_single(text: str, context_site: str) -> list[tuple[str, Any]] | None:
    """Parse one step. Returns list of (action, value) or None."""

    # ── Site openers ──────────────────────────────────────────────────────
    if text in {"open youtube", "youtube"}:
        return [("open_youtube", None)]
    if text in {"open google", "google", "open chrome", "open google chrome"}:
        return [("open_google", None)]
    if text in {"open spotify", "spotify"}:
        return [("open_spotify", None)]

    # ── Search ────────────────────────────────────────────────────────────
    if m := re.match(r"search youtube\s+(.+)", text):
        return [("search_youtube", m.group(1).strip())]
    if m := re.match(r"search google\s+(.+)", text):
        return [("search_google", m.group(1).strip())]
    if m := re.match(r"search for\s+(.+)", text):
        return [("search_google", m.group(1).strip())]
    if m := re.match(r"search\s+(.+)", text):
        q = m.group(1).strip()
        action = "search_youtube" if context_site == "youtube" else "search_google"
        return [(action, q)]

    # ── Play ──────────────────────────────────────────────────────────────
    if m := re.match(r"play\s+(.+)", text):
        return [("play_search", m.group(1).strip())]

    # ── Queue ─────────────────────────────────────────────────────────────
    if m := re.match(r"queue add\s+(.+)", text):
        return [("queue_add", m.group(1).strip())]
    if text in {"queue next", "play next in queue"}:
        return [("queue_next", None)]
    if text in {"queue list", "show queue", "list queue"}:
        return [("queue_list", None)]
    if text in {"queue clear", "clear queue", "empty queue"}:
        return [("queue_clear", None)]

    # ── Spotify extras ────────────────────────────────────────────────────
    if m := re.match(r"search spotify\s+(.+)", text):
        return [("spotify_search", m.group(1).strip())]
    if text in {"spotify liked songs", "open liked songs"}:
        return [("spotify_liked", None)]
    if text in {"spotify new releases", "new releases"}:
        return [("spotify_new_releases", None)]

    # ── Media controls ────────────────────────────────────────────────────
    if text in {"play", "resume", "continue", "start"}:
        return [("media_play", None)]
    if text in {"pause", "stop"}:
        return [("media_pause", None)]
    if text == "next":
        return [("media_next", None)]
    if text in {"skip ad", "skip ads"}:
        return [("media_skip_ad", None)]
    if text in {"volume up", "increase volume"}:
        return [("volume_change", +10)]
    if text in {"volume down", "decrease volume"}:
        return [("volume_change", -10)]
    if m := re.fullmatch(r"(?:set )?volume\s+(\d{1,3})(?:\s*percent)?", text):
        return [("volume_set", min(100, int(m.group(1))))]
    if text in {"fullscreen", "go fullscreen", "full screen"}:
        return [("fullscreen", None)]
    if text in {"go back", "back"}:
        return [("go_back", None)]
    if text in {"open first result", "select first result"}:
        return [("open_first_result", None)]

    # ── New features ──────────────────────────────────────────────────────
    if m := re.match(r"weather(?:\s+in)?\s+(.+)", text):
        return [("weather", m.group(1).strip())]
    if text in {"weather", "weather today", "what is the weather"}:
        return [("weather", "")]

    if m := re.fullmatch(r"set timer\s+(\d+)\s*(second|minute|hour)s?", text):
        n, unit = int(m.group(1)), m.group(2)
        secs = n * {"second": 1, "minute": 60, "hour": 3600}[unit]
        return [("timer", secs)]

    if text in {"status", "what are you doing", "what is your status"}:
        return [("status", None)]

    if m := re.match(r"history(?:\s+(\d+))?", text):
        n = int(m.group(1)) if m.group(1) else 10
        return [("history", n)]

    if text in {"help", "what can you do"}:
        return [("help", None)]

    if text in {"exit", "quit", "goodbye", "bye", "close"}:
        return [("exit", None)]

    # ── Desktop / OS controls ─────────────────────────────────────────────
    if m := re.match(r"(?:open|launch|run|start)\s+app\s+(.+)", text):
        return [("open_app", m.group(1).strip())]
    if m := re.match(r"(?:open|launch|run|start)\s+(notepad|calculator|task manager|paint|wordpad|control panel|file explorer|explorer|cmd|command prompt|powershell|word|excel|powerpoint|outlook|vs code|vscode|visual studio code|chrome|firefox|edge|vlc|discord|spotify|telegram|whatsapp|steam|obs|snipping tool)", text):
        return [("open_app", m.group(1).strip())]

    if m := re.match(r"(?:close|kill)\s+app\s+(.+)", text):
        return [("close_app", m.group(1).strip())]
    if m := re.match(r"(?:close|kill)\s+(notepad|calculator|task manager|paint|wordpad|chrome|firefox|edge|vlc|discord|spotify|telegram|whatsapp|steam|obs|cmd|powershell|word|excel|powerpoint|outlook|vs code|vscode)", text):
        return [("close_app", m.group(1).strip())]

    if m := re.match(r"open\s+(desktop|documents|downloads|pictures|music|videos|home|temp)(?:\s+folder)?", text):
        return [("open_folder", m.group(1).strip())]
    if m := re.match(r"open\s+(?:my\s+)?(desktop|documents|downloads|pictures|music|videos)", text):
        return [("open_folder", m.group(1).strip())]

    if m := re.match(r"(?:find|search for)\s+file\s+(.+)", text):
        return [("find_file", m.group(1).strip())]
    if m := re.match(r"find\s+(.+)", text):
        return [("find_file", m.group(1).strip())]

    if m := re.match(r"open\s+file\s+(.+)", text):
        return [("open_file", m.group(1).strip())]

    if text in {"take screenshot", "screenshot", "capture screen", "grab screenshot"}:
        return [("take_screenshot", None)]

    if text in {"lock screen", "lock my screen", "lock"}:
        return [("lock_screen", None)]

    if text in {"shutdown", "shut down", "turn off", "power off"}:
        return [("os_shutdown", None)]

    if text in {"restart", "reboot"}:
        return [("os_restart", None)]

    if text in {"cancel shutdown", "abort shutdown"}:
        return [("os_cancel_shutdown", None)]

    if text in {"system volume up"}:
        return [("sys_volume_change", +20)]
    if text in {"system volume down"}:
        return [("sys_volume_change", -20)]
    if m := re.fullmatch(r"set system volume\s+(\d{1,3})(?:\s*percent)?", text):
        return [("sys_volume_set", int(m.group(1)))]

    if m := re.match(r"(?:ask|tell)\s+(?:me\s+)?(.+)", text):
        return [("ask_ai", m.group(1).strip())]

    return None


def _fuzzy_match(text: str, context_site: str) -> list[tuple[str, Any]] | None:
    """Try rapidfuzz to salvage close-but-not-exact commands."""
    if not _FUZZY:
        return None
    candidates = list(APPROVED_FIXED)
    result = rf_process.extractOne(text, candidates, scorer=fuzz.token_sort_ratio)
    if result and result[1] >= 80:
        matched = result[0]
        print(f"Fuzzy match: '{text}' → '{matched}' ({result[1]:.0f}%)")
        return _parse_single(matched, context_site)
    return None


# ── Step executor ─────────────────────────────────────────────────────────────

HELP_TEXT = """
╔═══════════════════════════════════════════════════════════╗
║                    ULTRON COMMANDS                         ║
╠═══════════════════════════════════════════════════════════╣
║ NAVIGATION                                                 ║
║  open youtube / open google / open spotify                 ║
║  go back                                                   ║
║                                                            ║
║ SEARCH                                                     ║
║  search youtube <topic>                                    ║
║  search google <topic>                                     ║
║  search <topic>          ← context-aware (YT or Google)   ║
║  open first result                                         ║
║                                                            ║
║ MUSIC & VIDEO                                              ║
║  play <song or artist>                                     ║
║  pause / resume / next                                     ║
║  volume up / volume down / set volume <0-100>              ║
║  fullscreen                                                ║
║                                                            ║
║ QUEUE                                                      ║
║  queue add <song>                                          ║
║  queue next / queue list / queue clear                     ║
║                                                            ║
║ SPOTIFY                                                    ║
║  search spotify <artist>                                   ║
║  spotify liked songs / spotify new releases                ║
║                                                            ║
║ BUILT-INS                                                  ║
║  weather <city>                                            ║
║  set timer <N> minutes/seconds/hours                       ║
║  status                                                    ║
║  history [N]                                               ║
║  help / exit                                               ║
║ DESKTOP ACCESS                                             ║
║  open notepad / calculator / vs code / discord / ...       ║
║  close notepad / chrome / ...                              ║
║  open downloads / documents / desktop / pictures ...       ║
║  find file <name>                                          ║
║  open file <name>                                          ║
║  take screenshot                                           ║
║  system volume up / down / set system volume <0-100>       ║
║  lock screen                                               ║
║  shutdown / restart / cancel shutdown                      ║
╚═══════════════════════════════════════════════════════════╝
Voice: Say "Activate ULTRON" to start. Hold Ctrl+Space to PTT.
""".strip()


class CommandExecutor:
    """Execute plan steps against the browser and system."""

    def __init__(
        self,
        browser: "YouTubeBrowser",
        context: "Context",
        tts: "TTSWorker",
        config: dict[str, Any],
        api_key_getter,
        start_time: float,
    ) -> None:
        from ultron import config as cfg_mod
        from ultron.desktop import DesktopExecutor
        from ultron.ai import GeminiAgent
        self._browser = browser
        self._ctx = context
        self._tts = tts
        self._cfg = config
        self._get_api_key = api_key_getter
        self._start_time = start_time
        self._timers: list[threading.Timer] = []
        self._desktop = DesktopExecutor()
        self._ai = GeminiAgent(
            api_key=cfg_mod.get_gemini_api_key(self._cfg),
            model_name=config.get("gemini_model", "gemini-3.6-flash")
        )

    def _api_key(self) -> str:
        return self._get_api_key()

    def run_step(self, action: str, value: Any) -> tuple[bool, str]:
        """Execute one step. Returns (success, message)."""
        def try_once() -> tuple[bool, str]:
            return self._dispatch(action, value)

        ok, msg = try_once()
        if not ok:
            # Single retry after 1 second
            time.sleep(1.0)
            ok, msg = try_once()
        return ok, msg

    def _dispatch(self, action: str, value: Any) -> tuple[bool, str]:
        br = self._browser
        ctx = self._ctx

        if action == "open_youtube":
            ok = br.open("https://www.youtube.com")
            if ok:
                ctx.update_site("youtube")
            return ok, "YouTube opened" if ok else "YouTube did not open"

        if action == "open_google":
            ok = br.open("https://www.google.com")
            if ok:
                ctx.update_site("google")
            return ok, "Google opened" if ok else "Google did not open"

        if action == "open_spotify":
            ok = br.open("https://open.spotify.com")
            if ok:
                ctx.update_site("spotify")
            return ok, "Spotify opened" if ok else "Spotify did not open"

        if action == "search_youtube":
            q = str(value)
            ok = br.youtube_search(q)
            if ok:
                ctx.update_site("youtube", q)
            return ok, f"YouTube search done: {q}" if ok else f"YouTube search failed: {q}"

        if action == "search_google":
            q = str(value)
            url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(q)
            ok = br.open(url)
            if ok and br.driver and "google." in br.driver.current_url:
                ctx.update_site("google", q)
            return ok, f"Google search: {q}" if ok else f"Google search failed: {q}"

        if action == "play_search":
            q = str(value)
            video_id, err = br.top_video_id(q, self._api_key())
            if video_id:
                ok = br.play_video_id(video_id)
                if ok:
                    ctx.update_site("youtube")
                    ctx.last_video = q
                return ok, f"Playing {q}" if ok else f"Opened {q} but playback unconfirmed"
            # Fallback: search + first result
            ok = br.youtube_search(q) and br.open_first_result()
            if ok:
                ctx.update_site("youtube")
                ctx.last_video = q
                br.media_action("play")
            info = " (via search fallback)" if err else ""
            return ok, f"Playing {q}{info}" if ok else f"Could not play {q}"

        if action == "open_first_result":
            ok = br.open_first_result()
            return ok, "Opened first result" if ok else "Could not open first result"

        if action == "media_play":
            ok = br.media_action("play")
            return ok, "Playback resumed" if ok else "Could not resume playback"

        if action == "media_pause":
            ok = br.media_action("pause")
            return ok, "Paused" if ok else "Could not pause"

        if action == "media_next":
            ok = br.media_action("next")
            return ok, "Next video" if ok else "No next video (not in a playlist)"

        if action == "media_skip_ad":
            ok = br.media_action("skip_ad")
            return ok, "Skipped ad" if ok else "Could not skip ad (none found)"

        if action in {"volume_change", "volume_set"}:
            current = br.get_volume()
            target = (
                int(value)
                if action == "volume_set"
                else max(0, min(100, (current or ctx.volume) + int(value)))
            )
            ok = br.media_action("volume", target)
            if ok:
                ctx.volume = target
            return ok, f"Volume: {target}%" if ok else "Volume change failed"

        if action == "fullscreen":
            ok = br.media_action("fullscreen")
            return ok, "Fullscreen" if ok else "Fullscreen failed"

        if action == "go_back":
            ok = br.go_back()
            return ok, "Went back" if ok else "Could not go back"

        # ── Queue ─────────────────────────────────────────────────────────
        if action == "queue_add":
            br.queue_add(str(value))
            return True, f"Added '{value}' to queue"

        if action == "queue_next":
            nxt = br.queue_next()
            if nxt is None:
                return False, "Queue is empty"
            ok2, msg2 = self._dispatch("play_search", nxt)
            return ok2, f"Playing next in queue: {nxt}" if ok2 else msg2

        if action == "queue_list":
            items = br.queue_list()
            if not items:
                print("Queue is empty.")
                self._tts.say("The queue is empty.")
                return True, "Queue is empty"
            lines = "\n".join(f"  {i+1}. {q}" for i, q in enumerate(items))
            print(f"Queue ({len(items)} items):\n{lines}")
            self._tts.say(f"Queue has {len(items)} song{'s' if len(items) != 1 else ''}.")
            return True, "Queue listed"

        if action == "queue_clear":
            br.queue_clear()
            return True, "Queue cleared"

        # ── Spotify extras ────────────────────────────────────────────────
        if action == "spotify_search":
            ok = br.spotify_search(str(value))
            if ok:
                ctx.update_site("spotify")
            return ok, f"Spotify search: {value}" if ok else f"Spotify search failed"

        if action == "spotify_liked":
            ok = br.spotify_liked_songs()
            if ok:
                ctx.update_site("spotify")
            return ok, "Opened Spotify liked songs" if ok else "Could not open liked songs"

        if action == "spotify_new_releases":
            ok = br.spotify_new_releases()
            if ok:
                ctx.update_site("spotify")
            return ok, "Opened Spotify new releases" if ok else "Could not open new releases"

        # ── Weather ───────────────────────────────────────────────────────
        if action == "weather":
            return self._weather(str(value) if value else "")

        # ── Timer ─────────────────────────────────────────────────────────
        if action == "timer":
            secs = int(value)
            self._set_timer(secs)
            mins, s = divmod(secs, 60)
            h, m = divmod(mins, 60)
            parts = []
            if h:
                parts.append(f"{h} hour{'s' if h != 1 else ''}")
            if m:
                parts.append(f"{m} minute{'s' if m != 1 else ''}")
            if s:
                parts.append(f"{s} second{'s' if s != 1 else ''}")
            label = " and ".join(parts)
            return True, f"Timer set for {label}"

        # ── Status ────────────────────────────────────────────────────────
        if action == "status":
            uptime = int(time.time() - self._start_time)
            h, rem = divmod(uptime, 3600)
            m, s = divmod(rem, 60)
            up_str = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
            browser_ok = "connected" if br.driver else "not connected"
            site = ctx.site or "none"
            vol = ctx.volume
            msg = (
                f"Uptime: {up_str}. "
                f"Browser: {browser_ok}. "
                f"Current site: {site}. "
                f"Volume: {vol} percent. "
            )
            print(f"STATUS: {msg}")
            self._tts.say(msg)
            return True, msg

        # ── History ───────────────────────────────────────────────────────
        if action == "history":
            n = int(value) if value else 10
            text = self._ctx.format_history(n)
            print(text)
            self._tts.say(f"Showing last {n} commands. Check the console.")
            return True, "History displayed"

        # ── Help ──────────────────────────────────────────────────────────
        if action == "help":
            print(HELP_TEXT)
            self._tts.say("I printed the command list. Check the console.")
            return True, "Help displayed"

        if action == "exit":
            self._tts.say("Goodbye.")
            return True, "__exit__"

        # ── Desktop / OS ──────────────────────────────────────────────────
        if action == "open_app":
            ok, msg = self._desktop.open_app(str(value))
            self._tts.say(msg)
            return ok, msg

        if action == "close_app":
            ok, msg = self._desktop.close_app(str(value))
            self._tts.say(msg)
            return ok, msg

        if action == "open_folder":
            ok, msg = self._desktop.open_folder(str(value))
            self._tts.say(msg)
            return ok, msg

        if action == "open_file":
            ok, msg = self._desktop.open_file(str(value))
            self._tts.say(msg)
            return ok, msg

        if action == "find_file":
            ok, msg = self._desktop.find_file(str(value))
            self._tts.say(msg)
            return ok, msg

        if action == "take_screenshot":
            ok, msg = self._desktop.take_screenshot()
            self._tts.say(msg)
            return ok, msg

        if action == "lock_screen":
            ok, msg = self._desktop.lock_screen()
            self._tts.say(msg)
            return ok, msg

        if action == "os_shutdown":
            ok, msg = self._desktop.shutdown()
            self._tts.say(msg)
            return ok, msg

        if action == "os_restart":
            ok, msg = self._desktop.restart()
            self._tts.say(msg)
            return ok, msg

        if action == "os_cancel_shutdown":
            ok, msg = self._desktop.cancel_shutdown()
            self._tts.say(msg)
            return ok, msg

        if action == "sys_volume_change":
            ok, msg = self._desktop.set_system_volume(delta=int(value))
            self._tts.say(msg)
            return ok, msg

        if action == "sys_volume_set":
            ok, msg = self._desktop.set_system_volume(absolute=int(value))
            self._tts.say(msg)
            return ok, msg

        if action == "ask_ai":
            if not self._ai.ready:
                msg = "AI module is not connected. Check your Gemini API key."
                self._tts.say(msg)
                return False, msg
            
            # Use Gemini to parse intent instead of just chatting
            plan = self._ai.parse_intent(str(value))
            
            all_ok = True
            final_msg = "Executed AI plan."
            
            # If Gemini just wants to talk:
            if len(plan) == 1 and plan[0][0] == "ask_ai":
                # Fallback to chat if intent parser failed or returned ask_ai
                ctx_str = f"Current site: {self._ctx.site or 'None'}. Volume: {self._ctx.volume}. OS: Windows."
                answer = self._ai.ask(str(value), context=ctx_str)
                self._tts.say(answer)
                return True, f"AI response: {answer}"

            for sub_action, sub_value in plan:
                if sub_action == "ai_speak":
                    self._tts.say(str(sub_value))
                else:
                    ok, msg = self._dispatch(sub_action, sub_value)
                    if not ok:
                        all_ok = False
                        final_msg = msg
                        break

            return all_ok, final_msg

        return False, f"Unknown action: {action}"

    # ── Weather ───────────────────────────────────────────────────────────

    def _weather(self, city: str) -> tuple[bool, str]:
        loc = city.replace(" ", "+") or "auto"
        url = f"https://wttr.in/{urllib.parse.quote(loc)}?format=3"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Ultron/2.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = resp.read().decode("utf-8", errors="replace").strip()
            # Strip ANSI escape codes
            data = re.sub(r"\x1b\[[0-9;]*m", "", data)
            print(f"Weather: {data}")
            self._tts.say(data)
            return True, data
        except Exception as exc:
            msg = f"Weather lookup failed: {exc}"
            print(msg)
            return False, msg

    # ── Timer ─────────────────────────────────────────────────────────────

    def _set_timer(self, seconds: int) -> None:
        def _fire():
            self._tts.say("Timer done! Your time is up.")
            print("[TIMER] Alert fired.")

        t = threading.Timer(seconds, _fire)
        t.daemon = True
        t.start()
        self._timers.append(t)

    def cancel_timers(self) -> None:
        for t in self._timers:
            t.cancel()
        self._timers.clear()
