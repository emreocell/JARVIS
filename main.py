#!/usr/bin/env python3
"""
JARVIS Windows — Gercek zamanli sesli yardimci cekirdegi
Alp Ünlü tarafından yapılmıştır — @alppunlu
Windows ortamina uyarlanmis calisma akisi
"""

import asyncio
import datetime
import json
import threading
import traceback
import os
from pathlib import Path
from typing import Callable, Literal

import pyaudio  # type: ignore[reportMissingModuleSource]
from google import genai  # type: ignore[reportMissingImports]
from google.genai import types  # type: ignore[reportMissingImports]

from app_config import (
    get_app_config_value, has_nvidia_api_key, load_app_config, save_app_config,
    CONFIG_PATH, load_or_migrate,
)
from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, delete_memory, format_memory_for_prompt
from runtime.task_manager import TaskManager
from runtime.tool_runtime import ToolRuntime
from runtime.plugin_host import PluginHost
from runtime.builtin_tools import register_builtin_tools
from runtime.privacy_mode import PrivacyMode
from runtime.conversation_logger import ConversationLogger
from runtime.conversation_compactor import ConversationCompactor
from runtime.diagnostics import DiagnosticsLogger
from runtime.routine_engine import RoutineEngine
from runtime.clipboard import ClipboardManager
from runtime.hotkeys import HotkeyManager
from runtime.tray_agent import TrayAgent
from runtime.uac_translator import translate as uac_translate
from runtime.clients.gemini_client import GeminiClient, build_clients
from runtime.clients.nim_client import NimClient
from runtime.clients.groq_client import GroqClient
from runtime.clients.openrouter_client import OpenRouterClient
from runtime.clients.health import HealthProbe
from runtime.interruption import (
    intent_requests_interrupt,
    looks_like_interrupt,
    parse_intent_json,
    strip_wake_word,
)
from runtime.agent_visibility import format_tool_visibility
from runtime.model_router import ModelRouter, ModelRouterConfig
from runtime.personality_engine import PersonalityEngine
from runtime.transcript import (
    clean_transcript_text,
    is_meaningful_transcript,
    join_transcript_fragments,
    language_codes_for,
)
from voice.result_announcer import ResultAnnouncer
from voice.wake_word import WakeWordEngine
from ui.theme import ThemeEngine
from ui.mini_overlay import MiniOverlay
from skills.clipboard.tools import set_clipboard_manager as _set_clipboard_manager

# ── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR        = Path(__file__).resolve().parent
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"



# ── Model ───────────────────────────────────────────────────────────────────
LIVE_MODEL = "models/gemini-2.5-flash-native-audio-latest"

# ── Audio ───────────────────────────────────────────────────────────────────
FORMAT           = pyaudio.paInt16
CHANNELS         = 1
SEND_SAMPLE_RATE = 16000
RECV_SAMPLE_RATE = 24000
CHUNK_SIZE       = 1024
pya              = pyaudio.PyAudio()

# ── Tool tanımları ──────────────────────────────────────────────────────────
# TOOL_DECLARATIONS listesi v2'de kaldırıldı (Req 17.3, 18.2).
# Tool tanımları artık skill manifestolarından dinamik olarak yüklenir;
# bkz. JarvisLive.__init__ → PluginHost.discover + host.load.

NVIDIA_TOOL_NAMES = {
    "video_object_detect",
    "audio_to_table",
    "nvidia_text_task",
    "nvidia_image_analyze",
}


def get_api_key() -> str:
    return str(get_app_config_value("gemini_api_key", "") or "")


# ── Event Bus ───────────────────────────────────────────────────────────────

class EventBus:
    """Lightweight synchronous pub/sub event bus for intra-process signalling."""

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event: str, callback: Callable) -> None:
        """Register *callback* to be called when *event* is published."""
        with self._lock:
            self._listeners.setdefault(event, []).append(callback)

    def unsubscribe(self, event: str, callback: Callable) -> None:
        """Remove a previously registered callback."""
        with self._lock:
            listeners = self._listeners.get(event, [])
            if callback in listeners:
                listeners.remove(callback)

    def publish(self, event: str, **kwargs) -> None:
        """Call all callbacks registered for *event*, passing **kwargs**."""
        with self._lock:
            callbacks = list(self._listeners.get(event, []))
        for cb in callbacks:
            try:
                cb(**kwargs)
            except Exception as exc:  # noqa: BLE001
                print(f"[EventBus] ⚠️ {event} handler error: {exc}")


# Singleton event bus shared across the process
event_bus = EventBus()


