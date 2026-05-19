"""Smoke tests for task 5.7 — actions/calendar.py, actions/reminders.py and
actions/weather.py have been migrated to skills/productivity/ with
backwards-compat shims left in actions/.

These tests verify only the structural / wiring side of the migration:

- The new skill manifest is well-formed and discoverable by Plugin_Host.
- Each migrated handler still carries its ``__tool__`` metadata (Gemini
  declaration + ``execution_mode``).
- The legacy ``actions/calendar.py``, ``actions/reminders.py`` and
  ``actions/weather.py`` shims re-export the same callable objects as
  the new canonical module (``is`` identity check), so existing imports
  in ``main.py`` and ``ui.py`` keep working unchanged during the
  v1 -> v2 transition.

We deliberately avoid driving real Outlook COM / network calls here —
those side effects are covered by ad-hoc manual testing during the
v2 rollout.
"""

# Feature: jarvis-v2-upgrade, Task 5.7 — calendar/reminders/weather migration smoke

from __future__ import annotations

from pathlib import Path

import pytest

from runtime.plugin_host import PluginHost
from runtime.types import SkillManifest, ToolDescriptor


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS_ROOT = REPO_ROOT / "skills"


# ---------------------------------------------------------------------------
# Manifest shape
# ---------------------------------------------------------------------------


def test_productivity_manifest_publishes_expected_tools() -> None:
    from skills.productivity import __skill__ as productivity_skill

    manifest = productivity_skill.MANIFEST
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == "productivity"
    assert manifest.enabled is True
    assert manifest.entry_module == "skills.productivity.tools"

    expected = {
        "get_calendar_events",
        "add_calendar_event",
        "delete_calendar_event",
        "get_reminders",
        "add_reminder",
        "get_weather_summary",
    }
    assert expected.issubset(set(manifest.tools))


# ---------------------------------------------------------------------------
# Tool metadata (declaration + execution_mode)
# ---------------------------------------------------------------------------


def test_get_calendar_events_tool_metadata() -> None:
    from skills.productivity.tools import get_calendar_events

    meta = getattr(get_calendar_events, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "get_calendar_events"
    assert decl["parameters"]["type"] == "OBJECT"
    assert decl["parameters"]["required"] == ["query"]
    assert "query" in decl["parameters"]["properties"]
    assert "limit" in decl["parameters"]["properties"]
    assert meta["execution_mode"] == "inline"


def test_add_calendar_event_tool_metadata() -> None:
    from skills.productivity.tools import add_calendar_event

    meta = getattr(add_calendar_event, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "add_calendar_event"
    assert decl["parameters"]["type"] == "OBJECT"
    assert decl["parameters"]["required"] == ["title", "start_iso"]
    assert meta["execution_mode"] == "inline"


def test_delete_calendar_event_tool_metadata() -> None:
    from skills.productivity.tools import delete_calendar_event

    meta = getattr(delete_calendar_event, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "delete_calendar_event"
    assert decl["parameters"]["required"] == ["title"]
    assert meta["execution_mode"] == "inline"


def test_get_reminders_tool_metadata() -> None:
    from skills.productivity.tools import get_reminders

    meta = getattr(get_reminders, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "get_reminders"
    assert decl["parameters"]["required"] == ["query"]
    assert meta["execution_mode"] == "inline"


def test_add_reminder_tool_metadata() -> None:
    from skills.productivity.tools import add_reminder

    meta = getattr(add_reminder, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "add_reminder"
    assert decl["parameters"]["required"] == ["title"]
    assert meta["execution_mode"] == "inline"


def test_get_weather_summary_tool_metadata() -> None:
    from skills.productivity.tools import get_weather_summary

    meta = getattr(get_weather_summary, "__tool__")
    decl = meta["declaration"]
    # Tool name in main.py's TOOL_DECLARATIONS was "get_weather"; we
    # preserve that public-facing name even though the handler symbol
    # is `get_weather_summary` for backwards compatibility with
    # ``ui.py`` and ``main.py`` import sites.
    assert decl["name"] == "get_weather"
    assert decl["parameters"]["type"] == "OBJECT"
    assert "location" in decl["parameters"]["properties"]
    assert meta["execution_mode"] == "inline"


# ---------------------------------------------------------------------------
# Backwards-compat shims (actions/* re-exports)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shim_path,canonical_path,symbol",
    [
        ("actions.calendar", "skills.productivity.tools", "get_calendar_events"),
        ("actions.calendar", "skills.productivity.tools", "add_calendar_event"),
        ("actions.calendar", "skills.productivity.tools", "delete_calendar_event"),
        ("actions.reminders", "skills.productivity.tools", "get_reminders"),
        ("actions.reminders", "skills.productivity.tools", "add_reminder"),
        ("actions.weather", "skills.productivity.tools", "get_weather_summary"),
    ],
)
def test_actions_shim_reexports_canonical_handler(
    shim_path: str, canonical_path: str, symbol: str
) -> None:
    """Each legacy ``actions.*`` symbol must be the same callable as the
    canonical productivity skill handler — no wrappers, no copies."""
    import importlib

    shim_module = importlib.import_module(shim_path)
    canonical_module = importlib.import_module(canonical_path)

    shimmed = getattr(shim_module, symbol)
    canonical = getattr(canonical_module, symbol)
    assert shimmed is canonical


# ---------------------------------------------------------------------------
# Plugin_Host can load the productivity skill end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_calendar_events",
        "add_calendar_event",
        "delete_calendar_event",
        "get_reminders",
        "add_reminder",
        "get_weather_summary",
    ],
)
def test_plugin_host_loads_productivity_skill(tool_name: str) -> None:
    """Plugin_Host discover() + load() returns usable ToolDescriptors for
    every productivity tool."""
    host = PluginHost()
    manifests = host.discover([SKILLS_ROOT])
    by_name = {m.name: m for m in manifests}
    assert "productivity" in by_name, "productivity skill not discovered"

    descriptors = host.load(by_name["productivity"])
    names = {d.name for d in descriptors}
    # Note: `get_weather_summary` is published under the Gemini tool
    # name `get_weather` (see _tool_metadata above), so we look up the
    # descriptor by the declaration name when needed.
    expected_decl_name = (
        "get_weather" if tool_name == "get_weather_summary" else tool_name
    )
    assert expected_decl_name in names

    desc = next(d for d in descriptors if d.name == expected_decl_name)
    assert isinstance(desc, ToolDescriptor)
    assert desc.execution_mode == "inline"
    assert desc.skill_id == "productivity"
    assert callable(desc.handler)
