"""
JARVIS v2 — global pytest conftest.

Bu dosya tüm tests/ alt klasörlerinde geçerli ortak fixture'ları, Hypothesis
profillerini ve mock factory'leri sağlar.

Tasarım notları:
- Hypothesis profilleri burada bir kez kayıt edilir; pytest.ini "default"
  profilini seçer. "fast" ve "thorough" override için CLI'dan kullanılabilir.
- Tk root fixture'ı session ölçekli ve başsız (withdrawn) çalışır; her test
  kendi ihtiyacı kadar Toplevel oluşturur. Display olmayan ortamlarda (CI
  Linux runner) test otomatik skip edilir.
- freezegun yardımcıları zaman duyarlı testler (30 dk penceresi, UAC rate
  limit, log rotation) için tek noktadan importlanabilir.
- Mock factory'ler tekrar eden mock kurulumunu (Voice_Core, Task_Manager,
  Privacy_Mode, vb.) küçültür. Asıl bileşenler henüz yazılmadığı için
  factory'ler hafif, davranış tabanlı protokol mock'ları döndürür.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, Verbosity, settings


# ---------------------------------------------------------------------------
# Hypothesis profilleri
# ---------------------------------------------------------------------------
#
# default      : Spec gereksinimi (100 examples). Geliştirici makinesinde
#                ve CI'da çalışır.
# fast         : Lokal hızlı geri bildirim (20 examples).
# thorough     : Nightly / pre-release koşusu (500 examples, daha agresif
#                shrinking).
# ci           : CI runner için "default" ile aynı, ama deadline biraz
#                daha gevşek (Windows runner'larda Tk yavaş başlayabilir).
#
# pytest.ini içinde --hypothesis-profile=default seçilir; override için
# `pytest --hypothesis-profile=fast` veya thorough kullanılabilir.

settings.register_profile(
    "default",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
    print_blob=True,
)

settings.register_profile(
    "fast",
    parent=settings.get_profile("default"),
    max_examples=20,
)

settings.register_profile(
    "thorough",
    parent=settings.get_profile("default"),
    max_examples=500,
    verbosity=Verbosity.verbose,
)

settings.register_profile(
    "ci",
    parent=settings.get_profile("default"),
    deadline=None,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
    ],
)


# ---------------------------------------------------------------------------
# Yol yardımcıları
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def _ensure_repo_on_syspath() -> None:
    """Repo kökünü sys.path'e ekleyerek `runtime`, `voice`, `skills` gibi
    paketlerin testlerden import edilebilmesini sağlar."""
    repo = str(REPO_ROOT)
    if repo not in sys.path:
        sys.path.insert(0, repo)


_ensure_repo_on_syspath()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Repo kökünün mutlak Path'ini döner."""
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """`tests/fixtures/` mutlak Path'ini döner."""
    return FIXTURES_DIR


# ---------------------------------------------------------------------------
# Tk root fixture
# ---------------------------------------------------------------------------
#
# HUD (Theme_Engine, Task_Dock, Toast, Waveform, Sparkline, Command_Palette)
# testlerinin tek bir gizli (withdrawn) Tk root paylaşması performans için
# önemlidir; her test ayrı bir root.create() çağırırsa Windows'ta yavaşlama
# yaşanır. Test'ler kendi Toplevel'larını bu root'tan üretir.
#
# Headless CI ortamlarında (Linux runner) Tk import edilemez veya display
# yoksa hata fırlatır; bu durumda fixture testleri otomatik skip eder.


@pytest.fixture(scope="session")
def tk_root() -> Iterator[Any]:
    """Session boyunca paylaşılan, gizli (withdrawn) bir Tk root döner.

    Tkinter import edilemez veya başlatılamazsa test skip edilir.
    """
    try:
        import tkinter as tk
    except Exception as exc:  # pragma: no cover - import edilemiyorsa skip
        pytest.skip(f"Tkinter import edilemedi: {exc}")

    try:
        root = tk.Tk()
    except Exception as exc:  # pragma: no cover - display yoksa skip
        pytest.skip(f"Tk root oluşturulamadı (headless mi?): {exc}")

    root.withdraw()
    try:
        yield root
    finally:
        try:
            root.update_idletasks()
            root.destroy()
        except Exception:
            pass


