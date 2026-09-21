"""
ultron/ai.py — Gemini AI integration for Ultron.
"""

from __future__ import annotations

import json
import logging
from typing import Any

SYSTEM_PROMPT = """You are ULTRON, an advanced AI voice assistant running on the user's Windows PC.
You are helpful, concise, and intelligent.
You are proactive, confident, and slightly futuristic in tone — like the ULTRON from Marvel but helpful.
Keep all responses SHORT (2-3 sentences max) since they will be spoken aloud.
"""

INTENT_PROMPT = """You are an intent parser. Map the user's request to a JSON list of actions.
Available actions:
- "open_youtube": open youtube
- "open_google": open google
- "search_youtube": <query>
- "search_google": <query>
- "play_search": <query> (play on youtube)
- "media_play": None
- "media_pause": None
- "media_next": None
- "media_skip_ad": None
- "volume_change": <number> (relative, e.g. +10 or -20)
- "volume_set": <number> (absolute, 0-100)
- "fullscreen": None
- "go_back": None
- "take_screenshot": None
- "lock_screen": None
- "os_shutdown": None
- "os_restart": None
- "sys_volume_set": <number>
- "sys_volume_change": <number>
- "open_file": <file or app name>

If the user is just chatting or asking a general question (e.g. "who won the superbowl", "what is a nephron", "tell me a joke", "birthday"), return:
[{"action": "chat", "value": "Your intelligent, concise answer here (2-3 sentences max)."}]

Return ONLY valid JSON. Example:
[{"action": "media_pause", "value": null}, {"action": "sys_volume_set", "value": 50}]
"""

try:
    import google.generativeai as genai
    _GENAI = True
except ImportError:
    genai = None  # type: ignore
    _GENAI = False


class GeminiAgent:
    def __init__(self, api_key: str, model_name: str = "gemini-3.6-flash") -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._model = None
        self._chat = None
        self._intent_model = None
        self._ready = False
        self._init()

    def _init(self) -> None:
        if not _GENAI or not self._api_key:
            return
        try:
            genai.configure(api_key=self._api_key)
            self._model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=SYSTEM_PROMPT,
            )
            self._intent_model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=INTENT_PROMPT,
            )
            self._chat = self._model.start_chat(history=[])
            self._ready = True
            logging.info("AI: Gemini %s ready.", self._model_name)
        except Exception as exc:
            logging.warning("AI: Gemini init failed: %s", exc)

    @property
    def ready(self) -> bool:
        return self._ready

    def parse_intent(self, prompt: str) -> list[tuple[str, Any]]:
        """Ask Gemini to map the prompt to a list of commands, or a chat response."""
        if not self._ready or self._intent_model is None:
            return [("ask_ai", prompt)]
            
        try:
            response = self._intent_model.generate_content(prompt)
            text = response.text.strip()
            if text.startswith("```json"):
                text = text[7:]
            if text.endswith("```"):
                text = text[:-3]
            data = json.loads(text.strip())
            plan = []
            for item in data:
                action = item.get("action")
                value = item.get("value")
                if action == "chat":
                    plan.append(("ai_speak", value))
                elif action:
                    plan.append((action, value))
            return plan
        except Exception as exc:
            logging.warning("AI intent parsing failed: %s", exc)
            return [("ask_ai", prompt)]

    def ask(self, prompt: str, context: str = "") -> str:
        """Fallback conversational chat."""
        if not self._ready or self._chat is None:
            return "My AI brain is not connected."
        full_prompt = prompt
        if context:
            full_prompt = f"[Context: {context}]\n{prompt}"
        try:
            response = self._chat.send_message(full_prompt)
            return response.text.strip()
        except Exception as exc:
            return f"I encountered an error: {exc}"
