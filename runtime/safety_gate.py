"""Runtime safety gate for risky tool calls."""

from __future__ import annotations

import json
import re
from typing import Any

_SENSITIVE_TOOLS = {
    "mouse_control",
    "ui_automation",
    "browser_automation",
    "self_healing_click",
    "delete_memory",
    "delete_calendar_event",
    "memory_rag_forget",
}

_RISK_RE = re.compile(
    r"\b("
    r"sil|delete|remove|forget|unut|"
    r"gonder|gönder|send|submit|paylas|paylaş|"
    r"odeme|ödeme|pay|satin|satın|buy|purchase|"
    r"format|reset|factory|"
    r"kapat|shutdown"
    r")\b",
    re.IGNORECASE,
)

_SAFE_CONFIRM_KEYS = {
    "confirmed",
    "confirm",
    "user_confirmed",
    "allow_risky",
    "send_now",
}


def _args_text(args: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in args.items():
        if isinstance(value, (str, int, float, bool)):
            parts.append(f"{key}={value}")
        elif isinstance(value, (list, tuple)):
            parts.extend(str(item) for item in value[:6])
        elif isinstance(value, dict):
            parts.append(json.dumps(value, ensure_ascii=False)[:500])
    return " ".join(parts)


def has_explicit_confirmation(args: dict[str, Any]) -> bool:
    for key in _SAFE_CONFIRM_KEYS:
        value = args.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"true", "evet", "yes", "onay", "confirmed"}:
            return True
    return False


def is_potentially_risky(tool_name: str, args: dict[str, Any]) -> bool:
    name = str(tool_name or "").strip()
    if name not in _SENSITIVE_TOOLS:
        return False
    if name.startswith("delete_") or name.endswith("_forget"):
        return True
    return bool(_RISK_RE.search(_args_text(args)))


def evaluate_tool_call(tool_name: str, args: dict[str, Any], model_router: Any = None) -> dict[str, Any]:
    """Evaluate a tool call and return a compact decision dict."""
    if not is_potentially_risky(tool_name, args):
        return {"decision": "continue", "risk": "low", "source": "local_gate"}
    if has_explicit_confirmation(args):
        return {"decision": "continue", "risk": "medium", "source": "explicit_confirmation"}

    goal = f"Run tool {tool_name}"
    action = _args_text(args)
    if model_router is not None:
        try:
            from skills.metacognition.tools import self_evaluate_action

            raw = self_evaluate_action(goal, action, model_router=model_router)
            data = json.loads(str(raw or "{}"))
            if isinstance(data, dict):
                decision = str(data.get("decision", "")).strip().lower()
                risk = str(data.get("risk", "")).strip().lower()
                if decision in {"continue", "repair", "retry", "ask_user", "stop"}:
                    data.setdefault("source", "model_router")
                    if risk == "high" and decision == "continue":
                        data["decision"] = "ask_user"
                    return data
        except Exception:
            pass

    return {
        "decision": "ask_user",
        "risk": "high",
        "checks": ["Geri alinmasi zor veya hassas bir islem olabilir."],
        "next_step": "Kullanicidan acik onay iste.",
        "confidence": 0.62,
        "source": "local_gate",
    }


def block_message(tool_name: str, evaluation: dict[str, Any]) -> str:
    risk = str(evaluation.get("risk", "medium"))
    next_step = str(evaluation.get("next_step") or "Devam etmem icin acik onay gerekiyor.")
    return (
        f"Bu islem riskli gorunuyor ({tool_name}, risk={risk}). "
        f"{next_step} Onayliyorsan komutu acikca onaylayarak tekrar soyle."
    )


__all__ = [
    "block_message",
    "evaluate_tool_call",
    "has_explicit_confirmation",
    "is_potentially_risky",
]
