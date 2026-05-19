"""
JARVIS Windows — UI v3
Concentric teal rings · Segmented arcs
Alp Ünlü tarafından yapılmıştır — @alppunlu
Windows uyarlaması
"""

import os, time, math, random, threading, ctypes
import tkinter as tk
from collections import deque
from pathlib import Path
import psutil
from PIL import Image, ImageTk

from app_config import has_gemini_api_key, load_app_config, save_app_config
from actions.weather import get_weather_summary

BASE_DIR = Path(__file__).resolve().parent.parent

SYSTEM_NAME = "J.A.R.V.I.S"
MODEL_BADGE = "VOICE CORE · Windows"

# ── Renk paleti ──────────────────────────────────────────────────────────────
C_BG      = "#050611"
C_PRI     = "#8f7cff"
C_ORG     = "#ff6600"
C_ORG2    = "#ff9900"
C_MID     = "#3f3a73"
C_DIM     = "#15182b"
C_DIMMER  = "#0a0c18"
C_TEXT    = "#d8f6ff"
C_PANEL   = "#0a0c18"
C_SURFACE = "#090b17"
C_SURFACE_2 = "#0d1122"
C_LINE    = "#26244a"
C_GREEN   = "#00ff88"
C_RED     = "#ff3344"
C_MUTED   = "#cc2255"
C_BLUE    = "#6d9cff"
C_GOLD    = "#ffcc00"
C_VIOLET  = "#9b78ff"
C_LAV     = "#d8ccff"

# Orb durum renkleri
ORB_COLORS = {
    "LISTENING":    (164, 126, 255),
    "SPEAKING":     (218, 205, 255),
    "THINKING":     (118, 156, 255),
    "MUTED":        (200, 30, 80),
    "PAUSED":       (68, 58, 104),
    "ERROR":        (255, 51, 68),
    "INITIALISING": (160, 110, 255),
}

# ── Boyutlar ─────────────────────────────────────────────────────────────────
W_TARGET = 2200
H_TARGET = 1320
LEFT_W_T = 360
RIGHT_W_T = 410
HDR_H    = 72
FOOTER_H = 26
INPUT_H  = 34
CONTROL_H = 146

VOICES = ["Charon", "Puck", "Aoede", "Kore", "Fenrir", "Leda", "Orus", "Zephyr"]

# ── Font sistemi ─────────────────────────────────────────────────────────────
# Grift fontu kullanıcının sisteminde yüklü. Basliklarda daha sert bir vurgu
# icin ayri extra bold aile adini kullaniyoruz.
FONT_BODY_FAMILY = "Grift"
FONT_DISPLAY_FAMILY = "Grift Extra Bold"


def font_body(size: int):
    return (FONT_BODY_FAMILY, size)


def font_body_bold(size: int):
    return (FONT_BODY_FAMILY, size, "bold")


def font_display(size: int):
    return (FONT_DISPLAY_FAMILY, size)


STATE_HEX_COLORS = {
    "LISTENING": C_VIOLET,
    "SPEAKING": C_LAV,
    "THINKING": "#769cff",
    "INITIALISING": C_VIOLET,
    "ERROR": C_RED,
}


# ── SoundManager ─────────────────────────────────────────────────────────────
import subprocess as _sp

def _resolve_sfx_dir() -> Path:
    return BASE_DIR / "SFX"


_SFX_DIR = _resolve_sfx_dir()
_HUD_FILE = _SFX_DIR / "HUD.mp3"
_START_FILE = _SFX_DIR / "Start.mp3"
_THINK_FILE = _SFX_DIR / "Think.mp3"
_DONE_FILE = _SFX_DIR / "Done.mp3"
_ERROR_FILE = _SFX_DIR / "Error.mp3"


class _PygameMusicProc:
    """subprocess benzeri bir arayuzle pygame muzik oynatimini izler."""

    def __init__(self):
        self._stopped = False

    def poll(self):
        if self._stopped:
            return 0
        try:
            import pygame.mixer
            return None if pygame.mixer.music.get_busy() else 0
        except Exception:
            return 0

    def terminate(self):
        self._stopped = True
        try:
            import pygame.mixer
            pygame.mixer.music.stop()
        except Exception:
            pass

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        start = time.time()
        while self.poll() is None:
            if timeout is not None and (time.time() - start) >= float(timeout):
                break
            time.sleep(0.05)
        return 0


class SoundManager:
    def __init__(self):
        self._enabled = True
        self._ambient_proc = None
        self._volume = 0.20
        self._ambient_stop = None
        self._ambient_thread = None
        self._foreground_proc = None
        self._foreground_stop = None
        self._foreground_thread = None
        self._foreground_tag = ""
        self._all_sound_procs = set()
        self._lock = threading.RLock()
        # HUD bağlandığında JarvisUI tarafından doldurulur; "AUDIO" debug log
        # girişlerini yazmak için kullanılır.
        self._ui_log = None
        # Windows için pygame.mixer
        try:
            import pygame.mixer
            pygame.mixer.init()
            self._has_pygame = True
        except ImportError:
            self._has_pygame = False

    @staticmethod
    def _terminate_process(proc):
        if not proc:
            return
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            proc.wait(timeout=0.6)
        except Exception:
            pass

    def _play_with_pygame(self, path: Path, volume: float):
        """Windows'ta pygame ile ses çal."""
        if not self._has_pygame:
            return None
        try:
            import pygame.mixer
            pygame.mixer.music.set_volume(volume)
            pygame.mixer.music.load(str(path))
            pygame.mixer.music.play()
            return _PygameMusicProc()
        except Exception:
            return None

    def _start_audio_playback(self, path: Path, volume: float):
        """Ses dosyasını çal: önce pygame, başarısızsa Windows PowerShell SoundPlayer fallback.

        Hata durumunda HUD debug log'una "AUDIO" etiketiyle (level=WARN/ERROR) yazar.
        """
        # Önce pygame ile dene (Windows'ta birincil yol)
        if self._has_pygame:
            result = self._play_with_pygame(path, volume)
            if result:
                return result
            self._log_audio_error(
                f"pygame oynatımı başarısız, PowerShell SoundPlayer fallback'e geçiliyor: {path.name}",
                level="WARN",
            )

        # Fallback: System.Media.SoundPlayer üzerinden Windows PowerShell
        try:
            command = (
                "$ErrorActionPreference='Stop';"
                "$p=New-Object System.Media.SoundPlayer '" + str(path).replace("'", "''") + "';"
                "$p.PlaySync()"
            )
            creationflags = getattr(_sp, "CREATE_NO_WINDOW", 0)
            proc = _sp.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                stdout=_sp.DEVNULL,
                stderr=_sp.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as exc:
            self._log_audio_error(
                f"SoundPlayer fallback başlatılamadı ({path.name}): {exc}",
                level="ERROR",
            )
            raise
        with self._lock:
            self._all_sound_procs.add(proc)
        return proc

    def _log_audio_error(self, message: str, level: str = "ERROR") -> None:
        """SoundManager hatalarını HUD debug log'una "AUDIO" etiketiyle yazar.

        UI henüz bağlanmamışsa (örn. erken init veya headless test) sessizce yutar.
        """
        ui = getattr(self, "_ui_log", None)
        if ui is None:
            return
        try:
            ui.write_debug(f"[AUDIO] {message}", level=level)
        except Exception:
            # UI henüz hazır değilse veya thread güvenli değilse görmezden gel.
            pass

    def _forget_process(self, proc):
        if not proc:
            return
        with self._lock:
            self._all_sound_procs.discard(proc)

    def start_ambient(self):
        if not _HUD_FILE.exists():
            return
        with self._lock:
            if not self._enabled:
                return
            if self._foreground_proc and self._foreground_proc.poll() is None:
                return
            if self._ambient_thread and self._ambient_thread.is_alive():
                return
            stop_event = threading.Event()
            worker = threading.Thread(
                target=self._loop_ambient,
                args=(stop_event,),
                daemon=True,
            )
            self._ambient_stop = stop_event
            self._ambient_thread = worker
        worker.start()

    def _loop_ambient(self, stop_event: threading.Event):
        while not stop_event.is_set():
            with self._lock:
                if not self._enabled or self._ambient_stop is not stop_event:
                    break
                volume = self._volume
            try:
                proc = self._start_audio_playback(_HUD_FILE, volume)
            except Exception as exc:
                self._log_audio_error(f"ambient akış başlatılamadı: {exc}", level="ERROR")
                break

            with self._lock:
                if self._ambient_stop is not stop_event or not self._enabled:
                    self._terminate_process(proc)
                    self._forget_process(proc)
                    break
                self._ambient_proc = proc

            while proc.poll() is None and not stop_event.wait(0.2):
                pass

            if stop_event.is_set():
                self._terminate_process(proc)

            with self._lock:
                if self._ambient_proc is proc:
                    self._ambient_proc = None
            if proc.poll() is not None:
                self._forget_process(proc)

            if stop_event.is_set():
                break
            time.sleep(0.2)

        with self._lock:
            if self._ambient_stop is stop_event:
                self._ambient_stop = None
            if self._ambient_thread and self._ambient_thread.ident == threading.get_ident():
                self._ambient_thread = None

    def _stop_ambient(self):
        with self._lock:
            stop_event = self._ambient_stop
            proc = self._ambient_proc
            self._ambient_stop = None
            self._ambient_thread = None
            self._ambient_proc = None
        if stop_event:
            stop_event.set()
        self._terminate_process(proc)
        self._forget_process(proc)

    def _stop_foreground(self):
        with self._lock:
            stop_event = self._foreground_stop
            proc = self._foreground_proc
            self._foreground_stop = None
            self._foreground_thread = None
            self._foreground_proc = None
            self._foreground_tag = ""
        if stop_event:
            stop_event.set()
        self._terminate_process(proc)
        self._forget_process(proc)

    def _play_foreground(
        self,
        path: Path,
        tag: str,
        loop: bool = False,
        volume_factor: float = 1.0,
        pause_ambient: bool = True,
    ):
        if not path.exists():
            return
        with self._lock:
            if not self._enabled:
                return
            if loop and self._foreground_tag == tag and self._foreground_thread and self._foreground_thread.is_alive():
                return
            base_volume = self._volume
        if pause_ambient:
            self._stop_ambient()
        self._stop_foreground()

        stop_event = threading.Event()
        worker = threading.Thread(
            target=self._foreground_worker,
            args=(
                path,
                tag,
                stop_event,
                loop,
                max(0.0, min(1.0, base_volume * volume_factor)),
                pause_ambient,
            ),
            daemon=True,
        )
        with self._lock:
            self._foreground_stop = stop_event
            self._foreground_thread = worker
            self._foreground_tag = tag
        worker.start()

    def _foreground_worker(
        self,
        path: Path,
        tag: str,
        stop_event: threading.Event,
        loop: bool,
        volume: float,
        resume_ambient: bool,
    ):
        while not stop_event.is_set():
            try:
                proc = self._start_audio_playback(path, volume)
            except Exception as exc:
                self._log_audio_error(
                    f"ön plan akışı '{tag}' başlatılamadı: {exc}",
                    level="ERROR",
                )
                break

            with self._lock:
                if self._foreground_stop is not stop_event or not self._enabled:
                    self._terminate_process(proc)
                    self._forget_process(proc)
                    break
                self._foreground_proc = proc

            while proc.poll() is None and not stop_event.wait(0.12):
                pass

            if stop_event.is_set():
                self._terminate_process(proc)

            with self._lock:
                if self._foreground_proc is proc:
                    self._foreground_proc = None
            if proc.poll() is not None:
                self._forget_process(proc)

            if not loop or stop_event.is_set():
                break
            time.sleep(0.08)

        with self._lock:
            if self._foreground_stop is stop_event:
                self._foreground_stop = None
                self._foreground_thread = None
                self._foreground_tag = ""
            should_restart = resume_ambient and self._enabled and self._foreground_stop is None
        if should_restart:
            self.start_ambient()

    def play_startup(self):
        self._play_foreground(_START_FILE, tag="start", loop=False, volume_factor=0.95)

    def play_success(self):
        self._play_foreground(
            _DONE_FILE,
            tag="done",
            loop=False,
            volume_factor=0.68,
            pause_ambient=False,
        )

    def play_error(self):
        self._play_foreground(_ERROR_FILE, tag="error", loop=False, volume_factor=0.95)

    def start_thinking(self):
        self._play_foreground(
            _THINK_FILE,
            tag="think",
            loop=True,
            volume_factor=0.82,
            pause_ambient=False,
        )

    def stop_thinking(self):
        with self._lock:
            is_thinking = self._foreground_tag == "think"
        if is_thinking:
            self._stop_foreground()

    def toggle(self) -> bool:
        self.set_enabled(not self._enabled)
        return self._enabled

    def set_enabled(self, enabled: bool):
        enabled = bool(enabled)
        with self._lock:
            self._enabled = enabled
        if enabled:
            self.start_ambient()
        else:
            self._stop_ambient()
            self._stop_foreground()

    def set_volume(self, volume: float):
        with self._lock:
            self._volume = max(0.0, min(1.0, float(volume)))
            fg_tag = self._foreground_tag
            can_restart_ambient = self._enabled and not fg_tag
        if fg_tag == "think":
            self._stop_foreground()
            self.start_thinking()
        elif can_restart_ambient:
            self._stop_ambient()
            self.start_ambient()

    def stop_all(self):
        with self._lock:
            self._enabled = False
            ambient_stop = self._ambient_stop
            foreground_stop = self._foreground_stop
            procs = {
                proc
                for proc in (
                    self._ambient_proc,
                    self._foreground_proc,
                    *self._all_sound_procs,
                )
                if proc
            }
            self._ambient_stop = None
            self._ambient_thread = None
            self._ambient_proc = None
            self._foreground_stop = None
            self._foreground_thread = None
            self._foreground_proc = None
            self._foreground_tag = ""
            self._all_sound_procs.clear()
        if ambient_stop:
            ambient_stop.set()
        if foreground_stop:
            foreground_stop.set()
        for proc in procs:
            self._terminate_process(proc)

    def get_volume(self) -> float:
        return self._volume


# ─────────────────────────────────────────────────────────────────────────────

# ── DPI Farkındalığı (Req 14.1, 14.2, 14.3) ─────────────────────────────────
# Windows PerMonitorV2 awareness sabiti.  SetProcessDpiAwarenessContext'in
# beklediği DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 değeri -4'tür.
_DPI_AWARENESS_PER_MONITOR_V2 = -4


