"""Human-readable summaries for agent and browser automation reports."""

from __future__ import annotations

import json
from typing import Any


def _load_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _short(value: Any, limit: int = 80) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def format_agent_plan(value: Any) -> str:
    payload = _load_payload(value)
    if not payload or "steps" not in payload:
        return ""
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    name = _short(payload.get("name") or "agent_plan", 40)
    source = _short(payload.get("source") or "", 30)
    lines = [f"AGENT PLAN: {name} ({len(steps)} adim" + (f", {source}" if source else "") + ")"]
    for idx, step in enumerate(steps[:5], 1):
        if not isinstance(step, dict):
            continue
        label = _short(step.get("name") or step.get("tool"), 54)
        tool = _short(step.get("tool"), 32)
        lines.append(f"  {idx}. {label} [{tool}]")
    if len(steps) > 5:
        lines.append(f"  ... +{len(steps) - 5} adim")
    return "\n".join(lines)


def format_agent_report(value: Any) -> str:
    payload = _load_payload(value)
    if not payload or "completed" not in payload:
        return ""
    completed = payload.get("completed") if isinstance(payload.get("completed"), list) else []
    failed = payload.get("failed") if isinstance(payload.get("failed"), list) else []
    replans = payload.get("replans") if isinstance(payload.get("replans"), list) else []
    status = "OK" if payload.get("ok") else "ISSUE"
    name = _short(payload.get("name") or "agent_plan", 40)
    lines = [
        f"AGENT EXECUTE: {status} - {name} | tamamlanan={len(completed)} hata={len(failed)} recovery={len(replans)}"
    ]
    for item in completed[:4]:
        if isinstance(item, dict):
            lines.append(f"  done: {_short(item.get('name') or item.get('tool'), 62)}")
    for item in failed[:3]:
        if isinstance(item, dict):
            lines.append(f"  fail: {_short(item.get('name') or item.get('tool'), 42)} - {_short(item.get('error') or item.get('result'), 70)}")
    for item in replans[:3]:
        if isinstance(item, dict):
            step = item.get("step") if isinstance(item.get("step"), dict) else {}
            lines.append(f"  recovery: {_short(step.get('name') or step.get('tool'), 62)}")
    return "\n".join(lines)


def format_browser_timeline(value: Any) -> str:
    payload = _load_payload(value)
    if not payload or "items" not in payload:
        return ""
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    lines = [f"BROWSER TIMELINE: son {len(items)} adim"]
    for item in items[-5:]:
        if not isinstance(item, dict):
            continue
        ok = "ok" if item.get("ok") else "fail"
        action = _short(item.get("action"), 28)
        target = _short(item.get("target") or item.get("selector") or item.get("url"), 56)
        strategy = _short(item.get("strategy"), 24)
        suffix = f" ({strategy})" if strategy else ""
        lines.append(f"  {ok}: {action}{suffix} - {target}")
    return "\n".join(lines)


def format_tool_visibility(tool_name: str, args: dict[str, Any], result: Any) -> str:
    name = str(tool_name or "")
    if name == "agent_plan":
        return format_agent_plan(result)
    if name == "agent_execute":
        return format_agent_report(result)
    if name == "agent_status":
        payload = _load_payload(result)
        if payload and payload.get("last_report"):
            return format_agent_report(payload.get("last_report"))
        return ""
    if name == "browser_automation" and str((args or {}).get("action") or "").lower() in {"timeline", "history"}:
        return format_browser_timeline(result)
    return ""


__all__ = [
    "format_agent_plan",
    "format_agent_report",
    "format_browser_timeline",
    "format_tool_visibility",
]
