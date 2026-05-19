"""Plugin_Host smoke tests for Task 5.1.

Bu testler ``runtime/plugin_host.py``'in temel keşif → yükleme akışının
çalıştığını doğrular. Boundary / property testleri 5.2-5.4 alt
görevlerinde ayrıca yazılır (opsiyonel); buradaki amaç sadece manifesto
loader'ın hayatta olmasını ve temel mutlu yolun bozulmadığını
göstermektir.
"""

# Feature: jarvis-v2-upgrade, Task 5.1 — Plugin_Host manifesto loader smoke

from __future__ import annotations

import importlib
import sys
import textwrap
from pathlib import Path

import pytest

from runtime.plugin_host import PluginHost
from runtime.types import SkillManifest, ToolDescriptor


# ---------------------------------------------------------------------------
# Fixture yardımcıları
# ---------------------------------------------------------------------------


def _write_skill_pkg(
    root: Path,
    *,
    skill_name: str,
    manifest_kind: str,
    tools_module: str | None = None,
    enabled: bool = True,
) -> tuple[Path, str]:
    """Disk üzerinde minimal bir skill paketi oluştur.

    ``tools_module`` verilmemişse, tek ``noop`` tool'u olan basit bir
    ``tools.py`` üretilir. Dönen 2'li: skill klasörü ve test ortamına
    importlanabilir bir entry_module path'i.
    """
    pkg_dir = root / skill_name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    tools_src = tools_module or textwrap.dedent(
        """
        def noop(message: str = "ok") -> str:
            return f"echo: {message}"

        noop.__tool__ = {
            "declaration": {
                "name": "noop",
                "description": "Test echo tool.",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {
                        "message": {"type": "STRING", "description": "echo me"},
                    },
                },
            },
            "execution_mode": "inline",
        }
        """
    ).strip() + "\n"
    (pkg_dir / "tools.py").write_text(tools_src, encoding="utf-8")

    entry_module = f"{skill_name}.tools"

    if manifest_kind == "yaml":
        yaml_text = textwrap.dedent(
            f"""
            name: {skill_name}
            version: "1.0.0"
            enabled: {str(enabled).lower()}
            entry_module: {entry_module}
            tools:
              - noop
            description: Test skill
            """
        ).strip() + "\n"
        (pkg_dir / "skill.yaml").write_text(yaml_text, encoding="utf-8")
    elif manifest_kind == "py":
        py_text = textwrap.dedent(
            f"""
            from runtime.types import SkillManifest

            MANIFEST = SkillManifest(
                name="{skill_name}",
                version="1.0.0",
                enabled={enabled!r},
                entry_module="{entry_module}",
                tools=["noop"],
                description="Test skill",
            )
            """
        ).strip() + "\n"
        (pkg_dir / "__skill__.py").write_text(py_text, encoding="utf-8")
    else:
        raise ValueError(manifest_kind)

    return pkg_dir, entry_module


@pytest.fixture
def skills_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp_path altında ``skills/`` kökü; sys.path'e eklenir, sonra temizlenir."""
    skills_root = tmp_path / "skills_root"
    skills_root.mkdir()
    monkeypatch.syspath_prepend(str(skills_root))
    yield skills_root
    # Test'in load ettiği modülleri sys.modules'tan temizle ki sonraki
    # testler taze import yapabilsin.
    for mod in list(sys.modules):
        if mod.startswith(("test_skill_", "_jarvis_skill_manifest_")):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_yaml_manifest(skills_workspace: Path) -> None:
    """skill.yaml içeren bir paket discover tarafından bulunur."""
    _write_skill_pkg(skills_workspace, skill_name="test_skill_yaml", manifest_kind="yaml")

    host = PluginHost()
    manifests = host.discover([skills_workspace])

    assert len(manifests) == 1
    assert manifests[0].name == "test_skill_yaml"
    assert manifests[0].entry_module == "test_skill_yaml.tools"
    assert manifests[0].tools == ["noop"]


def test_discover_py_manifest(skills_workspace: Path) -> None:
    """__skill__.py içeren bir paket discover tarafından bulunur."""
    _write_skill_pkg(skills_workspace, skill_name="test_skill_py", manifest_kind="py")

    host = PluginHost()
    manifests = host.discover([skills_workspace])

    assert len(manifests) == 1
    assert manifests[0].name == "test_skill_py"
    assert isinstance(manifests[0], SkillManifest)


def test_discover_skips_dirs_without_manifest(skills_workspace: Path) -> None:
    """Manifesto içermeyen klasörler sessizce atlanır."""
    (skills_workspace / "not_a_skill").mkdir()
    (skills_workspace / "not_a_skill" / "random.txt").write_text("nope")

    host = PluginHost()
    manifests = host.discover([skills_workspace])

    assert manifests == []


