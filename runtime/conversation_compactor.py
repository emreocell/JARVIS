"""Conversation compaction helper for prompt context.

The compactor keeps a small, task-useful summary of recent conversation logs.
It prefers Groq through ModelRouter for speed, and falls back to a deterministic
local summary when no router/provider is available.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from runtime.conversation_logger import ConversationLogger
from runtime.types import ConversationLogEntry, Route, RouteProfile, RouteRequest


_GROQ_FAST = Route(provider="groq", model="llama-3.1-8b-instant")
_GEMINI_FALLBACK = Route(provider="gemini_primary", model="models/gemini-3.1-flash-lite")
_SUMMARY_PROFILE = RouteProfile(primary=_GROQ_FAST, fallback=(_GEMINI_FALLBACK,))


class ConversationCompactor:
    """Build and cache a compact summary of recent conversation history."""

    def __init__(
        self,
        logger: ConversationLogger,
        *,
        cache_path: Path | str = Path("memory") / "conversation_compact_summary.json",
        model_router: Any = None,
        max_entries: int = 80,
        max_chars: int = 12000,
        refresh_interval_sec: float = 300.0,
    ) -> None:
        self._logger = logger
        self._cache_path = Path(cache_path)
        self._model_router = model_router
        self._max_entries = max_entries
        self._max_chars = max_chars
        self._refresh_interval_sec = refresh_interval_sec

    def get_context(self, *, task_hint: str = "") -> str:
        """Return a compact prompt block, or an empty string when no logs exist."""
        entries = self._recent_entries()
        if not entries:
            return ""

        newest_ts = entries[-1].ts
        cached = self._read_cache()
        if (
            cached.get("newest_ts") == newest_ts
            and time.time() - float(cached.get("created_at", 0.0) or 0.0) < self._refresh_interval_sec
        ):
            return str(cached.get("summary") or "").strip()

        transcript = self._format_entries(entries)
        summary = self._summarize_with_router(transcript, task_hint=task_hint)
        if not summary:
            summary = self._fallback_summary(entries)
        summary = summary.strip()
        if summary:
            self._write_cache({"created_at": time.time(), "newest_ts": newest_ts, "summary": summary})
        return summary

    def _recent_entries(self) -> list[ConversationLogEntry]:
        entries = list(self._logger.iter_entries())
        meaningful = [
            entry for entry in entries
            if entry.text.strip() and entry.role in {"user", "assistant", "system"}
        ]
        return meaningful[-self._max_entries:]

    def _format_entries(self, entries: list[ConversationLogEntry]) -> str:
        lines: list[str] = []
        remaining = self._max_chars
        for entry in reversed(entries):
            line = f"{entry.ts} {entry.role}: {entry.text.strip()}"
            if len(line) > 900:
                line = line[:900] + "..."
            if remaining - len(line) <= 0:
                break
            lines.append(line)
            remaining -= len(line)
        lines.reverse()
        return "\n".join(lines)

    def _summarize_with_router(self, transcript: str, *, task_hint: str) -> str:
        if self._model_router is None:
            return ""
        system = (
            "You maintain compact memory for a Turkish desktop assistant. "
            "Summarize only durable, task-useful context: user preferences, current projects, "
            "recent unresolved bugs, chosen technical decisions, and next actions. "
            "Do not include secrets or API keys. Return concise Turkish bullet lines."
        )
        user = (
            f"Task hint: {task_hint[:500]}\n\n"
            "Recent conversation log:\n"
            f"{transcript}"
        )
        result = self._model_router.route(
            "conversation_compact_summary",
            RouteRequest(
                kind="chat",
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=700,
                temperature=0.1,
                timeout_sec=12.0,
            ),
            prefer=_SUMMARY_PROFILE,
        )
        if getattr(result, "ok", False) and getattr(result, "text", None):
            return str(result.text)
        return ""

    def _fallback_summary(self, entries: list[ConversationLogEntry]) -> str:
        lines = []
        for entry in entries[-16:]:
            text = entry.text.strip().replace("\n", " ")
            if len(text) > 220:
                text = text[:220] + "..."
            lines.append(f"- {entry.role}: {text}")
        return "\n".join(lines)

    def _read_cache(self) -> dict[str, Any]:
        try:
            if self._cache_path.is_file():
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        return {}

    def _write_cache(self, data: dict[str, Any]) -> None:
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass


__all__ = ["ConversationCompactor"]
