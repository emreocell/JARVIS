"""Notification_Toast — sağ alt köşede beliren bildirim balonları.

Design.md § 13 ve Requirements § 25'e karşılık gelir.

Sorumluluklar
-------------
* Tk overlay Toplevel'lar; sağ alt köşede dikey istif (Req 25.3).
* 3 sn fade-out (Req 25.1).
* 4'ü aşan toast: en eski erken kapatılır (Req 25.4).
* Background_Task tamamlanmasında ve tool hatasında otomatik tetiklenir.
"""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from collections import deque
from typing import Literal

log = logging.getLogger(__name__)

# Renk sabitleri
_C_BG = "#030f0f"
_C_PRI = "#00d4c0"
_C_TEXT = "#7dfff6"
_C_MID = "#006a62"
_C_GREEN = "#00ff88"
_C_RED = "#ff3344"
_C_GOLD = "#ffcc00"
_C_BLUE = "#4488ff"

ToastKind = Literal["success", "error", "info", "warning"]

_KIND_COLORS: dict[ToastKind, str] = {
    "success": _C_GREEN,
    "error": _C_RED,
    "info": _C_PRI,
    "warning": _C_GOLD,
}

_KIND_ICONS: dict[ToastKind, str] = {
    "success": "✓",
    "error": "✗",
    "info": "ℹ",
    "warning": "⚠",
}

_MAX_TOASTS = 4
_TOAST_WIDTH = 320
_TOAST_HEIGHT = 64
_TOAST_MARGIN_RIGHT = 18
_TOAST_MARGIN_BOTTOM = 48
_TOAST_GAP = 8
_DISPLAY_MS = 3000   # 3 sn görünür
_FADE_STEPS = 20
_FADE_INTERVAL_MS = 50  # toplam ~1 sn fade


class _Toast:
    """Tek bir toast bildirimi."""

    def __init__(
        self,
        root: tk.Tk,
        message: str,
        kind: ToastKind,
        x: int,
        y: int,
        on_close: "callable",
    ) -> None:
        self._root = root
        self._on_close = on_close
        self._closed = False
        self._alpha = 0.92

        win = tk.Toplevel(root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", self._alpha)
        win.configure(bg=_C_BG)
        win.geometry(f"{_TOAST_WIDTH}x{_TOAST_HEIGHT}+{x}+{y}")

        color = _KIND_COLORS.get(kind, _C_PRI)
        icon = _KIND_ICONS.get(kind, "•")

        # Sol renk şeridi
        tk.Frame(win, bg=color, width=4).pack(side="left", fill="y")

        body = tk.Frame(win, bg=_C_BG)
        body.pack(side="left", fill="both", expand=True, padx=8, pady=6)

        # İkon + mesaj
        top_row = tk.Frame(body, bg=_C_BG)
        top_row.pack(fill="x")

        tk.Label(
            top_row,
            text=icon,
            bg=_C_BG,
            fg=color,
            font=("Grift", 13, "bold"),
        ).pack(side="left")

        msg = message if len(message) <= 55 else message[:52] + "…"
        tk.Label(
            top_row,
            text=msg,
            bg=_C_BG,
            fg=_C_TEXT,
            font=("Grift", 10),
            anchor="w",
            wraplength=_TOAST_WIDTH - 40,
            justify="left",
        ).pack(side="left", padx=(6, 0))

        # Kapat butonu
        tk.Button(
            win,
            text="×",
            bg=_C_BG,
            fg=_C_MID,
            font=("Grift", 11),
            borderwidth=0,
            activebackground=_C_BG,
            activeforeground=_C_TEXT,
            command=self.close,
            cursor="hand2",
        ).pack(side="right", padx=4, pady=4)

        self._win = win

        # 3 sn sonra fade başlat
        win.after(_DISPLAY_MS, self._start_fade)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._win.destroy()
        except Exception:
            pass
        try:
            self._on_close(self)
        except Exception:
            pass

    def move_to(self, x: int, y: int) -> None:
        """Toast'u yeni konuma taşı (istif yeniden hesaplandığında)."""
        if not self._closed:
            try:
                self._win.geometry(f"+{x}+{y}")
            except Exception:
                pass

    def _start_fade(self) -> None:
        if self._closed:
            return
        self._fade_step = 0
        self._fade()

    def _fade(self) -> None:
        if self._closed:
            return
        self._fade_step += 1
        alpha = self._alpha * (1.0 - self._fade_step / _FADE_STEPS)
        if alpha <= 0.05 or self._fade_step >= _FADE_STEPS:
            self.close()
            return
        try:
            self._win.attributes("-alpha", alpha)
            self._win.after(_FADE_INTERVAL_MS, self._fade)
        except Exception:
            self.close()


class ToastManager:
    """Toast bildirimlerini yöneten merkezi sınıf.

    Parameters
    ----------
    root:
        Ana Tk penceresi.
    """

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._toasts: deque[_Toast] = deque()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ public

    def show(self, message: str, kind: ToastKind = "info") -> None:
        """Yeni bir toast göster.

        4'ü aşarsa en eski toast erken kapatılır (Req 25.4).
        """
        try:
            self._root.after(0, lambda: self._show_on_main(message, kind))
        except Exception as exc:
            log.debug("ToastManager.show: %s", exc)

    def show_success(self, message: str) -> None:
        self.show(message, "success")

    def show_error(self, message: str) -> None:
        self.show(message, "error")

    def show_warning(self, message: str) -> None:
        self.show(message, "warning")

    # ---------------------------------------------------------------- internal

    def _show_on_main(self, message: str, kind: ToastKind) -> None:
        """Tk main thread'inde toast oluştur."""
        with self._lock:
            # 4'ü aşarsa en eskiyi kapat
            while len(self._toasts) >= _MAX_TOASTS:
                oldest = self._toasts[0]
                oldest.close()
                # _on_toast_close deque'den kaldıracak ama lock içindeyiz;
                # doğrudan kaldır
                if self._toasts and self._toasts[0] is oldest:
                    self._toasts.popleft()

            x, y = self._next_position(len(self._toasts))
            toast = _Toast(
                self._root,
                message=message,
                kind=kind,
                x=x,
                y=y,
                on_close=self._on_toast_close,
            )
            self._toasts.append(toast)

    def _on_toast_close(self, toast: _Toast) -> None:
        """Toast kapandığında deque'den kaldır ve kalan toast'ları yeniden konumlandır."""
        with self._lock:
            try:
                self._toasts.remove(toast)
            except ValueError:
                pass
            self._restack()

    def _restack(self) -> None:
        """Kalan toast'ları alt alta yeniden konumlandır."""
        for i, t in enumerate(self._toasts):
            x, y = self._next_position(i)
            t.move_to(x, y)

    def _next_position(self, index: int) -> tuple[int, int]:
        """index numaralı toast'un ekran koordinatlarını hesapla."""
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = sw - _TOAST_WIDTH - _TOAST_MARGIN_RIGHT
        y = sh - _TOAST_MARGIN_BOTTOM - (_TOAST_HEIGHT + _TOAST_GAP) * (index + 1)
        return x, y


__all__ = ["ToastManager", "ToastKind"]