def _enable_per_monitor_v2_dpi():
    """PerMonitorV2 DPI farkındalığını etkinleştir.

    Windows 10 1703+ için ``SetProcessDpiAwarenessContext(-4)`` çağrılır.
    API erişilemez veya çağrı başarısız olursa Tk default ölçeklemesine düşeriz
    ve sebebi geri döndürürüz; çağıran taraf bunu HUD debug log'una yazar.

    Returns:
        ``(ok, detail)`` — ``ok`` çağrı başarılıysa True, aksi halde False.
        ``detail`` bilgi/hata mesajı (debug log için).
    """
    user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
    if user32 is None:
        return False, "windll.user32 yok (Windows dışı veya headless)"
    fn = getattr(user32, "SetProcessDpiAwarenessContext", None)
    if fn is None:
        return False, "SetProcessDpiAwarenessContext API mevcut değil"
    try:
        result = fn(_DPI_AWARENESS_PER_MONITOR_V2)
    except Exception as exc:  # pragma: no cover - platform bağımlı
        return False, f"SetProcessDpiAwarenessContext çağrısı başarısız: {exc}"
    if not result:
        return False, "SetProcessDpiAwarenessContext 0 döndürdü (zaten ayarlı olabilir)"
    return True, "PerMonitorV2 DPI awareness aktif"


def _query_window_dpi_scale(tk_root) -> float:
    """Verilen Tk root için geçerli monitör DPI ölçek faktörünü döndür.

    Windows'ta önce ``GetDpiForWindow`` denenir; başarısız olursa
    ``GetDpiForSystem`` fallback'i kullanılır.  Her ikisi de erişilemezse 1.0
    (96 DPI baseline) döner.
    """
    user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
    if user32 is None:
        return 1.0
    try:
        hwnd = int(tk_root.winfo_id())
    except Exception:
        hwnd = 0
    get_dpi_for_window = getattr(user32, "GetDpiForWindow", None)
    if get_dpi_for_window is not None and hwnd:
        try:
            dpi = int(get_dpi_for_window(hwnd))
            if dpi > 0:
                return dpi / 96.0
        except Exception:
            pass
    get_dpi_for_system = getattr(user32, "GetDpiForSystem", None)
    if get_dpi_for_system is not None:
        try:
            dpi = int(get_dpi_for_system())
            if dpi > 0:
                return dpi / 96.0
        except Exception:
            pass
    return 1.0


