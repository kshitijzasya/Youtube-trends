"""Free-tier LLM clients (Gemini, Groq) that return parsed JSON objects."""
from __future__ import annotations

import json
import logging
import re
from typing import Protocol, Sequence

import requests

from .config import LlmConfig, Secrets

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class LlmError(RuntimeError):
    """Raised when no provider returned usable JSON."""


class JsonLlm(Protocol):
    """Anything that turns a prompt into a JSON object."""

    name: str

    def complete_json(self, prompt: str) -> dict:
        """Return the model's reply parsed as a JSON object."""


def parse_json_object(text: str) -> dict:
    """Parse a JSON object from model output, tolerating ```json fences."""
    cleaned = re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LlmError(f"model did not return valid JSON: {exc.msg}") from None
    if not isinstance(data, dict):
        raise LlmError("model returned JSON but not an object")
    return data


def _post(session: requests.Session, url: str, provider: str, **kwargs) -> dict:
    """POST and return JSON; errors never include headers (where keys live)."""
    try:
        resp = session.post(url, timeout=120, **kwargs)
    except requests.RequestException as exc:
        raise LlmError(f"{provider}: network error ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise LlmError(f"{provider} HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


class GeminiClient:
    """Google Gemini via AI Studio API key (free tier)."""

    name = "gemini"

    def __init__(self, api_key: str, model: str, temperature: float, session: requests.Session) -> None:
        self._key, self._model, self._temperature, self._session = api_key, model, temperature, session

    def complete_json(self, prompt: str) -> dict:
        """Call generateContent in JSON mode."""
        data = _post(
            self._session, GEMINI_URL.format(model=self._model), self.name,
            headers={"x-goog-api-key": self._key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"responseMimeType": "application/json",
                                     "temperature": self._temperature},
            },
        )
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            raise LlmError(f"gemini: unexpected response shape {str(data)[:200]}") from None
        return parse_json_object(text)


class GroqClient:
    """Groq OpenAI-compatible chat completions (free tier)."""

    name = "groq"

    def __init__(self, api_key: str, model: str, temperature: float, session: requests.Session) -> None:
        self._key, self._model, self._temperature, self._session = api_key, model, temperature, session

    def complete_json(self, prompt: str) -> dict:
        """Call chat completions in JSON-object mode."""
        data = _post(
            self._session, GROQ_URL, self.name,
            headers={"Authorization": f"Bearer {self._key}"},
            json={
                "model": self._model,
                "temperature": self._temperature,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LlmError(f"groq: unexpected response shape {str(data)[:200]}") from None
        return parse_json_object(text)


class FallbackLlm:
    """Tries each provider in order and returns the first good answer."""

    def __init__(self, clients: Sequence[JsonLlm]) -> None:
        self.clients = list(clients)
        self.name = "+".join(c.name for c in self.clients) or "none"

    def complete_json(self, prompt: str) -> dict:
        """First successful provider wins; raises LlmError if all fail."""
        errors = []
        for client in self.clients:
            try:
                return client.complete_json(prompt)
            except LlmError as exc:
                log.warning("%s failed: %s", client.name, exc)
                errors.append(str(exc))
        raise LlmError(("all LLM providers failed: " + " | ".join(errors)) if errors else "no LLM configured")


def build_llm(config: LlmConfig, secrets: Secrets, session: requests.Session) -> FallbackLlm | None:
    """Create the provider chain from config + available keys; None if no key is set."""
    available = {
        "gemini": lambda: GeminiClient(secrets.gemini_api_key, config.gemini_model, config.temperature, session)
        if secrets.gemini_api_key else None,
        "groq": lambda: GroqClient(secrets.groq_api_key, config.groq_model, config.temperature, session)
        if secrets.groq_api_key else None,
    }
    clients = [c for c in (available[p]() for p in config.providers) if c]
    return FallbackLlm(clients) if clients else None
