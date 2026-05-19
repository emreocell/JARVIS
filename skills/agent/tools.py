"""Autonomous planner/executor tools."""

from __future__ import annotations

import json
from typing import Any

from runtime.autonomous_agent import AgentPlan, AgentStep, build_agent_plan, run_agent_plan_sync

_LAST_PLAN: AgentPlan | None = None
_LAST_REPORT: dict[str, Any] | None = None


def _registered_tools(tool_runtime: Any) -> list[str]:
    if tool_runtime is not None and hasattr(tool_runtime, "list_tools"):
        return list(tool_runtime.list_tools())
    return []


def _plan_from_json(plan_json: str) -> AgentPlan:
    payload = json.loads(plan_json)
    steps = [
        AgentStep(
            tool=str(item.get("tool") or ""),
            args=dict(item.get("args") or {}),
            name=str(item.get("name") or item.get("tool") or ""),
            on_error=str(item.get("on_error") or "stop"),
        )
        for item in payload.get("steps", [])
        if isinstance(item, dict)
    ]
    return AgentPlan(
        goal=str(payload.get("goal") or ""),
        name=str(payload.get("name") or "agent_plan"),
        source=str(payload.get("source") or "json"),
        steps=steps,
    )


def agent_plan(goal: str, model_router: Any = None, tool_runtime: Any = None) -> str:
    """Create an executable plan for a natural language goal."""
    global _LAST_PLAN
    plan = build_agent_plan(goal, _registered_tools(tool_runtime), model_router=model_router)
    _LAST_PLAN = plan
    payload = plan.to_dict()
    payload["ok"] = bool(plan.steps)
    if not plan.steps:
        payload["message"] = "Calistirilabilir adim bulunamadi; hedefi biraz daha net anlatin."
    return json.dumps(payload, ensure_ascii=False, indent=2)


def agent_execute(
    goal: str = "",
    plan_json: str = "",
    dry_run: bool = False,
    enable_replan: bool = True,
    max_replans: int = 1,
    verify_steps: bool = True,
    model_router: Any = None,
    tool_runtime: Any = None,
) -> str:
    """Plan a goal if needed, then execute steps via ToolRuntime."""
    global _LAST_PLAN, _LAST_REPORT
    if plan_json:
        plan = _plan_from_json(plan_json)
    elif goal:
        plan = build_agent_plan(goal, _registered_tools(tool_runtime), model_router=model_router)
    elif _LAST_PLAN is not None:
        plan = _LAST_PLAN
    else:
        return json.dumps({"ok": False, "error": "Calistirilacak plan yok."}, ensure_ascii=False)

    _LAST_PLAN = plan
    report = run_agent_plan_sync(
        plan,
        tool_runtime,
        dry_run=dry_run,
        model_router=model_router,
        enable_replan=enable_replan,
        max_replans=max_replans,
        verify_steps=verify_steps,
    )
    _LAST_REPORT = report
    return json.dumps(report, ensure_ascii=False, indent=2)


def agent_status() -> str:
    """Return the last agent plan and execution report."""
    return json.dumps(
        {
            "ok": True,
            "last_plan": _LAST_PLAN.to_dict() if _LAST_PLAN else None,
            "last_report": _LAST_REPORT,
        },
        ensure_ascii=False,
        indent=2,
    )


agent_plan.__tool__ = {
    "declaration": {
        "name": "agent_plan",
        "description": (
            "Dogal dil hedefini JARVIS tool adimlarina boler. Cok adimli gorevlerde "
            "once bunu kullan; kullanici onaylarsa agent_execute ile calistir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "Planlanacak hedef."},
            },
            "required": ["goal"],
        },
    },
    "execution_mode": "inline",
}


agent_execute.__tool__ = {
    "declaration": {
        "name": "agent_execute",
        "description": (
            "Bir hedefi planlayip ToolRuntime ile adim adim calistirir. Riskli tool "
            "cagrilarinda mevcut safety gate onay/engel mekanizmasi devrededir. "
            "Basarisiz adimlarda enable_replan=true ise kontrollu recovery adimi dener."
            " Playwright web adimlarinda verify_steps=true ise adim sonrasi durum "
            "dogrulamasi rapora eklenir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {"type": "STRING", "description": "Planlanip calistirilacak hedef."},
                "plan_json": {"type": "STRING", "description": "Opsiyonel hazir plan JSON'u."},
                "dry_run": {"type": "BOOLEAN", "description": "True ise adimlari calistirmadan raporlar."},
                "enable_replan": {"type": "BOOLEAN", "description": "True ise basarisiz adimdan sonra tek recovery adimi denenebilir."},
                "max_replans": {"type": "NUMBER", "description": "Maksimum recovery adimi sayisi."},
                "verify_steps": {"type": "BOOLEAN", "description": "True ise uygun Playwright adimlarindan sonra verify calistirir."},
            },
        },
    },
    "execution_mode": "inline",
}


agent_status.__tool__ = {
    "declaration": {
        "name": "agent_status",
        "description": "Son agent planini ve calistirma raporunu gosterir.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    "execution_mode": "inline",
}


__all__ = ["agent_plan", "agent_execute", "agent_status"]
