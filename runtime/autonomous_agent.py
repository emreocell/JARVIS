"""Small autonomous planner/executor for JARVIS tool use."""

from __future__ import annotations

import asyncio
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from runtime.types import Route, RouteRequest


@dataclass
class AgentStep:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)
    name: str = ""
    on_error: str = "stop"


@dataclass
class AgentPlan:
    goal: str
    name: str = "agent_plan"
    steps: list[AgentStep] = field(default_factory=list)
    source: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "name": self.name,
            "source": self.source,
            "steps": [
                {
                    "tool": step.tool,
                    "args": step.args,
                    "name": step.name,
                    "on_error": step.on_error,
                }
                for step in self.steps
            ],
        }


def _extract_json(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        return json.loads(raw[start : end + 1])
    raise ValueError("Plan JSON bulunamadi.")


def _coerce_plan(payload: Any, *, goal: str, registered_tools: set[str], source: str) -> AgentPlan:
    if not isinstance(payload, dict):
        raise ValueError("Plan kok nesnesi dict olmali.")
    raw_steps = payload.get("steps") or []
    if not isinstance(raw_steps, list):
        raw_steps = []
    steps: list[AgentStep] = []
    for item in raw_steps[:12]:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "").strip()
        if tool not in registered_tools:
            continue
        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        on_error = str(item.get("on_error") or "stop").lower()
        if on_error not in {"continue", "stop"}:
            on_error = "stop"
        steps.append(
            AgentStep(
                tool=tool,
                args=dict(args),
                name=str(item.get("name") or tool),
                on_error=on_error,
            )
        )
    return AgentPlan(
        goal=goal,
        name=str(payload.get("name") or "agent_plan"),
        steps=steps,
        source=source,
    )


def _local_browser_plan(goal: str, registered_tools: set[str]) -> AgentPlan | None:
    text = goal.strip()
    low = text.lower()
    if "browser_automation" not in registered_tools:
        return None

    if any(word in low for word in ("youtube", "google", "site", "tarayici", "tarayıcı", "chrome", "opera")):
        browser = ""
        if "opera" in low:
            browser = "opera_gx" if "gx" in low else "opera"
        elif "chrome" in low:
            browser = "chrome"
        elif "edge" in low:
            browser = "edge"
        url = ""
        if "youtube" in low:
            url = "https://www.youtube.com"
        elif "google" in low:
            url = "https://www.google.com"

        steps: list[AgentStep] = []
        if url:
            steps.append(
                AgentStep(
                    tool="browser_automation",
                    args={"action": "open_url", "url": url, "browser": browser, "engine": "auto"},
                    name=f"{url} ac",
                    on_error="stop",
                )
            )
        if any(phrase in low for phrase in ("oku", "link", "listele", "bul")):
            steps.append(
                AgentStep(
                    tool="browser_automation",
                    args={"action": "find_elements", "engine": "playwright", "query": "", "limit": 40},
                    name="Sayfadaki ogeleri tara",
                    on_error="continue",
                )
            )
        if steps:
            return AgentPlan(goal=goal, name="browser_task", steps=steps, source="local_heuristic")
    return None


