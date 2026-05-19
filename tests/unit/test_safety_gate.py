from __future__ import annotations

import asyncio

from runtime.safety_gate import evaluate_tool_call, is_potentially_risky
from runtime.task_manager import TaskManager
from runtime.tool_runtime import ToolRuntime
from runtime.types import ToolDescriptor


def test_safety_gate_allows_plain_click() -> None:
    assert not is_potentially_risky("self_healing_click", {"target": "Play"})
    result = evaluate_tool_call("self_healing_click", {"target": "Play"})
    assert result["decision"] == "continue"


def test_safety_gate_blocks_delete_target_without_confirmation() -> None:
    assert is_potentially_risky("self_healing_click", {"target": "delete account"})
    result = evaluate_tool_call("self_healing_click", {"target": "delete account"})
    assert result["decision"] == "ask_user"
    assert result["risk"] == "high"


def test_safety_gate_allows_explicit_confirmation() -> None:
    result = evaluate_tool_call(
        "self_healing_click",
        {"target": "delete account", "confirmed": True},
    )
    assert result["decision"] == "continue"


def test_tool_runtime_blocks_risky_inline_tool() -> None:
    called = False

    def _handler(**_kwargs):
        nonlocal called
        called = True
        return "clicked"

    tm = TaskManager()
    runtime = ToolRuntime(tm)
    runtime.register(
        ToolDescriptor(
            name="self_healing_click",
            declaration={"name": "self_healing_click"},
            handler=_handler,
            execution_mode="inline",
            skill_id="test",
        )
    )

    try:
        payload = asyncio.run(runtime.dispatch("self_healing_click", {"target": "delete"}))
    finally:
        tm.shutdown(wait=True, cancel_pending=True)

    assert payload["blocked"] is True
    assert called is False


def test_tool_runtime_allows_confirmed_risky_inline_tool() -> None:
    tm = TaskManager()
    runtime = ToolRuntime(tm)
    runtime.register(
        ToolDescriptor(
            name="self_healing_click",
            declaration={"name": "self_healing_click"},
            handler=lambda **_kwargs: "clicked",
            execution_mode="inline",
            skill_id="test",
        )
    )

    try:
        payload = asyncio.run(
            runtime.dispatch("self_healing_click", {"target": "delete", "confirmed": True})
        )
    finally:
        tm.shutdown(wait=True, cancel_pending=True)

    assert payload["result"] == "clicked"
