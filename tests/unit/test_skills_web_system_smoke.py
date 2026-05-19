"""Smoke tests for task 5.10 — actions/browser.py and actions/open_app.py
have been migrated to skills/web/ and skills/system/ with backwards-compat
shims left in actions/.

These tests verify only the structural / wiring side of the migration:

- Each new skill manifest is well-formed and discoverable by Plugin_Host.
- Each migrated handler still carries its ``__tool__`` metadata (Gemini
  declaration + ``execution_mode``).
- The legacy ``actions/browser.py`` and ``actions/open_app.py`` shims
  re-export the same callable objects as the new canonical modules
  (``is`` identity check), so existing imports keep working unchanged
  during the v1 -> v2 transition.

We deliberately avoid driving real browser / process launches here —
those side effects are covered by ad-hoc manual testing during the
v2 rollout.
"""

# Feature: jarvis-v2-upgrade, Task 5.10 — browser/open_app migration smoke

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


def test_web_manifest_publishes_browser_control() -> None:
    from skills.web import __skill__ as web_skill

    manifest = web_skill.MANIFEST
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == "web"
    assert manifest.enabled is True
    assert manifest.entry_module == "skills.web.tools"
    assert "browser_control" in manifest.tools


def test_system_manifest_publishes_open_app() -> None:
    from skills.system import __skill__ as system_skill

    manifest = system_skill.MANIFEST
    assert isinstance(manifest, SkillManifest)
    assert manifest.name == "system"
    assert manifest.enabled is True
    assert manifest.entry_module == "skills.system.tools"
    assert "open_app" in manifest.tools


# ---------------------------------------------------------------------------
# Tool metadata (declaration + execution_mode)
# ---------------------------------------------------------------------------


def test_browser_control_tool_metadata() -> None:
    from skills.web.tools import browser_control

    meta = getattr(browser_control, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "browser_control"
    assert decl["parameters"]["type"] == "OBJECT"
    assert "action" in decl["parameters"]["properties"]
    assert decl["parameters"]["required"] == ["action"]
    assert meta["execution_mode"] == "inline"


def test_open_app_tool_metadata() -> None:
    from skills.system.tools import open_app

    meta = getattr(open_app, "__tool__")
    decl = meta["declaration"]
    assert decl["name"] == "open_app"
    assert decl["parameters"]["type"] == "OBJECT"
    assert "app_name" in decl["parameters"]["properties"]
    assert decl["parameters"]["required"] == ["app_name"]
    assert meta["execution_mode"] == "inline"


# ---------------------------------------------------------------------------
# Backwards-compat shims (actions/* re-exports)
# ---------------------------------------------------------------------------


def test_actions_browser_shim_reexports_canonical_handler() -> None:
    from actions.browser import browser_control as shimmed
    from skills.web.tools import browser_control as canonical

    # Same callable object — no copy, no wrapper.
    assert shimmed is canonical


def test_actions_open_app_shim_reexports_canonical_handler() -> None:
    from actions.open_app import open_app as shimmed
    from skills.system.tools import open_app as canonical

    assert shimmed is canonical


def test_actions_open_app_shim_exposes_aliases_table() -> None:
    """Existing call sites read APP_ALIASES from the legacy module."""
    from actions.open_app import APP_ALIASES as shimmed
    from skills.system.tools import APP_ALIASES as canonical

    assert shimmed is canonical
    # Spot-check a few alias mappings to guard against accidental loss
    # during the migration.
    assert shimmed["chrome"] == "chrome"
    assert shimmed["takvim"] == "outlook"
    assert shimmed["dosya gezgini"] == "explorer"
    assert shimmed["steam"] == "steam://open/main"
    assert shimmed["epic games launcher"] == "EpicGamesLauncher"


# ---------------------------------------------------------------------------
# Plugin_Host can load both skills end-to-end
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("skill_name,tool_name", [("web", "browser_control"),
                                                  ("system", "open_app")])
def test_plugin_host_loads_skill(skill_name: str, tool_name: str) -> None:
    """Plugin_Host discover() + load() returns a usable ToolDescriptor."""
    host = PluginHost()
    manifests = host.discover([SKILLS_ROOT])
    by_name = {m.name: m for m in manifests}
    assert skill_name in by_name, f"{skill_name} skill not discovered"

    descriptors = host.load(by_name[skill_name])
    names = {d.name for d in descriptors}
    assert tool_name in names

    desc = next(d for d in descriptors if d.name == tool_name)
    assert isinstance(desc, ToolDescriptor)
    assert desc.execution_mode == "inline"
    assert desc.skill_id == skill_name
    assert callable(desc.handler)
