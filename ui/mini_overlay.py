"""Mini Overlay — modern dalgalı halka tasarımı.

Görsel:
- Kompakt kare pencere
- Ortada animasyonlu ince halkalar (her biri kendi hızında, hafif ovallikle döner)
- Halkanın merkezinde "JARVIS" yazısı
- Altta küçük durum göstergesi (renkli nokta + metin)
- Renk durumla anlık değişir
- Başlık çubuğu yok, her zaman üstte, sürüklenebilir
- Çift tık → ana HUD'a dön, mini gizlenir
- Sağ tık → bağlam menüsü
"""

from __future__ import annotations

import math
import tkinter as tk
from typing import Callable

# Durum → RGB
_STATE_COLORS_RGB: dict[str, tuple[int, int, int]] = {
    "LISTENING":    (164, 126, 255),
    "SPEAKING":     (218, 205, 255),
    "THINKING":     (118, 156, 255),
    "MUTED":        (200, 30, 80),    # kırmızı-pembe
    "PAUSED":       (88, 74, 132),
    "ERROR":        (255, 51, 68),    # kırmızı
    "INITIALISING": (160, 110, 255),  # mor (varsayılan)
}

_STATE_LABELS: dict[str, str] = {
    "LISTENING":    "Listening",
    "SPEAKING":     "Speaking",
    "THINKING":     "Thinking",
    "MUTED":        "Muted",
    "PAUSED":       "Paused",
    "ERROR":        "Error",
    "INITIALISING": "Initialising",
}

# Pencere
_W = 260
_H = 260
_MARGIN = 12

# Halka parametreleri
_RING_COUNT = 5


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    r, g, b = rgb
    return f"#{r:02x}{g:02x}{b:02x}"


def _blend(rgb: tuple[int, int, int], factor: float) -> str:
    r, g, b = rgb
    r = max(0, min(255, int(r * factor)))
    g = max(0, min(255, int(g * factor)))
    b = max(0, min(255, int(b * factor)))
    return f"#{r:02x}{g:02x}{b:02x}"


def _mix(rgb: tuple[int, int, int], bg: tuple[int, int, int], alpha: float) -> str:
    """rgb'yi bg üzerine alpha (0..1) ile karıştır — şeffaflık simülasyonu."""
    r = int(rgb[0] * alpha + bg[0] * (1 - alpha))
    g = int(rgb[1] * alpha + bg[1] * (1 - alpha))
    b = int(rgb[2] * alpha + bg[2] * (1 - alpha))
    return f"#{r:02x}{g:02x}{b:02x}"


_BG_RGB = (4, 6, 14)  # arka plan koyu lacivert
_C_BG   = _rgb_to_hex(_BG_RGB)


