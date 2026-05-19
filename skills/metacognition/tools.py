"""Low-latency intent and self-evaluation tools.

These tools are deliberately small and safe. They use ModelRouter with a Groq
preference when the runtime injects it; otherwise they fall back to local,
deterministic heuristics so the skill remains usable in tests and offline runs.
"""

from __future__ import annotations

import json
import re
from typing import Any

from runtime.types import Route, RouteProfile, RouteRequest

_GROQ_FAST = Route(provider="groq", model="llama-3.1-8b-instant")
_GEMINI_FALLBACK = Route(provider="gemini_primary", model="models/gemini-3.1-flash-lite")
_FAST_PROFILE = RouteProfile(primary=_GROQ_FAST, fallback=(_GEMINI_FALLBACK,))


def _route_chat(
    model_router: Any,
    *,
    tool_name: str,
    system: str,
    user: str,
    max_tokens: int = 256,
) -> str | None:
    if model_router is None:
        return None
    result = model_router.route(
        tool_name,
        RouteRequest(
            kind="chat",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=max_tokens,
            temperature=0.0,
            timeout_sec=8.0,
        ),
        prefer=_FAST_PROFILE,
    )
    if getattr(result, "ok", False) and getattr(result, "text", None):
        return str(result.text).strip()
    return None


def _json_line(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _fallback_intent(command: str) -> str:
    text = command.lower().strip()
    category = "chat"
    urgency = "normal"
    should_interrupt = False
    needs_confirmation = False
    suggested_tool = ""

    if re.search(r"\b(dur|iptal|sus|bekle|stop|cancel|pause)\b", text):
        category = "interrupt"
        urgency = "high"
        should_interrupt = True
    elif any(word in text for word in ("aç", "ac", "kapat", "tıkla", "tikla", "yaz", "bas")):
        category = "computer_control"
        needs_confirmation = any(word in text for word in ("sil", "gönder", "gonder", "satın", "satin", "format"))
    elif any(word in text for word in ("dosya", "klasör", "klasor", "indirilen", "masaüstü", "masaustu")):
        category = "file_ops"
    elif any(word in text for word in ("özetle", "ozetle", "çevir", "cevir", "düzelt", "duzelt")):
        category = "text_transform"

    return _json_line(
        {
            "category": category,
            "urgency": urgency,
            "should_interrupt": should_interrupt,
            "needs_confirmation": needs_confirmation,
            "suggested_tool": suggested_tool,
            "confidence": 0.55,
            "reason": "Yerel fallback siniflandirma kullanildi.",
        }
    )


def classify_intent_fast(command: str, context: str = "", model_router: Any = None) -> str:
    """Classify a user command quickly, preferring Groq via ModelRouter."""
    command = str(command or "").strip()
    context = str(context or "").strip()
    if not command:
        return _json_line(
            {
                "category": "unknown",
                "urgency": "normal",
                "should_interrupt": False,
                "needs_confirmation": False,
                "suggested_tool": "",
                "confidence": 0.0,
                "reason": "Komut bos.",
            }
        )

    system = (
        "You classify Turkish desktop-assistant commands. Return only compact JSON "
        "with keys: category, urgency, should_interrupt, needs_confirmation, "
        "suggested_tool, confidence, reason. Categories: interrupt, computer_control, "
        "browser, file_ops, text_transform, memory, chat, unknown."
    )
    user = f"Command: {command}\nContext: {context[:1000]}"
    routed = _route_chat(
        model_router,
        tool_name="classify_intent_fast",
        system=system,
        user=user,
        max_tokens=220,
    )
    return routed or _fallback_intent(command)


classify_intent_fast.__tool__ = {
    "declaration": {
        "name": "classify_intent_fast",
        "description": (
            "Kullanici komutunu dusuk gecikmeyle siniflandirir. Dur/iptal gibi "
            "barge-in komutlarinda, bilgisayar kontrolu isteklerinde ve tool seciminde kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {"type": "STRING", "description": "Kullanici komutu."},
                "context": {"type": "STRING", "description": "Opsiyonel ekran/konusma baglami."},
            },
            "required": ["command"],
        },
    },
    "execution_mode": "inline",
    "timeout_sec": 10,
    "route": {
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "fallback": [{"provider": "gemini_primary", "model": "models/gemini-3.1-flash-lite"}],
    },
}


def _fallback_self_eval(goal: str, action: str, observation: str) -> str:
    risk = "low"
    decision = "continue"
    checks: list[str] = []
    text = " ".join([goal, action, observation]).lower()
    if any(word in text for word in ("sil", "delete", "gönder", "gonder", "ödeme", "odeme", "satın", "satin")):
        risk = "high"
        decision = "ask_user"
        checks.append("Kritik veya geri alinmasi zor bir islem olabilir.")
    if observation and any(word in observation.lower() for word in ("hata", "error", "failed", "bulunamadı", "bulunamadi")):
        risk = "medium" if risk == "low" else risk
        decision = "repair" if decision == "continue" else decision
        checks.append("Gozlem beklenen basari sinyali vermiyor.")
    if not checks:
        checks.append("Belirgin risk sinyali yok; yine de ekran sonucu dogrulanmali.")
    return _json_line(
        {
            "decision": decision,
            "risk": risk,
            "checks": checks,
            "next_step": "Devam etmeden once ekran/pencere durumunu tekrar dogrula.",
            "confidence": 0.5,
            "source": "local_fallback",
        }
    )


def self_evaluate_action(
    goal: str,
    action: str,
    observation: str = "",
    model_router: Any = None,
) -> str:
    """Evaluate whether the latest computer-control action looks safe/correct."""
    goal = str(goal or "").strip()
    action = str(action or "").strip()
    observation = str(observation or "").strip()
    if not goal and not action:
        return _json_line(
            {
                "decision": "ask_user",
                "risk": "medium",
                "checks": ["Hedef ve aksiyon bos."],
                "next_step": "Kullanicidan hedefi tekrar iste.",
                "confidence": 0.2,
            }
        )

    system = (
        "You are a safety critic for a Windows desktop assistant. Return only compact "
        "JSON with keys: decision, risk, checks, next_step, confidence. decision must "
        "be continue, repair, retry, ask_user, or stop. Prefer ask_user for irreversible, "
        "payment, deletion, sending, or privacy-sensitive actions."
    )
    user = f"Goal: {goal}\nAction taken/planned: {action}\nObservation after action: {observation}"
    routed = _route_chat(
        model_router,
        tool_name="self_evaluate_action",
        system=system,
        user=user,
        max_tokens=260,
    )
    return routed or _fallback_self_eval(goal, action, observation)


self_evaluate_action.__tool__ = {
    "declaration": {
        "name": "self_evaluate_action",
        "description": (
            "Bir bilgisayar kontrolu adimindan once veya sonra risk/dogruluk "
            "degerlendirmesi yapar. Yanlis tiklama, silme, gonderme, odeme gibi "
            "durumlarda kullanicidan onay istemeyi onerir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "Kullanicinin hedefi."},
                "action": {"type": "STRING", "description": "Planlanan veya yapilan aksiyon."},
                "observation": {"type": "STRING", "description": "Aksiyon sonrasi ekran/uygulama gozlemi."},
            },
            "required": ["goal", "action"],
        },
    },
    "execution_mode": "inline",
    "timeout_sec": 10,
    "route": {
        "provider": "groq",
        "model": "llama-3.1-8b-instant",
        "fallback": [{"provider": "gemini_primary", "model": "models/gemini-3.1-flash-lite"}],
    },
}


__all__ = ["classify_intent_fast", "self_evaluate_action"]
