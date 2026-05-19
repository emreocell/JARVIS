"""Waveform — animasyonlu ses dalga formu bileşeni.

Design.md § 19 ve Requirements § 31'e karşılık gelir.

Sorumluluklar
-------------
* Voice_Core çıkış buffer'ı ve mikrofon giriş buffer'ı için ayrı 32 sütunlu
  dalga formu (Req 31.1).
* 30+ FPS; canvas bar item'ları koordinat güncellemesiyle taşınır (Req 31.2).
* "MUTED"/"PAUSED"'da sönük renk ve sabit pozisyon (Req 31.3, 31.4).
"""

from __future__ import annotations

import math
import random
import threading
import time
import tkinter as tk
from typing import Callable

# Renk sabitleri
_C_BG = "#020c0c"
_C_PRI = "#00d4c0"
_C_BLUE = "#4488ff"
_C_DIM = "#0a2a28"
_C_MUTED = "#1a3a38"

_BAR_COUNT = 32
_FPS_TARGET = 30
_TICK_MS = int(1000 / _FPS_TARGET)


class WaveformWidget:
    """Çift kanallı dalga formu canvas bileşeni.

    Parameters
    ----------
    parent:
        Tk parent widget.
    width, height:
        Canvas boyutları.
    color_output:
        JARVIS çıkış kanalı rengi.
    color_input:
        Mikrofon giriş kanalı rengi.
    """

    def __init__(
        self,
        parent: tk.Widget,
        *,
        width: int = 280,
        height: int = 60,
        color_output: str = _C_PRI,
        color_input: str = _C_BLUE,
    ) -> None:
        self._width = width
        self._height = height
        self._color_output = color_output
        self._color_input = color_input
        self._muted = False
        self._paused = False
        self._lock = threading.Lock()

        # Amplitüd buffer'ları (0.0–1.0)
        self._output_buf: list[float] = [0.0] * _BAR_COUNT
        self._input_buf: list[float] = [0.0] * _BAR_COUNT

        # Smooth hedef değerler
        self._output_target: list[float] = [0.0] * _BAR_COUNT
        self._input_target: list[float] = [0.0] * _BAR_COUNT

        # Canvas
        self.canvas = tk.Canvas(
            parent,
            width=width,
            height=height,
            bg=_C_BG,
            highlightthickness=0,
        )

        # Bar item'larını önceden oluştur (koordinat güncellemesi daha hızlı)
        self._output_bars: list[int] = []
        self._input_bars: list[int] = []
        self._init_bars()

        # Animasyon döngüsü
        self._animating = False
        self._start_animation()

    # ------------------------------------------------------------------ public

    def place(self, **kwargs) -> None:
        self.canvas.place(**kwargs)

    def pack(self, **kwargs) -> None:
        self.canvas.pack(**kwargs)

    def grid(self, **kwargs) -> None:
        self.canvas.grid(**kwargs)

    def set_output_levels(self, levels: list[float]) -> None:
        """JARVIS çıkış ses seviyelerini güncelle (0.0–1.0, 32 değer)."""
        with self._lock:
            for i in range(min(_BAR_COUNT, len(levels))):
                self._output_target[i] = max(0.0, min(1.0, levels[i]))

    def set_input_levels(self, levels: list[float]) -> None:
        """Mikrofon giriş ses seviyelerini güncelle (0.0–1.0, 32 değer)."""
        with self._lock:
            for i in range(min(_BAR_COUNT, len(levels))):
                self._input_target[i] = max(0.0, min(1.0, levels[i]))

    def set_output_rms(self, rms: float) -> None:
        """Tek RMS değerinden tüm output bar'larını güncelle."""
        amp = max(0.0, min(1.0, rms))
        levels = [amp * (0.5 + 0.5 * math.sin(i * 0.4)) for i in range(_BAR_COUNT)]
        self.set_output_levels(levels)

    def set_input_rms(self, rms: float) -> None:
        """Tek RMS değerinden tüm input bar'larını güncelle."""
        amp = max(0.0, min(1.0, rms))
        levels = [amp * (0.5 + 0.5 * math.cos(i * 0.35)) for i in range(_BAR_COUNT)]
        self.set_input_levels(levels)

    def set_muted(self, muted: bool) -> None:
        """Susturulmuş durumu ayarla (Req 31.3)."""
        self._muted = muted

    def set_paused(self, paused: bool) -> None:
        """Duraklatılmış durumu ayarla (Req 31.4)."""
        self._paused = paused

    def stop(self) -> None:
        """Animasyonu durdur."""
        self._animating = False

    # ---------------------------------------------------------------- internal

    def _init_bars(self) -> None:
        """Bar canvas item'larını oluştur."""
        bar_w = max(2, (self._width - 4) // (_BAR_COUNT * 2))
        gap = bar_w
        half_h = self._height // 2

        for i in range(_BAR_COUNT):
            x0 = 2 + i * (bar_w + gap)
            x1 = x0 + bar_w
            # Output bar (üst yarı, aşağı doğru büyür)
            bar_id = self.canvas.create_rectangle(
                x0, half_h, x1, half_h, fill=_C_PRI, outline=""
            )
            self._output_bars.append(bar_id)
            # Input bar (alt yarı, yukarı doğru büyür)
            bar_id = self.canvas.create_rectangle(
                x0, half_h, x1, half_h, fill=_C_BLUE, outline=""
            )
            self._input_bars.append(bar_id)

    def _start_animation(self) -> None:
        self._animating = True
        self.canvas.after(_TICK_MS, self._tick)

    def _tick(self) -> None:
        if not self._animating:
            return

        muted = self._muted or self._paused
        bar_w = max(2, (self._width - 4) // (_BAR_COUNT * 2))
        gap = bar_w
        half_h = self._height // 2
        max_bar_h = half_h - 2

        with self._lock:
            out_targets = list(self._output_target)
            in_targets = list(self._input_target)

        for i in range(_BAR_COUNT):
            # Smooth interpolation
            self._output_buf[i] += (out_targets[i] - self._output_buf[i]) * 0.25
            self._input_buf[i] += (in_targets[i] - self._input_buf[i]) * 0.25

            x0 = 2 + i * (bar_w + gap)
            x1 = x0 + bar_w

            if muted:
                # Sönük renk, sabit küçük yükseklik
                out_h = 2
                in_h = 2
                out_color = _C_MUTED
                in_color = _C_MUTED
            else:
                out_h = max(2, int(self._output_buf[i] * max_bar_h))
                in_h = max(2, int(self._input_buf[i] * max_bar_h))
                out_color = self._color_output
                in_color = self._color_input

            # Output bar (üst yarı)
            self.canvas.coords(
                self._output_bars[i],
                x0, half_h - out_h, x1, half_h,
            )
            self.canvas.itemconfig(self._output_bars[i], fill=out_color)

            # Input bar (alt yarı)
            self.canvas.coords(
                self._input_bars[i],
                x0, half_h, x1, half_h + in_h,
            )
            self.canvas.itemconfig(self._input_bars[i], fill=in_color)

        # Muted/paused durumunda hedefleri sıfırla
        if muted:
            with self._lock:
                self._output_target = [0.0] * _BAR_COUNT
                self._input_target = [0.0] * _BAR_COUNT

        try:
            self.canvas.after(_TICK_MS, self._tick)
        except Exception:
            self._animating = False


__all__ = ["WaveformWidget"]