def load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "Sen JARVIS'sin — Windows'ta çalışan kişisel AI asistanı. "
            "Türkçe konuş. Kısa ve net yanıtlar ver. "
            "Araçları kullanarak görevleri tamamla, asla taklit etme."
        )


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._last_audio_out_at = 0.0
        self._last_state_change_at = datetime.datetime.now().timestamp()
        self._suppress_input_until = 0.0
        self._speaking_lock = threading.Lock()

        # ── Voice state tracking ─────────────────────────────────────────
        self._state: Literal[
            "INITIALISING", "LISTENING", "SPEAKING",
            "THINKING", "MUTED", "ERROR", "PAUSED"
        ] = "INITIALISING"
        self._state_lock = threading.Lock()
        self._state_change_callbacks: list[Callable[[str], None]] = []

        self.ui.on_text_command  = self._on_text_command
        self.ui.on_pause_toggle  = self._on_pause_toggle
        self.ui.on_stop_command  = self._on_stop_command
        self.ui.on_effects_state_change = self._on_effects_state_change
        self.ui.on_voice_control_change = self._on_voice_control_change
        self._paused             = False

        # ── Privacy Mode ─────────────────────────────────────────────────
        load_result = load_or_migrate(CONFIG_PATH)
        cfg = load_result.config if load_result.config is not None else load_app_config()
        voice_control_cfg = cfg.get("voice_control", {})
        if not isinstance(voice_control_cfg, dict):
            voice_control_cfg = {}
        self._barge_in_enabled = bool(voice_control_cfg.get("barge_in_enabled", True))
        self._mute_mic_while_speaking = bool(voice_control_cfg.get("mute_mic_while_speaking", True))
        self._stop_only_barge_in = bool(voice_control_cfg.get("stop_only_barge_in", True))
        self._post_speech_input_cooldown_sec = max(
            0.0,
            float(voice_control_cfg.get("post_speech_input_cooldown_ms", 1600)) / 1000.0,
        )
        self._wake_word_resumes_pause = bool(voice_control_cfg.get("wake_word_resumes_pause", True))
        self._classify_interrupts_with_groq = bool(voice_control_cfg.get("classify_interrupts_with_groq", True))
        self._system_language = str(cfg.get("system_language", "tr-TR") or "tr-TR")
        _lang_codes = cfg.get("transcription_language_codes")
        if isinstance(_lang_codes, list) and all(isinstance(item, str) for item in _lang_codes):
            self._transcription_language_codes = [item for item in _lang_codes if item.strip()]
        else:
            self._transcription_language_codes = language_codes_for(self._system_language)
        self._last_interrupted_text = ""
        privacy_default = bool(cfg.get("privacy_mode_default", False))
        self._privacy = PrivacyMode(initial=privacy_default)
        self._diag = DiagnosticsLogger(BASE_DIR / "logs" / "debug" / "runtime.jsonl")
        self._diag.log(
            "voice_config",
            barge_in_enabled=self._barge_in_enabled,
            mute_mic_while_speaking=self._mute_mic_while_speaking,
            stop_only_barge_in=self._stop_only_barge_in,
            post_speech_input_cooldown_sec=self._post_speech_input_cooldown_sec,
            system_language=self._system_language,
            transcription_language_codes=self._transcription_language_codes,
        )

        # ── Model_Router + Dual Gemini + NIM Client ──────────────────────
        # Requirements: 1.7, 1.8, 2.6
        import logging as _logging
        _mr_log = _logging.getLogger(__name__)

        _primary_key = str(cfg.get("gemini_api_key", "") or "")
        _secondary_key = str(cfg.get("gemini_secondary_api_key", "") or "")
        _extra_gemini_keys = cfg.get("gemini_extra_api_keys", [])
        if not isinstance(_extra_gemini_keys, list):
            _extra_gemini_keys = []
        _nvidia_key = str(cfg.get("nvidia_api_key", "") or "")
        _groq_key = str(cfg.get("groq_api_key", "") or "")
        _openrouter_key = str(cfg.get("openrouter_api_key", "") or "")

        try:
            _gemini_a, _gemini_b = build_clients(_primary_key, _secondary_key)
        except Exception as _exc:
            _mr_log.error("Model_Router: Gemini istemcileri oluşturulamadı: %s", _exc)
            raise

        _gemini_pool: dict[str, GeminiClient] = {}
        for _idx, _key in enumerate(_extra_gemini_keys[:3], start=1):
            _key_str = str(_key or "").strip()
            if not _key_str:
                continue
            _provider_id = f"gemini_extra_{_idx}"
            try:
                _gemini_pool[_provider_id] = GeminiClient(_key_str, provider_id=_provider_id)
            except Exception as _exc:
                _mr_log.warning(
                    "Model_Router: %s istemcisi olusturulamadi: %s",
                    _provider_id,
                    _exc,
                )

        _nim: NimClient | None = None
        if _nvidia_key.strip():
            try:
                _nim = NimClient(_nvidia_key)
            except Exception as _exc:
                _mr_log.warning("Model_Router: NIM istemcisi oluşturulamadı: %s", _exc)
                _nim = None
        else:
            _mr_log.info("Model_Router: NVIDIA API anahtarı yok; NIM istemcisi devre dışı.")

        _groq: GroqClient | None = None
        if _groq_key.strip():
            try:
                _groq = GroqClient(_groq_key)
            except Exception as _exc:
                _mr_log.warning("Model_Router: Groq istemcisi oluşturulamadı: %s", _exc)
                _groq = None
        else:
            _mr_log.info("Model_Router: Groq API anahtarı yok; Groq istemcisi devre dışı.")

        _openrouter: OpenRouterClient | None = None
        if _openrouter_key.strip():
            try:
                _openrouter = OpenRouterClient(_openrouter_key)
            except Exception as _exc:
                _mr_log.warning("Model_Router: OpenRouter istemcisi olusturulamadi: %s", _exc)
                _openrouter = None
        else:
            _mr_log.info("Model_Router: OpenRouter API anahtari yok; OpenRouter istemcisi devre disi.")

        _mr_cfg_dict = cfg.get("model_router", {})
        _mr_config = ModelRouterConfig.from_dict(_mr_cfg_dict)

        self._model_router = ModelRouter(
            _gemini_a,
            _gemini_b,
            _nim,
            _groq,
            _openrouter,
            _mr_config,
            self._privacy,
            gemini_pool=_gemini_pool,
        )

        # ── Health_Probe ─────────────────────────────────────────────────
        # Req 2.6: açılışta her iki Gemini probe sonucu başlangıç log'una yazılır
        _interval = float(_mr_cfg_dict.get("health_check_interval_sec", 60.0))
        self._health_probe = HealthProbe(self._model_router, interval_sec=_interval)

        # Başlangıç probe'u: iki Gemini sağlayıcısını hemen bir kez denetle
        try:
            _initial_states = self._health_probe.probe_once()
            for _provider in ("gemini_primary", "gemini_secondary"):
                _hs = _initial_states.get(_provider)
                if _hs is not None:
                    _status = "✅ sağlıklı" if _hs.healthy else "❌ sağlıksız"
                    _latency = f"{_hs.last_latency_ms}ms" if _hs.last_latency_ms is not None else "N/A"
                    _mr_log.info(
                        "HealthProbe başlangıç: %s → %s (gecikme: %s, hata: %s)",
                        _provider, _status, _latency, _hs.last_error or "yok",
                    )
                    print(
                        f"[JARVIS] 🔍 HealthProbe {_provider}: {_status} "
                        f"(gecikme: {_latency})",
                        flush=True,
                    )
        except Exception as _exc:
            _mr_log.warning("HealthProbe başlangıç probe hatası: %s", _exc)

        # Arka plan döngüsünü başlat
        self._health_probe.start()

        # ── Conversation Logger ──────────────────────────────────────────
        self._conv_logger = ConversationLogger(
            log_dir=BASE_DIR / "logs" / "conversation",
            privacy=self._privacy,
        )
        self._conversation_compactor = ConversationCompactor(
            self._conv_logger,
            cache_path=BASE_DIR / "memory" / "conversation_compact_summary.json",
            model_router=self._model_router,
        )
        self._personality = PersonalityEngine()

        # ── Plugin system bootstrap ──────────────────────────────────────
        # Requirements 17.3, 18.2: TOOL_DECLARATIONS listesi kaldırıldı;
        # tool tanımları skill manifestolarından dinamik olarak yüklenir.
        self._task_manager = TaskManager()
        self._tool_runtime = ToolRuntime(
            self._task_manager,
            model_router=self._model_router,
            privacy_mode=self._privacy,
        )
        register_builtin_tools(self._tool_runtime, self._task_manager)

        host = PluginHost()
        manifests = host.discover([BASE_DIR / "skills"])
        for manifest in manifests:
            descriptors = host.load(manifest)
            for desc in descriptors:
                try:
                    self._tool_runtime.register(desc)
                except ValueError as _dup_exc:
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "JarvisLive: skipping duplicate tool %r from skill %r: %s",
                        desc.name, manifest.name, _dup_exc,
                    )

        # Email skill ayrıca yükle (communication manifest tek entry_module destekler)
        self._load_email_skill()

        # History skill'e ConversationLogger referansı ver
        try:
            from skills.history.tools import set_logger as _set_history_logger
            _set_history_logger(self._conv_logger)
        except Exception:
            pass

        # Bellek araçları skill sistemi dışında özel olarak işlenir;
        # sadece Gemini'nin bunları görmesi için declaration kaydedilir.
        # Handler'lar _execute_tool içinde doğrudan çağrılır.
        from runtime.types import ToolDescriptor as _TD
        _memory_tools = [
            _TD(
                name="save_memory",
                declaration={
                    "name": "save_memory",
                    "description": (
                        "Kullanıcı hakkında önemli bilgiyi kalıcı belleğe kaydeder. "
                        "İsim, tercihler, projeler vb. duyunca sessizce çağır."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "category": {"type": "STRING", "description": "identity | preferences | projects | notes"},
                            "key": {"type": "STRING", "description": "Kısa anahtar (örn. 'name')"},
                            "value": {"type": "STRING", "description": "Değer (İngilizce)"},
                        },
                        "required": ["category", "key", "value"],
                    },
                },
                handler=lambda **kw: "ok",  # placeholder; _execute_tool handles it
                execution_mode="inline",
                skill_id="_builtin",
            ),
            _TD(
                name="delete_memory",
                declaration={
                    "name": "delete_memory",
                    "description": (
                        "Kalıcı hafızadaki bir kaydı siler. "
                        "Kullanıcı 'bunu hafızandan kaldır', 'unut', 'sil' derse kullan."
                    ),
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {
                            "category": {"type": "STRING", "description": "Kaydın kategorisi"},
                            "key": {"type": "STRING", "description": "Silinecek anahtar"},
                            "match_text": {"type": "STRING", "description": "Kaydı bulmak için doğal dil parçası"},
                        },
                    },
                },
                handler=lambda **kw: "ok",  # placeholder; _execute_tool handles it
                execution_mode="inline",
                skill_id="_builtin",
            ),
        ]
        for _mt in _memory_tools:
            try:
                self._tool_runtime.register(_mt)
            except ValueError:
                pass

        # ── Result Announcer ─────────────────────────────────────────────
        self._result_announcer = ResultAnnouncer(
            voice=self,
            privacy=self._privacy,
            hud=self.ui,
        )
        # Task_Manager tamamlanan görevleri Result_Announcer'a ilet
        self._task_manager.on_state_change(self._on_task_state_change)

        # ── Routine Engine ───────────────────────────────────────────────
        self._routine_engine = RoutineEngine(
            routines_path=BASE_DIR / "routines.json",
            tool_runtime=self._tool_runtime,
        )

        # ── Clipboard Manager ────────────────────────────────────────────
        self._clipboard_manager = ClipboardManager(privacy=self._privacy)
        _set_clipboard_manager(self._clipboard_manager)
        self._clipboard_manager.start()

        # ── Theme Engine ─────────────────────────────────────────────────
        self._theme_engine = ThemeEngine()

        # ── Hotkey Manager ───────────────────────────────────────────────
        self._hotkey_manager = HotkeyManager()
        self._register_default_hotkeys()

        # ── Tray Agent ───────────────────────────────────────────────────
        minimize_on_close = bool(cfg.get("tray_minimize_on_close", True))
        self._tray_agent = TrayAgent(
            task_manager=self._task_manager,
            privacy_mode=self._privacy,
            on_show=self._tray_show,
            on_hide=self._tray_hide,
            on_mute_toggle=self._tray_mute_toggle,
            on_quit=self._tray_quit,
            minimize_on_close=minimize_on_close,
        )
        self._tray_agent.start()

        # HUD pencere kapat butonunu Tray_Agent'a bağla (Req 26.1)
        self.ui.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # Privacy değişikliklerini HUD'a yansıt
        self._privacy.on_change(self._on_privacy_change)

        self._wake_engine = WakeWordEngine(
            privacy_mode=self._privacy,
            on_wake=self._on_wake_word,
            enabled=bool(cfg.get("wake_word_enabled", False)),
        )

        # ── Mini Overlay ─────────────────────────────────────────────────
        # MiniOverlay creates Tk widgets (Toplevel + Canvas) and must therefore
        # be constructed on the Tk main thread. JarvisLive.__init__ runs on a
        # worker thread, so we marshal the construction through root.after.
        _overlay_done = threading.Event()
        _overlay_holder: list[MiniOverlay] = []
        _overlay_error: list[BaseException] = []

        def _build_mini_overlay() -> None:
            try:
                _overlay_holder.append(
                    MiniOverlay(
                        self.ui.root,
                        on_show_main=self._exit_mini_mode,
                        on_quit=self._tray_quit,
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                _overlay_error.append(exc)
            finally:
                _overlay_done.set()

        self.ui.root.after(0, _build_mini_overlay)
        # Bekleme süresi: HUD mainloop genelde anında çalıştırır; 10 sn yeterli.
        if not _overlay_done.wait(timeout=10.0):
            self._mini_overlay = None
            print("[JARVIS] ⚠️ MiniOverlay başlatılamadı: timeout", flush=True)
        elif _overlay_error:
            self._mini_overlay = None
            print(
                f"[JARVIS] ⚠️ MiniOverlay başlatılamadı: {_overlay_error[0]!r}",
                flush=True,
            )
        else:
            self._mini_overlay = _overlay_holder[0]

        # HUD'daki mini mod butonunu bağla
        self.ui._on_enter_mini_mode = self._enter_mini_mode

    def _load_email_skill(self) -> None:
        """Email skill tool'larını ayrıca kaydet."""
        try:
            from skills.communication.email_outlook import read_emails, send_email
            from runtime.types import ToolDescriptor as _TD
            for fn in (read_emails, send_email):
                meta = getattr(fn, "__tool__", None)
                if meta is None:
                    continue
                desc = _TD(
                    name=meta["declaration"]["name"],
                    declaration=meta["declaration"],
                    handler=fn,
                    execution_mode=meta.get("execution_mode", "inline"),
                    skill_id="communication",
                )
                try:
                    self._tool_runtime.register(desc)
                except ValueError:
                    pass
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning("Email skill yüklenemedi: %s", exc)

    def _register_default_hotkeys(self) -> None:
        """Varsayılan hotkey'leri kaydet."""
        cfg = load_app_config()
        hotkeys = cfg.get("hotkeys", {})
        palette_key = hotkeys.get("command_palette", "ctrl+shift+space")
        # Command_Palette hotkey'i ui/palette.py içinde Tk bind ile de kayıtlı;
        # burada HotkeyManager'a da ekliyoruz (global, Tk odağı olmasa da çalışır).
        self._hotkey_manager.register(
            palette_key,
            action="command_palette",
            description="Komut paletini aç",
        )

    # ── Tray callbacks ───────────────────────────────────────────────────

    def _tray_hide(self) -> None:
        try:
            self.ui.root.withdraw()
            if hasattr(self, "_mini_overlay"):
                self._mini_overlay.show()
        except Exception:
            pass

    def _tray_show(self) -> None:
        try:
            self.ui.root.deiconify()
            self.ui.root.lift()
        except Exception:
            pass

    def _enter_mini_mode(self) -> None:
        """HUD'u gizle, mini overlay'i öne çıkar."""
        try:
            self.ui.root.withdraw()
        except Exception:
            pass
        try:
            if hasattr(self, "_mini_overlay"):
                self._mini_overlay.show()
        except Exception:
            pass

    def _exit_mini_mode(self) -> None:
        """Mini overlay'den çıkıp HUD'u tam ekran getir."""
        # Mini'yi gizle
        try:
            if hasattr(self, "_mini_overlay"):
                self._mini_overlay.hide()
        except Exception:
            pass
        # HUD'u göster ve tam ekrana geç
        try:
            self.ui.root.deiconify()
            self.ui.root.lift()
            # HUD'un kendi tam ekran moduna geri dön
            try:
                if hasattr(self.ui, "_enter_fullscreen"):
                    self.ui._fullscreen = True
                    self.ui._enter_fullscreen()
            except Exception:
                pass
            self.ui.root.attributes("-topmost", True)
            self.ui.root.after(500, lambda: self.ui.root.attributes("-topmost", False))
        except Exception:
            pass

    def _tray_mute_toggle(self) -> None:
        try:
            self.ui._toggle_mute()
        except Exception:
            pass

    def _tray_quit(self) -> None:
        try:
            self._wake_engine.stop()
        except Exception:
            pass
        try:
            self.ui.root.after(0, self.ui.root.destroy)
        except Exception:
            pass

    def _on_window_close(self) -> None:
        """Pencere kapatma butonuna basıldığında Tray_Agent'a sor."""
        if not self._tray_agent.handle_window_close():
            # minimize_on_close False → normal kapat
            self._tray_quit()

    # ── Privacy callback ─────────────────────────────────────────────────

    def _on_privacy_change(self, active: bool) -> None:
        """Privacy değiştiğinde HUD'u güncelle."""
        try:
            if active:
                self.ui.write_log("SYS: Privacy Mode aktif — mikrofon ve kayıt duraklatıldı.")
                try:
                    self._wake_engine.stop()
                except Exception:
                    pass
                self._set_state("MUTED")
            else:
                self.ui.write_log("SYS: Privacy Mode devre dışı — dinleme devam ediyor.")
                try:
                    if self._paused:
                        self._wake_engine.start()
                except Exception:
                    pass
                self._set_state("LISTENING")
        except Exception:
            pass

    # ── Task state callback ──────────────────────────────────────────────

    def _on_wake_word(self) -> None:
        """Wake word callback from the background detector."""
        if self._privacy.is_active():
            return
        try:
            self._wake_engine.stop()
        except Exception:
            pass
        if self._paused and self._wake_word_resumes_pause:
            self._paused = False
            try:
                if self.ui.paused:
                    self.ui.root.after(0, self.ui._toggle_pause)
            except Exception:
                pass
        try:
            self.ui.write_log("SYS: Wake word algilandi.")
        except Exception:
            pass
        self._set_state("LISTENING")

    def _on_stop_command(self) -> None:
        self._request_interrupt("text", "stop")

    def _should_interrupt_text(self, text: str, *, use_router: bool = False) -> bool:
        if looks_like_interrupt(text):
            return True
        if not use_router or not self._classify_interrupts_with_groq:
            return False
        try:
            from skills.metacognition.tools import classify_intent_fast

            raw = classify_intent_fast(
                text,
                context=f"voice_state={self.state}; jarvis_speaking={self._is_speaking}",
                model_router=self._model_router,
            )
            return intent_requests_interrupt(parse_intent_json(raw))
        except Exception:
            return False

    def _request_interrupt(self, source: str, text: str = "") -> None:
        self._last_interrupted_text = text
        try:
            self.ui.write_log("SYS: Konusma kesildi.")
        except Exception:
            pass
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._interrupt_audio(), self._loop)
        else:
            self.set_speaking(False)

    def _on_task_state_change(self, task) -> None:
        """Tamamlanan görevleri Result_Announcer'a ilet."""
        from runtime.types import TaskState
        if task.state in (TaskState.SUCCEEDED, TaskState.FAILED):
            self._result_announcer.enqueue(task)

    def _on_pause_toggle(self, paused: bool):
        self._paused = paused
        if paused:
            try:
                self._wake_engine.start()
            except Exception:
                pass
            self._set_state("PAUSED")
        else:
            try:
                self._wake_engine.stop()
            except Exception:
                pass
            # Resume to LISTENING unless currently speaking
            with self._speaking_lock:
                speaking = self._is_speaking
            self._set_state("SPEAKING" if speaking else "LISTENING")

    def _on_effects_state_change(self, enabled: bool):
        pass

    def _on_voice_control_change(self, cfg: dict):
        if not isinstance(cfg, dict):
            return
        self._barge_in_enabled = bool(cfg.get("barge_in_enabled", self._barge_in_enabled))
        self._mute_mic_while_speaking = bool(
            cfg.get("mute_mic_while_speaking", self._mute_mic_while_speaking)
        )
        self._stop_only_barge_in = bool(cfg.get("stop_only_barge_in", self._stop_only_barge_in))
        self._post_speech_input_cooldown_sec = max(
            0.0,
            float(
                cfg.get(
                    "post_speech_input_cooldown_ms",
                    int(self._post_speech_input_cooldown_sec * 1000),
                )
            )
            / 1000.0,
        )
        try:
            self._diag.log(
                "voice_control_changed",
                barge_in_enabled=self._barge_in_enabled,
                mute_mic_while_speaking=self._mute_mic_while_speaking,
                stop_only_barge_in=self._stop_only_barge_in,
                post_speech_input_cooldown_sec=self._post_speech_input_cooldown_sec,
            )
        except Exception:
            pass

    # ── State property ───────────────────────────────────────────────────

    @property
    def state(self) -> str:
        """Current Voice_Core state.

        Valid values: INITIALISING | LISTENING | SPEAKING | THINKING | MUTED | ERROR | PAUSED
        """
        with self._state_lock:
            return self._state

    def _set_state(
        self,
        new_state: Literal[
            "INITIALISING", "LISTENING", "SPEAKING",
            "THINKING", "MUTED", "ERROR", "PAUSED"
        ],
    ) -> None:
        """Update internal state, notify callbacks, and update the HUD."""
        with self._state_lock:
            if self._state == new_state:
                return
            previous = self._state
            self._state = new_state
            self._last_state_change_at = datetime.datetime.now().timestamp()
            callbacks = list(self._state_change_callbacks)
        try:
            self._diag.log("state_change", previous=previous, state=new_state)
        except Exception:
            pass

        # Notify registered listeners (outside the lock to avoid deadlocks)
        for cb in callbacks:
            try:
                cb(new_state)
            except Exception as exc:  # noqa: BLE001
                print(f"[JarvisLive] ⚠️ state_change callback error: {exc}")

        # Keep the HUD in sync
        self.ui.set_state(new_state)

        # Mini overlay'i güncelle
        try:
            if hasattr(self, "_mini_overlay"):
                self._mini_overlay.set_state(new_state)
        except Exception:
            pass

    def on_state_change(self, callback: Callable[[str], None]) -> None:
        """Register *callback* to be called whenever the voice state changes.

        The callback receives the new state string as its sole argument.
        """
        self._state_change_callbacks.append(callback)

    # ── New Voice_Core hooks (Task 7.1) ──────────────────────────────────

    async def send_system_message(self, text: str) -> None:
        """Inject a system message into the active Gemini Live session.

        Used by Result_Announcer to deliver background-task results to the
        model without interrupting the current audio stream.

        If no session is active the message is silently dropped and a warning
        is written to the debug log.
        """
        if not self.session:
            self.ui.write_debug(
                "send_system_message: no active session — message dropped",
                level="WARN",
            )
            return
        try:
            await self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True,
            )
            print(f"[JarvisLive] 📨 system_message injected ({len(text)} chars)")
        except Exception as exc:
            self.ui.write_debug(
                f"send_system_message error: {exc}",
                level="ERROR",
            )

    async def on_turn_complete(self) -> None:
        """Called at every Turn_Boundary.

        Publishes the ``"voice.turn_complete"`` event on the global
        :data:`event_bus` so that Result_Announcer (and any other subscriber)
        can react without being tightly coupled to Voice_Core.
        """
        event_bus.publish("voice.turn_complete", voice=self)
        print("[JarvisLive] 🔔 voice.turn_complete published")

    def _focus_ui_section_for_tool(self, tool_name: str, args: dict):
        if tool_name == "sys_info":
            query = str(args.get("query", "")).strip().lower()
            if query in {"time", "saat", "zaman", "date", "tarih"}:
                self.ui.focus_panel("time", duration_ms=5200)
            else:
                self.ui.focus_panel("system", duration_ms=5200)
        elif tool_name == "get_weather":
            self.ui.focus_panel("weather", duration_ms=5600)
        elif tool_name == "get_health_data":
            self.ui.focus_panel("health", duration_ms=5600)

    def _on_text_command(self, text: str):
        if self._paused:
            return
        text, had_wake = strip_wake_word(text)
        if not text and had_wake:
            self._set_state("LISTENING")
            return
        try:
            self._personality.observe_user_message(text)
        except Exception:
            pass
        if self._should_interrupt_text(text, use_router=True):
            self.ui.write_log(f"Siz: {text}")
            self._request_interrupt("text", text)
            return
        self.ui.write_log(f"Siz: {text}")
        if not self._loop or not self.session:
            self.ui.write_log("ERR: JARVIS bağlantısı henüz hazır değil.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    async def _interrupt_audio(self):
        try:
            if self.audio_in_queue:
                while not self.audio_in_queue.empty():
                    try:
                        self.audio_in_queue.get_nowait()
                    except Exception:
                        break
            if self.out_queue:
                while not self.out_queue.empty():
                    try:
                        self.out_queue.get_nowait()
                    except Exception:
                        break
            if self.session:
                await self.session.send_realtime_input(audio_stream_end=True)
            self.set_speaking(False)
        except Exception:
            pass


    def set_speaking(self, value: bool):
        with self._speaking_lock:
            previous = self._is_speaking
            self._is_speaking = value
        if previous and not value and self._post_speech_input_cooldown_sec > 0:
            self._suppress_input_until = (
                datetime.datetime.now().timestamp() + self._post_speech_input_cooldown_sec
            )
            try:
                self._diag.log(
                    "post_speech_input_cooldown",
                    duration_sec=self._post_speech_input_cooldown_sec,
                )
            except Exception:
                pass
        if value:
            self._set_state("SPEAKING")
        else:
            self._set_state("LISTENING")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.ui.write_debug(f"{tool_name}: {short}", level="ERROR")
        self._diag.log("tool_exception", tool=tool_name, error=short)
        self._set_state("ERROR")

    @staticmethod
    def _result_looks_like_error(result) -> bool:
        raw_text = str(result or "").strip()
        if not raw_text:
            return False
        try:
            parsed = json.loads(raw_text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("blocked"):
                return True
            if "ok" in parsed:
                return parsed.get("ok") is False
            if "steps" in parsed and "failed" not in parsed:
                return False
            failed = parsed.get("failed")
            if isinstance(failed, list):
                return bool(failed)

        text = raw_text.lower()
        if not text:
            return False
        error_prefixes = (
            "hata:",
            "error:",
            "err:",
            "exception:",
            "traceback",
            "başarısız",
            "basarisiz",
        )
        if text.startswith(error_prefixes):
            return True
        success_markers = (
            "açıldı",
            "acildi",
            "gönderildi",
            "gonderildi",
            "tamam",
            "başlatıldı",
            "baslatildi",
        )
        if any(marker in text for marker in success_markers):
            return False
        error_markers = (
            "hata",
            "error",
            "alinamadi",
            "alınamadı",
            "bulunamadi",
            "bulunamadı",
            "acilamadi",
            "açılamadı",
            "tamamlanamadi",
            "tamamlanamadı",
            "gecersiz",
            "geçersiz",
            "izin gerekiyor",
            "izin gerekli",
            "baglanti",
            "bağlantı",
        )
        return any(marker in text for marker in error_markers)

    @staticmethod
    def _should_play_success_sfx(tool_name: str, args: dict, result) -> bool:
        action_tools = {
            "open_app",
            "add_calendar_event",
            "add_reminder",
            "delete_calendar_event",
            "remove_calendar_event",
        }
        if tool_name in action_tools:
            return True

        if tool_name == "send_whatsapp_message":
            text = str(result or "").lower()
            if bool(args.get("send_now", False)):
                return "gönderildi" in text or "gonderildi" in text
            return False

        if tool_name == "send_whatsapp_via_search":
            text = str(result or "").lower()
            if bool(args.get("send_now", True)):
                return "gönderildi" in text or "gonderildi" in text
            return False

        return False

    def _build_config(self) -> types.LiveConnectConfig:
        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)
        sys_p   = load_system_prompt()
        now     = datetime.datetime.now()
        time_ctx = f"[ŞU ANKİ ZAMAN]\n{now.strftime('%A, %d %B %Y — %H:%M')}\n\n"

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str + "\n\n")
        try:
            compact_context = self._conversation_compactor.get_context()
            if compact_context:
                parts.append(
                    "[COMPACT CONVERSATION CONTEXT]\n"
                    + compact_context
                    + "\n\n"
                )
        except Exception:
            pass
        try:
            personality_context = self._personality.format_for_prompt()
            if personality_context:
                parts.append(personality_context + "\n\n")
        except Exception:
            pass
        parts.append(sys_p)
        parts.append(
            "\n[VOICE/LANGUAGE]\n"
            f"Primary speech/transcription language: {self._system_language}. "
            "Prefer Turkish for everyday speech, but preserve English terms, code, names, "
            "and user-requested foreign words exactly when they are relevant.\n"
        )
        preferred_browser = str(get_app_config_value("preferred_browser", "") or "").strip()
        user_rules = str(get_app_config_value("user_rules", "") or "").strip()
        if preferred_browser:
            parts.append(
                "\n[USER BROWSER PREFERENCE]\n"
                f"Tarayıcı açma ve web otomasyonunda kullanıcı özellikle başka bir şey istemedikçe {preferred_browser} kullan.\n"
            )
        if user_rules:
            parts.append("\n[USER RULES]\n" + user_rules + "\n")

        activity_handling = (
            types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS
            if self._barge_in_enabled and not self._stop_only_barge_in
            else types.ActivityHandling.NO_INTERRUPTION
        )

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            # Gemini Developer API currently rejects AudioTranscriptionConfig.language_codes.
            # Keep transcription enabled, and steer language via system_instruction instead.
            output_audio_transcription={},
            input_audio_transcription={},
            realtime_input_config=types.RealtimeInputConfig(
                activity_handling=activity_handling,
                automatic_activity_detection=types.AutomaticActivityDetection(
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    silence_duration_ms=650,
                ),
            ),
            proactivity=types.ProactivityConfig(proactive_audio=False),
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": self._tool_runtime.declarations()}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=str(get_app_config_value("voice", "Charon") or "Charon")
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        print(f"[JARVIS] 🔧 {name} {args}")
        self._set_state("THINKING")

        result = "Tamam."
        had_exception = False

        # NVIDIA araçları için API anahtarı kontrolü
        if name in NVIDIA_TOOL_NAMES and not has_nvidia_api_key():
            self._set_state("ERROR")
            result = "NVIDIA API anahtari girilmedigi icin bu ozellik kapali. API ayarlarindan NVIDIA key ekleyin."
            return types.FunctionResponse(id=fc.id, name=name, response={"result": result})

        try:
            # Bellek araçları skill sistemi dışında özel olarak işlenir
            if name == "save_memory":
                cat = args.get("category", "notes")
                key = args.get("key", "")
                val = args.get("value", "")
                if key and val:
                    update_memory({cat: {key: {"value": val}}})
                    print(f"[Memory] 💾 {cat}/{key} = {val}")
                result = "ok"

            elif name == "delete_memory":
                result = delete_memory(
                    args.get("category", ""),
                    args.get("key", ""),
                    args.get("match_text", ""),
                )

            else:
                # UI odak efektleri (skill dispatch'ten önce)
                self._focus_ui_section_for_tool(name, args)

                # Tüm skill araçları Tool_Runtime üzerinden dispatch edilir
                # (Req 17.3, 18.2)
                response_payload = await self._tool_runtime.dispatch(
                    name, args, voice=self
                )

                # get_health_data için özel UI hologram gösterimi
                if name == "get_health_data":
                    raw_result = response_payload.get("result", "")
                    if raw_result:
                        try:
                            self.ui.show_health_hologram(args.get("query", "all"), raw_result)
                        except Exception:
                            pass

                # Background dispatch: task_id + message döner
                if "task_id" in response_payload:
                    result = response_payload.get("message", "Görev arka planda başlatıldı.")
                else:
                    result = response_payload.get("result", "Tamam.")

                visibility = format_tool_visibility(name, args, result)
                if visibility:
                    try:
                        self.ui.write_log("SYS: " + visibility)
                        self.ui.write_debug(visibility, level="INFO")
                        if hasattr(self.ui, "show_agent_timeline"):
                            self.ui.show_agent_timeline(visibility)
                    except Exception:
                        pass

        except Exception as e:
            # UAC / PermissionError için Türkçe mesaj dene
            uac_msg = uac_translate(e, tool_name=name)
            if uac_msg:
                result = uac_msg
            else:
                result = f"Hata: {e}"
            had_exception = True
            traceback.print_exc()
            self.speak_error(name, e)

        tool_failed = self._result_looks_like_error(result)
        self._diag.log(
            "tool_result",
            tool=name,
            failed=tool_failed,
            had_exception=had_exception,
            result_preview=str(result)[:240],
        )
        if tool_failed:
            if not had_exception:
                self._set_state("ERROR")
        elif self._should_play_success_sfx(name, args, result):
            self.ui.play_success_sfx()

        if not tool_failed and not self.ui.muted:
            self._set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mikrofon başladı")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT, channels=CHANNELS,
            rate=SEND_SAMPLE_RATE, input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        try:
            while True:
                data = await asyncio.to_thread(
                    stream.read, CHUNK_SIZE, exception_on_overflow=False)
                with self._speaking_lock:
                    jarvis_speaking = self._is_speaking
                if datetime.datetime.now().timestamp() < self._suppress_input_until:
                    continue
                if jarvis_speaking and self._mute_mic_while_speaking:
                    continue
                can_barge_in = self._barge_in_enabled and jarvis_speaking
                if (not jarvis_speaking or can_barge_in) and not self.ui.muted and not self._paused:
                    await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
        except Exception as e:
            print(f"[JARVIS] ❌ Mikrofon: {e}")
            raise
        finally:
            stream.close()

    async def _receive_audio(self):
        print("[JARVIS] 👂 Alım başladı")
        out_buf, in_buf = [], []
        output_noise = False
        output_noise_samples = []
        try:
            while True:
                async for response in self.session.receive():
                    if response.data:
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            self.set_speaking(True)
                            raw_txt = sc.output_transcription.text.strip()
                            if raw_txt:
                                txt, had_noise = clean_transcript_text(raw_txt)
                                if had_noise:
                                    output_noise = True
                                    if len(output_noise_samples) < 4:
                                        output_noise_samples.append(raw_txt)
                                if txt:
                                    out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt, _ = clean_transcript_text(sc.input_transcription.text)
                            if txt:
                                if datetime.datetime.now().timestamp() < self._suppress_input_until:
                                    self._diag.log("ignored_cooldown_transcript", text=txt)
                                    continue
                                with self._speaking_lock:
                                    jarvis_speaking = self._is_speaking
                                if self._barge_in_enabled and jarvis_speaking:
                                    if self._should_interrupt_text(
                                        txt,
                                        use_router=not self._stop_only_barge_in,
                                    ):
                                        in_buf = []
                                        out_buf = []
                                        output_noise = False
                                        output_noise_samples = []
                                        self._diag.log("voice_interrupt", text=txt)
                                        self._request_interrupt("voice", txt)
                                        continue
                                    self._diag.log("ignored_barge_in_transcript", text=txt)
                                    continue
                                in_buf.append(txt)
                                self.ui.mark_user_activity(True)

                        if sc.turn_complete:
                            self.set_speaking(False)

                            full_in = join_transcript_fragments(in_buf)
                            if full_in and is_meaningful_transcript(full_in):
                                try:
                                    self._personality.observe_user_message(full_in)
                                except Exception:
                                    pass
                                self.ui.write_log(f"Siz: {full_in}")
                                self._conv_logger.log_message("user", full_in)
                            elif full_in:
                                self._diag.log("ignored_transcript", role="user", text=full_in)
                            in_buf = []

                            full_out = join_transcript_fragments(out_buf)
                            if full_out:
                                self.ui.write_log(f"JARVIS: {full_out}")
                                self._conv_logger.log_message("assistant", full_out)
                                if output_noise_samples:
                                    self.ui.write_debug(
                                        "Kısmen filtrelenen ses transcripti: " + " | ".join(output_noise_samples),
                                        level="WARN",
                                    )
                            elif output_noise:
                                self.ui.write_log("ERR: JARVIS sesli yanıtını çözümlerken bir hata oluştu.")
                                if output_noise_samples:
                                    self.ui.write_debug(
                                        "Filtrelenen ham transcript: " + " | ".join(output_noise_samples),
                                        level="WARN",
                                    )
                                self._set_state("ERROR")
                            out_buf = []
                            output_noise = False
                            output_noise_samples = []

                            # Notify subscribers (e.g. Result_Announcer) that
                            # the turn boundary has been reached.
                            await self.on_turn_complete()

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            self._conv_logger.log_message(
                                "tool", f"tool_call: {fc.name}",
                                tool_name=fc.name
                            )
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses)

        except Exception as e:
            print(f"[JARVIS] ❌ Alım: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Ses çalma başladı")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT, channels=CHANNELS,
            rate=RECV_SAMPLE_RATE, output=True,
        )
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
                self._last_audio_out_at = datetime.datetime.now().timestamp()
        except Exception as e:
            print(f"[JARVIS] ❌ Ses: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.close()

    async def _speaking_watchdog(self):
        """Recover from stale SPEAKING state when no audio is being played."""
        while True:
            await asyncio.sleep(1.0)
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking:
                continue
            now_ts = datetime.datetime.now().timestamp()
            audio_idle = now_ts - max(self._last_audio_out_at, self._last_state_change_at)
            try:
                queue_size = self.audio_in_queue.qsize() if self.audio_in_queue else 0
            except Exception:
                queue_size = -1
            if queue_size == 0 and audio_idle >= 4.0:
                self._diag.log(
                    "speaking_watchdog_recovered",
                    audio_idle_sec=round(audio_idle, 2),
                    audio_queue_size=queue_size,
                    state=self.state,
                )
                self.set_speaking(False)

    async def run(self):
        client = genai.Client(
            api_key=get_api_key(),
            http_options={"api_version": "v1alpha"}
        )

        while True:
            # Duraklatılmışsa bağlanma, bekle
            if self._paused:
                await asyncio.sleep(1)
                continue

            try:
                print("[JARVIS] 🔌 Bağlanıyor...")
                self._set_state("THINKING")
                config = self._build_config()

                async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)

                    print("[JARVIS] ✅ Bağlandı.")
                    self._set_state("LISTENING")
                    self.ui.write_log("SYS: JARVIS hazır. Dinliyorum...")

                    # Create tasks without TaskGroup for Python 3.10 compatibility
                    t1 = asyncio.create_task(self._send_realtime())
                    t2 = asyncio.create_task(self._listen_audio())
                    t3 = asyncio.create_task(self._receive_audio())
                    t4 = asyncio.create_task(self._play_audio())
                    t5 = asyncio.create_task(self._speaking_watchdog())
                    
                    await asyncio.gather(t1, t2, t3, t4, t5)

            except Exception as e:
                print(f"[JARVIS] ⚠️ {e}")
                traceback.print_exc()
                self.set_speaking(False)
                self.ui.write_log(f"ERR: JARVIS baglantisi kesildi veya internete ulasilamiyor — {e}")
                self._set_state("ERROR")
                print("[JARVIS] 🔄 3 saniyede yeniden bağlanıyor...")
                await asyncio.sleep(3)