@pytest.fixture
def tk_toplevel(tk_root: Any) -> Iterator[Any]:
    """Test başına izole bir Toplevel pencere döner; test sonunda kapatır."""
    import tkinter as tk

    top = tk.Toplevel(tk_root)
    top.withdraw()
    try:
        yield top
    finally:
        try:
            top.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Geçici dizin / log yardımcıları
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_logs_dir(tmp_path: Path) -> Path:
    """`logs/conversation` ve `logs/debug` alt klasörleri içeren izole bir
    geçici dizin sağlar. Conversation_Logger ve Privacy testleri için."""
    (tmp_path / "logs" / "conversation").mkdir(parents=True)
    (tmp_path / "logs" / "debug").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def temp_memory_dir(tmp_path: Path) -> Path:
    """Clipboard_Manager ve memory_manager testleri için izole `memory/`."""
    (tmp_path / "memory").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# freezegun yardımcıları
# ---------------------------------------------------------------------------
#
# Zaman duyarlı testler için tek noktadan import. Hypothesis property
# testleri freezegun ile birlikte kullanıldığında her örnekte zamanı
# farklı bir noktaya sabitleyebilir; bu yardımcılar ortak başlangıç
# değerlerini sağlar.


@pytest.fixture
def frozen_now() -> Iterator[datetime]:
    """Sabit bir UTC zamana zamanı dondurur ve datetime objesini döner.

    Result_Announcer 30 dk penceresi, UAC rate limit ve log rotation gibi
    zamana bağlı testlerde kullanılır.
    """
    from freezegun import freeze_time

    fixed = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    with freeze_time(fixed) as frozen:
        # freezegun'ın ileri sarma için tick fonksiyonu açıkta tutulmaz;
        # testler `frozen.tick(seconds=...)` ile ilerleyebilir.
        yield frozen  # type: ignore[misc]


@pytest.fixture
def freezer():
    """`freezegun.freeze_time()` context manager'ını import-edilmiş döner.

    Kullanım: `with freezer("2025-06-01"): ...`
    """
    from freezegun import freeze_time

    return freeze_time


# ---------------------------------------------------------------------------
# Mock factory'leri
# ---------------------------------------------------------------------------
#
# Bileşenler henüz yazılmadığı için bu factory'ler protokol-tabanlı ufak
# mock'lar döner; ileride gerçek sınıflar geldiğinde aynı isimleri koruyarak
# spec=GerçekSınıf parametresi eklenebilir.


@pytest.fixture
def mock_voice_core() -> MagicMock:
    """Voice_Core mock'u. send_text/send_system_message/on_turn_complete
    coroutine'leri ve `state` property'i sağlar."""
    voice = MagicMock(name="VoiceCore")
    voice.state = "LISTENING"
    voice.send_text = MagicMock(name="send_text")
    voice.send_system_message = MagicMock(name="send_system_message")
    voice.on_turn_complete = MagicMock(name="on_turn_complete")
    voice.on_state_change = MagicMock(name="on_state_change")
    return voice


@pytest.fixture
def mock_privacy_mode() -> MagicMock:
    """Privacy_Mode mock'u; `is_active()` False döner varsayılan olarak."""
    privacy = MagicMock(name="PrivacyMode")
    privacy.is_active = MagicMock(return_value=False)
    privacy.enable = MagicMock()
    privacy.disable = MagicMock()
    privacy.on_change = MagicMock()
    return privacy


@pytest.fixture
def mock_task_manager() -> MagicMock:
    """Task_Manager mock'u. submit/cancel/get/list_recent stub'ları."""
    tm = MagicMock(name="TaskManager")
    tm.submit = MagicMock()
    tm.cancel = MagicMock(return_value=True)
    tm.get = MagicMock(return_value=None)
    tm.list_recent = MagicMock(return_value=[])
    tm.on_state_change = MagicMock()
    return tm


@pytest.fixture
def mock_tool_runtime() -> MagicMock:
    """Tool_Runtime mock'u. register/unregister/declarations/dispatch."""
    tr = MagicMock(name="ToolRuntime")
    tr.register = MagicMock()
    tr.unregister = MagicMock()
    tr.declarations = MagicMock(return_value=[])
    tr.dispatch = MagicMock()
    return tr


@pytest.fixture
def mock_plugin_host() -> MagicMock:
    """Plugin_Host mock'u; discover/load boş liste döner."""
    ph = MagicMock(name="PluginHost")
    ph.discover = MagicMock(return_value=[])
    ph.load = MagicMock(return_value=[])
    ph.reload = MagicMock()
    ph.disabled_skills = MagicMock(return_value=set())
    return ph


@pytest.fixture
def mock_hud() -> MagicMock:
    """JarvisUI / HUD mock'u; toast/dock/log alanı çağrıları için."""
    hud = MagicMock(name="JarvisUI")
    hud.toast = MagicMock()
    hud.task_dock = MagicMock()
    hud.append_log = MagicMock()
    return hud


