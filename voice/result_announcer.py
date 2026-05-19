"""Result_Announcer — proaktif arka plan görev sonuç duyurucu.

Tamamlanan (succeeded / failed) Background_Task'leri bir iç kuyrukta biriktirir
ve Voice_Core'un bir Turn_Boundary'e ulaştığı anda (``on_turn_complete``) hepsini
tek bir birleşik sistem mesajı olarak Gemini Live oturumuna enjekte eder.

Davranış özeti (design.md § Result_Announcer + requirements.md Req 3):

- ``enqueue(task)`` → iç ``deque[BackgroundTask]`` kuyruğuna ekler; sıra
  ``created_at`` artan sırasına göre korunur (Req 3.6).
- ``on_turn_complete()``:
  - Kuyruk boşsa no-op.
  - Voice_Core durumu ``SPEAKING`` ise no-op (Req 3.2).
  - Aksi hâlde tüm bekleyen task'leri ``created_at`` artan sırada birleşik
    tek mesaja çevirir, ``voice.send_system_message(combined)`` ile gönderir
    ve hepsini ``ANNOUNCED`` olarak işaretler (Req 3.3, 3.4, 3.5).
- Privacy_Mode etkinse sesli anons gönderilmez; HUD log alanına yazılır
  (Req 3.7).
- Hatalı task'ler "X görevi başarısız oldu — <sebep>" formatıyla anlatılır
  (Req 3.1).
- Birleştirme şablonu: "Tamamlanan görevler: 1) {name1} — {result1}; 2) ..."
  (Req 3.6).
- Aynı task asla iki kez anons edilmez (Req 3.5).

Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7
"""

# Feature: jarvis-v2-upgrade, Result_Announcer (Task 7.2)

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from runtime.types import BackgroundTask, TaskState

if TYPE_CHECKING:
    # Avoid circular imports at runtime; only used for type hints.
    from runtime.privacy_mode import PrivacyMode
    from ui import JarvisUI

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Protocols — keeps ResultAnnouncer decoupled from concrete implementations
# ---------------------------------------------------------------------------


@runtime_checkable
class VoiceCore(Protocol):
    """Minimal Voice_Core contract required by Result_Announcer."""

    @property
    def state(self) -> str:
        """Current voice state string (e.g. SPEAKING, LISTENING, …)."""
        ...  # pragma: no cover

    async def send_system_message(self, text: str) -> None:
        """Inject a system message into the active Gemini Live session."""
        ...  # pragma: no cover


@runtime_checkable
class PrivacyGate(Protocol):
    """Minimal Privacy_Mode contract."""

    def is_active(self) -> bool:
        ...  # pragma: no cover


@runtime_checkable
class HUDLog(Protocol):
    """Minimal HUD contract — only the log-writing surface is needed."""

    def write_log(self, text: str) -> None:
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IDLE_STATES: frozenset[str] = frozenset(
    {"LISTENING", "THINKING", "INITIALISING", "PAUSED", "MUTED", "ERROR"}
)
"""Voice states in which the announcer is allowed to fire.

``SPEAKING`` is the only state where we must stay silent (Req 3.2).
Any other state (including THINKING, PAUSED, MUTED, ERROR) is treated as
"not actively speaking", so we can safely inject a system message.
"""


def _build_combined_message(tasks: list[BackgroundTask]) -> str:
    """Build the combined announcement string from a list of tasks.

    Template (Req 3.6):
        "Tamamlanan görevler: 1) {name1} — {result1}; 2) {name2} — ..."

    Failed tasks use the format (Req 3.1):
        "{name} görevi başarısız oldu — {reason}"
    """
    parts: list[str] = []
    for idx, task in enumerate(tasks, start=1):
        if task.state == TaskState.FAILED:
            reason = task.error_text or "bilinmeyen hata"
            parts.append(f"{idx}) {task.name} görevi başarısız oldu — {reason}")
        else:
            result = task.result_text or ""
            parts.append(f"{idx}) {task.name} — {result}")

    return "Tamamlanan görevler: " + "; ".join(parts)


# ---------------------------------------------------------------------------
# ResultAnnouncer
# ---------------------------------------------------------------------------