class JarvisUI:
    def __init__(self):
        # ── DPI farkındalığı (Req 14.1, 14.3) ────────────────────────────────
        # Tk root'undan ÖNCE çağrılmalı; aksi halde Windows pencere oluşturma
        # noktasına kadar süreç sistem-DPI farkında olarak işaretlenir ve
        # PerMonitorV2'ye geçiş bazı sürümlerde reddedilir.
        self._dpi_ok, self._dpi_detail = _enable_per_monitor_v2_dpi()

        self.root = tk.Tk()
        self.root.title("J.A.R.V.I.S")
        self.root.update_idletasks()

        # ── DPI ölçek faktörü (Req 14.2) ─────────────────────────────────────
        # Pencere oluştuktan sonra geçerli monitör DPI'ını sorgula; yerleşim
        # hesaplarında çarpan olarak kullanılır.  Hata olursa 1.0'a düşer.
        try:
            self._dpi_scale = float(_query_window_dpi_scale(self.root))
        except Exception:
            self._dpi_scale = 1.0
        if not self._dpi_scale or self._dpi_scale <= 0:
            self._dpi_scale = 1.0

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        margin_x = max(self._scale(24), int(sw * 0.025))
        margin_y = max(self._scale(54), int(sh * 0.055))
        self.W = min(max(self._scale(640), sw - margin_x), sw, self._scale(W_TARGET))
        self.H = min(max(self._scale(520), sh - margin_y), sh, self._scale(H_TARGET))
        _geo = f"{self.W}x{self.H}+{(sw-self.W)//2}+{max(0, (sh-self.H)//2 - 8)}"
        self.root.geometry(_geo)
        self.root.minsize(min(self.W, sw), min(self.H, sh))
        self.root.resizable(True, True)
        self.root.configure(bg=C_BG)
        self.root.attributes('-topmost', True)
        self.root.lift()
        self.root.focus_force()
        # macOS window manager bazen geometry'yi override eder, tekrar zorla.
        for delay in (80, 220, 600, 1200):
            self.root.after(delay, self._force_startup_size)
        # Birkaç saniye sonra topmost'u kapat (normal davranış)
        self.root.after(3000, lambda: self.root.attributes('-topmost', False))

        self._window_geometry = _geo
        self._normal_size = (self.W, self.H)
        self._fullscreen = True

        self._set_layout_metrics(self.W, self.H)

        # ── State ────────────────────────────────────────────────────────────
        self.speaking        = False
        self.user_speaking   = False
        self.muted           = False
        self.paused          = False
        self.scale           = 1.0
        self.target_scale    = 1.0
        self.halo_a          = 55.0
        self.target_halo     = 55.0
        self.last_t          = time.time()
        self.tick            = 0
        self.rings_spin      = [0.0, 45.0, 90.0, 200.0]  # 4 ayrı halka
        self.pulse_r         = []
        self.status_blink    = True
        self._jarvis_state   = "INITIALISING"
        self._user_speaking_until = 0.0

        # ── Health overlay ───────────────────────────────────────────────────
        self._health_visible  = False
        self._health_query    = "all"
        self._health_display  = ""
        self._health_hide_job = None
        self._agent_visible  = False
        self._agent_display  = ""
        self._agent_hide_job = None
        self._weather_card = {
            "city": "Istanbul",
            "primary": "--",
            "details": ["Hava durumu yükleniyor..."],
        }
        self._health_card_lines = ["Sağlık özeti yükleniyor..."]
        self._voice_guard_status = {
            "smart_stop": True,
            "mute_while_speaking": False,
            "cooldown_ms": 1600,
            "preferred_browser": "",
        }
        self._panel_focus = ""
        self._panel_focus_until = 0.0
        self._brief_refresh_busy = False
        self._started_at = time.time()
        self._error_hold_until = 0.0
        self._settings_open = False
        self._settings_tab = "settings"
        self._debug_entries = deque(maxlen=160)
        self._startup_sfx_played = False
        # DPI farkındalığı sonucunu debug log'a düş (Req 14.3).
        try:
            _dpi_msg = (
                f"[DPI] {self._dpi_detail} | scale={self._dpi_scale:.3f}"
                if self._dpi_ok
                else f"[DPI] Tk default'a düşüldü: {self._dpi_detail} | scale={self._dpi_scale:.3f}"
            )
            self.write_debug(_dpi_msg, level="INFO" if self._dpi_ok else "WARN")
        except Exception:
            pass
        self._settings_geometry = {
            "btn_x": 14,
            "btn_y": 12,
            "btn_w": 250,
            "btn_h": 46,
            "panel_x": 14,
            "panel_y": HDR_H + 10,
            "panel_w": 430,
            "panel_h": 560,
        }
        self.setup_frame = None
        self.api_entry = None
        self.nvidia_api_entry = None
        self.youtube_api_entry = None
        self.youtube_handle_entry = None

        # ── Callbacks ────────────────────────────────────────────────────────
        self.on_text_command = None
        self.on_pause_toggle = None
        self.on_stop_command = None
        self.on_voice_change = None
        self.on_effects_state_change = None
        self.on_voice_control_change = None

        # ── Voice ────────────────────────────────────────────────────────────
        self._current_voice = self._load_voice()

        # ── Sound ────────────────────────────────────────────────────────────
        self.sound = SoundManager()
        # SoundManager debug log'larını HUD üzerine yönlendir (AUDIO etiketi).
        self.sound._ui_log = self

        # ── Stats ────────────────────────────────────────────────────────────
        self._stats      = {'cpu': 0.0, 'ram': 0.0, 'disk': 0.0,
                            'battery': 100.0, 'net_up': 0.0, 'net_down': 0.0}
        self._cpu_hist   = [0.0] * 24
        self._last_net   = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._wave_jarvis = [random.randint(4, 26) for _ in range(18)]
        self._wave_user   = [random.randint(2, 10) for _ in range(18)]

        # ── Typing ───────────────────────────────────────────────────────────
        self.typing_queue = deque()
        self.is_typing    = False

        # ── Partiküller (arka plan, az sayıda) ───────────────────────────────
        self.particles = [
            {
                'x':  random.uniform(0, self.W),
                'y':  random.uniform(0, self.H),
                'vx': random.uniform(-0.15, 0.15),
                'vy': random.uniform(-0.15, 0.15),
                'r':  random.uniform(0.5, 1.8),
                'a':  random.randint(15, 70),
            }
            for _ in range(24)
        ]

        self.orb_particles = [
            {
                'angle': random.uniform(0, math.tau),
                'orbit': random.uniform(0.06, 0.98),
                'speed': random.uniform(-0.030, 0.030),
                'size': random.uniform(0.8, 2.8),
                'phase': random.uniform(0, math.tau),
                'wobble': random.uniform(0.010, 0.040),
                'depth': random.uniform(0.30, 1.00),
            }
            for _ in range(160)
        ]
        self.orb_shell_particles = [
            {
                'angle': random.uniform(0, math.tau),
                'speed': random.uniform(-0.020, 0.020),
                'size': random.uniform(1.4, 3.8),
                'phase': random.uniform(0, math.tau),
                'glow': random.uniform(0.4, 1.0),
            }
            for _ in range(84)
        ]

        # ── Canvas ───────────────────────────────────────────────────────────
        self.bg = tk.Canvas(self.root, width=self.W, height=self.H,
                            bg=C_BG, highlightthickness=0)
        self.bg.place(x=0, y=0)

        # ── Log ──────────────────────────────────────────────────────────────
        self.log_frame = tk.Frame(self.root, bg=C_SURFACE,
                                  highlightbackground=C_LINE,
                                  highlightthickness=1)
        self.log_frame.place(x=self.CHAT_X, y=self.CHAT_Y,
                             width=self.CHAT_W, height=self.CHAT_H)
        self.log_text = tk.Text(
            self.log_frame, fg=C_TEXT, bg=C_SURFACE,
            insertbackground=C_TEXT, borderwidth=0,
            wrap="word", font=font_body(12), padx=14, pady=10,
            relief="flat", selectbackground="#31285a")
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        self.log_text.tag_config("you", foreground="#ffffff", lmargin1=6, lmargin2=6, rmargin=28, spacing1=4, spacing3=8)
        self.log_text.tag_config("ai",  foreground="#bdaeff", lmargin1=6, lmargin2=6, rmargin=10, spacing1=4, spacing3=8)
        self.log_text.tag_config("sys", foreground=C_GOLD, lmargin1=6, lmargin2=6, spacing1=4, spacing3=8)
        self.log_text.tag_config("err", foreground=C_RED, lmargin1=6, lmargin2=6, spacing1=4, spacing3=8)

        self._build_input_bar(self.CHAT_W)
        self._build_mute_button()
        self._build_pause_button()
        self._build_shutdown_button()
        self._build_mini_mode_button()
        self._build_settings_panel()
        self._build_voice_selector(self._settings_body)
        self._build_sfx_button(self._settings_body)
        self._build_api_button(self._settings_body)
        self._build_fx_slider(self._settings_body)
        self._layout_settings_controls()
        self._place_layout_widgets()

        # Orb tıklama = pause/resume
        self.bg.bind("<Button-1>", self._on_canvas_click)

        self.root.bind("<F4>",        lambda e: self._toggle_mute())
        self.root.bind("<Escape>",    lambda e: self._shutdown())
        self.root.bind("<F5>",        lambda e: self._toggle_pause())
        self.root.bind("<F11>",       lambda e: self._toggle_fullscreen())

        self._api_key_ready = has_gemini_api_key()
        if not self._api_key_ready:
            self._show_setup_ui()

        self._effects_active = None
        self._sync_sound_state()
        self.root.after(180, self._play_startup_sfx_once)
        self._kick_brief_refresh()
        self.root.after(120, self._enter_fullscreen)
        self._animate()
        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)

    def _force_startup_size(self):
        if self._fullscreen:
            self._enter_fullscreen()
            return
        self.root.geometry(self._window_geometry)
        self._resize_surface(*self._normal_size)
        self.root.update_idletasks()

    def _enter_fullscreen(self):
        sw = max(self.root.winfo_screenwidth(), self.root.winfo_width(), self.W)
        sh = max(self.root.winfo_screenheight(), self.root.winfo_height(), self.H)
        self.root.attributes("-fullscreen", True)
        self.root.geometry(f"{sw}x{sh}+0+0")
        self._resize_surface(sw, sh)

    def _set_layout_metrics(self, width: int, height: int):
        self.W = int(width)
        self.H = int(height)
        self.LEFT_W = min(LEFT_W_T, int(self.W * 0.23))
        self.RIGHT_W = min(RIGHT_W_T, int(self.W * 0.25))
        center_w = self.W - self.LEFT_W - self.RIGHT_W
        orb_area_h = self.H - HDR_H - CONTROL_H - FOOTER_H - 24
        self.FCX = self.LEFT_W + center_w // 2
        self.FCY = HDR_H + orb_area_h // 2 + 6
        self.FACE = min(int(orb_area_h * 0.90), int(center_w * 0.86), 860)

        self.CENTER_X0 = self.LEFT_W
        self.CENTER_X1 = self.W - self.RIGHT_W
        self.CTRL_X = self.LEFT_W + 18
        self.CTRL_Y = HDR_H + orb_area_h + 2
        self.CTRL_W = center_w - 36
        self.CHAT_PANEL_X = self.W - self.RIGHT_W + 8
        self.CHAT_PANEL_Y = HDR_H + 8
        self.CHAT_PANEL_W = self.RIGHT_W - 14
        self.CHAT_PANEL_H = self.H - HDR_H - FOOTER_H - 16
        self.CHAT_X = self.CHAT_PANEL_X + 10
        self.CHAT_Y = self.CHAT_PANEL_Y + 34
        self.CHAT_W = self.CHAT_PANEL_W - 20
        self.CHAT_H = self.CHAT_PANEL_H - 90
        self.CHAT_INPUT_Y = self.CHAT_PANEL_Y + self.CHAT_PANEL_H - INPUT_H - 10

    def _scale(self, px: float) -> int:
        """DPI ölçek faktörünü piksel değerine uygula (Req 14.2).

        ``self._dpi_scale`` 1.0 olduğunda kimliktir; daha yüksek ölçeklerde
        (1.25, 1.5, 1.75, 2.0) yerleşim sayıları lineer olarak büyür.
        """
        try:
            return int(round(float(px) * float(self._dpi_scale)))
        except Exception:
            return int(px)

    # ── Voice ─────────────────────────────────────────────────────────────────
    def _load_voice(self) -> str:
        try:
            return str(load_app_config().get("voice", "Charon") or "Charon")
        except Exception:
            return "Charon"

    # ── Shutdown button (sağ alt, büyük) ────────────────────────────────────
    def _build_shutdown_button(self):
        BW, BH = 140, 36
        self._shutdown_canvas = tk.Canvas(
            self.root, width=BW, height=BH,
            bg=C_BG, highlightthickness=0, cursor="hand2")
        self._shutdown_canvas.bind("<Button-1>", lambda e: self._shutdown())
        self._draw_shutdown_button()

    def _draw_shutdown_button(self):
        c = self._shutdown_canvas
        BW, BH = 140, 36
        c.delete("all")
        self._round_rect(c, 1, 1, BW-1, BH-1, r=10, fill=C_SURFACE, outline="#44212c", width=1)
        c.create_line(16, 1, BW-16, 1, fill=C_RED, width=2)
        c.create_text(BW//2, BH//2, text="⏻  SHUTDOWN",
                      fill=C_RED, font=font_display(11))

    # ── Mini mod butonu (sağ üst köşe) ──────────────────────────────────────
    def _build_mini_mode_button(self):
        """Sağ üst köşeye mini mod geçiş butonu ekle."""
        BW, BH = 36, 36
        self._mini_btn = tk.Canvas(
            self.root, width=BW, height=BH,
            bg=C_BG, highlightthickness=0, cursor="hand2")
        self._mini_btn.bind("<Button-1>", lambda e: self._enter_mini_mode())
        self._mini_btn.bind("<Enter>",    lambda e: self._mini_btn_hover(True))
        self._mini_btn.bind("<Leave>",    lambda e: self._mini_btn_hover(False))
        self._mini_btn_hovered = False
        self._draw_mini_btn()

    def _draw_mini_btn(self):
        c = self._mini_btn
        BW, BH = 36, 36
        c.delete("all")
        col = C_LAV if self._mini_btn_hovered else C_MID
        self._round_rect(c, 3, 3, BW-3, BH-3, r=9, fill=C_SURFACE, outline=col, width=1)
        c.create_text(BW//2, BH//2, text="⊟", fill=col, font=("Grift", 14))

    def _mini_btn_hover(self, hovered: bool):
        self._mini_btn_hovered = hovered
        self._draw_mini_btn()

    def _enter_mini_mode(self):
        """HUD'u gizle, mini overlay'i göster."""
        # Mini overlay callback'i varsa çağır (main.py tarafından set edilir)
        if hasattr(self, "_on_enter_mini_mode") and self._on_enter_mini_mode:
            self._on_enter_mini_mode()
        else:
            # Fallback: sadece pencereyi gizle
            self.root.withdraw()

    def _build_settings_panel(self):
        geo = self._settings_geometry
        panel_bg = "#080b16"
        body_bg = "#0b1020"
        input_bg = "#070a16"
        self._settings_btn_canvas = tk.Canvas(
            self.root,
            width=geo["btn_w"],
            height=geo["btn_h"],
            bg=C_BG,
            highlightthickness=0,
            cursor="hand2",
        )
        self._settings_btn_canvas.place(x=geo["btn_x"], y=geo["btn_y"])
        self._settings_btn_canvas.bind("<Button-1>", lambda e: self._toggle_settings_panel())
        self._draw_settings_button()

        self._settings_panel = tk.Frame(
            self.root,
            bg=panel_bg,
            highlightbackground="#5f66d9",
            highlightthickness=1,
        )
        self._settings_panel.place_forget()

        self._settings_title = tk.Label(
            self._settings_panel,
            text="SYSTEM SETTINGS",
            fg=C_LAV,
            bg=panel_bg,
            font=font_display(13),
            anchor="w",
        )
        self._settings_tab_settings = tk.Canvas(
            self._settings_panel,
            width=108,
            height=28,
            bg=panel_bg,
            highlightthickness=0,
            cursor="hand2",
        )
        self._settings_tab_settings.bind("<Button-1>", lambda e: self._set_settings_tab("settings"))
        self._settings_tab_debug = tk.Canvas(
            self._settings_panel,
            width=96,
            height=28,
            bg=panel_bg,
            highlightthickness=0,
            cursor="hand2",
        )
        self._settings_tab_debug.bind("<Button-1>", lambda e: self._set_settings_tab("debug"))
        self._settings_tab_rules = tk.Canvas(
            self._settings_panel,
            width=86,
            height=28,
            bg=panel_bg,
            highlightthickness=0,
            cursor="hand2",
        )
        self._settings_tab_rules.bind("<Button-1>", lambda e: self._set_settings_tab("rules"))
        self._settings_tab_logs = tk.Canvas(
            self._settings_panel,
            width=76,
            height=28,
            bg=panel_bg,
            highlightthickness=0,
            cursor="hand2",
        )
        self._settings_tab_logs.bind("<Button-1>", lambda e: self._set_settings_tab("logs"))
        self._settings_body = tk.Frame(self._settings_panel, bg=body_bg)
        self._debug_body = tk.Frame(self._settings_panel, bg=body_bg)
        self._rules_body = tk.Frame(self._settings_panel, bg=body_bg)
        self._logs_body = tk.Frame(self._settings_panel, bg=body_bg)
        self._settings_sfx_label = tk.Label(
            self._settings_body,
            text="SOUND",
            fg=C_BLUE,
            bg="#0e1428",
            font=font_body_bold(9),
        )
        self._settings_status_primary = tk.Label(
            self._settings_body,
            text="",
            fg=C_TEXT,
            bg="#0e1428",
            font=font_body_bold(10),
            anchor="w",
            justify="left",
        )
        self._settings_status_secondary = tk.Label(
            self._settings_body,
            text="",
            fg="#9aa5d6",
            bg="#0e1428",
            font=font_body(9),
            anchor="w",
            justify="left",
        )
        self._debug_text = tk.Text(
            self._debug_body,
            fg=C_TEXT,
            bg=input_bg,
            insertbackground=C_TEXT,
            borderwidth=0,
            wrap="word",
            font=font_body(10),
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#273154",
        )
        self._debug_text.tag_config("info", foreground=C_TEXT)
        self._debug_text.tag_config("warn", foreground=C_GOLD)
        self._debug_text.tag_config("err", foreground=C_RED)
        self._debug_text.configure(state="disabled")
        self._build_dynamic_settings_controls()
        self._build_rules_controls()
        self._build_log_viewer()
        self._draw_settings_tabs()
        self._render_debug_logs()
        self._render_file_logs()
        self._refresh_settings_status()

    def _build_dynamic_settings_controls(self):
        cfg = load_app_config()
        vc = cfg.get("voice_control", {}) if isinstance(cfg.get("voice_control", {}), dict) else {}
        self._smart_stop_var = tk.BooleanVar(value=bool(vc.get("stop_only_barge_in", True)))
        self._mute_while_speaking_var = tk.BooleanVar(value=bool(vc.get("mute_mic_while_speaking", False)))
        self._barge_in_var = tk.BooleanVar(value=bool(vc.get("barge_in_enabled", True)))
        self._cooldown_var = tk.StringVar(value=str(int(vc.get("post_speech_input_cooldown_ms", 1600) or 1600)))
        self._browser_var = tk.StringVar(value=str(cfg.get("preferred_browser", "") or "default"))

        self._settings_status_card = tk.Frame(
            self._settings_body, bg="#0e1428", highlightthickness=1, highlightbackground="#202846"
        )
        self._settings_sound_card = tk.Frame(
            self._settings_body, bg="#0e1428", highlightthickness=1, highlightbackground="#202846"
        )
        self._settings_voice_card = tk.Frame(
            self._settings_body, bg="#0e1428", highlightthickness=1, highlightbackground="#202846"
        )
        self._voice_mode_label = tk.Label(
            self._settings_body, text="VOICE CONTROL", fg=C_BLUE, bg="#0e1428", font=font_body_bold(9), anchor="w"
        )
        self._smart_stop_check = self._make_check(
            self._settings_body,
            "Akilli durma: dur / sus / hayir / yanlis ile kes",
            self._smart_stop_var,
            self._save_voice_control_from_ui,
        )
        self._mute_speaking_check = self._make_check(
            self._settings_body,
            "Jarvis konusurken mikrofonu kis",
            self._mute_while_speaking_var,
            self._save_voice_control_from_ui,
        )
        self._barge_check = self._make_check(
            self._settings_body,
            "Ben konusunca cevabi kesebilsin",
            self._barge_in_var,
            self._save_voice_control_from_ui,
        )
        self._cooldown_label = tk.Label(
            self._settings_body, text="ECHO COOLDOWN", fg="#9aa5d6", bg="#0b1020", font=font_body_bold(8), anchor="w"
        )
        self._cooldown_entry = tk.Entry(
            self._settings_body, textvariable=self._cooldown_var, fg=C_TEXT, bg="#070a16",
            insertbackground=C_TEXT, font=font_body(10), borderwidth=0, highlightthickness=1,
            highlightbackground=C_VIOLET,
        )
        self._cooldown_entry.bind("<Return>", lambda _e: self._save_voice_control_from_ui())
        self._cooldown_apply = tk.Button(
            self._settings_body, text="APPLY", command=self._save_voice_control_from_ui,
            fg=C_BLUE, bg="#111832", activeforeground=C_BG, activebackground=C_BLUE,
            font=font_body_bold(8), borderwidth=0, cursor="hand2",
        )
        self._browser_label = tk.Label(
            self._settings_body, text="DEFAULT BROWSER", fg="#9aa5d6", bg="#0b1020", font=font_body_bold(8), anchor="w"
        )
        self._browser_menu = tk.OptionMenu(
            self._settings_body,
            self._browser_var,
            "default",
            "chrome",
            "edge",
            "opera",
            "opera_gx",
            "firefox",
            "brave",
            command=lambda _v: self._save_browser_pref(),
        )
        self._browser_menu.config(
            fg=C_LAV, bg="#070a16", activeforeground=C_BG, activebackground=C_PRI,
            font=font_body(9), borderwidth=0, highlightthickness=1, highlightbackground="#273154",
        )
        self._browser_menu["menu"].config(
            fg=C_LAV, bg="#070a16", font=font_body(9), activeforeground=C_BG, activebackground=C_PRI
        )

    def _make_check(self, parent, text: str, variable: tk.BooleanVar, command):
        return tk.Checkbutton(
            parent,
            text=text,
            variable=variable,
            command=command,
            fg=C_TEXT,
            bg="#0e1428",
            selectcolor="#111832",
            activeforeground=C_LAV,
            activebackground="#0e1428",
            font=font_body(9),
            anchor="w",
            padx=6,
            pady=1,
        )

    def _build_rules_controls(self):
        cfg = load_app_config()
        self._rules_hint = tk.Label(
            self._rules_body,
            text="Jarvis'in her cevapta dikkate alacagi kalici kurallar.",
            fg="#9aa5d6",
            bg="#0b1020",
            font=font_body(9),
            anchor="w",
            justify="left",
        )
        self._rules_text = tk.Text(
            self._rules_body,
            fg=C_TEXT,
            bg="#070a16",
            insertbackground=C_TEXT,
            borderwidth=0,
            wrap="word",
            font=font_body(10),
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#273154",
        )
        self._rules_text.insert("1.0", str(cfg.get("user_rules", "") or ""))
        self._rules_save = tk.Button(
            self._rules_body,
            text="SAVE RULES",
            command=self._save_rules_from_ui,
            fg=C_GREEN,
            bg="#111832",
            activeforeground=C_BG,
            activebackground=C_GREEN,
            font=font_body_bold(9),
            borderwidth=0,
            cursor="hand2",
        )

    def _build_log_viewer(self):
        self._log_viewer = tk.Text(
            self._logs_body,
            fg=C_TEXT,
            bg="#070a16",
            insertbackground=C_TEXT,
            borderwidth=0,
            wrap="word",
            font=font_body(9),
            padx=10,
            pady=10,
            highlightthickness=1,
            highlightbackground="#273154",
        )
        self._log_viewer.configure(state="disabled")
        self._logs_refresh = tk.Button(
            self._logs_body,
            text="REFRESH LOGS",
            command=self._render_file_logs,
            fg=C_BLUE,
            bg="#111832",
            activeforeground=C_BG,
            activebackground=C_BLUE,
            font=font_body_bold(9),
            borderwidth=0,
            cursor="hand2",
        )

    def _draw_settings_button(self):
        c = self._settings_btn_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        accent = C_BLUE if self._settings_open else "#7f8cff"
        inner = "#111832" if self._settings_open else "#0b1020"
        self._round_rect(c, 0, 0, bw, bh, r=8, fill=inner, outline="#30385f", width=1)
        if self._settings_open:
            c.create_line(12, 0, bw - 12, 0, fill=C_BLUE, width=2)
        c.create_text(14, 15, text="SYSTEM SETTINGS", fill=C_LAV, font=font_display(10), anchor="w")
        c.create_text(14, 34, text=MODEL_BADGE, fill="#8c98c9", font=font_body(9), anchor="w")
        c.create_text(bw - 14, bh // 2, text="▾" if self._settings_open else "▸",
                      fill=accent, font=font_body_bold(12), anchor="e")

    def _draw_settings_button(self):
        c = self._settings_btn_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        accent = C_BLUE if self._settings_open else "#7f8cff"
        inner = "#111832" if self._settings_open else "#0b1020"
        self._round_rect(c, 0, 0, bw, bh, r=8, fill=inner, outline="#30385f", width=1)
        if self._settings_open:
            c.create_line(12, 0, bw - 12, 0, fill=C_BLUE, width=2)
        c.create_text(14, 15, text="SYSTEM SETTINGS", fill=C_LAV, font=font_display(10), anchor="w")
        c.create_text(14, 34, text=MODEL_BADGE, fill="#8c98c9", font=font_body(9), anchor="w")
        c.create_text(bw - 16, bh // 2, text="v" if self._settings_open else ">",
                      fill=accent, font=font_body_bold(12), anchor="e")

    def _toggle_settings_panel(self):
        self._settings_open = not self._settings_open
        self._draw_settings_button()
        self._place_layout_widgets()

    def _draw_settings_tabs(self):
        for key, canvas, label in (
            ("settings", self._settings_tab_settings, "SETTINGS"),
            ("debug", self._settings_tab_debug, "DEBUG"),
            ("rules", self._settings_tab_rules, "RULES"),
            ("logs", self._settings_tab_logs, "LOGS"),
        ):
            active = self._settings_tab == key
            bw = int(canvas["width"])
            bh = int(canvas["height"])
            canvas.delete("all")
            outline = "#5f66d9" if active else "#202846"
            fill = "#172044" if active else "#0b1020"
            text_col = C_LAV if active else "#9aa5d6"
            self._round_rect(canvas, 1, 1, bw - 1, bh - 1, r=7, fill=fill, outline=outline, width=1)
            if active:
                canvas.create_line(14, 2, bw - 14, 2, fill=C_VIOLET, width=2)
            canvas.create_text(bw // 2, bh // 2 + 1, text=label, fill=text_col, font=font_body_bold(9))

    def _set_settings_tab(self, tab: str):
        self._settings_tab = tab if tab in {"settings", "debug", "rules", "logs"} else "settings"
        self._draw_settings_tabs()
        if self._settings_tab == "logs":
            self._render_file_logs()
        self._place_layout_widgets()

    def _layout_settings_controls(self):
        inner_w = self._settings_geometry["panel_w"] - 24
        self._settings_status_card.place(x=0, y=38, width=inner_w, height=58)
        self._settings_sound_card.place(x=0, y=100, width=inner_w, height=88)
        self._settings_voice_card.place(x=0, y=238, width=inner_w, height=214)
        self._settings_status_card.lower()
        self._settings_sound_card.lower()
        self._settings_voice_card.lower()
        self._api_canvas.place(x=0, y=2)
        self._sfx_canvas.place(x=inner_w - int(self._sfx_canvas["width"]) - 4, y=0)
        self._settings_status_primary.place(x=12, y=50, width=inner_w - 24)
        self._settings_status_secondary.place(x=12, y=72, width=inner_w - 24)
        self._settings_sfx_label.place(x=12, y=110)
        self._volume_label.place(x=12, y=134)
        self._volume_scale.place(x=12, y=154, width=inner_w - 24, height=28)
        self._voice_label.place(x=0, y=200)
        self._voice_menu.place(x=116, y=194, width=inner_w - 116, height=30)
        self._voice_mode_label.place(x=12, y=250, width=inner_w - 24)
        self._smart_stop_check.place(x=12, y=276, width=inner_w - 24)
        self._barge_check.place(x=12, y=304, width=inner_w - 24)
        self._mute_speaking_check.place(x=12, y=332, width=inner_w - 24)
        self._cooldown_label.place(x=12, y=374)
        self._cooldown_entry.place(x=162, y=368, width=90, height=28)
        self._cooldown_apply.place(x=262, y=368, width=82, height=28)
        self._browser_label.place(x=12, y=424)
        self._browser_menu.place(x=162, y=418, width=inner_w - 174, height=30)

    def _refresh_settings_status(self):
        if not hasattr(self, "_settings_status_primary"):
            return
        cfg = load_app_config()
        vc = cfg.get("voice_control", {}) if isinstance(cfg.get("voice_control", {}), dict) else {}
        self._voice_guard_status = {
            "smart_stop": bool(vc.get("stop_only_barge_in", True)),
            "mute_while_speaking": bool(vc.get("mute_mic_while_speaking", False)),
            "cooldown_ms": int(vc.get("post_speech_input_cooldown_ms", 1600) or 1600),
            "preferred_browser": str(cfg.get("preferred_browser", "") or ""),
        }
        gemini_ready = bool(str(cfg.get("gemini_api_key", "") or "").strip())
        nvidia_ready = bool(str(cfg.get("nvidia_api_key", "") or "").strip())
        yt_key_ready = bool(str(cfg.get("youtube_api_key", "") or "").strip())
        yt_handle = str(cfg.get("youtube_channel_handle", "") or "").strip()

        primary = [
            "Gemini hazir" if gemini_ready else "Gemini API eksik",
            "NVIDIA hazir" if nvidia_ready else "NVIDIA API eksik",
            "YouTube hazir" if yt_key_ready and yt_handle else "YouTube ayari eksik",
        ]
        if yt_handle:
            handle_text = yt_handle
        else:
            handle_text = "@handle girilmedi"
        secondary = f"Kanal: {handle_text}"

        self._settings_status_primary.configure(text="  ·  ".join(primary))
        self._settings_status_secondary.configure(text=secondary)

    def _save_voice_control_from_ui(self):
        cfg = load_app_config()
        vc = cfg.get("voice_control", {}) if isinstance(cfg.get("voice_control", {}), dict) else {}
        try:
            cooldown = max(0, min(8000, int(float(self._cooldown_var.get() or 0))))
        except (TypeError, ValueError):
            cooldown = 1600
            self._cooldown_var.set(str(cooldown))
        vc.update(
            {
                "barge_in_enabled": bool(self._barge_in_var.get()),
                "mute_mic_while_speaking": bool(self._mute_while_speaking_var.get()),
                "stop_only_barge_in": bool(self._smart_stop_var.get()),
                "post_speech_input_cooldown_ms": cooldown,
            }
        )
        save_app_config({"voice_control": vc})
        self._voice_guard_status.update(
            {
                "smart_stop": bool(vc["stop_only_barge_in"]),
                "mute_while_speaking": bool(vc["mute_mic_while_speaking"]),
                "cooldown_ms": int(vc["post_speech_input_cooldown_ms"]),
            }
        )
        if self.on_voice_control_change:
            threading.Thread(target=self.on_voice_control_change, args=(vc,), daemon=True).start()
        self.write_debug("Voice control ayarlari guncellendi.", level="INFO")

    def _save_browser_pref(self):
        browser = str(self._browser_var.get() or "").strip()
        if browser == "default":
            browser = ""
        save_app_config({"preferred_browser": browser})
        self._voice_guard_status["preferred_browser"] = browser
        self.write_debug(f"Varsayilan tarayici: {browser or 'sistem varsayilani'}", level="INFO")

    def _save_rules_from_ui(self):
        text = self._rules_text.get("1.0", tk.END).strip()
        save_app_config({"user_rules": text})
        self.write_log("SYS: Jarvis kurallari guncellendi.")
        self.write_debug("Kalici kullanici kurallari kaydedildi.", level="INFO")

    def _render_file_logs(self):
        if not hasattr(self, "_log_viewer"):
            return
        paths = [
            BASE_DIR / "logs" / "debug" / "runtime.jsonl",
            BASE_DIR / "logs" / "conversation" / f"{time.strftime('%Y-%m-%d')}.jsonl",
        ]
        chunks = []
        for path in paths:
            try:
                label = str(path.relative_to(BASE_DIR))
            except Exception:
                label = str(path)
            chunks.append(f"== {label} ==")
            try:
                lines = path.read_text(encoding="utf-8").splitlines()[-80:]
                chunks.extend(lines or ["(bos)"])
            except Exception as exc:
                chunks.append(f"(okunamadi: {exc})")
            chunks.append("")
        self._log_viewer.configure(state="normal")
        self._log_viewer.delete("1.0", tk.END)
        self._log_viewer.insert(tk.END, "\n".join(chunks))
        self._log_viewer.see(tk.END)
        self._log_viewer.configure(state="disabled")

    def write_debug(self, text: str, level: str = "INFO"):
        clean = " ".join(str(text or "").split())
        if not clean:
            return
        self.root.after(0, self._append_debug_entry, clean, level)

    def _append_debug_entry(self, text: str, level: str = "INFO"):
        stamp = time.strftime("%H:%M:%S")
        lvl = (level or "INFO").upper()
        self._debug_entries.append((lvl, f"[{stamp}] {lvl}: {text}"))
        self._render_debug_logs()

    def _render_debug_logs(self):
        if not hasattr(self, "_debug_text"):
            return
        self._debug_text.configure(state="normal")
        self._debug_text.delete("1.0", tk.END)
        if not self._debug_entries:
            self._debug_text.insert(tk.END, "Henüz not edilebilir hata yok.\n", "info")
        else:
            for level, line in self._debug_entries:
                tag = "err" if level == "ERROR" else "warn" if level == "WARN" else "info"
                self._debug_text.insert(tk.END, line + "\n", tag)
        self._debug_text.see(tk.END)
        self._debug_text.configure(state="disabled")

    def _build_api_button(self, parent=None):
        parent = parent or self.root
        bw, bh = 154, 28
        self._api_canvas = tk.Canvas(
            parent, width=bw, height=bh,
            bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        self._api_canvas.bind("<Button-1>", lambda e: self._open_api_settings())
        self._draw_api_button()

    def _draw_api_button(self):
        c = self._api_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        self._round_rect(c, 1, 1, bw - 1, bh - 1, r=7, fill="#111832", outline="#2d5fc6", width=1)
        c.create_text(bw // 2, bh // 2, text="⌘ API SETTINGS",
                      fill=C_BLUE, font=font_body_bold(10))

    def _draw_api_button(self):
        c = self._api_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        self._round_rect(c, 1, 1, bw - 1, bh - 1, r=7, fill="#111832", outline="#2d5fc6", width=1)
        c.create_text(bw // 2, bh // 2 + 1, text="API KEYS",
                      fill=C_BLUE, font=font_body_bold(10))

    def _build_fx_slider(self, parent=None):
        parent = parent or self.root
        slider_w = 280
        self._volume_label = tk.Label(
            parent,
            text=f"FX LEVEL  {int(self.sound.get_volume() * 100)}%",
            fg=C_PRI,
            bg="#0e1428" if parent is getattr(self, "_settings_body", None) else parent.cget("bg"),
            font=font_body_bold(10),
        )
        self._volume_scale = tk.Scale(
            parent,
            from_=0,
            to=100,
            orient="horizontal",
            length=slider_w,
            showvalue=False,
            resolution=1,
            troughcolor="#070a16",
            bg="#0e1428" if parent is getattr(self, "_settings_body", None) else parent.cget("bg"),
            fg=C_TEXT,
            activebackground=C_PRI,
            highlightthickness=0,
            borderwidth=0,
            sliderlength=18,
            width=10,
            command=self._on_volume_change,
        )
        self._volume_scale.set(int(self.sound.get_volume() * 100))

    def _on_volume_change(self, value):
        try:
            volume = max(0, min(100, int(float(value))))
        except (TypeError, ValueError):
            return
        self._volume_label.configure(text=f"FX LEVEL  {volume}%")
        self.sound.set_volume(volume / 100.0)

    def _play_startup_sfx_once(self):
        pass

    def _sync_sound_state(self):
        enabled = self._sfx_on and not self.paused
        self.sound.set_enabled(enabled)
        if enabled and self._jarvis_state == "THINKING":
            self.sound.start_thinking()
        if enabled != self._effects_active:
            self._effects_active = enabled
            if self.on_effects_state_change:
                threading.Thread(
                    target=self.on_effects_state_change,
                    args=(enabled,),
                    daemon=True,
                ).start()

    def _open_api_settings(self):
        self._show_setup_ui(edit_mode=self._api_key_ready)

    def _close_setup_ui(self):
        if self.setup_frame and self.setup_frame.winfo_exists():
            self.setup_frame.destroy()
        self.setup_frame = None
        self.api_entry = None
        self.nvidia_api_entry = None
        self.youtube_api_entry = None
        self.youtube_handle_entry = None

    # ── SFX toggle ───────────────────────────────────────────────────────────
    def _build_sfx_button(self, parent=None):
        parent = parent or self.root
        BW, BH = 98, 36
        self._sfx_canvas = tk.Canvas(parent, width=BW, height=BH,
                                     bg=parent.cget("bg"), highlightthickness=0, cursor="hand2")
        self._sfx_canvas.bind("<Button-1>", lambda e: self._toggle_sfx())
        self._sfx_on = True
        self._draw_sfx_button()

    def _draw_sfx_button(self):
        c = self._sfx_canvas
        BW = int(c["width"])
        BH = int(c["height"])
        c.delete("all")
        col  = C_PRI if self._sfx_on else C_MID
        text = "♪ SFX ON"  if self._sfx_on else "♪ SFX OFF"
        bl = 6
        for bx, by, sx, sy in [(0, 0, 1, 1), (BW, 0, -1, 1),
                                (0, BH, 1, -1), (BW, BH, -1, -1)]:
            c.create_line(bx, by, bx+sx*bl, by, fill=col, width=1)
            c.create_line(bx, by, bx, by+sy*bl, fill=col, width=1)
        c.create_text(BW//2, BH//2, text=text, fill=col, font=font_body_bold(9))

    def _draw_sfx_button(self):
        c = self._sfx_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        col = C_PRI if self._sfx_on else "#7f8cff"
        fill = "#111832" if self._sfx_on else "#0b1020"
        self._round_rect(c, 1, 1, bw - 1, bh - 1, r=7, fill=fill, outline="#30385f", width=1)
        c.create_text(bw // 2, bh // 2 + 1, text="SFX ON" if self._sfx_on else "SFX OFF",
                      fill=col, font=font_body_bold(9))

    def _toggle_sfx(self):
        self._sfx_on = not self._sfx_on
        self._draw_sfx_button()
        self._sync_sound_state()

    # ── Voice selector ───────────────────────────────────────────────────────
    def _build_voice_selector(self, parent=None):
        parent = parent or self.root
        self._voice_var = tk.StringVar(value=self._current_voice)
        in_settings = parent is getattr(self, "_settings_body", None)
        self._voice_label = tk.Label(parent, text="VOICE", fg="#9aa5d6", bg=parent.cget("bg"),
                                     font=font_body_bold(8))

        self._voice_menu = tk.OptionMenu(parent, self._voice_var, *VOICES,
                                         command=self._on_voice_select)
        self._voice_menu.config(
            fg=C_LAV, bg="#070a16" if in_settings else C_PANEL, activeforeground=C_BG,
            activebackground=C_PRI, font=font_body(10),
            borderwidth=0, highlightthickness=1,
            highlightbackground="#273154" if in_settings else C_MID, width=12)
        self._voice_menu["menu"].config(
            fg=C_LAV, bg="#070a16" if in_settings else C_PANEL, font=font_body(10),
            activeforeground=C_BG, activebackground=C_PRI)

    def _on_voice_select(self, voice: str):
        self._current_voice = voice
        save_app_config({"voice": voice})
        if self.on_voice_change:
            threading.Thread(target=self.on_voice_change, args=(voice,), daemon=True).start()

    # ── Mute button ──────────────────────────────────────────────────────────
    def _build_mute_button(self):
        self._mute_canvas = tk.Canvas(self.root, width=126, height=36,
                                      bg=C_BG, highlightthickness=0, cursor="hand2")
        self._mute_canvas.bind("<Button-1>", lambda e: self._toggle_mute())
        self._draw_mute_button()

    def _draw_mute_button(self):
        c = self._mute_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        if self.muted:
            col, icon, lbl = C_MUTED, "🔇", " MUTED"
        else:
            col, icon, lbl = C_GREEN, "🎙", " LIVE"
        self._round_rect(c, 1, 1, bw-1, bh-1, r=10, fill=C_SURFACE, outline=C_LINE, width=1)
        c.create_line(14, 1, bw-14, 1, fill=col, width=2)
        c.create_text(bw//2, bh//2, text=f"{icon}{lbl}",
                      fill=col, font=font_body_bold(11))

    def _build_pause_button(self):
        self._pause_canvas = tk.Canvas(self.root, width=126, height=36,
                                       bg=C_BG, highlightthickness=0, cursor="hand2")
        self._pause_canvas.bind("<Button-1>", lambda e: self._toggle_pause())
        self._draw_pause_button()

    def _draw_pause_button(self):
        c = self._pause_canvas
        bw = int(c["width"])
        bh = int(c["height"])
        c.delete("all")
        if self.paused:
            col, text = C_GOLD, "▶ RESUME"
        else:
            col, text = C_BLUE, "⏸ PAUSE"
        self._round_rect(c, 1, 1, bw-1, bh-1, r=10, fill=C_SURFACE, outline=C_LINE, width=1)
        c.create_line(14, 1, bw-14, 1, fill=col, width=2)
        c.create_text(bw//2, bh//2, text=text, fill=col, font=font_body_bold(11))

    def _toggle_mute(self):
        self.muted = not self.muted
        self._draw_mute_button()
        if self.muted:
            self.write_log("SYS: Mikrofon kapatıldı.")
        else:
            self.write_log("SYS: Mikrofon açık.")
        self._sync_sound_state()

    # ── Orb tıklama = pause ──────────────────────────────────────────────────
    def _on_canvas_click(self, event):
        dx = event.x - self.FCX
        dy = event.y - self.FCY
        if dx*dx + dy*dy <= (self.FACE * 0.40)**2:
            self._toggle_pause()

    def _toggle_pause(self):
        self.paused = not self.paused
        self._draw_pause_button()
        if self.paused:
            self.set_state("PAUSED")
            self.write_log("SYS: JARVIS duraklatıldı.")
        else:
            self.set_state("THINKING")
            self.write_log("SYS: JARVIS devam ediyor...")
        self._sync_sound_state()
        if self.on_pause_toggle:
            threading.Thread(target=self.on_pause_toggle, args=(self.paused,), daemon=True).start()

    def _shutdown(self):
        self.sound.stop_all()
        self.write_log("SYS: JARVIS kapatılıyor...")
        self.root.after(380, os._exit, 0)

    def _toggle_fullscreen(self):
        self._fullscreen = not self._fullscreen
        if self._fullscreen:
            self._enter_fullscreen()
        else:
            self.root.attributes("-fullscreen", False)
            self.root.geometry(self._window_geometry)
            self._resize_surface(*self._normal_size)

    def _resize_surface(self, width: int, height: int):
        self._set_layout_metrics(width, height)
        self.bg.configure(width=self.W, height=self.H)
        self.bg.place(x=0, y=0)
        self._place_layout_widgets()
        for p in self.particles:
            p["x"] %= self.W
            p["y"] %= self.H

    # ── Input bar ────────────────────────────────────────────────────────────
    def _build_input_bar(self, lw: int):
        x0 = self.CHAT_X
        btn_w = 76
        gap = 8
        inp_w = lw - btn_w - gap

        self._input_var   = tk.StringVar()
        self._input_entry = tk.Entry(
            self.root, textvariable=self._input_var,
            fg=C_TEXT, bg=C_SURFACE_2, insertbackground=C_LAV,
            borderwidth=0, font=font_body(11),
            highlightthickness=1, highlightbackground=C_LINE,
            highlightcolor=C_VIOLET)
        self._input_entry.place(
            x=x0, y=self.CHAT_INPUT_Y, width=inp_w, height=INPUT_H)
        self._input_entry.bind("<Return>",   self._on_input_submit)
        self._input_entry.bind("<KP_Enter>", self._on_input_submit)

        self._send_btn = tk.Button(
            self.root, text="SEND ▸",
            command=self._on_input_submit,
            fg=C_LAV, bg=C_SURFACE_2,
            activeforeground=C_BG, activebackground=C_LAV,
            font=font_body_bold(10),
            borderwidth=0, cursor="hand2",
            highlightthickness=1, highlightbackground=C_VIOLET)
        self._send_btn.place(
            x=x0+inp_w+gap, y=self.CHAT_INPUT_Y,
            width=btn_w, height=INPUT_H)

    def _place_layout_widgets(self):
        self.log_frame.place(x=self.CHAT_X, y=self.CHAT_Y, width=self.CHAT_W, height=self.CHAT_H)
        gap = 12
        mute_w = 126
        pause_w = 126
        shutdown_w = int(self._shutdown_canvas["width"])
        total = mute_w + pause_w + shutdown_w + gap * 2
        start_x = self.FCX - total // 2
        row1_y = self.CTRL_Y + 20

        self._mute_canvas.place(x=start_x, y=row1_y)
        self._pause_canvas.place(x=start_x + mute_w + gap, y=row1_y)
        self._shutdown_canvas.place(x=start_x + mute_w + pause_w + gap * 2, y=row1_y)

        # Mini mod butonu — sağ üst köşe, her zaman görünür
        self._mini_btn.place(x=self.W - 44, y=8)
        # Canvas widget'ı için Misc.lift kullan (Canvas.lift item raise için)
        try:
            tk.Misc.lift(self._mini_btn)
        except Exception:
            pass

        geo = self._settings_geometry
        panel_x = geo["panel_x"]
        panel_y = geo["panel_y"]
        panel_w = geo["panel_w"]
        panel_h = geo["panel_h"]
        if self._settings_open:
            self._settings_panel.place(x=panel_x, y=panel_y, width=panel_w, height=panel_h)
            self._settings_panel.lift()
            self._settings_title.place(x=14, y=12)
            self._settings_tab_settings.place(x=14, y=40)
            self._settings_tab_debug.place(x=130, y=40)
            self._settings_tab_rules.place(x=232, y=40)
            self._settings_tab_logs.place(x=324, y=40)
            body_y = 76
            body_h = panel_h - 88
            body_w = panel_w - 24
            if self._settings_tab == "debug":
                self._settings_body.place_forget()
                self._rules_body.place_forget()
                self._logs_body.place_forget()
                self._debug_body.place(x=12, y=76, width=panel_w - 24, height=panel_h - 88)
                self._debug_text.place(x=0, y=0, width=panel_w - 24, height=panel_h - 88)
                self._debug_body.lift()
            elif self._settings_tab == "rules":
                self._settings_body.place_forget()
                self._debug_body.place_forget()
                self._logs_body.place_forget()
                self._rules_body.place(x=12, y=body_y, width=body_w, height=body_h)
                self._rules_hint.place(x=0, y=0, width=body_w)
                self._rules_text.place(x=0, y=28, width=body_w, height=body_h - 76)
                self._rules_save.place(x=0, y=body_h - 38, width=130, height=30)
                self._rules_body.lift()
            elif self._settings_tab == "logs":
                self._settings_body.place_forget()
                self._debug_body.place_forget()
                self._rules_body.place_forget()
                self._logs_body.place(x=12, y=body_y, width=body_w, height=body_h)
                self._logs_refresh.place(x=0, y=0, width=130, height=30)
                self._log_viewer.place(x=0, y=40, width=body_w, height=body_h - 40)
                self._logs_body.lift()
            else:
                self._debug_body.place_forget()
                self._rules_body.place_forget()
                self._logs_body.place_forget()
                self._settings_body.place(x=12, y=76, width=panel_w - 24, height=panel_h - 88)
                self._settings_body.lift()
        else:
            self._settings_panel.place_forget()
            self._settings_title.place_forget()
            self._settings_tab_settings.place_forget()
            self._settings_tab_debug.place_forget()
            self._settings_tab_rules.place_forget()
            self._settings_tab_logs.place_forget()
            self._settings_body.place_forget()
            self._debug_body.place_forget()
            self._rules_body.place_forget()
            self._logs_body.place_forget()

        inp_w = self.CHAT_W - 84
        self._input_entry.place(x=self.CHAT_X, y=self.CHAT_INPUT_Y, width=inp_w, height=INPUT_H)
        self._send_btn.place(x=self.CHAT_X + inp_w + 8, y=self.CHAT_INPUT_Y, width=76, height=INPUT_H)

    def _on_input_submit(self, event=None):
        text = self._input_var.get().strip()
        if not text:
            return
        if self.paused:
            self.write_log("SYS: JARVIS duraklatılmış durumda. Devam etmek için pause'u kapat.")
            return
        self._input_var.set("")
        if text.lower() in ("sus", "dur", "stop", "sessiz", "kes"):
            self.write_log("SYS: ⏹ Ses kesildi.")
            if self.on_stop_command:
                threading.Thread(target=self.on_stop_command, daemon=True).start()
            return
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(text,), daemon=True).start()

    # ── State & callbacks ────────────────────────────────────────────────────
    def set_state(self, state: str):
        previous = getattr(self, "_jarvis_state", "")
        self._jarvis_state = state
        self.speaking = (state == "SPEAKING")
        if state == "THINKING":
            self.sound.start_thinking()
        elif previous == "THINKING":
            self.sound.stop_thinking()
        if state == "ERROR" and previous != "ERROR":
            self.sound.play_error()

    def set_user_speaking(self, value: bool):
        self.mark_user_activity(value)

    def mark_user_activity(self, active: bool = True):
        self.user_speaking = active
        self._user_speaking_until = time.time() + (0.9 if active else 0.0)

    def get_effects_volume(self) -> float:
        return self.sound.get_volume()

    def effects_enabled(self) -> bool:
        return bool(self._effects_active)

    def play_success_sfx(self):
        self.root.after(0, self.sound.play_success)

    def play_error_sfx(self):
        self.root.after(0, self.sound.play_error)

    def focus_panel(self, section: str, duration_ms: int = 4200):
        section = (section or "").strip().lower()
        if not section:
            return

        def _apply():
            self._panel_focus = section
            self._panel_focus_until = time.time() + max(0.8, duration_ms / 1000.0)

        self.root.after(0, _apply)

    def _state_color(self, state: str | None = None) -> str:
        effective = state or self._jarvis_state
        if effective == "PAUSED":
            return C_MID
        return STATE_HEX_COLORS.get(effective, C_PRI)

    @staticmethod
    def _state_badge_text(state: str) -> str:
        if state == "INITIALISING":
            return "CONNECTING"
        if state == "ERROR":
            return "ERROR"
        return "ONLINE"

    # ── Log ──────────────────────────────────────────────────────────────────
    def write_log(self, text: str):
        try:
            if threading.current_thread() is not threading.main_thread():
                self.root.after(0, lambda: self.write_log(text))
                return
        except Exception:
            pass
        self._write_log_now(text)

    def _write_log_now(self, text: str):
        if text.startswith("SYS: AGENT PLAN") or text.startswith("SYS: AGENT EXECUTE"):
            self._append_log_line(text, "sys")
            return
        self.typing_queue.append(text)
        tl = text.lower()
        if tl.startswith("siz:") or tl.startswith("you:"):
            self.mark_user_activity(True)
            self.set_state("THINKING")
        elif tl.startswith("err:") or "error" in tl:
            self._error_hold_until = time.time() + 8.0
            self.set_state("ERROR")
            self.write_debug(text, level="ERROR")
        if not self.is_typing:
            self._start_typing()

    def _append_log_line(self, text: str, tag: str = "sys"):
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert(tk.END, str(text) + "\n", tag)
            self.log_text.see(tk.END)
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _start_typing(self):
        if not self.typing_queue:
            self.is_typing = False
            if self._jarvis_state == "ERROR" and time.time() < self._error_hold_until:
                return
            if not self.speaking:
                self.set_state("LISTENING")
            return
        self.is_typing = True
        text = self.typing_queue.popleft()
        tl   = text.lower()
        if   tl.startswith("siz:") or tl.startswith("you:"):   tag = "you"
        elif tl.startswith("jarvis:") or tl.startswith("ai:"): tag = "ai"
        elif tl.startswith("err:") or "error" in tl:           tag = "err"
        else:                                                    tag = "sys"
        self.log_text.configure(state="normal")
        self._type_char(text, 0, tag)

    def _type_char(self, text, i, tag):
        if i < len(text):
            self.log_text.insert(tk.END, text[i], tag)
            self.log_text.see(tk.END)
            self.root.after(7, self._type_char, text, i+1, tag)
        else:
            self.log_text.insert(tk.END, "\n")
            self.log_text.configure(state="disabled")
            self.root.after(20, self._start_typing)

    # ── Stats ────────────────────────────────────────────────────────────────
    def _update_stats(self):
        try:
            self._stats['cpu']  = psutil.cpu_percent()
            self._stats['ram']  = psutil.virtual_memory().percent
            self._stats['disk'] = psutil.disk_usage('/').percent
            batt = psutil.sensors_battery()
            self._stats['battery'] = batt.percent if batt else 100.0
            now = time.time()
            net = psutil.net_io_counters()
            dt  = now - self._last_net_t
            if dt > 0:
                self._stats['net_up']   = max(0, (net.bytes_sent - self._last_net.bytes_sent) / dt / 1024)
                self._stats['net_down'] = max(0, (net.bytes_recv - self._last_net.bytes_recv) / dt / 1024)
            self._last_net   = net
            self._last_net_t = now
            self._cpu_hist.pop(0)
            self._cpu_hist.append(self._stats['cpu'])
        except Exception:
            pass

    # ── Animation loop ───────────────────────────────────────────────────────
    def _animate(self):
        self.tick += 1
        t   = self.tick
        now = time.time()

        if self.user_speaking and now > self._user_speaking_until:
            self.user_speaking = False

        if t % 90 == 0:
            threading.Thread(target=self._update_stats, daemon=True).start()
        if t % 1800 == 1:
            self._kick_brief_refresh()

        if self.speaking and t % 3 == 0:
            self._wave_jarvis = [random.randint(6, 30) for _ in range(18)]
        if self.user_speaking and t % 3 == 0:
            self._wave_user = [random.randint(5, 24) for _ in range(18)]

        if now - self.last_t > (0.12 if self.speaking else 0.50):
            if self.paused:
                self.target_scale = random.uniform(0.58, 0.64)
                self.target_halo  = random.uniform(5, 10)
            elif self.speaking:
                self.target_scale = random.uniform(0.98, 1.10)
                self.target_halo  = random.uniform(180, 250)
            elif self.user_speaking:
                self.target_scale = random.uniform(0.88, 0.98)
                self.target_halo  = random.uniform(120, 175)
            elif self._jarvis_state in ("THINKING", "INITIALISING"):
                self.target_scale = random.uniform(0.80, 0.88)
                self.target_halo  = random.uniform(95, 145)
            else:
                self.target_scale = random.uniform(0.72, 0.80)
                self.target_halo  = random.uniform(34, 58)
            self.last_t = now

        sp          = 0.34 if self.speaking else 0.18
        self.scale  += (self.target_scale - self.scale) * sp
        self.halo_a += (self.target_halo   - self.halo_a) * sp

        if self.paused:
            spds = [0.0, 0.0, 0.0, 0.0]
        elif self.speaking:
            spds = [1.6, -1.1, 2.4, -0.7]
        else:
            spds = [0.55, -0.35, 0.90, -0.28]
        for i, spd in enumerate(spds):
            self.rings_spin[i] = (self.rings_spin[i] + spd) % 360

        # Pulse rings
        pspd  = 4.2 if self.speaking else 1.8
        limit = self.FACE * 0.68
        self.pulse_r = [r + pspd for r in self.pulse_r if r + pspd < limit]
        if len(self.pulse_r) < 3 and random.random() < (0.07 if self.speaking else 0.02):
            self.pulse_r.append(0.0)

        for p in self.particles:
            p['x'] = (p['x'] + p['vx']) % self.W
            p['y'] = (p['y'] + p['vy']) % self.H

        if t % 38 == 0:
            self.status_blink = not self.status_blink

        try:
            self._draw()
        except Exception as exc:
            try:
                self.write_debug(f"HUD draw hatasi: {exc}", level="WARN")
            except Exception:
                pass
        finally:
            try:
                self.root.after(33, self._animate)
            except Exception:
                pass

    # ── Yardımcı ─────────────────────────────────────────────────────────────
    @staticmethod
    def _ac(r, g, b, a):
        f = max(0, min(255, int(a))) / 255.0
        return f"#{int(r*f):02x}{int(g*f):02x}{int(b*f):02x}"

    def _orb_rgb(self):
        state = "PAUSED" if self.paused else self._jarvis_state
        return ORB_COLORS.get(state, ORB_COLORS["LISTENING"])

    @staticmethod
    def _split_summary_lines(text: str, limit: int = 4) -> list[str]:
        raw = (text or "").strip()
        if not raw:
            return []
        raw = raw.replace(" ve ", ", ")
        parts = [part.strip(" .") for part in raw.split(",") if part.strip()]
        return parts[:limit]

    def _parse_weather_card(self, text: str) -> dict:
        if not text or "alınamadı" in text.lower() or "alınamadi" in text.lower():
            return {
                "city": "Istanbul",
                "primary": "--",
                "details": ["Hava durumu alınamadı."],
            }

        prefix, _, body = text.partition(":")
        city = "Istanbul"
        if " için" in prefix:
            city = prefix.split(" için", 1)[0].strip().title()

        details = [part.strip(" .") for part in body.split(",") if part.strip()]
        primary = "--"
        if details:
            primary = details[0].replace(" derece", "°C")
        return {
            "city": city,
            "primary": primary,
            "details": details[1:4] or ["Anlık veri hazır."],
        }

    def _parse_health_card(self, text: str) -> list[str]:
        if not text or "alınamadı" in text.lower() or "alınamadi" in text.lower():
            return ["Sağlık verisi alınamadı."]
        lines = self._split_summary_lines(text, limit=4)
        return lines or ["Sağlık özeti hazır değil."]

    def _kick_brief_refresh(self):
        if self._brief_refresh_busy:
            return
        self._brief_refresh_busy = True
        threading.Thread(target=self._refresh_brief_cards, daemon=True).start()

    def _refresh_brief_cards(self):
        try:
            weather = get_weather_summary("Istanbul")
            self._weather_card = self._parse_weather_card(weather)
        except Exception:
            self._weather_card = {
                "city": "Istanbul",
                "primary": "--",
                "details": ["Hava durumu alınamadı."],
            }
        finally:
            self._brief_refresh_busy = False

    def _bar(self, c, x, y, w, h, pct, color):
        self._round_rect(c, x, y, x+w, y+h, r=max(2, h // 2), fill="#111426", outline=C_LINE, width=1)
        fw = max(1, int(w * pct / 100))
        self._round_rect(c, x+1, y+1, x+fw, y+h-1, r=max(2, h // 2 - 1), fill=color, outline="")

    def _sparkline(self, c, x, y, w, h, data):
        c.create_rectangle(x, y, x+w, y+h, fill="#050e0e", outline=C_DIM, width=1)
        n = len(data)
        if n < 2:
            return
        step = (w - 2) / (n - 1)
        h2   = h - 2
        coords = []
        for i, v in enumerate(data):
            coords.append(x + 1 + i * step)
            coords.append(y + h - 1 - int(h2 * v / 100))
        c.create_line(*coords, fill=C_PRI, width=1, smooth=True)

    def _bracket(self, c, x0, y0, pw, ph, col=None, bl=12):
        col = col or C_PRI
        for bx, by, sx, sy in [(x0, y0, 1, 1), (x0+pw, y0, -1, 1),
                                (x0, y0+ph, 1, -1), (x0+pw, y0+ph, -1, -1)]:
            c.create_line(bx, by, bx+sx*bl, by, fill=col, width=2)
            c.create_line(bx, by, bx, by+sy*bl, fill=col, width=2)

    def _round_rect(self, c, x0, y0, x1, y1, r=10, **kwargs):
        r = max(1, min(int(r), int((x1 - x0) / 2), int((y1 - y0) / 2)))
        points = [
            x0+r, y0, x1-r, y0, x1, y0, x1, y0+r,
            x1, y1-r, x1, y1, x1-r, y1, x0+r, y1,
            x0, y1, x0, y1-r, x0, y0+r, x0, y0,
        ]
        return c.create_polygon(points, smooth=True, splinesteps=10, **kwargs)

    def _draw_info_card(self, c, x0, y0, pw, ph, title, accent=C_PRI):
        focus = max(0.0, min(1.0, getattr(self, "_card_focus_boost", 0.0)))
        dimmed = bool(getattr(self, "_card_dimmed", False))
        glow = int(55 + 120 * focus)
        border = accent if focus > 0.08 else ("#34364f" if dimmed else C_LINE)
        fill = "#080b16" if dimmed else "#090d1b"
        self._round_rect(c, x0, y0, x0+pw, y0+ph, r=8, fill=fill, outline="")
        self._round_rect(c, x0+1, y0+1, x0+pw-1, y0+ph-1, r=7, fill="", outline="#18203a", width=1)
        c.create_rectangle(x0 + 1, y0 + 1, x0 + 5, y0 + ph - 1, fill=border, outline="")
        if focus > 0.08:
            for inset in range(3):
                self._round_rect(
                    c,
                    x0-inset, y0-inset, x0+pw+inset, y0+ph+inset,
                    r=8 + inset,
                    outline=self._ac(*ORB_COLORS["LISTENING"], max(12, glow - inset * 28)),
                    width=1,
                )
        c.create_line(x0+18, y0+1, x0+min(pw-18, 136), y0+1, fill=border, width=2)
        title_fill = "#6f7d7b" if dimmed else accent
        line_fill = "#1c2037" if dimmed else C_LINE
        c.create_text(x0+16, y0+16, text=title, fill=title_fill,
                      font=font_display(10), anchor="w")
        c.create_line(x0+16, y0+31, x0+pw-16, y0+31, fill=line_fill)

    def _draw_metric_tile(self, c, x, y, w, h, label, value, accent):
        self._round_rect(c, x, y, x + w, y + h, r=6, fill="#060a15", outline="#17213a", width=1)
        c.create_text(x + 10, y + 13, text=label, fill=C_MID, font=font_body_bold(8), anchor="w")
        c.create_text(x + 10, y + 34, text=value, fill=accent, font=font_body_bold(13), anchor="w")

    def _focus_boost_for(self, section: str) -> float:
        if self._panel_focus != section:
            return 0.0
        remaining = self._panel_focus_until - time.time()
        if remaining <= 0:
            return 0.0
        pulse = 0.65 + 0.35 * math.sin(self.tick * 0.12)
        return min(1.0, remaining / 4.0) * pulse

    # ── Health overlay (sol panel) ────────────────────────────────────────────
    def show_health_hologram(self, query: str, data_str: str):
        def _show():
            self._health_visible = True
            self._health_query   = query.lower()
            self._health_display = data_str
            self._panel_focus = "health"
            self._panel_focus_until = time.time() + 5.0
            if self._health_hide_job:
                self.root.after_cancel(self._health_hide_job)
            self._health_hide_job = self.root.after(14000, self._hide_health_hologram)
        self.root.after(0, _show)

    def show_agent_timeline(self, summary: str, duration_ms: int = 10000):
        def _show():
            clean = str(summary or "").strip()
            if not clean:
                return
            self._agent_visible = True
            self._agent_display = clean
            self._panel_focus = "agent"
            self._panel_focus_until = time.time() + max(2.0, duration_ms / 1000.0)
            if self._agent_hide_job:
                self.root.after_cancel(self._agent_hide_job)
            self._agent_hide_job = self.root.after(max(1200, int(duration_ms)), self._hide_agent_timeline)
        self.root.after(0, _show)

    def _hide_agent_timeline(self):
        self._agent_visible = False
        self._agent_hide_job = None

    def _draw_agent_overlay(self, c):
        if not self._agent_visible:
            return
        lines = [line.rstrip() for line in self._agent_display.split("\n") if line.strip()]
        if not lines:
            return

        max_lines = 8
        shown = lines[:max_lines]
        card_w = min(max(460, int(self.W * 0.34)), max(460, self.CENTER_W - 72))
        card_h = 56 + len(shown) * 22
        x0 = self.LEFT_W + max(36, int((self.CENTER_W - card_w) * 0.5))
        y0 = HDR_H + 38
        pulse = 0.62 + 0.38 * abs(math.sin(self.tick * 0.045))

        self._round_rect(c, x0, y0, x0 + card_w, y0 + card_h, r=18, fill="#080a17", outline="")
        self._round_rect(
            c,
            x0 + 1,
            y0 + 1,
            x0 + card_w - 1,
            y0 + card_h - 1,
            r=17,
            fill="",
            outline=self._ac(155, 120, 255, int(120 + 70 * pulse)),
            width=1,
        )
        c.create_line(x0 + 18, y0 + 1, x0 + min(card_w - 18, 210), y0 + 1, fill=C_LAV, width=2)
        c.create_text(x0 + 20, y0 + 22, text="AGENT TIMELINE", fill=C_LAV, font=font_display(12), anchor="w")
        c.create_text(x0 + card_w - 20, y0 + 22, text="LIVE", fill=C_GREEN, font=font_body_bold(9), anchor="e")
        c.create_line(x0 + 20, y0 + 38, x0 + card_w - 20, y0 + 38, fill=C_LINE)

        ly = y0 + 60
        for idx, line in enumerate(shown):
            low = line.lower()
            if idx == 0:
                col = C_TEXT
                font = font_body_bold(10)
                text = line
            elif "fail:" in low or "issue" in low or "hata" in low:
                col = C_RED
                font = font_body(10)
                text = line
            elif "recovery:" in low or "replan" in low:
                col = C_GOLD
                font = font_body(10)
                text = line
            elif "done:" in low or " ok:" in low or low.strip().startswith("ok:"):
                col = C_GREEN
                font = font_body(10)
                text = line
            else:
                col = C_TEXT
                font = font_body(10)
                text = line
            c.create_text(x0 + 22, ly, text=text[:86], fill=col, font=font, anchor="w")
            ly += 22

    def _hide_health_hologram(self):
        self._health_visible  = False
        self._health_hide_job = None

    def _draw_health_overlay(self, c):
        x0, y0 = 4, HDR_H + 4
        pw = self.LEFT_W - 8
        ph = self.H - HDR_H - FOOTER_H - 90
        pulse = 0.5 + 0.5 * math.sin(self.tick * 0.08)

        c.create_rectangle(x0, y0, x0+pw, y0+ph,
                           fill="#011510", outline=C_PRI, width=1)
        self._bracket(c, x0, y0, pw, ph, col=C_ORG, bl=10)

        title_col = self._ac(0, 212, 192, int(200 + 55*pulse))
        c.create_text(x0+pw//2, y0+18, text="◈ HEALTH ◈",
                      fill=title_col, font=font_display(11))
        c.create_line(x0+8, y0+30, x0+pw-8, y0+30, fill=C_MID)

        lines = [l for l in self._health_display.split('\n') if l.strip()]
        ly = y0 + 44
        for line in lines:
            if ly > y0 + ph - 14:
                break
            if line.startswith("──"):
                c.create_line(x0+8, ly, x0+pw-8, ly, fill=C_DIM)
                ly += 10
            elif ":" in line:
                parts = line.split(":", 1)
                lbl   = parts[0].strip()
                val   = parts[1].strip() if len(parts) > 1 else ""
                c.create_text(x0+10, ly, text=lbl+":", fill=C_MID,
                              font=font_body(10), anchor="w")
                c.create_text(x0+pw-10, ly, text=val, fill=C_ORG,
                              font=font_body_bold(10), anchor="e")
                ly += 20
            else:
                c.create_text(x0+10, ly, text=line, fill=C_TEXT,
                              font=font_body(9), anchor="w")
                ly += 17

    # ── Sol panel ─────────────────────────────────────────────────────────────
    def _draw_left_panel(self, c):
        if self._health_visible:
            self._draw_health_overlay(c)
            return

        x0 = 10
        y0 = HDR_H + 10
        pw = self.LEFT_W - 18
        gap = 12
        total_h = self.H - HDR_H - FOOTER_H - 20
        card_area_h = total_h - gap * 4
        pad = 14
        bw = pw - 2 * pad

        cards = [
            ("time", 0.20, "TIME", C_GOLD),
            ("weather", 0.20, "WEATHER · ISTANBUL", C_BLUE),
            ("system", 0.27, "SYSTEM LOAD", C_PRI),
            ("voice", 0.18, "VOICE GUARD", C_GREEN),
            ("session", 0.17, "SESSION", C_LAV),
        ]
        any_focus_active = bool(self._panel_focus) and (self._panel_focus_until > time.time())
        weights = []
        for section, weight, _, _ in cards:
            weights.append(weight + (0.12 if self._focus_boost_for(section) > 0.08 else 0.0))
        total_weight = sum(weights)
        heights = [int(card_area_h * (weight / total_weight)) for weight in weights]
        heights[-1] += card_area_h - sum(heights)

        current_y = y0
        for (section, _, title, accent), ph in zip(cards, heights):
            focus_boost = self._focus_boost_for(section)
            dimmed = any_focus_active and focus_boost <= 0.08
            shift_x = int(14 * focus_boost)
            extra_w = int(22 * focus_boost)
            section_x = x0 + shift_x
            section_pw = pw + extra_w
            section_pad = pad + int(2 * focus_boost)
            section_bw = section_pw - 2 * section_pad
            muted_label = "#647270" if dimmed else C_MID
            muted_text = "#7e8a88" if dimmed else C_TEXT
            muted_primary = "#8ea19d" if dimmed else C_PRI
            muted_blue = "#829594" if dimmed else C_BLUE
            muted_green = "#85a393" if dimmed else C_GREEN
            muted_gold = "#a1997e" if dimmed else C_GOLD
            muted_warn = "#8d7f77" if dimmed else C_ORG2
            muted_red = "#8a7779" if dimmed else C_RED
            self._card_focus_boost = focus_boost
            self._card_dimmed = dimmed
            self._draw_info_card(c, section_x, current_y, section_pw, ph, title, accent=accent if not dimmed else "#72807f")

            if section == "time":
                c.create_text(section_x+section_pad, current_y+64, text=time.strftime("%H:%M"),
                              fill=muted_primary, font=font_display(36 if focus_boost > 0.08 else 34), anchor="w")
                c.create_text(section_x+section_pad, current_y+92, text=time.strftime(":%S"),
                              fill=muted_label, font=font_body_bold(13), anchor="w")
                c.create_text(section_x+section_pad, current_y+118, text=time.strftime("%d %B %Y").upper(),
                              fill=muted_gold, font=font_body_bold(11), anchor="w")
                c.create_text(section_x+section_pad, current_y+138, text=time.strftime("%A").upper(),
                              fill=muted_text, font=font_body(10), anchor="w")

            elif section == "weather":
                c.create_text(section_x+section_pad, current_y+58, text=self._weather_card["primary"],
                              fill=muted_primary, font=font_display(30 if focus_boost > 0.08 else 28), anchor="w")
                c.create_text(section_x+section_pad, current_y+84, text=self._weather_card["city"].upper(),
                              fill=muted_label, font=font_body_bold(10), anchor="w")
                wy = current_y + 108
                for line in self._weather_card["details"][:3]:
                    c.create_text(section_x+section_pad, wy, text=f"• {line}", fill=muted_text,
                                  font=font_body(10), anchor="w")
                    wy += 17

            elif section == "system":
                cy = current_y + 44
                uptime = int(time.time() - self._started_at)
                up_min, up_sec = divmod(uptime, 60)
                up_hr, up_min = divmod(up_min, 60)
                c.create_text(section_x+section_pad, cy, text=f"UPTIME  {up_hr:02d}:{up_min:02d}:{up_sec:02d}",
                              fill=muted_label, font=font_body_bold(9), anchor="w")
                cy += 22
                for label, key, unit in [("CPU", "cpu", "%"), ("RAM", "ram", "%"), ("DISK", "disk", "%"), ("BATTERY", "battery", "%")]:
                    val = self._stats[key]
                    col = C_RED if val > 80 and key != "battery" else C_ORG if val > 55 and key != "battery" else (C_RED if key == "battery" and val < 20 else C_GREEN if key == "battery" else C_PRI)
                    if dimmed:
                        col = muted_red if col == C_RED else muted_warn if col == C_ORG else muted_green if col == C_GREEN else muted_primary
                    c.create_text(section_x+section_pad, cy, text=label, fill=muted_label, font=font_body(10), anchor="w")
                    c.create_text(section_x+section_pw-section_pad, cy, text=f"{val:.0f}{unit}", fill=col, font=font_body_bold(10), anchor="e")
                    cy += 14
                    self._bar(c, section_x+section_pad, cy, section_bw, 7, val, col)
                    cy += 16
                up = self._stats["net_up"]
                down = self._stats["net_down"]
                up_s = f"{up:.1f} KB/s" if up < 1000 else f"{up/1024:.1f} MB/s"
                down_s = f"{down:.1f} KB/s" if down < 1000 else f"{down/1024:.1f} MB/s"
                c.create_line(section_x+section_pad, cy-4, section_x+section_pw-section_pad, cy-4, fill="#173130" if dimmed else C_DIM)
                c.create_text(section_x+section_pad, cy+10, text=f"▲ {up_s}", fill=muted_warn, font=font_body(10), anchor="w")
                c.create_text(section_x+section_pw-section_pad, cy+10, text=f"▼ {down_s}", fill=muted_green, font=font_body(10), anchor="e")

            elif section == "voice":
                vg = self._voice_guard_status
                smart = "ON" if vg.get("smart_stop") else "OFF"
                mic = "MUTE" if vg.get("mute_while_speaking") else "OPEN"
                cooldown = f"{int(vg.get('cooldown_ms', 0))}ms"
                browser = str(vg.get("preferred_browser") or "default").upper()
                tile_w = (section_bw - 10) // 2
                self._draw_metric_tile(c, section_x+section_pad, current_y+48, tile_w, 48, "SMART STOP", smart, muted_green if smart == "ON" else muted_warn)
                self._draw_metric_tile(c, section_x+section_pad+tile_w+10, current_y+48, tile_w, 48, "MIC DURING AI", mic, muted_blue)
                self._draw_metric_tile(c, section_x+section_pad, current_y+106, tile_w, 48, "COOLDOWN", cooldown, muted_gold)
                self._draw_metric_tile(c, section_x+section_pad+tile_w+10, current_y+106, tile_w, 48, "BROWSER", browser[:10], muted_primary)

            elif section == "session":
                runtime_log = BASE_DIR / "logs" / "debug" / "runtime.jsonl"
                convo_log = BASE_DIR / "logs" / "conversation" / f"{time.strftime('%Y-%m-%d')}.jsonl"
                try:
                    runtime_events = len(runtime_log.read_text(encoding="utf-8").splitlines()) if runtime_log.exists() else 0
                except Exception:
                    runtime_events = 0
                try:
                    convo_events = len(convo_log.read_text(encoding="utf-8").splitlines()) if convo_log.exists() else 0
                except Exception:
                    convo_events = 0
                c.create_text(section_x+section_pad, current_y+54, text="Runtime events", fill=muted_label, font=font_body(9), anchor="w")
                c.create_text(section_x+section_pw-section_pad, current_y+54, text=str(runtime_events), fill=muted_primary, font=font_body_bold(12), anchor="e")
                c.create_text(section_x+section_pad, current_y+82, text="Conversation lines", fill=muted_label, font=font_body(9), anchor="w")
                c.create_text(section_x+section_pw-section_pad, current_y+82, text=str(convo_events), fill=muted_primary, font=font_body_bold(12), anchor="e")
                c.create_text(section_x+section_pad, current_y+116, text="Logs sekmesinden ayrintilari inceleyebilirsin.", fill=muted_text, font=font_body(9), anchor="w")

            elif False:
                hy = current_y + 48
                for line in self._health_card_lines[:5]:
                    c.create_text(section_x+section_pad, hy, text=f"• {line}", fill=muted_text,
                                  font=font_body(10), anchor="w")
                    hy += 21

            current_y += ph + gap

        self._card_focus_boost = 0.0
        self._card_dimmed = False

    # ── Sağ panel ─────────────────────────────────────────────────────────────
    def _draw_right_panel(self, c):
        x0  = self.CHAT_PANEL_X
        y0  = self.CHAT_PANEL_Y
        pw  = self.CHAT_PANEL_W
        ph  = self.CHAT_PANEL_H
        pad = 10

        self._round_rect(c, x0, y0, x0+pw, y0+ph, r=14, fill=C_SURFACE, outline="")
        self._round_rect(c, x0+1, y0+1, x0+pw-1, y0+ph-1, r=13, fill="", outline=C_LINE, width=1)
        c.create_line(x0+16, y0+1, x0+150, y0+1, fill=C_VIOLET, width=2)

        if self.paused:
            sc, st = C_MID, "PAUSED"
        else:
            sc, st = self._state_color(self._jarvis_state), self._jarvis_state

        c.create_text(x0+16, y0+18, text="CONVERSATION", fill=C_LAV,
                      font=font_display(11), anchor="w")
        c.create_text(x0+pw-pad-4, y0+18, text=st, fill=sc,
                      font=font_body_bold(10), anchor="e")
        c.create_line(x0+16, y0+34, x0+pw-16, y0+34, fill=C_LINE)

    # ── ORB (ana çizim) ───────────────────────────────────────────────────────
    def _draw_living_ribbons(self, c, cx, cy, field_r, rgb, activity):
        r, g, b = rgb
        t = self.tick
        for band in range(10):
            pts = []
            phase = t * (0.0045 + band * 0.0008) + band * 0.71
            spin = phase * (1 if band % 2 == 0 else -1)
            cos_s = math.cos(spin)
            sin_s = math.sin(spin)
            base = field_r * (0.54 + band * 0.045)
            a = base * (1.22 + 0.10 * math.sin(phase + band * 0.4))
            b_axis = base * (0.70 + 0.09 * math.cos(phase * 1.2))
            for k in range(86):
                ang = k * math.tau / 85
                wave = 1.0 + 0.055 * math.sin(ang * 3.0 + phase * 3.6)
                ex = a * math.cos(ang) * wave
                ey = b_axis * math.sin(ang) * (1.0 + 0.04 * math.cos(ang * 2.0 - phase))
                rx = ex * cos_s - ey * sin_s
                ry = ex * sin_s + ey * cos_s
                pts.extend([cx + rx, cy + ry])
            alpha = int((20 + band * 5) * (0.45 + activity * 0.75))
            width = 1 + (1 if band in (2, 6, 8) else 0)
            c.create_line(
                *pts,
                fill=self._ac(r, g, b, min(115, alpha)),
                width=width,
                smooth=True,
                capstyle="round",
            )

    def _draw_orb(self, c):
        state = "PAUSED" if self.paused else self._jarvis_state
        t    = self.tick
        speak_pulse = 1.0
        if self.speaking:
            speak_pulse = 1.0 + 0.15 * math.sin(t * 0.23) + 0.06 * math.sin(t * 0.11 + 1.2)
        elif self.user_speaking:
            speak_pulse = 1.0 + 0.075 * math.sin(t * 0.18 + 0.7)
        elif state in ("THINKING", "INITIALISING"):
            speak_pulse = 1.0 + 0.045 * math.sin(t * 0.10)
        else:
            speak_pulse = 1.0 + 0.026 * math.sin(t * 0.07)

        move_x = 0
        move_y = 0
        if self.user_speaking:
            move_x = int(6 * math.sin(t * 0.06))
            move_y = int(4 * math.cos(t * 0.09 + 0.5))
        elif state in ("THINKING", "INITIALISING"):
            move_x = int(3 * math.sin(t * 0.045))
            move_y = int(2 * math.cos(t * 0.05 + 0.4))

        FCX  = self.FCX + move_x
        FCY  = self.FCY + move_y
        FW   = int(self.FACE * self.scale * speak_pulse)
        R, G, B = self._orb_rgb()
        ha   = self.halo_a
        field_r = int(FW * 0.49)
        inner_r = int(FW * 0.34)
        activity = (
            0.10 if self.paused else
            1.00 if self.speaking else
            0.78 if self.user_speaking else
            0.62 if state in ("THINKING", "INITIALISING") else
            0.26
        )
        if state in ("THINKING", "INITIALISING"):
            accent_rgb = (132, 164, 255)
        elif self.speaking:
            accent_rgb = (236, 226, 255)
        elif self.user_speaking:
            accent_rgb = (196, 178, 255)
        else:
            accent_rgb = (190, 160, 255)

        # Pulse rings
        for pr in self.pulse_r:
            alpha = max(0, int(160 * (1.0 - pr / (FW * 0.70))))
            rr = int(pr + field_r * 0.96)
            c.create_oval(
                FCX-rr, FCY-rr, FCX+rr, FCY+rr,
                outline=self._ac(R, G, B, alpha),
                width=1,
            )

        # Large outer glow
        if not self.paused:
            for i in range(10, 0, -1):
                frac = i / 10
                rr = int(field_r * (1.02 + 0.045 * frac))
                alpha = int(ha * 0.10 * frac)
                if self.speaking:
                    ox = 0
                    oy = 0
                else:
                    ox = int(3 * math.sin(t * 0.010 + i))
                    oy = int(3 * math.cos(t * 0.009 + i * 1.3))
                c.create_oval(
                    FCX-rr+ox, FCY-rr+oy, FCX+rr+ox, FCY+rr+oy,
                    outline=self._ac(R, G, B, alpha),
                    width=3,
                )

        # Structural circles
        for frac, width, alpha_mult in (
            (1.00, 2, 0.34),
            (0.90, 2, 0.24),
            (0.76, 1, 0.18),
            (0.62, 1, 0.12),
        ):
            rr = int(field_r * frac)
            c.create_oval(
                FCX-rr, FCY-rr, FCX+rr, FCY+rr,
                outline=self._ac(R, G, B, int(ha * alpha_mult * (0.4 if self.paused else 1.0))),
                width=width,
            )

        self._draw_living_ribbons(c, FCX, FCY, field_r, (R, G, B), activity)

        speak_shell_push = 1.16 if self.speaking else 1.07 if self.user_speaking else 1.0
        # Orb shell particles
        shell_r = field_r * 0.93 * speak_shell_push
        for idx, sp in enumerate(self.orb_shell_particles):
            angle = sp['angle'] + t * sp['speed'] * (2.8 if self.speaking else 1.6 if self.user_speaking else 1.1)
            wobble = 1.0 + (0.07 if self.speaking else 0.035) * math.sin(t * 0.08 + sp['phase'])
            x = FCX + math.cos(angle) * shell_r * wobble
            y = FCY + math.sin(angle) * shell_r * wobble
            alpha = int((70 + 120 * sp['glow']) * (0.26 if self.paused else 0.52 + activity * 0.45))
            if idx % 9 == 0 and not self.paused:
                col = self._ac(accent_rgb[0], accent_rgb[1], accent_rgb[2], min(255, alpha + 30))
            else:
                col = self._ac(R, G, B, alpha)
            pr = sp['size'] * (1.0 + 0.24 * math.sin(t * 0.05 + sp['phase']))
            c.create_oval(x-pr, y-pr, x+pr, y+pr, fill=col, outline="")

        # Rotating segmented arcs
        arc_r1 = int(field_r * 0.96)
        arc_r2 = int(field_r * 0.78)
        for start, extent, width, accent in (
            (self.rings_spin[0], 52 if self.speaking else 34, 3, False),
            ((self.rings_spin[0] + 148) % 360, 26, 2, True),
            ((self.rings_spin[2] + 28) % 360, 64 if self.user_speaking else 40, 3, False),
            ((self.rings_spin[2] + 212) % 360, 18, 2, True),
        ):
            rr = arc_r1 if width == 3 else arc_r2
            if accent and not self.paused:
                col = self._ac(accent_rgb[0], accent_rgb[1], accent_rgb[2], int(120 + 80 * activity))
            else:
                col = self._ac(R, G, B, int(ha * (1.2 if width == 3 else 0.7)))
            c.create_arc(
                FCX-rr, FCY-rr, FCX+rr, FCY+rr,
                start=start, extent=extent,
                outline=col, width=width, style="arc",
            )

        # Particle orb field
        field_limit = inner_r * (
            0.82 if self.paused else
            1.36 if self.speaking else
            1.16 if self.user_speaking else
            1.0
        )
        for idx, p in enumerate(self.orb_particles):
            speed_mult = (
                0.10 if self.paused else
                3.10 if self.speaking else
                2.00 if self.user_speaking else
                1.10
            )
            angle = p['angle'] + t * p['speed'] * speed_mult
            wobble = 1.0 + (0.30 if self.speaking else 0.18) * math.sin(t * p['wobble'] + p['phase'])
            orbit = field_limit * p['orbit'] * wobble
            depth = 0.5 + 0.5 * math.sin(angle * 2.0 + t * 0.013 + p['phase'])
            y_squash = 0.62 + depth * 0.38
            drift = (8.0 if self.speaking else 5.0 if self.user_speaking else 4.0) * p['depth']
            x = FCX + math.cos(angle) * orbit + math.sin(t * 0.011 + p['phase']) * drift
            y = FCY + math.sin(angle) * orbit * y_squash + math.cos(t * 0.010 + p['phase']) * drift
            base_alpha = int((18 + 155 * p['depth']) * (0.24 + activity * 0.86) * (0.45 + depth * 0.75))
            if self.paused:
                base_alpha = int(base_alpha * 0.40)
            if idx % 11 == 0 and not self.paused:
                col = self._ac(accent_rgb[0], accent_rgb[1], accent_rgb[2], min(255, base_alpha + 25))
            elif self.user_speaking and idx % 7 == 0:
                col = self._ac(120, 205, 255, min(255, base_alpha + 20))
            else:
                col = self._ac(R, G, B, base_alpha)
            pr = p['size'] * (0.70 if self.paused else 0.90 + depth * 0.65 + 0.30 * activity * p['depth'])
            c.create_oval(x-pr, y-pr, x+pr, y+pr, fill=col, outline="")
            if idx % 18 == 0 and not self.paused:
                c.create_line(
                    FCX + (x-FCX) * 0.18,
                    FCY + (y-FCY) * 0.18,
                    x, y,
                    fill=self._ac(R, G, B, int(18 + 35 * p['depth'] * activity)),
                    width=1,
                )

        # Center void keeps the orb airy instead of lens-like.
        void_r = int(inner_r * (0.18 if self.paused else 0.12))
        if void_r > 0:
            c.create_oval(
                FCX-void_r, FCY-void_r, FCX+void_r, FCY+void_r,
                fill=C_BG,
                outline="",
            )

    # ── Ana çizim ─────────────────────────────────────────────────────────────
    def _draw(self):
        c  = self.bg
        W  = self.W
        H  = self.H
        t  = self.tick
        c.delete("all")

        # ── Arka plan ────────────────────────────────────────────────────────
        # Nokta ızgarası — çok ince
        step = 48
        for x in range(0, W, step):
            for y in range(0, H, step):
                c.create_rectangle(x, y, x+1, y+1, fill=C_DIMMER, outline="")

        # Tarama çizgisi (yavaş, çok soluk)
        scan_y = (t * 0.7) % (H + 60) - 30
        for i in range(2):
            ly = (scan_y + i * 20) % H
            c.create_line(0, ly, W, ly+35, fill="#081818", width=1)

        # Partiküller
        R, G, B = self._orb_rgb()
        for p in self.particles:
            if self.speaking:
                col = self._ac(255, 110, 0, p['a'])
            else:
                col = self._ac(R, G, B, p['a'])
            r = p['r']
            c.create_oval(p['x']-r, p['y']-r, p['x']+r, p['y']+r,
                          fill=col, outline="")

        # ── Bölücü çizgiler (ince, soluk) ────────────────────────────────────
        c.create_line(self.LEFT_W, HDR_H+16, self.LEFT_W, H-FOOTER_H-16,
                      fill=C_LINE, width=1)
        c.create_line(W-self.RIGHT_W, HDR_H+16, W-self.RIGHT_W, H-FOOTER_H-16,
                      fill=C_LINE, width=1)

        # ── Yan paneller ──────────────────────────────────────────────────────
        self._draw_left_panel(c)
        self._draw_right_panel(c)

        center_w = max(260, W - self.LEFT_W - self.RIGHT_W - 54)
        self._round_rect(
            c,
            self.LEFT_W + 26, HDR_H + 26,
            self.LEFT_W + 26 + center_w, H - FOOTER_H - 28,
            r=22,
            fill="#070914",
            outline="#14172a",
            width=1,
        )
        c.create_line(self.LEFT_W + 56, HDR_H + 27, self.LEFT_W + 210, HDR_H + 27, fill="#3c326f", width=2)

        # ── Orb ──────────────────────────────────────────────────────────────
        self._draw_orb(c)
        self._draw_agent_overlay(c)

        state_label = "PAUSED" if self.paused else self._jarvis_state
        state_col = self._state_color(state_label)
        c.create_text(self.FCX, self.CTRL_Y - 34, text=SYSTEM_NAME,
                      fill=C_TEXT, font=font_display(18))
        c.create_text(self.FCX, self.CTRL_Y - 12, text=f"● {state_label.title()}",
                      fill=state_col, font=font_body_bold(11))

        # ── HEADER ───────────────────────────────────────────────────────────
        c.create_rectangle(0, 0, W, HDR_H, fill="#060812", outline="")
        # Alt çizgi — teal parlak
        c.create_line(0, HDR_H, W, HDR_H, fill=C_LINE, width=1)
        for i in range(3):
            a = 54 - i * 16
            c.create_line(0, HDR_H-1-i, W, HDR_H-1-i,
                          fill=self._ac(155, 120, 255, a), width=1)

        # Büyük başlık
        c.create_text(W//2, 24, text=SYSTEM_NAME,
                      fill=C_LAV, font=font_display(26))
        c.create_text(W//2, 52, text="Just A Rather Very Intelligent System",
                      fill="#6f75a8", font=font_body(11))

        # Sol: model badge
        c.create_text(22, 36, text=MODEL_BADGE,
                      fill=C_DIM, font=font_body(10), anchor="w")

        # Sağ: durum indikatörü
        indicator_state = "PAUSED" if self.paused else self._jarvis_state
        ind_col = self._state_color(indicator_state)
        indicator_text = self._state_badge_text(indicator_state)
        sym = "●" if self.status_blink else "○"
        c.create_text(W-22, 36, text=f"{sym}  {indicator_text}",
                      fill=ind_col, font=font_body_bold(11), anchor="e")

        # ── FOOTER ───────────────────────────────────────────────────────────
        c.create_rectangle(0, H-FOOTER_H, W, H, fill="#060812", outline="")
        c.create_line(0, H-FOOTER_H, W, H-FOOTER_H, fill=C_LINE, width=1)
        c.create_text(W//2, H-13, fill=C_DIM, font=font_body(9),
                      text="JARVIS · Windows Edition · Realtime Voice Core")
        c.create_text(W-18, H-13, fill=C_DIM, font=font_body(9),
                      text="[F4] MUTE  [F5] PAUSE  [ESC] EXIT", anchor="e")

    def wait_for_api_key(self):
        while not self._api_key_ready:
            time.sleep(0.1)

    def _show_setup_ui(self, edit_mode: bool = False):
        self._close_setup_ui()

        self.setup_frame = tk.Frame(self.root, bg="#00080d",
                                    highlightbackground=C_PRI,
                                    highlightthickness=1)
        setup_w = min(760, max(560, int(self.W * 0.42)))
        setup_h = min(620, max(500, int(self.H * 0.50)))
        self.setup_frame.place(relx=0.5, rely=0.5, anchor="center", width=setup_w, height=setup_h)
        self.setup_frame.pack_propagate(False)

        title = "◈ API AYARLARI" if edit_mode else "◈ İLK KURULUM GEREKLİ"
        subtitle = (
            "Gemini, NVIDIA ve YouTube ayarlarinizi guncelleyin."
            if edit_mode else
            "Gemini API anahtarini girin. NVIDIA ve YouTube alanlari opsiyoneldir."
        )
        config = load_app_config()

        tk.Label(self.setup_frame, text=title,
                 fg=C_PRI, bg="#00080d", font=font_display(20)).pack(pady=(28, 6))
        tk.Label(self.setup_frame, text=subtitle,
                 fg=C_MID, bg="#00080d", font=font_body(13)).pack(pady=(0, 14))
        tk.Label(self.setup_frame, text="GEMINI API KEY",
                 fg=C_DIM, bg="#00080d", font=font_body(12)).pack(pady=(8, 4))

        self.api_entry = tk.Entry(
            self.setup_frame, width=60,
            fg=C_TEXT, bg="#000d12", insertbackground=C_TEXT,
            borderwidth=0, font=font_body(14), show="*")
        self.api_entry.pack(pady=(0, 8), ipady=5)

        current_key = str(config.get("gemini_api_key", "") or "")
        if current_key:
            self.api_entry.insert(0, current_key)

        tk.Label(self.setup_frame, text="NVIDIA API KEY",
                 fg=C_DIM, bg="#00080d", font=font_body(12)).pack(pady=(10, 4))

        self.nvidia_api_entry = tk.Entry(
            self.setup_frame, width=60,
            fg=C_TEXT, bg="#000d12", insertbackground=C_TEXT,
            borderwidth=0, font=font_body(14), show="*")
        self.nvidia_api_entry.pack(pady=(0, 8), ipady=5)
        current_nvidia_key = str(config.get("nvidia_api_key", "") or "")
        if current_nvidia_key:
            self.nvidia_api_entry.insert(0, current_nvidia_key)

        tk.Label(self.setup_frame, text="YOUTUBE API KEY",
                 fg=C_DIM, bg="#00080d", font=font_body(12)).pack(pady=(10, 4))

        self.youtube_api_entry = tk.Entry(
            self.setup_frame, width=60,
            fg=C_TEXT, bg="#000d12", insertbackground=C_TEXT,
            borderwidth=0, font=font_body(14), show="*")
        self.youtube_api_entry.pack(pady=(0, 8), ipady=5)
        current_youtube_key = str(config.get("youtube_api_key", "") or "")
        if current_youtube_key:
            self.youtube_api_entry.insert(0, current_youtube_key)

        tk.Label(self.setup_frame, text="YOUTUBE HANDLE / CHANNEL",
                 fg=C_DIM, bg="#00080d", font=font_body(12)).pack(pady=(10, 4))

        self.youtube_handle_entry = tk.Entry(
            self.setup_frame, width=60,
            fg=C_TEXT, bg="#000d12", insertbackground=C_TEXT,
            borderwidth=0, font=font_body(14))
        self.youtube_handle_entry.pack(pady=(0, 8), ipady=5)
        current_handle = str(config.get("youtube_channel_handle", "") or "")
        if current_handle:
            self.youtube_handle_entry.insert(0, current_handle)

        buttons = tk.Frame(self.setup_frame, bg="#00080d")
        buttons.pack(pady=14)

        tk.Button(buttons, text="▸ KAYDET",
                  command=self._save_api_key, bg=C_BG, fg=C_PRI,
                  activebackground="#003344", font=font_body_bold(13),
                  borderwidth=0, padx=24, pady=10).pack(side="left", padx=8)

        if edit_mode:
            tk.Button(buttons, text="KAPAT",
                      command=self._close_setup_ui, bg="#08111a", fg=C_DIM,
                      activebackground="#10202b", font=font_body_bold(13),
                      borderwidth=0, padx=24, pady=10).pack(side="left", padx=8)

    def _save_api_key(self):
        was_ready = self._api_key_ready
        key = self.api_entry.get().strip() if self.api_entry else ""
        if not key:
            return
        nvidia_key = self.nvidia_api_entry.get().strip() if self.nvidia_api_entry else ""
        youtube_key = self.youtube_api_entry.get().strip() if self.youtube_api_entry else ""
        youtube_handle = self.youtube_handle_entry.get().strip() if self.youtube_handle_entry else ""
        save_app_config(
            {
                "gemini_api_key": key,
                "nvidia_api_key": nvidia_key,
                "youtube_api_key": youtube_key,
                "youtube_channel_handle": youtube_handle,
                "voice": self._current_voice,
            }
        )
        self._close_setup_ui()
        self._api_key_ready = True
        self._refresh_settings_status()
        if was_ready:
            self.write_log("SYS: API ayarlari guncellendi.")
        else:
            self.set_state("LISTENING")
            self.write_log("SYS: JARVIS hazır. Dinliyorum...")