def _local_native_app_plan(goal: str, registered_tools: set[str]) -> AgentPlan | None:
    """Build a conservative desktop-app plan for native Windows launcher tasks."""
    text = goal.strip()
    low = text.lower()
    app_aliases = (
        ("steam", "steam"),
        ("epic games", "epic"),
        ("epic", "epic"),
        ("battle.net", "battle.net"),
        ("battle net", "battle.net"),
        ("riot client", "riot client"),
        ("xbox", "xbox"),
    )
    app_name = ""
    for token, alias in app_aliases:
        if token in low:
            app_name = alias
            break
    if not app_name or "open_app" not in registered_tools:
        return None

    steps: list[AgentStep] = [
        AgentStep(
            tool="open_app",
            args={"app_name": app_name},
            name=f"{app_name} ac",
            on_error="stop",
        )
    ]
    if "window_tracking" in registered_tools:
        steps.append(
            AgentStep(
                tool="window_tracking",
                args={"action": "active_window"},
                name="Aktif pencereyi dogrula",
                on_error="continue",
            )
        )
    if app_name == "steam" and any(word in low for word in ("cs2", "counter")) and "open_app" in registered_tools:
        steps.append(
            AgentStep(
                tool="open_app",
                args={"app_name": "steam://nav/games/details/730"},
                name="CS2 Steam kutuphane sayfasini ac",
                on_error="continue",
            )
        )
    if "ui_automation" in registered_tools:
        steps.append(
            AgentStep(
                tool="ui_automation",
                args={"action": "list", "query": "", "control_type": "any", "limit": 80},
                name="Uygulama UI ogelerini listele",
                on_error="continue",
            )
        )
    elif "screen_ocr" in registered_tools:
        steps.append(
            AgentStep(
                tool="screen_ocr",
                args={"capture": "active_window", "provider": "auto", "allow_cloud": False},
                name="Uygulamadaki metni oku",
                on_error="continue",
            )
        )

    wants_update = any(word in low for word in ("update", "guncelle", "güncelle", "guncelleme", "güncelleme"))
    if wants_update and "ui_automation" in registered_tools:
        game_query = "Counter-Strike 2" if any(word in low for word in ("cs2", "counter")) else text
        steps.append(
            AgentStep(
                tool="ui_automation",
                args={"action": "click", "query": game_query, "control_type": "ListItem", "index": 1},
                name="Oyunu kutuphanede sec",
                on_error="continue",
            )
        )
        if app_name == "steam" and "steam_click_update_button" in registered_tools:
            steps.append(
                AgentStep(
                    tool="steam_click_update_button",
                    args={"game": game_query},
                    name="Steam guncelle dugmesine tikla",
                    on_error="stop",
                )
            )
        else:
            steps.append(
                AgentStep(
                    tool="ui_automation",
                    args={"action": "click", "query": "GÜNCELLE", "control_type": "any", "index": 1},
                    name="Guncelle dugmesine tikla",
                    on_error="stop",
                )
            )
    elif "detect_screen_elements" in registered_tools:
        steps.append(
            AgentStep(
                tool="detect_screen_elements",
                args={"query": text, "capture": "active_window", "provider": "auto", "allow_cloud": False},
                name="Uygulamadaki ilgili ogeleri bul",
                on_error="continue",
            )
        )

    return AgentPlan(goal=goal, name="native_app_task", steps=steps, source="local_native_app")


def _fallback_plan(goal: str, registered_tools: set[str]) -> AgentPlan:
    native_plan = _local_native_app_plan(goal, registered_tools)
    if native_plan is not None:
        return native_plan
    browser_plan = _local_browser_plan(goal, registered_tools)
    if browser_plan is not None:
        return browser_plan
    return AgentPlan(goal=goal, name="empty_plan", steps=[], source="local_empty")


def build_agent_plan(goal: str, registered_tools: list[str], model_router: Any = None) -> AgentPlan:
    """Build a small executable plan from a natural language goal."""
    clean_goal = (goal or "").strip()
    tools = {name for name in registered_tools if isinstance(name, str)}
    if not clean_goal:
        return AgentPlan(goal="", name="empty_goal", steps=[], source="local_empty")

    native_plan = _local_native_app_plan(clean_goal, tools)
    if native_plan is not None:
        return native_plan

    if model_router is None:
        return _fallback_plan(clean_goal, tools)

    tool_list = ", ".join(sorted(tools))
    system = (
        "You are JARVIS task planner. Return ONLY JSON. Schema: "
        "{\"name\":\"short_name\",\"steps\":[{\"tool\":\"registered_tool\","
        "\"args\":{},\"name\":\"short Turkish step\",\"on_error\":\"stop|continue\"}]}. "
        "Use only registered tools. Keep 1-8 steps. Match the domain before choosing tools: "
        "native Windows apps and launchers such as Steam, Epic Games, Battle.net, Riot Client, "
        "Xbox, Spotify, WhatsApp, Discord, Outlook, File Explorer or Settings should start with "
        "open_app, then use window_tracking, screen_ocr, detect_screen_elements, ui_automation, "
        "self_healing_click or mouse_control for app UI. Do not use browser_automation for these "
        "unless the user explicitly asks for a website or browser. Use browser_automation only for "
        "browser tasks: engine auto for open/new tab/back/refresh/default browser commands, and "
        "engine playwright for DOM reading, form filling, link listing, verify and click_smart. "
        "Use memory_rag/search_history/clipboard tools for recall tasks, document/doc_intel tools "
        "for local files and screenshots, productivity tools for calendar/reminders/weather, "
        "communication tools for WhatsApp/email, media tools for YouTube/Spotify playback, and "
        "safety/metacognition tools before risky destructive, sending, purchasing or privacy-sensitive steps."
    )
    user = f"Goal: {clean_goal}\nRegistered tools: {tool_list}"
    request = RouteRequest(
        kind="chat",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=1200,
        temperature=0.1,
        timeout_sec=60,
    )
    result = model_router.route(
        "agent_plan",
        request,
        prefer=Route(provider="openrouter", model="qwen/qwen3-coder:free"),
    )
    if not result.ok or not result.text:
        return _fallback_plan(clean_goal, tools)
    try:
        return _coerce_plan(_extract_json(result.text), goal=clean_goal, registered_tools=tools, source=result.provider)
    except Exception:
        return _fallback_plan(clean_goal, tools)


