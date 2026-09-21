"""
ultron/ai.py — Gemini AI integration for Ultron.

Uses the new google-genai SDK (google.genai) for all AI interactions.
Two roles:
  1. Intent parser  — converts free-form voice input into action commands
  2. Conversational — answers general questions clearly and gently
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

# ── Personality prompts ────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are a friendly and highly capable AI voice assistant living on the user's Windows PC. "
    "Your name is Ultron. You are clear, gentle, warm, and helpful. "
    "You always try your best to fulfill the user's expectations. "
    "Speak in short, natural sentences (2-3 max) because your replies will be spoken aloud. "
    "Never use markdown, bullet points, asterisks, or any formatting — plain conversational speech only. "
    "If you do not know something, say so honestly and offer to help in another way."
)

INTENT_PROMPT = (
    "You are an intent parser for a Windows voice assistant called Ultron. "
    "Your ONLY job is to output a valid JSON array mapping the user's request to actions. "
    "Return ONLY the JSON array — no explanation, no markdown fences, no extra text. "
    "\n\nAvailable actions (action: value):\n"
    '  "open_youtube": null\n'
    '  "open_google": null\n'
    '  "open_spotify": null\n'
    '  "search_youtube": "<query>"\n'
    '  "search_google": "<query>"\n'
    '  "play_search": "<song or video name>"\n'
    '  "media_play": null\n'
    '  "media_pause": null\n'
    '  "media_next": null\n'
    '  "media_skip_ad": null\n'
    '  "volume_change": <+N or -N>\n'
    '  "volume_set": <0-100>\n'
    '  "fullscreen": null\n'
    '  "go_back": null\n'
    '  "take_screenshot": null\n'
    '  "lock_screen": null\n'
    '  "os_shutdown": null\n'
    '  "os_restart": null\n'
    '  "sys_volume_set": <0-100>\n'
    '  "sys_volume_change": <+N or -N>\n'
    '  "open_file": "<app or file name>"\n'
    '  "chat": "<your short spoken answer to the question>"\n'
    "\n"
    "Rules:\n"
    "- If the user is asking a general knowledge question, giving a greeting, or having a "
    'conversation, return: [{"action": "chat", "value": "your helpful answer in 2-3 sentences"}]\n'
    "- If the intent maps to a command, return that command's action. "
    "- You may chain multiple actions, e.g. open youtube then play a song.\n"
    "- When you are not sure, default to chat and answer helpfully.\n"
    "\nExamples:\n"
    '  User: "pause the music" → [{"action": "media_pause", "value": null}]\n'
    '  User: "play Nenjame" → [{"action": "play_search", "value": "Nenjame"}]\n'
    '  User: "open youtube and play Godzilla trailer" → '
    '[{"action": "open_youtube", "value": null}, {"action": "play_search", "value": "Godzilla trailer"}]\n'
    '  User: "who won IPL 2024" → [{"action": "chat", "value": "Kolkata Knight Riders won IPL 2024, '
    'defeating Sunrisers Hyderabad in the final."}]\n'
    '  User: "take a screenshot" → [{"action": "take_screenshot", "value": null}]\n'
    '  User: "volume up" → [{"action": "volume_change", "value": 15}]\n'
)

# ── SDK import ─────────────────────────────────────────────────────────────────

try:
    from google import genai
    from google.genai import types as genai_types
    _SDK = "new"   # google-genai
except ImportError:
    try:
        import google.generativeai as _old_genai  # type: ignore
        _SDK = "old"
    except ImportError:
        _old_genai = None
        _SDK = "none"


class GeminiAgent:
    """
    Wraps Gemini for:
      - parse_intent(text) → list of (action, value) tuples
      - ask(text)          → spoken conversational reply
    """

    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash") -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._client = None          # new SDK client
        self._chat_session = None    # new SDK chat
        self._old_model = None       # old SDK fallback
        self._old_chat = None
        self._ready = False
        self._sdk = _SDK
        self._init()

    # ── Initialisation ────────────────────────────────────────────────────────

    def _init(self) -> None:
        if not self._api_key:
            logging.warning("AI: No Gemini API key set.")
            return

        if self._sdk == "new":
            self._init_new()
        elif self._sdk == "old":
            self._init_old()
        else:
            logging.warning("AI: No Gemini SDK installed. Run: pip install google-genai")

    def _init_new(self) -> None:
        """Initialise using the modern google-genai SDK."""
        try:
            self._client = genai.Client(api_key=self._api_key)
            # Start a persistent chat session with the conversational system prompt
            self._chat_session = self._client.chats.create(
                model=self._model_name,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.7,
                    max_output_tokens=256,
                ),
            )
            self._ready = True
            logging.info("AI: Gemini %s ready (new SDK).", self._model_name)
            print(f"[AI] Gemini ready — model={self._model_name}, SDK=google-genai")
        except Exception as exc:
            logging.warning("AI: Gemini init failed (new SDK): %s", exc)
            print(f"[AI] Gemini unavailable: {exc}")

    def _init_old(self) -> None:
        """Fallback to deprecated google-generativeai SDK."""
        try:
            _old_genai.configure(api_key=self._api_key)
            self._old_model = _old_genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=SYSTEM_PROMPT,
            )
            self._old_chat = self._old_model.start_chat(history=[])
            self._ready = True
            logging.info("AI: Gemini %s ready (old SDK).", self._model_name)
        except Exception as exc:
            logging.warning("AI: Gemini init failed (old SDK): %s", exc)

    @property
    def ready(self) -> bool:
        return self._ready

    # ── Intent parsing ────────────────────────────────────────────────────────

    def parse_intent(self, user_text: str) -> list[tuple[str, Any]]:
        """
        Ask Gemini to classify what the user said into a list of (action, value).
        Falls back gracefully to ask_ai if parsing fails.
        """
        if not self._ready:
            return [("ask_ai", user_text)]

        try:
            raw = self._generate_intent(user_text)
            if not raw:
                return [("ask_ai", user_text)]

            # Strip markdown fences if model disobeys
            raw = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()

            data = json.loads(raw)
            if not isinstance(data, list):
                raise ValueError("Expected a JSON array")

            plan: list[tuple[str, Any]] = []
            for item in data:
                action = item.get("action", "").strip()
                value = item.get("value")
                if not action:
                    continue
                if action == "chat":
                    plan.append(("ai_speak", value or ""))
                else:
                    plan.append((action, value))

            return plan if plan else [("ask_ai", user_text)]

        except Exception as exc:
            logging.warning("AI: Intent parsing failed (%s): %s", type(exc).__name__, exc)
            # Graceful fallback — let the AI answer conversationally
            return [("ask_ai", user_text)]

    def _generate_intent(self, user_text: str) -> str | None:
        """Call Gemini with the intent prompt and return raw text."""
        full_prompt = INTENT_PROMPT + f'\nUser: "{user_text}"'
        try:
            if self._sdk == "new" and self._client:
                resp = self._client.models.generate_content(
                    model=self._model_name,
                    contents=full_prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.1,
                        max_output_tokens=300,
                    ),
                )
                return resp.text.strip() if resp.text else None

            if self._sdk == "old" and self._old_model:
                resp = self._old_model.generate_content(full_prompt)
                return resp.text.strip() if resp.text else None

        except Exception as exc:
            logging.warning("AI: _generate_intent error: %s", exc)
        return None

    # ── Conversational chat ───────────────────────────────────────────────────

    def ask(self, user_text: str, context: str = "") -> str:
        """
        Send a conversational message to Gemini and return the spoken reply.
        Maintains conversation history for follow-up awareness.
        """
        if not self._ready:
            return "I am sorry, my AI connection is not available right now."

        full_text = user_text
        if context:
            full_text = f"[System context: {context}]\n{user_text}"

        try:
            if self._sdk == "new" and self._chat_session:
                resp = self._chat_session.send_message(full_text)
                return self._clean(resp.text)

            if self._sdk == "old" and self._old_chat:
                resp = self._old_chat.send_message(full_text)
                return self._clean(resp.text)

        except Exception as exc:
            logging.warning("AI: ask() error: %s", exc)
            return f"I ran into a problem: {exc}"

        return "I am not sure how to respond to that."

    def reset_chat(self) -> None:
        """Clear conversation history to start fresh."""
        if self._sdk == "new" and self._client:
            self._chat_session = self._client.chats.create(
                model=self._model_name,
                config=genai_types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    temperature=0.7,
                    max_output_tokens=256,
                ),
            )
            logging.info("AI: Chat history cleared.")
        elif self._sdk == "old" and self._old_model:
            self._old_chat = self._old_model.start_chat(history=[])

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _clean(text: str) -> str:
        """Strip markdown from TTS output."""
        text = re.sub(r"\*{1,2}([^*]+)\*{1,2}", r"\1", text)   # bold/italic
        text = re.sub(r"#{1,6}\s+", "", text)                   # headings
        text = re.sub(r"`+[^`]*`+", "", text)                   # code
        text = re.sub(r"\n+", " ", text).strip()
        # Truncate to 4 sentences max for TTS
        sentences = re.split(r"(?<=[.!?])\s+", text)
        return " ".join(sentences[:4]).strip()