def test_discover_skips_invalid_manifest(
    skills_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Eksik alan içeren manifesto atlanır ve sebep log'a yazılır (Req 16.4)."""
    pkg = skills_workspace / "broken_skill"
    pkg.mkdir()
    (pkg / "skill.yaml").write_text(
        "name: broken_skill\nversion: '1.0.0'\n", encoding="utf-8"
    )

    host = PluginHost()
    with caplog.at_level("ERROR", logger="runtime.plugin_host"):
        manifests = host.discover([skills_workspace])

    assert manifests == []
    assert any("invalid" in rec.message for rec in caplog.records)


def test_discover_returns_disabled_manifests_too(skills_workspace: Path) -> None:
    """enabled=False manifesto liste içinde döner; filtreleme load sırasında olur."""
    _write_skill_pkg(
        skills_workspace,
        skill_name="test_skill_disabled",
        manifest_kind="yaml",
        enabled=False,
    )

    host = PluginHost()
    manifests = host.discover([skills_workspace])

    assert len(manifests) == 1
    assert manifests[0].enabled is False


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_returns_descriptor_for_valid_tool(skills_workspace: Path) -> None:
    """Geçerli bir manifesto yüklendiğinde tek ToolDescriptor döner."""
    _write_skill_pkg(skills_workspace, skill_name="test_skill_load", manifest_kind="yaml")

    host = PluginHost()
    manifest = host.discover([skills_workspace])[0]
    descriptors = host.load(manifest)

    assert len(descriptors) == 1
    desc = descriptors[0]
    assert isinstance(desc, ToolDescriptor)
    assert desc.name == "noop"
    assert desc.execution_mode == "inline"
    assert desc.skill_id == "test_skill_load"
    assert desc.handler(message="hello") == "echo: hello"


def test_load_skips_when_enabled_false(skills_workspace: Path) -> None:
    """enabled=False olan manifest yüklenmez (Req 16.3)."""
    _write_skill_pkg(
        skills_workspace,
        skill_name="test_skill_off",
        manifest_kind="yaml",
        enabled=False,
    )

    host = PluginHost()
    manifest = host.discover([skills_workspace])[0]
    descriptors = host.load(manifest)

    assert descriptors == []


def test_load_skips_disabled_skill_via_config(skills_workspace: Path) -> None:
    """app_config.disabled_skills'te listelenen skill yüklenmez (Req 18.3)."""
    _write_skill_pkg(skills_workspace, skill_name="test_skill_cfgoff", manifest_kind="yaml")

    host = PluginHost(disabled_skills={"test_skill_cfgoff"})
    manifest = host.discover([skills_workspace])[0]
    descriptors = host.load(manifest)

    assert descriptors == []


def test_load_skips_invalid_declaration(
    skills_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Bozuk declaration tool'u atlanır, diğerleri etkilenmez (Req 17.2)."""
    bad_module = textwrap.dedent(
        """
        def good():
            return "ok"
        good.__tool__ = {
            "declaration": {"name": "good"},
            "execution_mode": "inline",
        }

        def bad():
            return "fail"
        bad.__tool__ = {
            "declaration": {
                "name": "bad",
                "parameters": {
                    "type": "OBJECT",
                    "properties": {"x": {"type": "WIDGET"}},
                },
            },
            "execution_mode": "inline",
        }
        """
    ).strip() + "\n"

    pkg_dir = skills_workspace / "test_skill_mixed"
    pkg_dir.mkdir()
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    (pkg_dir / "tools.py").write_text(bad_module, encoding="utf-8")
    (pkg_dir / "skill.yaml").write_text(
        textwrap.dedent(
            """
            name: test_skill_mixed
            version: "1.0.0"
            entry_module: test_skill_mixed.tools
            tools:
              - good
              - bad
            """
        ).strip() + "\n",
        encoding="utf-8",
    )

    host = PluginHost()
    manifest = host.discover([skills_workspace])[0]
    with caplog.at_level("ERROR", logger="runtime.plugin_host"):
        descriptors = host.load(manifest)

    names = [d.name for d in descriptors]
    assert "good" in names
    assert "bad" not in names
    assert any("invalid" in rec.message.lower() for rec in caplog.records)


def test_load_handles_import_failure(
    skills_workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """entry_module import edilemezse boş liste döner ve log'lanır."""
    pkg = skills_workspace / "test_skill_brokenimport"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "tools.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    (pkg / "skill.yaml").write_text(
        textwrap.dedent(
            """
            name: test_skill_brokenimport
            version: "1.0.0"
            entry_module: test_skill_brokenimport.tools
            tools:
              - noop
            """
        ).strip() + "\n",
        encoding="utf-8",
    )

    host = PluginHost()
    manifest = host.discover([skills_workspace])[0]
    with caplog.at_level("ERROR", logger="runtime.plugin_host"):
        descriptors = host.load(manifest)

    assert descriptors == []
    assert any("failed to import" in rec.message for rec in caplog.records)