class MiniOverlay:
    """Modern dalgalı halka mini display."""

    def __init__(
        self,
        root: tk.Tk,
        *,
        on_show_main: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
    ) -> None:
        self._root = root
        self._on_show_main = on_show_main
        self._on_quit = on_quit
        self._state = "INITIALISING"
        self._drag_x = 0
        self._drag_y = 0
        self._tick = 0
        self._visible = False

        # Sağ üst köşe — ekran kenarına olabildiğince yakın
        sw = root.winfo_screenwidth()
        x = sw - _W - _MARGIN
        y = _MARGIN

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.95)
        self._win.configure(bg=_C_BG)
        self._win.geometry(f"{_W}x{_H}+{x}+{y}")
        self._win.resizable(False, False)
        self._win.withdraw()

        # Canvas — pencere ile aynı boyut
        self._canvas = tk.Canvas(
            self._win,
            width=_W, height=_H,
            bg=_C_BG,
            highlightthickness=0,
        )
        self._canvas.pack(fill="both", expand=True)

        # Her halkanın bağımsız parametreleri
        self._rings = [
            {
                "phase":      i * 0.7,
                "spin_speed": 0.012 + i * 0.004,    # her halka farklı hızda döner
                "wobble":     0.10 + i * 0.03,      # ovallik miktarı
                "wobble_freq": 0.020 + i * 0.005,
                "radius_frac": 0.95 - i * 0.13,     # iç içe halkalar
                "thickness":  3 - i * 0.3,
                "alpha":      0.85 - i * 0.10,
            }
            for i in range(_RING_COUNT)
        ]
        self._particles = [
            {
                "angle": i * 2.3999632297,
                "orbit": 0.23 + (i % 31) / 31 * 0.78,
                "speed": 0.0025 + (i % 11) * 0.00055,
                "phase": i * 0.61,
                "size": 0.8 + (i % 5) * 0.28,
                "depth": 0.35 + (i % 13) / 13 * 0.65,
            }
            for i in range(96)
        ]

        # Bağlamalar
        self._canvas.bind("<ButtonPress-1>",   self._on_drag_start)
        self._canvas.bind("<B1-Motion>",       self._on_drag_motion)
        self._canvas.bind("<Double-Button-1>", self._on_double_click)
        self._canvas.bind("<Button-3>",        self._on_right_click)

        # Animasyon
        self._animating = True
        self._animate()

    # ------------------------------------------------------------------ public

    def set_state(self, state: str) -> None:
        self._state = state
        try:
            self._win.after(0, self._draw)
        except Exception:
            pass

    def show(self) -> None:
        self._visible = True
        try:
            self._win.deiconify()
            self._win.attributes("-topmost", True)
            self._win.lift()
        except Exception:
            pass

    def hide(self) -> None:
        self._visible = False
        try:
            self._win.withdraw()
        except Exception:
            pass

    def destroy(self) -> None:
        self._animating = False
        try:
            self._win.destroy()
        except Exception:
            pass

    def is_visible(self) -> bool:
        return self._visible

    # ---------------------------------------------------------------- drawing

    def _draw(self) -> None:
        c = self._canvas
        c.delete("all")

        rgb = _STATE_COLORS_RGB.get(self._state, (160, 110, 255))
        label = _STATE_LABELS.get(self._state, self._state.title())

        cx = _W // 2
        cy = _H // 2 - 4
        max_r = min(_W, _H) // 2 - 16

        t = self._tick
        energy = {
            "SPEAKING": 1.0,
            "THINKING": 0.78,
            "INITIALISING": 0.62,
            "LISTENING": 0.54,
            "MUTED": 0.22,
            "PAUSED": 0.14,
            "ERROR": 0.85,
        }.get(self._state, 0.45)
        breath = 1.0 + (0.018 + 0.035 * energy) * math.sin(t * 0.045)
        voice = 1.0
        if self._state == "SPEAKING":
            voice += 0.065 * math.sin(t * 0.24) + 0.030 * math.sin(t * 0.41 + 0.8)
        elif self._state == "LISTENING":
            voice += 0.018 * math.sin(t * 0.11 + 1.7)
        max_r = int(max_r * breath * voice)

        # Soft living ribbons inspired by the purple reference orb.
        for band in range(8):
            pts = []
            phase = t * (0.006 + band * 0.0012) + band * 0.73
            spin = phase * (1 if band % 2 == 0 else -1)
            cos_s = math.cos(spin)
            sin_s = math.sin(spin)
            base_r = max_r * (0.58 + band * 0.055)
            a = base_r * (1.18 + 0.10 * math.sin(phase + band))
            b = base_r * (0.72 + 0.08 * math.cos(phase * 1.3))
            for k in range(74):
                ang = k * math.tau / 73
                wave = 1.0 + 0.055 * math.sin(ang * 3 + phase * 4.0)
                ex = a * math.cos(ang) * wave
                ey = b * math.sin(ang) * (1.0 + 0.04 * math.cos(ang * 2 - phase))
                rx = ex * cos_s - ey * sin_s
                ry = ex * sin_s + ey * cos_s
                pts.extend([cx + rx, cy + ry])
            alpha = 0.13 + 0.055 * energy + band * 0.012
            c.create_line(
                *pts,
                fill=_mix(rgb, _BG_RGB, min(0.42, alpha)),
                width=1 + (1 if band in (2, 5) else 0),
                smooth=True,
                capstyle="round",
            )

        # Dim particle veil: gives the orb a body without making the window busy.
        for p in self._particles:
            angle = p["angle"] + t * p["speed"] * (0.55 + energy * 2.0)
            orbit = max_r * p["orbit"] * (1.0 + 0.06 * math.sin(t * 0.035 + p["phase"]))
            depth = 0.45 + 0.55 * math.sin(angle * 1.7 + p["phase"] + t * 0.006)
            x = cx + math.cos(angle) * orbit
            y = cy + math.sin(angle) * orbit * (0.70 + 0.22 * depth)
            alpha = (0.06 + 0.18 * depth) * (0.35 + energy * 0.75)
            if self._state in ("PAUSED", "MUTED"):
                alpha *= 0.45
            color = _mix(rgb, _BG_RGB, min(0.32, alpha))
            r = p["size"] * (0.75 + depth * 0.75 + energy * 0.25)
            c.create_oval(x - r, y - r, x + r, y + r, fill=color, outline="")

        # Halkalar — her biri farklı eğimde, farklı hızda dönen oval
        for i, ring in enumerate(self._rings):
            spin = ring["phase"] + t * ring["spin_speed"]
            wobble_amount = ring["wobble"] * (1 + 0.3 * math.sin(t * ring["wobble_freq"])) * (1 + energy * 0.18)
            base_r = max_r * ring["radius_frac"]
            color = _mix(rgb, _BG_RGB, min(0.95, ring["alpha"] * (0.72 + energy * 0.30)))
            thickness = max(1, int(ring["thickness"] + (1 if self._state == "SPEAKING" and i < 2 else 0)))

            # Oval halkayı parametrik çiz — 64 nokta, ekseni dönen elips
            pts = []
            angle_step = math.tau / 64
            cos_s = math.cos(spin)
            sin_s = math.sin(spin)
            a = base_r * (1 + wobble_amount)
            b = base_r * (1 - wobble_amount * 0.6)
            for k in range(65):
                ang = k * angle_step
                # Eksen dönüşü
                ex = a * math.cos(ang)
                ey = b * math.sin(ang)
                rx = ex * cos_s - ey * sin_s
                ry = ex * sin_s + ey * cos_s
                pts.extend([cx + rx, cy + ry])
            c.create_line(*pts, fill=color, width=thickness, smooth=True, capstyle="round")

        # Halo glow (içten dışa hafif parlaklık)
        for r_off, alpha in ((max_r + 4, 0.18), (max_r + 10, 0.10), (max_r + 16, 0.05)):
            color = _mix(rgb, _BG_RGB, alpha)
            c.create_oval(
                cx - r_off, cy - r_off, cx + r_off, cy + r_off,
                outline=color, width=1,
            )

        # Ortada "JARVIS" yazısı
        c.create_text(
            cx, cy,
            text="J.A.R.V.I.S",
            fill="#f7f2ff",
            font=("Grift", 14, "bold"),
        )

        # Alt durum göstergesi
        status_y = _H - 22
        # nokta
        dot_r = 4
        col_main = _rgb_to_hex(rgb)
        c.create_oval(
            cx - 38 - dot_r, status_y - dot_r,
            cx - 38 + dot_r, status_y + dot_r,
            fill=col_main, outline="",
        )
        # metin
        c.create_text(
            cx - 28, status_y,
            text=label,
            fill=col_main,
            font=("Grift", 10, "bold"),
            anchor="w",
        )

        # Sağ üst — kapatma "×" işareti (sağ tıka alternatif)
        c.create_text(
            _W - 14, 12,
            text="×",
            fill=_mix(rgb, _BG_RGB, 0.5),
            font=("Grift", 14, "bold"),
            tags=("close_btn",),
        )
        c.tag_bind("close_btn", "<Button-1>", lambda e: self.hide())
        c.tag_bind("close_btn", "<Enter>", lambda e: c.itemconfigure("close_btn", fill="#ffffff"))
        c.tag_bind("close_btn", "<Leave>", lambda e: c.itemconfigure("close_btn", fill=_mix(rgb, _BG_RGB, 0.5)))

    def _animate(self) -> None:
        if not self._animating:
            return
        self._tick += 1
        self._draw()
        try:
            self._win.after(33, self._animate)  # ~30 FPS
        except Exception:
            self._animating = False

    # ── Sürükleme ────────────────────────────────────────────────────────────

    def _on_drag_start(self, event: tk.Event) -> None:
        self._drag_x = event.x_root - self._win.winfo_x()
        self._drag_y = event.y_root - self._win.winfo_y()

    def _on_drag_motion(self, event: tk.Event) -> None:
        x = event.x_root - self._drag_x
        y = event.y_root - self._drag_y
        # Tüm monitörleri kapsa: sadece üst sınırı 0 yap, diğer yönlerde
        # ekran sınırlarına takılma (çoklu monitör için).
        # Yine de aşırı uçtan kaçınmak için pencerenin %10'u görünür kalsın.
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        # En sağ-üst köşeye gidebilsin diye alt sınır gevşek
        x = max(-_W // 2, min(x, sw - _W // 4))
        y = max(0, min(y, sh - _H // 4))
        self._win.geometry(f"+{x}+{y}")

    # ── Tıklama ──────────────────────────────────────────────────────────────

    def _on_double_click(self, event: tk.Event) -> None:
        """Çift tık: HUD'u aç ve mini'yi kapat."""
        self.hide()
        if self._on_show_main:
            try:
                self._on_show_main()
            except Exception:
                pass

    def _on_right_click(self, event: tk.Event) -> None:
        menu = tk.Menu(
            self._win, tearoff=0,
            bg=_C_BG, fg="#ffffff",
            activebackground="#1a1a2e", activeforeground="#ffffff",
            font=("Grift", 10),
            borderwidth=0,
        )
        menu.add_command(label="Ana Pencereyi Aç", command=self._cmd_show_main)
        menu.add_command(label="Mini Modu Kapat",  command=self.hide)
        menu.add_separator()
        menu.add_command(label="Çıkış",            command=self._cmd_quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _cmd_show_main(self) -> None:
        self.hide()
        if self._on_show_main:
            try:
                self._on_show_main()
            except Exception:
                pass

    def _cmd_quit(self) -> None:
        if self._on_quit:
            try:
                self._on_quit()
            except Exception:
                pass


__all__ = ["MiniOverlay"]
