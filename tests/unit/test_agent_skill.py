from __future__ import annotations

import json

from runtime.plugin_host import PluginHost
from runtime.task_manager import TaskManager
from runtime.tool_runtime import ToolRuntime
from runtime.types import ToolDescriptor
from skills.agent.__skill__ import MANIFEST
from skills.agent import tools


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def list_tools(self) -> list[str]:
        return ["browser_automation", "agent_plan", "agent_execute"]

    async def dispatch(self, tool: str, args: dict):  # noqa: ANN201
        self.calls.append((tool, args))
        return {"result": "ok"}


def test_agent_manifest_loads() -> None:
    descriptors = PluginHost().load(MANIFEST)
    names = {item.name for item in descriptors}
    assert {"agent_plan", "agent_execute", "agent_status"}.issubset(names)


def test_agent_plan_returns_json() -> None:
    result = json.loads(tools.agent_plan("YouTube ac", tool_runtime=_Runtime()))
    assert result["ok"] is True
    assert result["steps"][0]["tool"] == "browser_automation"


def test_agent_execute_dry_run() -> None:
    runtime = _Runtime()
    result = json.loads(tools.agent_execute("YouTube ac", dry_run=True, tool_runtime=runtime, enable_replan=True))
    assert result["ok"] is True
    assert result["dry_run"] is True
    assert runtime.calls == []


def test_agent_execute_accepts_replan_options() -> None:
    runtime = _Runtime()
    plan_json = json.dumps(
        {
            "goal": "test",
            "steps": [
                {
                    "tool": "browser_automation",
                    "args": {"action": "open_url", "url": "https://example.com"},
                    "name": "open",
                }
            ],
        }
    )
    result = json.loads(
        tools.agent_execute(
            plan_json=plan_json,
            tool_runtime=runtime,
            enable_replan=False,
            max_replans=0,
        )
    )
    assert result["ok"] is True
    assert result["replans"] == []


def test_tool_runtime_injects_itself_into_agent_tool() -> None:
    import asyncio

    task_manager = TaskManager()
    runtime = ToolRuntime(task_manager)
    runtime.register(
        ToolDescriptor(
            name="browser_automation",
            declaration={"name": "browser_automation", "parameters": {"type": "OBJECT", "properties": {}}},
            handler=lambda **_kwargs: "ok",
            execution_mode="inline",
            skill_id="fake",
        )
    )
    for desc in PluginHost().load(MANIFEST):
        runtime.register(desc)

    result = asyncio.run(runtime.dispatch("agent_plan", {"goal": "YouTube ac"}))
    payload = json.loads(result["result"])

    assert payload["ok"] is True
    assert payload["steps"][0]["tool"] == "browser_automation"
