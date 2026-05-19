from __future__ import annotations

import json

from runtime.agent_visibility import (
    format_agent_plan,
    format_agent_report,
    format_browser_timeline,
    format_tool_visibility,
)


def test_format_agent_plan() -> None:
    text = format_agent_plan(
        json.dumps(
            {
                "name": "browser_task",
                "source": "openrouter",
                "steps": [
                    {"tool": "browser_automation", "name": "YouTube ac"},
                    {"tool": "browser_automation", "name": "Video ara"},
                ],
            }
        )
    )

    assert "AGENT PLAN" in text
    assert "YouTube ac" in text


def test_format_agent_report_includes_recovery() -> None:
    text = format_agent_report(
        {
            "ok": False,
            "name": "task",
            "completed": [{"name": "Acilis"}],
            "failed": [{"name": "Tikla", "error": "bulunamadi"}],
            "replans": [{"step": {"name": "Timeline oku", "tool": "browser_automation"}}],
        }
    )

    assert "AGENT EXECUTE" in text
    assert "recovery" in text
    assert "Timeline oku" in text


def test_format_browser_timeline() -> None:
    text = format_browser_timeline(
        {
            "items": [
                {"ok": True, "action": "open_url", "url": "https://example.com"},
                {"ok": False, "action": "click_smart", "target": "Play", "strategy": "text"},
            ]
        }
    )

    assert "BROWSER TIMELINE" in text
    assert "click_smart" in text
    assert "Play" in text


def test_format_tool_visibility_dispatches_by_tool() -> None:
    result = json.dumps({"items": [{"ok": True, "action": "open_url", "url": "https://example.com"}]})
    text = format_tool_visibility("browser_automation", {"action": "timeline"}, result)

    assert text.startswith("BROWSER TIMELINE")
