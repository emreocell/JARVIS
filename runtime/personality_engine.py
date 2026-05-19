"""Long-term personality and emotional tone engine for JARVIS.

The engine keeps a small, durable profile that can be injected into the live
system prompt. It is deliberately deterministic and local-first; model-based
reflection can be layered on later without changing the storage contract.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE_PATH = BASE_DIR / "memory" / "personality_profile.json"

_DEFAULT_PROFILE: dict[str, Any] = {
    "schema_version": 1,
    "tone": {
        "warmth": 0.72,
        "brevity": 0.58,
        "proactivity": 0.70,
        "technical_depth": 0.66,
        "playfulness": 0.34,
    },
    "relationship": {
        "preferred_address": "",
        "working_style": "collaborative",
        "language": "tr",
    },
    "emotional_state": {
        "current": "steady",
        "intensity": 0.25,
        "last_signal": "",
        "updated_at": "",
    },
    "preferences": {
        "likes_concise_updates": True,
        "likes_modern_ui": True,
        "likes_proactive_building": True,
    },
    "interaction_stats": {
        "turns_observed": 0,
        "last_user_message_at": "",
    },
}

_FRUSTRATION_RE = re.compile(
    r"\b(yetersiz|olmuyor|calismiyor|çalışmıyor|hata|yanlis|yanlış|kotu|kötü|sikildim|sıkıldım)\b",
    re.IGNORECASE,
)
_APPRECIATION_RE = re.compile(
    r"\b(tamamdir|tamamdır|guzel|güzel|super|süper|harika|iyi|tesekkur|teşekkür|eline saglik|eline sağlık)\b",
    re.IGNORECASE,
)
_URGENCY_RE = re.compile(
    r"\b(hizli|hızlı|acil|hemen|simdi|şimdi|devam|baslayabilirsin|başlayabilirsin)\b",
    re.IGNORECASE,
)
_UI_RE = re.compile(r"\b(ui|hud|tasarim|tasarım|modern|gorsel|görsel|ekran|animasyon)\b", re.IGNORECASE)
_CONTROL_RE = re.compile(r"\b(mouse|tikla|tıkla|kontrol|ekran|ocr|browser|pencere|monitor|monitör)\b", re.IGNORECASE)


def _now() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class PersonalityEngine:
    """Tracks durable personality guidance and transient emotional tone."""

    def __init__(self, path: str | Path = DEFAULT_PROFILE_PATH) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    return _deep_merge(_DEFAULT_PROFILE, raw)
        except Exception:
            pass
        return json.loads(json.dumps(_DEFAULT_PROFILE))

    def save(self, profile: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(profile, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def observe_user_message(self, text: str) -> dict[str, Any]:
        """Update profile from a user message and return the new profile."""
        msg = str(text or "").strip()
        profile = self.load()
        stats = profile.setdefault("interaction_stats", {})
        stats["turns_observed"] = int(stats.get("turns_observed", 0) or 0) + 1
        stats["last_user_message_at"] = _now()

        tone = profile.setdefault("tone", {})
        prefs = profile.setdefault("preferences", {})
        emotional = profile.setdefault("emotional_state", {})

        if _FRUSTRATION_RE.search(msg):
            emotional.update(
                {
                    "current": "frustrated",
                    "intensity": _clamp(float(emotional.get("intensity", 0.2) or 0.2) + 0.22),
                    "last_signal": "frustration",
                    "updated_at": _now(),
                }
            )
            tone["warmth"] = _clamp(float(tone.get("warmth", 0.7)) + 0.05)
            tone["proactivity"] = _clamp(float(tone.get("proactivity", 0.7)) + 0.06)
        elif _APPRECIATION_RE.search(msg):
            emotional.update(
                {
                    "current": "positive",
                    "intensity": _clamp(float(emotional.get("intensity", 0.2) or 0.2) + 0.12),
                    "last_signal": "appreciation",
                    "updated_at": _now(),
                }
            )
            tone["playfulness"] = _clamp(float(tone.get("playfulness", 0.34)) + 0.03)
        else:
            emotional["intensity"] = _clamp(float(emotional.get("intensity", 0.25) or 0.25) * 0.92)
            if float(emotional["intensity"]) < 0.18:
                emotional["current"] = "steady"
                emotional["last_signal"] = ""
                emotional["updated_at"] = _now()

        if _URGENCY_RE.search(msg):
            tone["brevity"] = _clamp(float(tone.get("brevity", 0.58)) + 0.04)
            tone["proactivity"] = _clamp(float(tone.get("proactivity", 0.70)) + 0.04)
            prefs["likes_concise_updates"] = True
        if _UI_RE.search(msg):
            prefs["likes_modern_ui"] = True
        if _CONTROL_RE.search(msg):
            prefs["likes_computer_control"] = True
            tone["technical_depth"] = _clamp(float(tone.get("technical_depth", 0.66)) + 0.03)

        self.save(profile)
        return profile

    def format_for_prompt(self, profile: dict[str, Any] | None = None) -> str:
        profile = profile or self.load()
        tone = profile.get("tone", {})
        emotional = profile.get("emotional_state", {})
        prefs = profile.get("preferences", {})

        lines = ["[JARVIS PERSONALITY ENGINE]"]
        lines.append(
            "Tone: "
            f"warmth={float(tone.get('warmth', 0.7)):.2f}, "
            f"brevity={float(tone.get('brevity', 0.6)):.2f}, "
            f"proactivity={float(tone.get('proactivity', 0.7)):.2f}, "
            f"technical_depth={float(tone.get('technical_depth', 0.65)):.2f}, "
            f"playfulness={float(tone.get('playfulness', 0.35)):.2f}."
        )
        lines.append(
            "Emotional state: "
            f"{emotional.get('current', 'steady')} "
            f"(intensity={float(emotional.get('intensity', 0.25)):.2f})."
        )
        if prefs:
            active = [key for key, value in prefs.items() if bool(value)]
            if active:
                lines.append("User style preferences: " + ", ".join(sorted(active)) + ".")
        lines.append(
            "Apply this as subtle style guidance only; do not mention the engine unless asked."
        )
        return "\n".join(lines)


__all__ = ["DEFAULT_PROFILE_PATH", "PersonalityEngine"]
