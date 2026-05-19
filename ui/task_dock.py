"""Task_Dock — aktif arka plan görevlerini gösteren HUD paneli.

Design.md § 19 (HUD ek bileşenleri) ve Requirements § 5'e karşılık gelir.

Sorumluluklar
-------------
* Task_Manager.on_state_change callback'iyle 500 ms içinde satır ekle/güncelle
  (Req 5.1, 5.2).
* Tamamlanan satır 5 sn sonra silinir (Req 5.3).
* Boş durumda "Görev kuyruğu boş" mesajı (Req 5.4).
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from typing import TYPE_CHECKING

from runtime.types import BackgroundTask, TaskState

if TYPE_CHECKING:
    from runtime.task_manager import TaskManager

log = logging.getLogger(__name__)

# Renk sabitleri
_C_BG = "#030f0f"
_C_PRI = "#00d4c0"
_C_TEXT = "#7dfff6"
_C_DIM = "#0a2a28"
_C_MID = "#006a62"
_C_GOLD = "#ffcc00"
_C_GREEN = "#00ff88"
_C_RED = "#ff3344"
_C_GREY = "#3a5a58"

_STATE_COLORS = {
    TaskState.QUEUED: _C_GREY,
    TaskState.RUNNING: _C_GOLD,
    TaskState.SUCCEEDED: _C_GREEN,
    TaskState.FAILED: _C_RED,
    TaskState.CANCELLED: _C_GREY,
    TaskState.ANNOUNCED: _C_DIM,
}

_STATE_LABELS = {
    TaskState.QUEUED: "bekliyor",
    TaskState.RUNNING: "çalışıyor",
    TaskState.SUCCEEDED: "tamamlandı",
    TaskState.FAILED: "başarısız",
    TaskState.CANCELLED: "iptal",
    TaskState.ANNOUNCED: "duyuruldu",
}

_REMOVE_AFTER_MS = 5000  # tamamlanan satır 5 sn sonra silinir


class TaskDock:
    """Arka plan görevlerini listeleyen HUD bileşeni.

    Parameters
    ----------
    parent:
        Tk parent widget.
    task_manager:
        Dinlenecek TaskManager örneği.
    width, height:
        Dock boyutları (piksel).
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        task_manager: "TaskManager | None" = None,
        width: int = 340,
        height: int = 180,
    ) -> None:
        self._parent = parent
        self._width = width
        self._height = height
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}  # task_id → {frame, label_name, label_state, after_id}

        # Ana çerçeve
        self.frame = tk.Frame(
            parent,
            bg=_C_BG,
            highlightbackground=_C_MID,
            highlightthickness=1,
            width=width,
            height=height,
        )

        # Başlık
        tk.Label(
            self.frame,
            text="ARKA PLAN GÖREVLERİ",
            bg=_C_BG,
            fg=_C_MID,
            font=("Grift", 8, "bold"),
            anchor="w",
            padx=8,
            pady=3,
        ).pack(fill="x")

        tk.Frame(self.frame, bg=_C_MID, height=1).pack(fill="x")

        # Görev listesi alanı
        self._list_frame = tk.Frame(self.frame, bg=_C_BG)
        self._list_frame.pack(fill="both", expand=True, padx=4, pady=4)

        # Boş durum etiketi
        self._empty_label = tk.Label(
            self._list_frame,
            text="Görev kuyruğu boş",
            bg=_C_BG,
            fg=_C_GREY,
            font=("Grift", 10),
            anchor="center",
        )
        self._empty_label.pack(fill="both", expand=True)

        if task_manager is not None:
            self.connect(task_manager)

    # ------------------------------------------------------------------ public

    def connect(self, task_manager: "TaskManager") -> None:
        """TaskManager'a bağlan ve state değişikliklerini dinle."""
        task_manager.on_state_change(self._on_state_change)

    def place(self, **kwargs) -> None:
        """Dock'u parent üzerine yerleştir."""
        self.frame.place(**kwargs)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    # ---------------------------------------------------------------- internal

    def _on_state_change(self, task: BackgroundTask) -> None:
        """TaskManager callback — thread-safe, Tk after ile UI günceller."""
        try:
            self.frame.after(0, lambda: self._update_row(task))
        except Exception as exc:
            log.debug("TaskDock._on_state_change: %s", exc)

    def _update_row(self, task: BackgroundTask) -> None:
        """Görev satırını ekle veya güncelle (Tk main thread'inde çalışır)."""
        task_id = task.id

        with self._lock:
            existing = self._rows.get(task_id)

        if existing is None:
            # Yeni satır oluştur
            row_frame = tk.Frame(self._list_frame, bg=_C_BG)
            row_frame.pack(fill="x", pady=1)

            name_label = tk.Label(
                row_frame,
                text=self._truncate(task.name, 22),
                bg=_C_BG,
                fg=_C_TEXT,
                font=("Grift", 10),
                anchor="w",
                width=22,
            )
            name_label.pack(side="left", padx=(4, 2))

            state_label = tk.Label(
                row_frame,
                text=_STATE_LABELS.get(task.state, task.state),
                bg=_C_BG,
                fg=_STATE_COLORS.get(task.state, _C_TEXT),
                font=("Grift", 9),
                anchor="e",
                width=12,
            )
            state_label.pack(side="right", padx=(2, 4))

            with self._lock:
                self._rows[task_id] = {
                    "frame": row_frame,
                    "label_name": name_label,
                    "label_state": state_label,
                    "after_id": None,
                }
            self._hide_empty_label()
        else:
            # Mevcut satırı güncelle
            state_label = existing["label_state"]
            state_label.config(
                text=_STATE_LABELS.get(task.state, task.state),
                fg=_STATE_COLORS.get(task.state, _C_TEXT),
            )

        # Terminal durumlarda 5 sn sonra sil
        if task.state in (
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
            TaskState.ANNOUNCED,
        ):
            with self._lock:
                row_data = self._rows.get(task_id)
                if row_data:
                    old_after = row_data.get("after_id")
                    if old_after:
                        try:
                            self.frame.after_cancel(old_after)
                        except Exception:
                            pass
                    after_id = self.frame.after(
                        _REMOVE_AFTER_MS, lambda tid=task_id: self._remove_row(tid)
                    )
                    row_data["after_id"] = after_id

    def _remove_row(self, task_id: str) -> None:
        """Satırı listeden kaldır."""
        with self._lock:
            row_data = self._rows.pop(task_id, None)

        if row_data:
            try:
                row_data["frame"].destroy()
            except Exception:
                pass

        # Tüm satırlar gittiyse boş etiketi göster
        with self._lock:
            empty = len(self._rows) == 0
        if empty:
            self._show_empty_label()

    def _hide_empty_label(self) -> None:
        try:
            self._empty_label.pack_forget()
        except Exception:
            pass

    def _show_empty_label(self) -> None:
        try:
            self._empty_label.pack(fill="both", expand=True)
        except Exception:
            pass

    @staticmethod
    def _truncate(text: str, max_len: int) -> str:
        return text if len(text) <= max_len else text[: max_len - 1] + "…"


__all__ = ["TaskDock"]