def _looks_failed_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("blocked"):
        return True
    payload = result.get("result")
    if isinstance(payload, str):
        low = payload.lower()
        return any(token in low for token in ("basarisiz", "başarısız", "bulunamadi", "bulunamadı", "error", "hata"))
    return False


def _parse_tool_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {}
    payload = result.get("result")
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, str):
        try:
            decoded = json.loads(payload)
            if isinstance(decoded, dict):
                return decoded
        except Exception:
            return {}
    return {}


def _verification_step_for(step: AgentStep) -> AgentStep | None:
    if step.tool != "browser_automation":
        return None
    args = dict(step.args)
    verify_after = args.get("verify_after")
    if isinstance(verify_after, dict):
        verification_args = {"action": "verify", "engine": "playwright", **verify_after}
        return AgentStep(
            tool="browser_automation",
            args=verification_args,
            name=f"{step.name or step.tool} dogrula",
            on_error="continue",
        )

    if str(args.get("engine") or "").lower() != "playwright":
        return None
    action = str(args.get("action") or "").lower()
    if action == "open_url" and args.get("url"):
        parsed = urllib.parse.urlparse(str(args.get("url") or ""))
        expected = parsed.netloc or parsed.path.split("/")[0]
        if expected:
            return AgentStep(
                tool="browser_automation",
                args={"action": "verify", "engine": "playwright", "url_contains": expected},
                name=f"{step.name or 'Sayfa'} dogrula",
                on_error="continue",
            )
    if action == "search" and (args.get("query") or args.get("text")):
        return AgentStep(
            tool="browser_automation",
            args={"action": "verify", "engine": "playwright", "url_contains": "google.com/search"},
            name=f"{step.name or 'Arama'} dogrula",
            on_error="continue",
        )
    return None


def _local_replan_step(
    *,
    failed_step: AgentStep,
    failure: dict[str, Any],
    registered_tools: set[str],
) -> AgentStep | None:
    if "browser_automation" not in registered_tools:
        return None
    if failed_step.tool != "browser_automation":
        return None
    action = str(failed_step.args.get("action") or "").lower()
    target = str(failed_step.args.get("target") or failed_step.args.get("query") or failed_step.args.get("text") or "")
    if action in {"click_smart", "click_text", "click_selector"}:
        return AgentStep(
            tool="browser_automation",
            args={"action": "find_elements", "engine": "playwright", "query": target, "limit": 40},
            name="Hedef icin DOM adaylarini tara",
            on_error="continue",
        )
    if action in {"open_url", "search"} and failed_step.args.get("browser"):
        args = dict(failed_step.args)
        args["browser"] = ""
        args["engine"] = "auto"
        return AgentStep(
            tool="browser_automation",
            args=args,
            name="Varsayilan tarayici ile tekrar dene",
            on_error="continue",
        )
    return AgentStep(
        tool="browser_automation",
        args={"action": "timeline", "engine": "playwright", "limit": 10},
        name="Tarayici timeline ile hatayi incele",
        on_error="continue",
    )


def replan_after_failure(
    *,
    goal: str,
    failed_step: AgentStep,
    failure: dict[str, Any],
    registered_tools: list[str],
    model_router: Any = None,
) -> AgentStep | None:
    """Return one recovery step after a failed action."""
    tools = {name for name in registered_tools if isinstance(name, str)}
    local = _local_replan_step(failed_step=failed_step, failure=failure, registered_tools=tools)
    if local is not None:
        return local
    if model_router is None:
        return None

    system = (
        "You are JARVIS replanner. Return ONLY JSON for one recovery step: "
        "{\"tool\":\"registered_tool\",\"args\":{},\"name\":\"short Turkish step\",\"on_error\":\"continue\"}. "
        "Use only registered tools. Prefer inspection/timeline steps before risky actions."
    )
    user = (
        f"Goal: {goal}\nFailed step: {failed_step.__dict__}\nFailure: {failure}\n"
        f"Registered tools: {', '.join(sorted(tools))}"
    )
    request = RouteRequest(
        kind="chat",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=600,
        temperature=0.1,
        timeout_sec=45,
    )
    result = model_router.route(
        "agent_replan",
        request,
        prefer=Route(provider="openrouter", model="qwen/qwen3-coder:free"),
    )
    if not result.ok or not result.text:
        return None
    try:
        payload = _extract_json(result.text)
        if not isinstance(payload, dict):
            return None
        tool = str(payload.get("tool") or "").strip()
        if tool not in tools:
            return None
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        return AgentStep(
            tool=tool,
            args=dict(args),
            name=str(payload.get("name") or f"{tool} recovery"),
            on_error="continue",
        )
    except Exception:
        return None


