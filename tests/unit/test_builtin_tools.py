"""Unit tests for ``runtime/builtin_tools.py``.

Feature: jarvis-v2-upgrade, Task 4.8 — register the inline ``list_background_tasks``
ve ``cancel_background_task`` tool'larını Tool_Runtime'a bağlayan modül.

Tests focus on:
* Registration: iki tool da Tool_Runtime'a kayıt olur, declaration'lar Plugin_Host
  doğrulayıcısının kabul ettiği şemaya uyar (alt çizgi adlandırması manifesto
  keşfine sızmaz).
* ``list_background_tasks``: boş kuyruk ve dolu kuyruk için insan-okunabilir
  özet üretir; sıralamayı TaskManager'ın verdiği şekilde korur.
* ``cancel_background_task``:
    - Bilinmeyen kimlik → açıklayıcı hata, TaskManager.cancel çağrılmaz.
    - Terminal görev → `state/result_text/error_text` değişmez (Req 4.5).
    - Aktif görev → cancel başarılı, durum cancelled.
    - End-to-end ToolRuntime.dispatch akışı: inline çağrı `{"result": ...}`
      sözleşmesini sağlar.

Tests gerçek bir TaskManager ile çalışır: external dependency yok ve
TaskManager + ToolRuntime davranışı zaten ayrı testlerle doğrulanmış olduğu
için bu testlerde mock kullanmaya gerek yok. Sadece tek bir senaryoda
``MagicMock`` ile cancel davranışını doğrudan gözlemliyoruz (terminal-no-mutation
invariantını tamamen izole etmek için).
"""

from __future__ import annotations

import asyncio
import threading
import time
from unittest.mock import MagicMock

import pytest

from runtime.builtin_tools import (
    BUILTIN_SKILL_ID,
    CANCEL_BACKGROUND_TASK_DECLARATION,
    LIST_BACKGROUND_TASKS_DECLARATION,
    RECENT_WINDOW_MINUTES,
    register_builtin_tools,
)
from runtime.plugin_host import _validate_declaration  # noqa: F401 — re-used
from runtime.task_manager import TaskManager
from runtime.tool_runtime import ToolRuntime
from runtime.types import BackgroundTask, TaskState, ToolDescriptor


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def task_manager() -> TaskManager:
    """Gerçek bir TaskManager döner; her testin sonunda temiz kapatılır."""
    tm = TaskManager()
    yield tm
    tm.shutdown(wait=True, cancel_pending=True)


@pytest.fixture
def runtime(task_manager: TaskManager) -> ToolRuntime:
    """Gerçek bir ToolRuntime döner."""
    return ToolRuntime(task_manager)


@pytest.fixture
def registered(runtime: ToolRuntime, task_manager: TaskManager) -> list[ToolDescriptor]:
    """Yerleşik tool'lar runtime'a kayıtlı şekilde döner."""
    return register_builtin_tools(runtime, task_manager)


# ---------------------------------------------------------------------------
# Registration shape
# ---------------------------------------------------------------------------


def test_register_builtin_tools_adds_both_to_runtime(
    runtime: ToolRuntime, task_manager: TaskManager
) -> None:
    descriptors = register_builtin_tools(runtime, task_manager)

    names = [d.name for d in descriptors]
    assert names == ["list_background_tasks", "cancel_background_task"]
    assert "list_background_tasks" in runtime
    assert "cancel_background_task" in runtime

    for desc in descriptors:
        assert desc.execution_mode == "inline"
        assert desc.skill_id == BUILTIN_SKILL_ID


def test_register_builtin_tools_emits_declarations_in_runtime(
    runtime: ToolRuntime, task_manager: TaskManager
) -> None:
    register_builtin_tools(runtime, task_manager)

    decl_names = {d["name"] for d in runtime.declarations()}
    assert {"list_background_tasks", "cancel_background_task"}.issubset(decl_names)


def test_builtin_declarations_pass_plugin_host_validator() -> None:
    """Builtin declaration'lar Plugin_Host'un Gemini şema validator'ını geçmeli.

    Her ne kadar bu tool'lar Plugin_Host yolu üzerinden kayıt edilmese de,
    aynı validator kuralını kullanmak Gemini Live'ın doğru kabul etmesini
    garanti eder.
    """
    from runtime.plugin_host import _validate_declaration

    assert _validate_declaration(LIST_BACKGROUND_TASKS_DECLARATION) is None
    assert _validate_declaration(CANCEL_BACKGROUND_TASK_DECLARATION) is None


