"""
ultron/browser.py — Resilient Selenium Chrome session.

Improvements over original:
- Multiple CSS selector fallbacks for every YouTube element
- Auto-retry on transient WebDriver errors (1 retry, 1s delay)
- Video ID LRU cache (saves YouTube API quota)
- Graceful degradation: falls back to webbrowser.open() when Selenium unavailable
- Next-button disabled detection with helpful message
- Explicit autoplay JS + play-button click fallback
- Queue support for sequential playback
- Spotify enhanced navigation
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import OrderedDict
from pathlib import Path
from typing import Any

try:
    from selenium import webdriver
    from selenium.common.exceptions import (
        ElementClickInterceptedException,
        NoSuchElementException,
        TimeoutException,
        WebDriverException,
    )
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions as EC
    from selenium.webdriver.support.ui import WebDriverWait
    _SELENIUM = True
except ImportError:
    webdriver = None  # type: ignore
    WebDriverException = Exception  # type: ignore
    ChromeOptions = None  # type: ignore
    By = None  # type: ignore
    WebDriverWait = None  # type: ignore
    EC = None  # type: ignore
    _SELENIUM = False

PROJECT_DIR = Path(__file__).resolve().parent.parent
CACHE_FILE = PROJECT_DIR / "video_id_cache.json"

# ── Selector fallback lists ───────────────────────────────────────────────────

_VIDEO_TITLE_SELECTORS = [
    "a#video-title",
    "a.ytd-video-renderer",
    "h3.ytd-video-renderer a",
    "ytd-video-renderer h3 a",
]
_NEXT_BTN_SELECTORS = [
    "button.ytp-next-button",
    ".ytp-next-button",
    "a.ytp-next-button",
]
_PLAYER_SELECTORS = [
    ".html5-video-player",
    "#movie_player",
    "ytd-player",
]


def _open_chrome(url: str) -> None:
    """Open a URL in Chrome when installed, else default browser."""
    paths = [
        os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
    ]
    for p in paths:
        if os.path.isfile(p):
            subprocess.Popen([p, url])
            return
    webbrowser.open(url)


def _retry(fn, retries: int = 1, delay: float = 1.0):
    """Call fn(); on WebDriverException retry once after delay."""
    for attempt in range(retries + 1):
        try:
            return fn()
        except WebDriverException as exc:
            if attempt < retries:
                logging.warning("WebDriver error (retry %d): %s", attempt + 1, exc)
                time.sleep(delay)
            else:
                raise


# ── LRU video ID cache ────────────────────────────────────────────────────────

class VideoIDCache:
    """Simple LRU cache for YouTube video IDs, persisted to disk."""

    def __init__(self, max_size: int = 100) -> None:
        self._max = max_size
        self._data: OrderedDict[str, str] = OrderedDict()
        self._load()

    def _load(self) -> None:
        try:
            if CACHE_FILE.is_file():
                raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    for k, v in raw.items():
                        self._data[k] = v
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        try:
            CACHE_FILE.write_text(
                json.dumps(dict(self._data), indent=2) + "\n", encoding="utf-8"
            )
        except OSError as exc:
            logging.warning("Cache save failed: %s", exc)

    def get(self, key: str) -> str | None:
        key = key.lower().strip()
        if key in self._data:
            self._data.move_to_end(key)
            return self._data[key]
        return None

    def put(self, key: str, value: str) -> None:
        key = key.lower().strip()
        self._data[key] = value
        self._data.move_to_end(key)
        while len(self._data) > self._max:
            self._data.popitem(last=False)
        self._save()


# ── YouTubeBrowser ────────────────────────────────────────────────────────────

class YouTubeBrowser:
    """Resilient Selenium Chrome session with enhanced YouTube control."""

    def __init__(self, cache_max: int = 100) -> None:
        self.driver = None
        self._cache = VideoIDCache(cache_max)
        self._queue: list[str] = []    # song/query queue

    # ── Session management ────────────────────────────────────────────────

    def _ensure_driver(self) -> bool:
        if not _SELENIUM:
            print("Selenium not installed. Install it with: pip install selenium")
            return False
        if self.driver is not None:
            return True
        try:
            options = ChromeOptions()
            options.add_argument("--disable-notifications")
            options.add_argument("--autoplay-policy=no-user-gesture-required")
            
            # Anti-bot evasion: prevent YouTube from detecting Selenium and blocking playback
            options.add_argument("--disable-blink-features=AutomationControlled")
            
            # Use a persistent profile so the user stays logged in
            profile_dir = PROJECT_DIR / "ultron_chrome_profile"
            options.add_argument(f"--user-data-dir={profile_dir}")
            
            # Hide "Chrome is being controlled by automated test software" infobar
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            
            # Force Selenium to use standard installed Chrome instead of downloading Chromium
            chrome_paths = [
                os.path.join(os.environ.get("PROGRAMFILES", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), "Google", "Chrome", "Application", "chrome.exe"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe"),
            ]
            for p in chrome_paths:
                if os.path.isfile(p):
                    options.binary_location = p
                    break
                    
            self.driver = webdriver.Chrome(options=options)
            
            # Anti-bot evasion: wipe the navigator.webdriver property on every new page
            self.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
                "source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            })
            
            self.driver.set_page_load_timeout(30)
            print("Controlled Chrome session started.")
            return True
        except WebDriverException as exc:
            print(f"Chrome session error: {type(exc).__name__}: {exc}")
            logging.error("Chrome session start failed: %s", exc)
            self.driver = None
            return False

    def open(self, url: str) -> bool:
        if not self._ensure_driver():
            _open_chrome(url)
            return False
        try:
            def _nav():
                self.driver.get(url)
                cur = self.driver.current_url
                if not cur.startswith(("http://", "https://")):
                    raise WebDriverException(f"Bad URL after nav: {cur}")
                print(f"Browser → {cur}")
            _retry(_nav)
            return True
        except WebDriverException as exc:
            print(f"Browser navigation failed: {exc}")
            self.close()
            return False

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except WebDriverException:
                pass
            finally:
                self.driver = None

    # ── Navigation helpers ────────────────────────────────────────────────

    def is_on(self, domain: str) -> bool:
        if self.driver is None:
            return False
        if domain in self.driver.current_url:
            return True
        # Check other tabs in this Selenium session
        try:
            for handle in reversed(self.driver.window_handles):
                self.driver.switch_to.window(handle)
                if domain in self.driver.current_url:
                    return True
        except WebDriverException:
            pass
        return False

    def youtube_search(self, query: str) -> bool:
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
        if not self.open(url):
            return False
        try:
            WebDriverWait(self.driver, 10).until(
                lambda d: self._find_element(d, _VIDEO_TITLE_SELECTORS) is not None
            )
            return True
        except (WebDriverException, TimeoutException) as exc:
            print(f"YouTube search wait failed: {exc}")
            return False

    def _find_element(self, driver, selectors: list[str], visible_only: bool = True):
        """Try each selector in order; return first visible match or None."""
        for sel in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                for el in els:
                    if not visible_only or el.is_displayed():
                        return el
            except WebDriverException:
                continue
        return None

    def _find_elements(self, driver, selectors: list[str]) -> list:
        for sel in selectors:
            try:
                els = driver.find_elements(By.CSS_SELECTOR, sel)
                if els:
                    return [e for e in els if e.is_displayed()]
            except WebDriverException:
                continue
        return []

    def open_first_result(self) -> bool:
        if not self.is_on("youtube.com"):
            print("open_first_result: not on YouTube.")
            return False
        try:
            before = self.driver.current_url
            def _click():
                results = self._find_elements(self.driver, _VIDEO_TITLE_SELECTORS)
                if not results:
                    raise WebDriverException("No video results found.")
                results[0].click()
            _retry(_click)
            WebDriverWait(self.driver, 10).until(
                lambda d: d.current_url != before and "youtube.com" in d.current_url
            )
            print(f"Opened result: {self.driver.current_url}")
            return True
        except (WebDriverException, TimeoutException) as exc:
            print(f"open_first_result failed: {exc}")
            return False

    def go_back(self) -> bool:
        if self.driver is None:
            return False
        try:
            before = self.driver.current_url
            self.driver.back()
            WebDriverWait(self.driver, 10).until(lambda d: d.current_url != before)
            print(f"Went back → {self.driver.current_url}")
            return True
        except (WebDriverException, TimeoutException) as exc:
            print(f"go_back failed: {exc}")
            return False

    # ── Media actions ─────────────────────────────────────────────────────

    def _video_js(self, script: str, *args) -> Any:
        """Execute JS on the current page; return result."""
        if self.driver is None:
            return None
        try:
            return self.driver.execute_script(script, *args)
        except WebDriverException as exc:
            logging.debug("JS error: %s", exc)
            return None

    def _async_video_js(self, script: str, *args) -> Any:
        if self.driver is None:
            return None
        try:
            return self.driver.execute_async_script(script, *args)
        except WebDriverException as exc:
            logging.debug("Async JS error: %s", exc)
            return None

    def _try_play(self) -> bool:
        """Try JS play(); if that fails, click the play button."""
        result = self._async_video_js(
            "const done=arguments[arguments.length-1];"
            "const v=document.querySelector('video');"
            "if(!v)return done(false);"
            "v.play().then(()=>done(!v.paused)).catch(()=>done(false));"
        )
        if result:
            return True
        # Fallback: click play button
        btn = self._video_js(
            "const b=document.querySelector('.ytp-play-button');"
            "if(b&&(b.getAttribute('aria-label')||'').includes('Play')){b.click();return true;}"
            "return false;"
        )
        return bool(btn)

    def media_action(self, action: str, amount: int | None = None) -> bool:
        if not self.is_on("youtube.com"):
            print(f"media_action({action}): not on YouTube.")
            return False

        if action == "play":
            result = self._video_js(
                "const b=document.querySelector('.ytp-play-button');"
                "if(b&&(b.getAttribute('aria-label')||'').includes('Play')){b.click(); return true;}"
                "return false;"
            )
            if not result: result = self._try_play()

        elif action == "pause":
            result = self._video_js(
                "const b=document.querySelector('.ytp-play-button');"
                "if(b&&(b.getAttribute('aria-label')||'').includes('Pause')){b.click(); return true;}"
                "return false;"
            )
            if not result:
                result = self._video_js(
                    "const v=document.querySelector('video');"
                    "if(!v)return false; v.pause(); return v.paused;"
                )

        elif action == "next":
            result = self._video_js(
                "const p=document.getElementById('movie_player');"
                "if(!p)return false; p.nextVideo(); return true;"
            )
            # Fallback to click if not in a playlist
            if not result:
                result = self._video_js(
                    "const b=document.querySelector('.ytp-next-button');"
                    "if(!b||b.disabled)return false; b.click(); return true;"
                )

        elif action == "volume":
            result = self._video_js(
                "const p=document.getElementById('movie_player');"
                "if(!p)return false;"
                "p.unMute();"
                "p.setVolume(arguments[0]);"
                "return p.getVolume() === arguments[0];",
                amount,
            )

        elif action == "skip_ad":
            result = self._video_js(
                "const skip = document.querySelector('.ytp-ad-skip-button, .ytp-skip-ad-button, .ytp-ad-skip-button-modern, .ytp-ad-skip-button-container');"
                "if(skip){skip.click(); return true;}"
                "const vid = document.querySelector('video');"
                "if(vid && document.querySelector('.ad-showing, .ytp-ad-player-overlay')){ vid.currentTime = isNaN(vid.duration) ? 999 : vid.duration; return true; }"
                "return false;"
            )

        elif action == "fullscreen":
            if self.driver:
                from selenium.webdriver.common.keys import Keys
                from selenium.webdriver.common.action_chains import ActionChains
                try:
                    ActionChains(self.driver).send_keys('f').perform()
                    print(f"media_action({action}): True")
                    return True
                except Exception as e:
                    print(f"Fullscreen error: {e}")
            return False
            
        else:
            return False

        ok = bool(result)
        print(f"media_action({action}): {ok}")
        return ok

    def get_volume(self) -> int | None:
        v = self._video_js(
            "const v=document.querySelector('video');"
            "return v?Math.round(v.volume*100):null;"
        )
        return int(v) if v is not None else None

    # ── Video playback ────────────────────────────────────────────────────

    def play_video_id(self, video_id: str) -> bool:
        url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
        ok = self.open(url)
        if ok:
            time.sleep(1.5)     # Brief wait for player load
            
            # Check if YouTube threw a "Something went wrong" error screen and refresh if so
            is_error = self._video_js(
                "return !!document.querySelector('.yt-playability-error-supported-renderers, .ytp-error-content');"
            )
            if is_error:
                print("YouTube error screen detected. Refreshing...")
                self.driver.refresh()
                time.sleep(2.0)
                
            self._try_play()    # Attempt autoplay
        return ok

    # ── Queue ─────────────────────────────────────────────────────────────

    def queue_add(self, query: str) -> None:
        self._queue.append(query)
        print(f"Queue: added '{query}' ({len(self._queue)} items)")

    def queue_next(self) -> str | None:
        if self._queue:
            return self._queue.pop(0)
        return None

    def queue_clear(self) -> None:
        self._queue.clear()

    def queue_list(self) -> list[str]:
        return list(self._queue)

    # ── YouTube API search ────────────────────────────────────────────────

    def top_video_id(self, query: str, api_key: str) -> tuple[str | None, str | None]:
        """Look up video ID via YouTube Data API v3 with caching."""
        if not api_key:
            return None, "No YouTube API key configured."

        cached = self._cache.get(query)
        if cached:
            print(f"YouTube cache hit for '{query}': {cached}")
            return cached, None

        params = urllib.parse.urlencode({
            "part": "snippet", "q": query,
            "type": "video", "maxResults": "1", "key": api_key,
        })
        req = urllib.request.Request(
            "https://www.googleapis.com/youtube/v3/search?" + params,
            headers={"User-Agent": "Ultron/2.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.load(resp)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            print(f"YouTube API HTTP {exc.code}:\n{body.replace(api_key, '[redacted]')}")
            return None, f"YouTube API HTTP {exc.code}."
        except urllib.error.URLError as exc:
            return None, f"Network error: {exc.reason}"
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            return None, f"Bad API response: {exc}"

        try:
            video_id = result["items"][0]["id"]["videoId"]
        except (KeyError, IndexError, TypeError) as exc:
            return None, f"No video found: {exc}"

        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            return None, "Invalid video ID from API."

        self._cache.put(query, video_id)
        return video_id, None

    # ── Spotify helpers ───────────────────────────────────────────────────

    def spotify_search(self, query: str) -> bool:
        url = "https://open.spotify.com/search/" + urllib.parse.quote_plus(query)
        return self.open(url)

    def spotify_liked_songs(self) -> bool:
        return self.open("https://open.spotify.com/collection/tracks")

    def spotify_new_releases(self) -> bool:
        return self.open("https://open.spotify.com/genre/new-releases-page")
