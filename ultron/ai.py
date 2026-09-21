"""
ultron/ai.py — Gemini AI integration for Ultron.

Provides:
  - GeminiAgent: sends prompts to Gemini and returns spoken responses
  - Falls back gracefully if google-generativeai is not installed
  - Maintains a short conversation history for contextual awareness
  - System prompt makes Gemini behave like the ULTRON AI assistant
"""

from __future__ import annotations

import logging
from typing import Any

SYSTEM_PROMPT = """You are ULTRON, an advanced AI voice assistant running on the user's Windows PC.
You are helpful, concise, and intelligent. You can control the user's desktop, browser, and media.
Keep all responses SHORT (2-3 sentences max) since they will be spoken aloud via text-to-speech.
Do not use markdown, bullet points, asterisks or any formatting — plain spoken sentences only.
If asked to do something you cannot do (like control hardware), explain briefly and suggest an alternative.
You are proactive, confident, and slightly futuristic in tone — like the ULTRON from Marvel but helpful."""

try:
    import google.generativeai as genai
    _GENAI = True
except ImportError:
    genai = None  # type: ignore
    _GENAI = False


class GeminiAgent:
    """Talks to Gemini API to answer general questions and handle unknown commands."""

    MAX_HISTORY = 10  # Keep last N turns in memory

    def __init__(self, api_key: str, model_name: str = "gemini-2.0-flash") -> None:
        self._api_key = api_key
        self._model_name = model_name
        self._history: list[dict[str, str]] = []
        self._model = None
        self._chat = None
        self._ready = False
        self._init()

    def _init(self) -> None:
        if not _GENAI:
            logging.warning("AI: google-generativeai not installed. Run: pip install google-generativeai")
            return
        if not self._api_key:
            logging.warning("AI: No Gemini API key configured.")
            return
        try:
            genai.configure(api_key=self._api_key)
            self._model = genai.GenerativeModel(
                model_name=self._model_name,
                system_instruction=SYSTEM_PROMPT,
            )
            self._chat = self._model.start_chat(history=[])
            self._ready = True
            logging.info("AI: Gemini %s ready.", self._model_name)
            print(f"Gemini AI ready (model={self._model_name}).")
        except Exception as exc:
            logging.warning("AI: Gemini init failed: %s", exc)
            print(f"Gemini AI unavailable: {exc}")

    @property
    def ready(self) -> bool:
        return self._ready

    def ask(self, prompt: str, context: str = "") -> str:
        """
        Send a prompt to Gemini and return the spoken response.
        Includes optional context string (e.g. current site, last command).
        """
        if not self._ready or self._chat is None:
            return "My AI brain is not connected. Please check the Gemini API key."

        full_prompt = prompt
        if context:
            full_prompt = f"[Context: {context}]\n{prompt}"

        try:
            response = self._chat.send_message(full_prompt)
            text = response.text.strip()
            # Trim to 3 sentences max for TTS
            sentences = text.replace(".\n", ". ").split(". ")
            if len(sentences) > 4:
                text = ". ".join(sentences[:4]).strip()
                if not text.endswith("."):
                    text += "."
            logging.info("AI response: %s", text[:80])
            return text
        except Exception as exc:
            logging.warning("AI: Gemini request failed: %s", exc)
            return f"I encountered an error: {exc}"

    def reset(self) -> None:
        """Clear conversation history and start fresh."""
        if self._model is not None:
            self._chat = self._model.start_chat(history=[])
            logging.info("AI: Chat history cleared.")