def test_register_twice_raises(runtime: ToolRuntime, task_manager: TaskManager) -> None:
    """Aynı runtime üzerine iki kez kayıt çakışma fırlatır.

    ToolRuntime.register'in çift kayıt politikasıyla uyumlu olmalıyız;
    bu testle birlikte builtin tarafının "zaten kayıtlıyım, sessizce yut"
    gibi bir kestirme uygulamadığını sabitliyoruz.
    """
    register_builtin_tools(runtime, task_manager)
    with pytest.raises(ValueError):
        register_builtin_tools(runtime, task_manager)


# ---------------------------------------------------------------------------
# list_background_tasks behaviour
# ---------------------------------------------------------------------------


def test_list_background_tasks_empty(
    runtime: ToolRuntime, registered: list[ToolDescriptor]
) -> None:
    desc = runtime.get("list_background_tasks")
    assert desc is not None

    result = desc.handler()

    assert "yok" in result.lower()


def test_list_background_tasks_includes_completed_task(
    runtime: ToolRuntime,
    task_manager: TaskManager,
    registered: list[ToolDescriptor],
) -> None:
    done = threading.Event()

    def handler(task: BackgroundTask) -> str:
        return "ok-result"

    def listener(task: BackgroundTask) -> None:
        if task.state in (TaskState.SUCCEEDED, TaskState.FAILED):
            done.set()

    task_manager.on_state_change(listener)
    submitted = task_manager.submit("noop_tool", handler, {"k": "v"})
    assert done.wait(2.0)

    desc = runtime.get("list_background_tasks")
    assert desc is not None

    text = desc.handler()
    assert submitted.id in text
    assert "noop_tool" in text
    assert "succeeded" in text


def test_list_background_tasks_orders_newest_first(
    runtime: ToolRuntime,
    task_manager: TaskManager,
    registered: list[ToolDescriptor],
) -> None:
    """Birden fazla görev varken çıktı newest-first sıralı olmalı."""
    done = threading.Event()
    finished = []

    def handler(task: BackgroundTask) -> str:
        return f"done-{task.name}"

    def listener(task: BackgroundTask) -> None:
        if task.state is TaskState.SUCCEEDED:
            finished.append(task.id)
            if len(finished) >= 3:
                done.set()

    task_manager.on_state_change(listener)
    a = task_manager.submit("tool_a", handler, {})
    time.sleep(0.005)
    b = task_manager.submit("tool_b", handler, {})
    time.sleep(0.005)
    c = task_manager.submit("tool_c", handler, {})

    assert done.wait(2.0)

    desc = runtime.get("list_background_tasks")
    assert desc is not None
    text = desc.handler()

    # newest first: c, b, a
    pos_a = text.find(a.id)
    pos_b = text.find(b.id)
    pos_c = text.find(c.id)
    assert -1 < pos_c < pos_b < pos_a


# ---------------------------------------------------------------------------
# cancel_background_task behaviour
# ---------------------------------------------------------------------------


def test_cancel_unknown_id(
    runtime: ToolRuntime, registered: list[ToolDescriptor]
) -> None:
    desc = runtime.get("cancel_background_task")
    assert desc is not None

    msg = desc.handler(id="nonexistent")
    assert "bilinmeyen" in msg.lower()


def test_cancel_blank_id(
    runtime: ToolRuntime, registered: list[ToolDescriptor]
) -> None:
    desc = runtime.get("cancel_background_task")
    assert desc is not None

    msg = desc.handler(id="   ")
    assert "geçersiz" in msg.lower()


