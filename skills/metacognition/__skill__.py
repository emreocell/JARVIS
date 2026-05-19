"""Metacognition skill manifest."""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="metacognition",
    version="0.1.0",
    enabled=True,
    entry_module="skills.metacognition.tools",
    tools=[
        "classify_intent_fast",
        "self_evaluate_action",
    ],
    description=(
        "Groq destekli dusuk gecikmeli intent siniflandirma ve "
        "AI self-evaluation araclari."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
