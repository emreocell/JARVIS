from __future__ import annotations

from runtime.autonomous_agent import (
    AgentPlan,
    AgentStep,
    build_agent_plan,
    replan_after_failure,
    run_agent_plan_sync,
)


class _FakeRouter:
    def route(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
        class _Result:
            ok = True
            text = '{"name":"open_youtube","steps":[{"tool":"browser_automation","args":{"action":"open_url","url":"https://www.youtube.com","engine":"auto"},"name":"YouTube ac","on_error":"stop"}]}'
            provider = "openrouter"

        return _Result()


class _FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
        self.calls.append((tool, args))
        return {"result": "ok"}

    def list_tools(self) -> list[str]:
        return ["browser_automation"]


def test_build_agent_plan_uses_router_json() -> None:
    plan = build_agent_plan(
        "YouTube ac",
        ["browser_automation", "screen_ocr"],
        model_router=_FakeRouter(),
    )

    assert plan.source == "openrouter"
    assert plan.steps[0].tool == "browser_automation"
    assert plan.steps[0].args["url"] == "https://www.youtube.com"


def test_build_agent_plan_local_browser_fallback() -> None:
    plan = build_agent_plan("Opera'dan YouTube ac", ["browser_automation"], model_router=None)

    assert plan.source == "local_heuristic"
    assert plan.steps[0].args["browser"] == "opera"


def test_build_agent_plan_prefers_native_launcher_tools_for_steam() -> None:
    plan = build_agent_plan(
        "Steam uygulamasindan Counter Strike 2 oyununu guncelle",
        [
            "open_app",
            "window_tracking",
            "screen_ocr",
            "ui_automation",
            "steam_click_update_button",
            "browser_automation",
        ],
        model_router=_FakeRouter(),
    )

    assert plan.source == "local_native_app"
    assert plan.steps[0].tool == "open_app"
    assert plan.steps[0].args["app_name"] == "steam"
    assert any(step.tool == "ui_automation" for step in plan.steps)
    assert any(step.tool == "steam_click_update_button" for step in plan.steps)
    assert not any(step.tool == "screen_ocr" for step in plan.steps)
    assert all(step.tool != "browser_automation" for step in plan.steps)


def test_run_agent_plan_sync_dispatches_steps() -> None:
    runtime = _FakeRuntime()
    plan = AgentPlan(
        goal="test",
        steps=[AgentStep(tool="browser_automation", args={"action": "open_url"}, name="open")],
    )

    report = run_agent_plan_sync(plan, runtime)

    assert report["ok"] is True
    assert runtime.calls == [("browser_automation", {"action": "open_url"})]
    assert report["completed"][0]["name"] == "open"


def test_run_agent_plan_sync_stops_on_blocked_step() -> None:
    class _BlockedRuntime:
        async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
            return {"blocked": True, "result": "blocked"}

        def list_tools(self) -> list[str]:
            return ["browser_automation"]

    plan = AgentPlan(goal="test", steps=[AgentStep(tool="browser_automation", on_error="stop")])
    report = run_agent_plan_sync(plan, _BlockedRuntime())

    assert report["ok"] is False
    assert report["failed"][0]["result"]["blocked"] is True
    assert report["replans"] == []


def test_replan_after_failed_click_smart_returns_find_elements() -> None:
    step = AgentStep(
        tool="browser_automation",
        args={"action": "click_smart", "target": "Play"},
    )
    recovery = replan_after_failure(
        goal="Play'e tikla",
        failed_step=step,
        failure={"result": {"result": "bulunamadi"}},
        registered_tools=["browser_automation"],
    )

    assert recovery is not None
    assert recovery.args["action"] == "find_elements"
    assert recovery.args["query"] == "Play"


def test_run_agent_plan_sync_adds_recovery_step_after_failed_result() -> None:
    class _FailThenOkRuntime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def list_tools(self) -> list[str]:
            return ["browser_automation"]

        async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
            self.calls.append((tool, args))
            if len(self.calls) == 1:
                return {"result": "Hedef bulunamadi"}
            return {"result": "ok"}

    runtime = _FailThenOkRuntime()
    plan = AgentPlan(
        goal="Play'e tikla",
        steps=[
            AgentStep(
                tool="browser_automation",
                args={"action": "click_smart", "target": "Play", "engine": "playwright"},
                name="Play tikla",
                on_error="stop",
            )
        ],
    )

    report = run_agent_plan_sync(plan, runtime, enable_replan=True)

    assert report["replans"]
    assert runtime.calls[1][1]["action"] == "find_elements"
    assert report["completed"][0]["name"] == "Hedef icin DOM adaylarini tara"


def test_run_agent_plan_sync_continue_failure_does_not_fail_report() -> None:
    class _Runtime:
        def list_tools(self) -> list[str]:
            return ["ui_automation"]

        async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
            return {"result": '{"ok": false, "error": "gecici ui denemesi basarisiz"}'}

    plan = AgentPlan(
        goal="fallback test",
        steps=[AgentStep(tool="ui_automation", args={"action": "list"}, name="UIA dene", on_error="continue")],
    )

    report = run_agent_plan_sync(plan, _Runtime(), enable_replan=False)

    assert report["ok"] is True
    assert report["failed"][0]["name"] == "UIA dene"


def test_run_agent_plan_sync_verifies_playwright_open_url() -> None:
    class _Runtime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def list_tools(self) -> list[str]:
            return ["browser_automation"]

        async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
            self.calls.append((tool, args))
            if args.get("action") == "verify":
                return {"result": '{"ok": true, "verified": true}'}
            return {"result": '{"ok": true}'}

    runtime = _Runtime()
    plan = AgentPlan(
        goal="YouTube ac",
        steps=[
            AgentStep(
                tool="browser_automation",
                args={"action": "open_url", "engine": "playwright", "url": "https://www.youtube.com"},
                name="YouTube ac",
            )
        ],
    )

    report = run_agent_plan_sync(plan, runtime)

    assert report["ok"] is True
    assert runtime.calls[1][1]["action"] == "verify"
    assert runtime.calls[1][1]["url_contains"] == "www.youtube.com"
    assert report["verifications"][0]["after"] == "YouTube ac"


def test_run_agent_plan_sync_uses_explicit_verify_after() -> None:
    class _Runtime:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def list_tools(self) -> list[str]:
            return ["browser_automation"]

        async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
            self.calls.append((tool, args))
            if args.get("action") == "verify":
                return {"result": '{"ok": true, "verified": true}'}
            return {"result": '{"ok": true}'}

    runtime = _Runtime()
    plan = AgentPlan(
        goal="Play'e tikla",
        steps=[
            AgentStep(
                tool="browser_automation",
                args={
                    "action": "click_smart",
                    "engine": "playwright",
                    "target": "Play",
                    "verify_after": {"text_contains": "Playing"},
                },
                name="Play tikla",
            )
        ],
    )

    report = run_agent_plan_sync(plan, runtime)

    assert report["ok"] is True
    assert runtime.calls[1][1] == {
        "action": "verify",
        "engine": "playwright",
        "text_contains": "Playing",
    }