def main():
    if os.environ.get("TERM_PROGRAM") == "vscode":
        print("[JARVIS] VS Code icinden baslatildi.")

    ui = JarvisUI()

    def runner():
        ui.wait_for_api_key()
        try:
            jarvis = JarvisLive(ui)
        except Exception as exc:
            print(f"[JARVIS] ❌ JarvisLive başlatılamadı: {exc}", flush=True)
            traceback.print_exc()
            try:
                ui.write_log(f"ERR: JARVIS başlatılamadı — {exc}")
            except Exception:
                pass
            return

        # Command_Palette'i HUD'a bağla.
        # CommandPalette uses tk.StringVar/bind_all and must be constructed on
        # the Tk main thread.
        try:
            from ui.palette import CommandPalette
            loop = asyncio.new_event_loop()
            _palette_done = threading.Event()
            _palette_holder: list[CommandPalette] = []
            _palette_error: list[BaseException] = []

            def _build_palette() -> None:
                try:
                    _palette_holder.append(
                        CommandPalette(
                            ui.root,
                            tool_runtime=jarvis._tool_runtime,
                            routine_engine=jarvis._routine_engine,
                            loop=loop,
                        )
                    )
                except BaseException as exc:  # noqa: BLE001
                    _palette_error.append(exc)
                finally:
                    _palette_done.set()

            ui.root.after(0, _build_palette)
            if not _palette_done.wait(timeout=10.0):
                print("[JARVIS] CommandPalette başlatılamadı: timeout", flush=True)
            elif _palette_error:
                print(f"[JARVIS] CommandPalette başlatılamadı: {_palette_error[0]}", flush=True)
            else:
                ui._command_palette = _palette_holder[0]
        except Exception as exc:
            print(f"[JARVIS] CommandPalette başlatılamadı: {exc}")

        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Kapatılıyor...")
        except Exception as exc:
            print(f"[JARVIS] ❌ Run loop hatası: {exc}")
            traceback.print_exc()
            try:
                ui.write_log(f"ERR: JARVIS çalışma döngüsü çöktü — {exc}")
            except Exception:
                pass
        finally:
            # Temizlik
            try:
                jarvis._health_probe.stop()
            except Exception:
                pass
            try:
                jarvis._clipboard_manager.stop()
            except Exception:
                pass
            try:
                jarvis._hotkey_manager.shutdown()
            except Exception:
                pass
            try:
                jarvis._tray_agent.stop()
            except Exception:
                pass
            try:
                jarvis._task_manager.shutdown(wait=False)
            except Exception:
                pass

    threading.Thread(target=runner, daemon=True).start()
    try:
        ui.root.mainloop()
    except KeyboardInterrupt:
        print("\n[JARVIS] Kapatildi.", flush=True)
        try:
            ui.root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    main()
