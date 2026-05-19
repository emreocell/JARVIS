"""Sparkline — CPU/RAM/GPU performans grafiği bileşeni.

Design.md § 19 ve Requirements § 32'ye karşılık gelir.

Sorumluluklar
-------------
* psutil.cpu_percent, virtual_memory().percent, pynvml GPU (Req 32.1, 32.3).
* 1 sn örnekleme, 60 örnek tampon (Req 32.2).
* Sağa hizalı sabit genişlikli sayısal gösterim (Req 32.4).
* NVML yoksa "N/A" (Req 32.3).
"""

from __future__ import annotations

import threading
import time
import tkinter as tk
from collections import deque
from typing import Optional

import psutil

# Renk sabitleri
_C_BG = "#020c0c"
_C_PRI = "#00d4c0"
_C_TEXT = "#7dfff6"
_C_DIM = "#0a2a28"
_C_MID = "#006a62"
_C_GOLD = "#ffcc00"
_C_GREEN = "#00ff88"
_C_RED = "#ff3344"
_C_BLUE = "#4488ff"

_SAMPLE_INTERVAL_SEC = 1.0
_BUFFER_SIZE = 60
_SPARKLINE_W = 80
_SPARKLINE_H = 22


def _try_init_nvml() -> bool:
    """pynvml başlatmayı dene; başarısızsa False döner."""
    try:
        import pynvml
        pynvml.nvmlInit()
        return True
    except Exception:
        return False


