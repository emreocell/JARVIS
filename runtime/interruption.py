"""Low-latency interruption helpers for voice and text commands."""

from __future__ import annotations

import json
import re
from typing import Any

_STOP_WORDS = (
    "dur",
    "sus",
    "kes",
    "hayir",
    "hayır",
    "yanlis",
    "yanlış",
    "iptal",
    "bekle",
    "stop",
    "cancel",
    "pause",
    "durdur",
    "konusma",
    "konuşma",
    "yeter",
)

_WAKE_WORDS = (
    "jarvis",
    "hey jarvis",
    "hey carvis",
    "merhaba jarvis",
)


def normalize_command(text: str) -> str:
    return " ".join(str(text or "").strip().lower().split())


def looks_like_interrupt(text: str) -> bool:
    """Return True for short, direct Turkish/English stop commands."""
    normalized = normalize_command(text)
    if not normalized:
        return False
    if normalized in _STOP_WORDS:
        return True
    if re.search(
        r"\b(dur|sus|kes|hayir|hayır|yanlis|yanlış|iptal|bekle|stop|cancel|pause|durdur|yeter)\b",
        normalized,
    ):
        return True
    return False


def strip_wake_word(text: str) -> tuple[str, bool]:
    """Remove a wake phrase from the beginning of a command."""
    normalized = normalize_command(text)
    for phrase in sorted(_WAKE_WORDS, key=len, reverse=True):
        if normalized == phrase:
            return "", True
        if normalized.startswith(phrase + " "):
            return normalized[len(phrase) + 1 :].strip(), True
    return str(text or "").strip(), False


def parse_intent_json(raw: str) -> dict[str, Any]:
    """Best-effort parser for compact JSON returned by metacognition tools."""
    try:
        data = json.loads(str(raw or "").strip())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def intent_requests_interrupt(data: dict[str, Any]) -> bool:
    if not data:
        return False
    if bool(data.get("should_interrupt")):
        return True
    category = str(data.get("category", "")).strip().lower()
    return category == "interrupt"


__all__ = [
    "intent_requests_interrupt",
    "looks_like_interrupt",
    "normalize_command",
    "parse_intent_json",
    "strip_wake_word",
]
