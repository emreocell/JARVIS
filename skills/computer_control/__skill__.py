"""Computer control skill manifest."""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="computer_control",
    version="0.1.0",
    enabled=True,
    entry_module="skills.computer_control.tools",
    tools=[
        "mouse_control",
        "screen_ocr",
        "detect_screen_elements",
        "window_tracking",
        "ui_automation",
        "steam_click_update_button",
        "browser_automation",
        "self_healing_click",
        "selector_memory",
    ],
    description=(
        "Mouse/keyboard control, OCR, screen element detection, browser "
        "automation, Windows UI Automation, multi-monitor window tracking "
        "and self-healing selector memory."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