class SparklineWidget:
    """CPU, RAM ve GPU sparkline'larını gösteren bileşen.

    Parameters
    ----------
    parent:
        Tk parent widget.
    """

    def __init__(self, parent: tk.Widget) -> None:
        self._parent = parent
        self._lock = threading.Lock()

        # Veri buffer'ları
        self._cpu_buf: deque[float] = deque([0.0] * _BUFFER_SIZE, maxlen=_BUFFER_SIZE)
        self._ram_buf: deque[float] = deque([0.0] * _BUFFER_SIZE, maxlen=_BUFFER_SIZE)
        self._gpu_buf: deque[float] = deque([0.0] * _BUFFER_SIZE, maxlen=_BUFFER_SIZE)

        # NVML
        self._nvml_ok = _try_init_nvml()
        self._gpu_handle = None
        if self._nvml_ok:
            try:
                import pynvml
                self._gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
            except Exception:
                self._nvml_ok = False

        # Ana çerçeve
        self.frame = tk.Frame(parent, bg=_C_BG)

        # CPU satırı
        self._cpu_canvas = self._make_canvas()
        self._cpu_label = self._make_label("CPU")
        self._cpu_val = self._make_value("  0%")

        # RAM satırı
        self._ram_canvas = self._make_canvas()
        self._ram_label = self._make_label("RAM")
        self._ram_val = self._make_value("  0%")

        # GPU satırı
        self._gpu_canvas = self._make_canvas()
        self._gpu_label = self._make_label("GPU")
        self._gpu_val = self._make_value("N/A" if not self._nvml_ok else "  0%")

        self._layout()

        # Örnekleme thread'i
        self._running = True
        self._thread = threading.Thread(
            target=self._sample_loop, daemon=True, name="jarvis-sparkline"
        )
        self._thread.start()

        # Çizim döngüsü
        self._draw()

    # ------------------------------------------------------------------ public

    def place(self, **kwargs) -> None:
        self.frame.place(**kwargs)

    def pack(self, **kwargs) -> None:
        self.frame.pack(**kwargs)

    def stop(self) -> None:
        """Örnekleme thread'ini durdur."""
        self._running = False

    # ---------------------------------------------------------------- internal

    def _make_canvas(self) -> tk.Canvas:
        return tk.Canvas(
            self.frame,
            width=_SPARKLINE_W,
            height=_SPARKLINE_H,
            bg=_C_BG,
            highlightthickness=0,
        )

    def _make_label(self, text: str) -> tk.Label:
        return tk.Label(
            self.frame,
            text=text,
            bg=_C_BG,
            fg=_C_MID,
            font=("Grift", 8, "bold"),
            width=4,
            anchor="w",
        )

    def _make_value(self, text: str) -> tk.Label:
        return tk.Label(
            self.frame,
            text=text,
            bg=_C_BG,
            fg=_C_TEXT,
            font=("Grift", 9),
            width=5,
            anchor="e",
        )

    def _layout(self) -> None:
        """Widget'ları grid ile yerleştir."""
        rows = [
            (self._cpu_label, self._cpu_canvas, self._cpu_val),
            (self._ram_label, self._ram_canvas, self._ram_val),
            (self._gpu_label, self._gpu_canvas, self._gpu_val),
        ]
        for r, (lbl, canvas, val) in enumerate(rows):
            lbl.grid(row=r, column=0, padx=(4, 2), pady=1, sticky="w")
            canvas.grid(row=r, column=1, padx=2, pady=1)
            val.grid(row=r, column=2, padx=(2, 4), pady=1, sticky="e")

    def _sample_loop(self) -> None:
        """Arka planda 1 sn aralıklarla örnekle."""
        while self._running:
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            gpu = self._read_gpu()

            with self._lock:
                self._cpu_buf.append(cpu)
                self._ram_buf.append(ram)
                self._gpu_buf.append(gpu if gpu is not None else 0.0)

            time.sleep(_SAMPLE_INTERVAL_SEC)

    def _read_gpu(self) -> Optional[float]:
        if not self._nvml_ok or self._gpu_handle is None:
            return None
        try:
            import pynvml
            util = pynvml.nvmlDeviceGetUtilizationRates(self._gpu_handle)
            return float(util.gpu)
        except Exception:
            return None

    def _draw(self) -> None:
        """Sparkline'ları çiz ve 1 sn sonra tekrar çağır."""
        with self._lock:
            cpu_data = list(self._cpu_buf)
            ram_data = list(self._ram_buf)
            gpu_data = list(self._gpu_buf)

        self._draw_sparkline(self._cpu_canvas, cpu_data, _C_PRI)
        self._draw_sparkline(self._ram_canvas, ram_data, _C_BLUE)
        self._draw_sparkline(self._gpu_canvas, gpu_data, _C_GOLD)

        # Sayısal değerleri güncelle
        cpu_val = cpu_data[-1] if cpu_data else 0.0
        ram_val = ram_data[-1] if ram_data else 0.0

        self._cpu_val.config(text=f"{cpu_val:3.0f}%")
        self._ram_val.config(text=f"{ram_val:3.0f}%")

        if self._nvml_ok:
            gpu_val = gpu_data[-1] if gpu_data else 0.0
            self._gpu_val.config(text=f"{gpu_val:3.0f}%")
        else:
            self._gpu_val.config(text=" N/A", fg=_C_MID)

        try:
            self.frame.after(int(_SAMPLE_INTERVAL_SEC * 1000), self._draw)
        except Exception:
            pass

    @staticmethod
    def _draw_sparkline(canvas: tk.Canvas, data: list[float], color: str) -> None:
        """Veri noktalarından sparkline çiz."""
        canvas.delete("all")
        if not data:
            return

        w = _SPARKLINE_W
        h = _SPARKLINE_H
        n = len(data)
        max_val = max(data) if max(data) > 0 else 100.0

        points = []
        for i, val in enumerate(data):
            x = int(i * (w - 2) / max(n - 1, 1)) + 1
            y = h - 2 - int((val / max_val) * (h - 4))
            points.extend([x, y])

        if len(points) >= 4:
            canvas.create_line(points, fill=color, width=1, smooth=True)

        # Son değer noktası
        if points:
            lx, ly = points[-2], points[-1]
            canvas.create_oval(lx - 2, ly - 2, lx + 2, ly + 2, fill=color, outline="")


__all__ = ["SparklineWidget"]
