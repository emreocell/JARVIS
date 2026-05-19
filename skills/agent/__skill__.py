"""Autonomous planner/executor skill manifest."""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="agent",
    version="0.1.0",
    enabled=True,
    entry_module="skills.agent.tools",
    tools=[
        "agent_plan",
        "agent_execute",
        "agent_status",
    ],
    description="Autonomous task planning and step-by-step tool execution.",
    requires=[],
)


__all__ = ["MANIFEST"]
