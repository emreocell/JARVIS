"""Plugin_Host NVIDIA anahtar gating testleri — Task 8.2.

NVIDIA anahtarı boş olduğunda NVIDIA bağımlı skill'lerin yüklenmediğini,
Gemini-only skill'lerin etkilenmediğini ve tek bir Türkçe uyarı log
mesajının üretildiğini doğrular.

Requirements: 17.1, 17.4
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from runtime.plugin_host import PluginHost, _NVIDIA_DEPENDENT_SKILLS
from runtime.types import SkillManifest


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------


def _write_py_skill(
    root: Path,
    *,
    skill_name: str,
    enabled: bool = True,
) -> Path:
    """Disk üzerinde minimal bir __skill__.py tabanlı skill paketi oluştur."""
    pkg_dir = root / skill_name
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    tools_src = textwrap.dedent(
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

    return pkg_dir


@pytest.fixture
def skills_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """tmp_path altında skills kökü; sys.path'e eklenir, sonra temizlenir."""
    skills_root = tmp_path / "skills_root"
    skills_root.mkdir()
    monkeypatch.syspath_prepend(str(skills_root))
    yield skills_root
    for mod in list(sys.modules):
        if mod.startswith(("memory_rag", "doc_intel", "reasoning", "translate",
                           "safety", "creative", "image_search", "embodied",
                           "audio_structured", "gemini_skill",
                           "_jarvis_skill_manifest_")):
            sys.modules.pop(mod, None)


# ---------------------------------------------------------------------------
# Testler
# ---------------------------------------------------------------------------


def test_nvidia_dependent_skills_set_contains_expected_names() -> None:
    """_NVIDIA_DEPENDENT_SKILLS kümesi beklenen 9 skill adını içerir."""
    expected = {
        "memory_rag", "doc_intel", "reasoning", "translate", "safety",
        "creative", "image_search", "embodied", "audio_structured",
    }
    assert expected == _NVIDIA_DEPENDENT_SKILLS


def test_nvidia_skill_not_loaded_when_key_empty(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NVIDIA anahtarı boşken NVIDIA bağımlı skill yüklenmez."""
    # memory_rag NVIDIA bağımlı bir skill
    _write_py_skill(skills_workspace, skill_name="memory_rag")

    # has_nvidia_api_key() → False döndür
    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: False)

    host = PluginHost()
    manifests = host.discover([skills_workspace])
    assert len(manifests) == 1

    with caplog.at_level("WARNING", logger="runtime.plugin_host"):
        descriptors = host.load(manifests[0])

    assert descriptors == []


def test_nvidia_skill_loaded_when_key_present(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NVIDIA anahtarı mevcutken NVIDIA bağımlı skill yüklenir."""
    _write_py_skill(skills_workspace, skill_name="memory_rag")

    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: True)

    host = PluginHost()
    manifests = host.discover([skills_workspace])
    assert len(manifests) == 1

    descriptors = host.load(manifests[0])
    assert len(descriptors) == 1
    assert descriptors[0].name == "noop"


def test_nvidia_api_key_requires_entry_is_config_sentinel() -> None:
    """``nvidia_api_key`` requires degeri import edilebilir paket gibi aranmaz."""
    manifest = SkillManifest(
        name="memory_rag",
        version="1.0.0",
        enabled=True,
        entry_module="memory_rag.tools",
        tools=[],
        requires=["nvidia_api_key"],
        description="Test skill",
    )

    assert PluginHost._find_missing_requires(manifest) == []


def test_gemini_only_skill_unaffected_when_nvidia_key_empty(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NVIDIA anahtarı boşken Gemini-only skill'ler etkilenmez."""
    # "gemini_skill" NVIDIA bağımlı değil
    _write_py_skill(skills_workspace, skill_name="gemini_skill")

    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: False)

    host = PluginHost()
    manifests = host.discover([skills_workspace])
    assert len(manifests) == 1

    descriptors = host.load(manifests[0])
    # Gemini-only skill yüklenmeli
    assert len(descriptors) == 1
    assert descriptors[0].name == "noop"


def test_warning_logged_once_for_multiple_nvidia_skills(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Birden fazla NVIDIA skill yüklenmeye çalışıldığında uyarı yalnızca bir kez log'lanır."""
    _write_py_skill(skills_workspace, skill_name="memory_rag")
    _write_py_skill(skills_workspace, skill_name="doc_intel")
    _write_py_skill(skills_workspace, skill_name="reasoning")

    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: False)

    host = PluginHost()
    manifests = host.discover([skills_workspace])
    assert len(manifests) == 3

    with caplog.at_level("WARNING", logger="runtime.plugin_host"):
        for manifest in manifests:
            host.load(manifest)

    # Uyarı mesajı yalnızca bir kez log'lanmalı
    warning_records = [
        r for r in caplog.records
        if "NVIDIA anahtarı bulunamadığı" in r.message
    ]
    assert len(warning_records) == 1


def test_warning_message_contains_expected_skill_names(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Uyarı mesajı beklenen skill adlarını içerir."""
    _write_py_skill(skills_workspace, skill_name="safety")

    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: False)

    host = PluginHost()
    manifests = host.discover([skills_workspace])

    with caplog.at_level("WARNING", logger="runtime.plugin_host"):
        host.load(manifests[0])

    warning_msgs = [
        r.message for r in caplog.records
        if "NVIDIA anahtarı bulunamadığı" in r.message
    ]
    assert len(warning_msgs) == 1
    msg = warning_msgs[0]
    # Mesaj beklenen skill adlarını içermeli
    for skill in ("memory_rag", "doc_intel", "reasoning", "translate",
                  "safety", "creative", "image_search", "embodied"):
        assert skill in msg, f"'{skill}' uyarı mesajında bulunamadı: {msg!r}"


def test_all_nvidia_skills_blocked_when_key_empty(
    skills_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NVIDIA anahtarı boşken tüm 9 NVIDIA bağımlı skill yüklenmez."""
    for skill_name in _NVIDIA_DEPENDENT_SKILLS:
        _write_py_skill(skills_workspace, skill_name=skill_name)

    monkeypatch.setattr("app_config.has_nvidia_api_key", lambda: False)

    host = PluginHost()
    manifests = host.discover([skills_workspace])
    assert len(manifests) == len(_NVIDIA_DEPENDENT_SKILLS)

    all_descriptors = []
    for manifest in manifests:
        all_descriptors.extend(host.load(manifest))

    assert all_descriptors == [], (
        f"NVIDIA anahtarı yokken hiçbir NVIDIA skill yüklenmemeli; "
        f"yüklenenler: {[d.name for d in all_descriptors]}"
    )