async def execute_agent_plan(
    plan: AgentPlan,
    tool_runtime: Any,
    *,
    dry_run: bool = False,
    model_router: Any = None,
    enable_replan: bool = True,
    max_replans: int = 1,
    verify_steps: bool = True,
) -> dict[str, Any]:
    """Execute a plan through ToolRuntime and return a compact report."""
    started = time.monotonic()
    report: dict[str, Any] = {
        "ok": True,
        "goal": plan.goal,
        "name": plan.name,
        "source": plan.source,
        "dry_run": dry_run,
        "completed": [],
        "failed": [],
        "replans": [],
        "verifications": [],
        "steps": [step.__dict__ for step in plan.steps],
    }
    if dry_run:
        report["duration_sec"] = 0.0
        return report
    if tool_runtime is None:
        report.update({"ok": False, "error": "ToolRuntime yok; plan calistirilamadi."})
        return report

    try:
        registered_tools = list(tool_runtime.list_tools()) if hasattr(tool_runtime, "list_tools") else []
    except Exception:
        registered_tools = []

    steps_queue: list[tuple[int, AgentStep, bool]] = [(idx, step, False) for idx, step in enumerate(plan.steps, 1)]
    replans_used = 0
    while steps_queue:
        idx, step, is_replan = steps_queue.pop(0)
        label = step.name or step.tool
        try:
            result = await tool_runtime.dispatch(step.tool, step.args)
            blocked = bool(result.get("blocked")) if isinstance(result, dict) else False
            failed_like = _looks_failed_result(result)
            item = {"index": idx, "name": label, "tool": step.tool, "result": result}
            if blocked or failed_like:
                report["failed"].append(item)
                if blocked or step.on_error == "stop":
                    report["ok"] = False
                if (
                    enable_replan
                    and not blocked
                    and not is_replan
                    and replans_used < max_replans
                ):
                    recovery = replan_after_failure(
                        goal=plan.goal,
                        failed_step=step,
                        failure=item,
                        registered_tools=registered_tools,
                        model_router=model_router,
                    )
                    if recovery is not None:
                        replans_used += 1
                        report["replans"].append({"after": label, "step": recovery.__dict__})
                        steps_queue.insert(0, (idx + 0.1, recovery, True))  # type: ignore[arg-type]
                        continue
                if step.on_error == "stop":
                    break
            else:
                report["completed"].append(item)
                if verify_steps:
                    verification = _verification_step_for(step)
                    if verification is not None:
                        verification_result = await tool_runtime.dispatch(verification.tool, verification.args)
                        verification_payload = _parse_tool_payload(verification_result)
                        verification_failed = (
                            bool(verification_result.get("blocked")) if isinstance(verification_result, dict) else False
                        ) or _looks_failed_result(verification_result)
                        if verification_payload and verification_payload.get("ok") is False:
                            verification_failed = True
                        verification_item = {
                            "after": label,
                            "name": verification.name,
                            "tool": verification.tool,
                            "args": verification.args,
                            "result": verification_result,
                        }
                        report["verifications"].append(verification_item)
                        if verification_failed:
                            report["ok"] = False
                            report["failed"].append(
                                {
                                    "index": idx,
                                    "name": verification.name,
                                    "tool": verification.tool,
                                    "result": verification_result,
                                }
                            )
                            if step.on_error == "stop":
                                break
        except Exception as exc:  # noqa: BLE001
            report["ok"] = False
            item = {"index": idx, "name": label, "tool": step.tool, "error": f"{type(exc).__name__}: {exc}"}
            report["failed"].append(item)
            if enable_replan and not is_replan and replans_used < max_replans:
                recovery = replan_after_failure(
                    goal=plan.goal,
                    failed_step=step,
                    failure=item,
                    registered_tools=registered_tools,
                    model_router=model_router,
                )
                if recovery is not None:
                    replans_used += 1
                    report["replans"].append({"after": label, "step": recovery.__dict__})
                    steps_queue.insert(0, (idx + 0.1, recovery, True))  # type: ignore[arg-type]
                    continue
            if step.on_error == "stop":
                break

    report["duration_sec"] = round(time.monotonic() - started, 3)
    return report


def run_agent_plan_sync(
    plan: AgentPlan,
    tool_runtime: Any,
    *,
    dry_run: bool = False,
    model_router: Any = None,
    enable_replan: bool = True,
    max_replans: int = 1,
    verify_steps: bool = True,
) -> dict[str, Any]:
    return asyncio.run(
        execute_agent_plan(
            plan,
            tool_runtime,
            dry_run=dry_run,
            model_router=model_router,
            enable_replan=enable_replan,
            max_replans=max_replans,
            verify_steps=verify_steps,
        )
    )


__all__ = [
    "AgentPlan",
    "AgentStep",
    "build_agent_plan",
    "execute_agent_plan",
    "replan_after_failure",
    "run_agent_plan_sync",
]