@pytest.fixture
def mock_clock() -> Callable[[], float]:
    """Test'in kontrol edebildiği bir saat fonksiyonu döner. `mock_clock()`
    çağrıldığında en son set edilen zamanı döner; zamanı ileri almak için
    `mock_clock.advance(seconds)`."""

    state = {"now": 1_700_000_000.0}

    def _now() -> float:
        return state["now"]

    def _advance(seconds: float) -> None:
        state["now"] += seconds

    _now.advance = _advance  # type: ignore[attr-defined]
    return _now


# ---------------------------------------------------------------------------
# Background_Task factory
# ---------------------------------------------------------------------------
#
# Task_Manager, Result_Announcer ve Task_Dock testlerinde sıkça gerekli olan
# basit Background_Task benzeri bir nesne üretir. Gerçek dataclass
# (`runtime/types.py::BackgroundTask`) hazır olduğunda factory ona delege
# edebilir; şu an için protokol uyumlu hafif bir nesne döner.


class _FakeBackgroundTask:
    """Test amaçlı, runtime/types.py::BackgroundTask ile alan-uyumlu nesne."""

    __slots__ = (
        "id",
        "name",
        "args",
        "state",
        "created_at",
        "started_at",
        "finished_at",
        "result_text",
        "error_text",
        "skill_id",
    )

    def __init__(
        self,
        *,
        id: str,
        name: str,
        args: dict | None = None,
        state: str = "queued",
        created_at: float = 0.0,
        started_at: float | None = None,
        finished_at: float | None = None,
        result_text: str | None = None,
        error_text: str | None = None,
        skill_id: str = "",
    ) -> None:
        self.id = id
        self.name = name
        self.args = args or {}
        self.state = state
        self.created_at = created_at
        self.started_at = started_at
        self.finished_at = finished_at
        self.result_text = result_text
        self.error_text = error_text
        self.skill_id = skill_id

    def __repr__(self) -> str:  # pragma: no cover - debug
        return f"_FakeBackgroundTask(id={self.id!r}, name={self.name!r}, state={self.state!r})"


@pytest.fixture
def make_background_task() -> Callable[..., _FakeBackgroundTask]:
    """Çağrıldığında alan-uyumlu sahte bir Background_Task döner.

    Örnek:
        task = make_background_task(name="video_object_detect")
    """
    counter = {"n": 0}

    def _factory(**overrides: Any) -> _FakeBackgroundTask:
        counter["n"] += 1
        defaults = dict(
            id=overrides.pop("id", f"task-{counter['n']:04d}"),
            name=overrides.pop("name", "fake_tool"),
            args=overrides.pop("args", {}),
            state=overrides.pop("state", "queued"),
            created_at=overrides.pop("created_at", 1_700_000_000.0 + counter["n"]),
            started_at=overrides.pop("started_at", None),
            finished_at=overrides.pop("finished_at", None),
            result_text=overrides.pop("result_text", None),
            error_text=overrides.pop("error_text", None),
            skill_id=overrides.pop("skill_id", ""),
        )
        if overrides:
            raise TypeError(f"Bilinmeyen alanlar: {sorted(overrides)}")
        return _FakeBackgroundTask(**defaults)

    return _factory


# ---------------------------------------------------------------------------
# Genel yardımcı fixture'lar
# ---------------------------------------------------------------------------


@pytest.fixture
def announcer_queue() -> deque:
    """Result_Announcer testleri için boş bir kronolojik kuyruk döner."""
    return deque()


@pytest.fixture
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Çevre değişkenlerini ve cwd'yi izole bir geçici klasöre yönlendirir.

    JARVIS_CONFIG_DIR ve JARVIS_LOG_DIR gibi runtime'ın okuyabileceği
    değişkenler gelecekte tanımlanırsa burada set edilir.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JARVIS_TEST_MODE", "1")
    monkeypatch.setenv("JARVIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("JARVIS_LOG_DIR", str(tmp_path / "logs"))
    return tmp_path


# ---------------------------------------------------------------------------
# Otomatik temizleme: testler arası global state sızıntısını önle
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_random_seed() -> None:
    """Hypothesis kendi seed yönetimini yapsa da `random` modülü ile karışık
    çalışan kodlarda determinizm için her test başında seed sıfırlanır."""
    import random

    random.seed(0)


# ---------------------------------------------------------------------------
# Pytest collection hook'u: Windows-only testleri non-Windows'ta skip et.
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """`@pytest.mark.windows_only` ile işaretli testleri Windows dışında skip eder."""
    if os.name == "nt":
        return
    skip_marker = pytest.mark.skip(reason="Yalnızca Windows üzerinde anlamlı")
    for item in items:
        if "windows_only" in item.keywords:
            item.add_marker(skip_marker)
