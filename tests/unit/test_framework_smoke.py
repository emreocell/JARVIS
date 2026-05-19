"""Test çatısı smoke testleri.

Bu testler herhangi bir JARVIS bileşenini test etmez; yalnızca
`tests/conftest.py` ve `pytest.ini` yapılandırmasının doğru kurulduğunu
doğrular. Sonraki görevlerde gerçek bileşen testleri buradan referansla
fixture'ları kullanır.
"""

# Feature: jarvis-v2-upgrade, Test çatısı smoke testleri (Task 1.3)

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st


def test_repo_root_resolves_to_workspace(repo_root: Path) -> None:
    """`repo_root` fixture'ı workspace kökünü işaret eder."""
    assert repo_root.is_dir()
    assert (repo_root / "tests").is_dir()


def test_fixtures_dir_is_under_tests(fixtures_dir: Path, repo_root: Path) -> None:
    """`fixtures_dir` tests/fixtures konumundadır."""
    assert fixtures_dir == repo_root / "tests" / "fixtures"


def test_temp_logs_dir_layout(temp_logs_dir: Path) -> None:
    """`temp_logs_dir` conversation ve debug alt klasörlerini sağlar."""
    assert (temp_logs_dir / "logs" / "conversation").is_dir()
    assert (temp_logs_dir / "logs" / "debug").is_dir()


def test_mock_voice_core_has_expected_interface(mock_voice_core) -> None:
    """Voice_Core mock'u proaktif anlatım için gereken metodları taşır."""
    assert mock_voice_core.state == "LISTENING"
    mock_voice_core.send_system_message("hello")
    mock_voice_core.send_system_message.assert_called_once_with("hello")


def test_mock_privacy_default_inactive(mock_privacy_mode) -> None:
    """Privacy_Mode mock'u varsayılan olarak inactive."""
    assert mock_privacy_mode.is_active() is False


def test_make_background_task_factory_increments(make_background_task) -> None:
    """Factory ardışık çağrılarda farklı id üretir ve override edilebilir."""
    a = make_background_task(name="alpha")
    b = make_background_task(name="beta", state="running")
    assert a.id != b.id
    assert a.name == "alpha"
    assert b.state == "running"


def test_make_background_task_rejects_unknown_kwargs(make_background_task) -> None:
    """Bilinmeyen alan TypeError fırlatır."""
    with pytest.raises(TypeError):
        make_background_task(unknown_field="oops")


def test_mock_clock_advance(mock_clock) -> None:
    """Mock clock zamanı ileri alabilir."""
    start = mock_clock()
    mock_clock.advance(60.0)
    assert mock_clock() == start + 60.0


def test_freezer_freezes_time(freezer) -> None:
    """`freezer` fixture'ı freezegun'ın freeze_time fonksiyonunu döner."""
    with freezer("2025-01-01 00:00:00"):
        from datetime import datetime

        assert datetime.now().year == 2025
        assert datetime.now().month == 1
        assert datetime.now().day == 1


def test_isolated_env_sets_test_mode(isolated_env, monkeypatch) -> None:
    """isolated_env JARVIS_TEST_MODE=1 set eder."""
    import os

    assert os.environ.get("JARVIS_TEST_MODE") == "1"
    assert isolated_env.is_dir()


def test_hypothesis_default_profile_is_active() -> None:
    """pytest.ini --hypothesis-profile=default ile çalıştırır; max_examples=100."""
    current = settings()
    assert current.max_examples == 100


@settings(max_examples=10)  # smoke için 10 yeterli; gerçek property testler 100+
@given(st.integers(min_value=0, max_value=1000))
def test_hypothesis_can_run_example(n: int) -> None:
    """Hypothesis property testi koşabiliyor."""
    assert n >= 0


@pytest.mark.unit
def test_unit_marker_registered() -> None:
    """`unit` marker pytest.ini'de tanımlı."""
    # Marker eksik olsaydı --strict-markers altında collection hatası olurdu.
    assert True


@pytest.mark.property
def test_property_marker_registered() -> None:
    """`property` marker pytest.ini'de tanımlı."""
    assert True


@pytest.mark.integration
def test_integration_marker_registered() -> None:
    """`integration` marker pytest.ini'de tanımlı."""
    assert True