def test_cancel_active_task(
    runtime: ToolRuntime,
    task_manager: TaskManager,
    registered: list[ToolDescriptor],
) -> None:
    """queued/running aşamasında iptal CANCELLED'a geçirir."""
    proceed = threading.Event()
    started = threading.Event()
    cancelled_event = threading.Event()

    def slow_handler(task: BackgroundTask) -> str:
        started.set()
        # Cooperative cancel: cancel_event ya da serbest geçiş.
        if task.cancel_event.wait(timeout=2.0):
            return "should-be-ignored"
        return "no-cancel"

    def listener(task: BackgroundTask) -> None:
        if task.state is TaskState.CANCELLED:
            cancelled_event.set()

    task_manager.on_state_change(listener)
    submitted = task_manager.submit("slow_tool", slow_handler, {})
    assert started.wait(2.0)

    desc = runtime.get("cancel_background_task")
    assert desc is not None
    msg = desc.handler(id=submitted.id)

    assert "iptal edildi" in msg.lower()
    assert cancelled_event.wait(2.0)
    assert task_manager.get(submitted.id).state is TaskState.CANCELLED


def test_cancel_terminal_task_does_not_mutate(
    runtime: ToolRuntime,
    task_manager: TaskManager,
    registered: list[ToolDescriptor],
) -> None:
    """Req 4.5 — terminal görev için state/result_text/error_text değişmez."""
    done = threading.Event()

    def handler(task: BackgroundTask) -> str:
        return "final-text"

    def listener(task: BackgroundTask) -> None:
        if task.state is TaskState.SUCCEEDED:
            done.set()

    task_manager.on_state_change(listener)
    submitted = task_manager.submit("done_tool", handler, {})
    assert done.wait(2.0)

    snapshot = task_manager.get(submitted.id)
    assert snapshot.state is TaskState.SUCCEEDED
    pre_state = snapshot.state
    pre_result = snapshot.result_text
    pre_error = snapshot.error_text

    desc = runtime.get("cancel_background_task")
    assert desc is not None
    msg = desc.handler(id=submitted.id)

    assert "zaten" in msg.lower() or "sonlan" in msg.lower()

    after = task_manager.get(submitted.id)
    assert after.state is pre_state
    assert after.result_text == pre_result
    assert after.error_text == pre_error


def test_cancel_uses_task_manager_cancel(
    runtime: ToolRuntime,
) -> None:
    """Aktif görev için cancel handler ``TaskManager.cancel``'i çağırır.

    Burada gerçek TaskManager yerine MagicMock kullanıyoruz: cancel
    handler'ının mutasyon yapmadan, yalnızca TaskManager.cancel'i
    çağırarak iş gördüğünü izole şekilde doğrular.
    """
    fake_task = BackgroundTask(
        id="abc123",
        name="dummy",
        args={},
        state=TaskState.RUNNING,
        started_at=time.time(),
    )

    fake_tm = MagicMock(spec=TaskManager)
    fake_tm.get.return_value = fake_task
    fake_tm.cancel.return_value = True

    isolated_runtime = ToolRuntime(fake_tm)
    register_builtin_tools(isolated_runtime, fake_tm)

    desc = isolated_runtime.get("cancel_background_task")
    msg = desc.handler(id="abc123")

    fake_tm.cancel.assert_called_once_with("abc123")
    assert "iptal edildi" in msg.lower()


# ---------------------------------------------------------------------------
# End-to-end through ToolRuntime.dispatch
# ---------------------------------------------------------------------------


def test_dispatch_list_inline(
    runtime: ToolRuntime, registered: list[ToolDescriptor]
) -> None:
    payload = asyncio.run(runtime.dispatch("list_background_tasks", {}))
    assert "result" in payload
    assert isinstance(payload["result"], str)


def test_dispatch_cancel_inline(
    runtime: ToolRuntime, registered: list[ToolDescriptor]
) -> None:
    payload = asyncio.run(runtime.dispatch("cancel_background_task", {"id": "missing"}))
    assert "result" in payload
    assert "bilinmeyen" in payload["result"].lower()


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


def test_recent_window_matches_requirement() -> None:
    """Req 4.2 — pencere süresi 30 dakika olmalı."""
    assert RECENT_WINDOW_MINUTES == 30


def test_builtin_skill_id_starts_with_underscore() -> None:
    """PluginHost.discover alt çizgi ile başlayan klasörleri atlar; bu
    yüzden builtin skill_id'nin alt çizgi ile başlaması, disk üzerinde
    aynı isimli bir paketin yanlışlıkla ele geçirememesi için kritiktir.
    """
    assert BUILTIN_SKILL_ID.startswith("_")