class ResultAnnouncer:
    """Proaktif arka plan görev sonuç duyurucusu.

    Tek bir ``JarvisLive`` oturumu boyunca yaşar; ``main.py`` içinde
    instantiate edilir ve ``event_bus.subscribe("voice.turn_complete", ...)``
    ile bağlanır.

    Thread-safety: ``enqueue`` herhangi bir thread'den (Task_Manager callback
    thread'i dahil) çağrılabilir. ``on_turn_complete`` asyncio event loop'unda
    çalışır. İç deque ve ``_announced_ids`` kümesi bir ``threading.Lock``
    ile korunur.
    """

    def __init__(
        self,
        voice: VoiceCore,
        privacy: PrivacyGate | None = None,
        hud: HUDLog | None = None,
    ) -> None:
        """
        Parameters
        ----------
        voice:
            Voice_Core instance (``JarvisLive``). Kullanılan arayüz:
            ``voice.state`` ve ``voice.send_system_message(text)``.
        privacy:
            Privacy_Mode instance. ``None`` ise privacy her zaman kapalı
            kabul edilir.
        hud:
            HUD instance. Privacy aktifken log yazmak için kullanılır.
            ``None`` ise HUD log atlanır ve sadece Python logger'a yazılır.
        """
        self._voice = voice
        self._privacy = privacy
        self._hud = hud

        # Kronolojik sıralı bekleyen task kuyruğu.
        # deque seçildi: O(1) append + popleft, thread-safe iteration
        # (lock altında snapshot alınır).
        self._queue: deque[BackgroundTask] = deque()

        # Daha önce anons edilmiş task id'leri — çift anons koruması (Req 3.5).
        self._announced_ids: set[str] = set()

        self._lock = threading.Lock()

    # ------------------------------------------------------------------ API

    def enqueue(self, task: BackgroundTask) -> None:
        """Tamamlanan bir task'i anons kuyruğuna ekle.

        Yalnızca ``SUCCEEDED`` veya ``FAILED`` durumundaki task'ler kabul
        edilir; diğerleri sessizce yok sayılır. Daha önce anons edilmiş
        task'ler de yok sayılır (Req 3.5).

        Sıra ``created_at`` artan sırasına göre korunur (Req 3.6): yeni
        task, kuyruğun sonuna eklenir ve ``on_turn_complete`` sıralayarak
        işler.
        """
        if task.state not in (TaskState.SUCCEEDED, TaskState.FAILED):
            log.debug(
                "ResultAnnouncer.enqueue: task %r state=%s — skipped (not terminal)",
                task.id,
                task.state,
            )
            return

        with self._lock:
            if task.id in self._announced_ids:
                log.debug(
                    "ResultAnnouncer.enqueue: task %r already announced — skipped",
                    task.id,
                )
                return
            self._queue.append(task)
            log.debug(
                "ResultAnnouncer.enqueue: task %r enqueued (queue size=%d)",
                task.id,
                len(self._queue),
            )

    async def on_turn_complete(self) -> None:
        """Turn_Boundary'de çağrılır; bekleyen anonsları işler.

        Davranış (Req 3.2, 3.3, 3.4, 3.5, 3.6, 3.7):

        1. Kuyruk boşsa no-op.
        2. Voice_Core durumu ``SPEAKING`` ise no-op.
        3. Aksi hâlde kuyruktaki tüm task'leri ``created_at`` artan sırada
           sıralar, birleşik mesaj oluşturur.
        4. Privacy aktifse sesli anons gönderilmez; HUD log'a yazılır.
        5. Privacy kapalıysa ``voice.send_system_message(combined)`` çağrılır.
        6. Tüm task'ler ``ANNOUNCED`` olarak işaretlenir ve kuyruktan çıkarılır.
        """
        # Snapshot al ve kuyruğu temizle — lock dışında async işlem yapacağız.
        with self._lock:
            if not self._queue:
                return  # Kuyruk boş, no-op (Req 3.3 implicit)

            # Voice durumu SPEAKING ise no-op (Req 3.2)
            current_state = self._voice.state
            if current_state == "SPEAKING":
                log.debug(
                    "ResultAnnouncer.on_turn_complete: voice is SPEAKING — deferred"
                )
                return

            # Snapshot: created_at artan sırada sırala (Req 3.6)
            pending = sorted(self._queue, key=lambda t: t.created_at)
            self._queue.clear()

        # Birleşik mesaj oluştur
        combined = _build_combined_message(pending)

        # Privacy kontrolü (Req 3.7)
        privacy_active = self._privacy is not None and self._privacy.is_active()

        if privacy_active:
            # Sesli anons yok; HUD log alanına yaz
            log_line = f"[ResultAnnouncer] Privacy aktif — sesli anons yok: {combined}"
            log.info(log_line)
            if self._hud is not None:
                try:
                    self._hud.write_log(log_line)
                except Exception:
                    log.exception("ResultAnnouncer: HUD write_log failed")
        else:
            # Gemini Live oturumuna sistem mesajı enjekte et (Req 3.3)
            try:
                await self._voice.send_system_message(combined)
                log.info(
                    "ResultAnnouncer: announced %d task(s) via system_message",
                    len(pending),
                )
            except Exception:
                log.exception("ResultAnnouncer: send_system_message failed")

        # Tüm task'leri ANNOUNCED olarak işaretle (Req 3.4, 3.5)
        with self._lock:
            for task in pending:
                try:
                    task.transition_to(TaskState.ANNOUNCED)
                except RuntimeError:
                    # Transition zaten yapılmışsa (race condition) yok say.
                    log.debug(
                        "ResultAnnouncer: task %r already in state %s — skipping ANNOUNCED transition",
                        task.id,
                        task.state,
                    )
                self._announced_ids.add(task.id)

    def pending_count(self) -> int:
        """Kuyrukta bekleyen (henüz anons edilmemiş) task sayısı."""
        with self._lock:
            return len(self._queue)

    def announced_count(self) -> int:
        """Toplam anons edilmiş benzersiz task sayısı (test/debug için)."""
        with self._lock:
            return len(self._announced_ids)


__all__ = [
    "ResultAnnouncer",
    "VoiceCore",
    "PrivacyGate",
    "HUDLog",
]
