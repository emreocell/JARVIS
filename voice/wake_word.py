"""Wake_Word_Engine — "hey jarvis" tetikleyici.

Design.md § 7 ve Requirements § 19'a karşılık gelir.

Sorumluluklar
-------------
* openWakeWord ONNX/TFLite "hey_jarvis" modeli; ayrı PyAudio 16kHz mono
  thread (Req 19.1, 19.3).
* start/stop/is_running; app_config.wake_word_enabled ile toggle (Req 19.2).
* Tetiklendiğinde Privacy aktif değilse Voice_Core'a "wake" sinyali yolla
  (Req 19.4).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from runtime.privacy_mode import PrivacyMode

log = logging.getLogger(__name__)

_SAMPLE_RATE = 16000
_CHUNK_SIZE = 1280  # ~80ms @ 16kHz
_WAKE_THRESHOLD = 0.5
_MODEL_NAME = "hey_jarvis"


class WakeWordEngine:
    """openWakeWord tabanlı uyandırma kelimesi motoru.

    Parameters
    ----------
    privacy_mode:
        Privacy aktifken tetikleme yok sayılır (Req 19.4).
    on_wake:
        Tetiklendiğinde çağrılacak callback (sync veya coroutine).
    loop:
        asyncio event loop (coroutine callback için).
    enabled:
        Başlangıç durumu; ``False`` ise ``start()`` çağrısı no-op.
    """

    def __init__(
        self,
        *,
        privacy_mode: "PrivacyMode | None" = None,
        on_wake: Callable[[], None] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
        enabled: bool = False,
    ) -> None:
        self._privacy = privacy_mode
        self._on_wake = on_wake
        self._loop = loop
        self._enabled = enabled
        self._running = False
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Bağımlılık kontrolü
        self._oww_available = False
        self._pyaudio_available = False
        self._check_dependencies()

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Dinlemeyi başlat."""
        if not self._enabled:
            log.debug("WakeWordEngine: devre dışı (wake_word_enabled=False).")
            return
        if self._running:
            return
        if not self._oww_available or not self._pyaudio_available:
            log.warning(
                "WakeWordEngine: openWakeWord veya PyAudio yüklü değil; başlatılamıyor."
            )
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._listen_loop,
            daemon=True,
            name="jarvis-wake-word",
        )
        self._thread.start()
        log.info("WakeWordEngine: dinleme başladı.")

    def stop(self) -> None:
        """Dinlemeyi durdur."""
        self._running = False
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        self._thread = None
        log.debug("WakeWordEngine: durduruldu.")

    def is_running(self) -> bool:
        """Şu an aktif dinleme yapılıyor mu?"""
        return self._running and bool(self._thread and self._thread.is_alive())

    def set_enabled(self, enabled: bool) -> None:
        """Motoru etkinleştir/devre dışı bırak."""
        self._enabled = enabled
        if not enabled and self._running:
            self.stop()
        elif enabled and not self._running:
            self.start()

    # ---------------------------------------------------------------- internal

    def _check_dependencies(self) -> None:
        try:
            import openwakeword  # noqa: F401
            self._oww_available = True
        except ImportError:
            log.warning("WakeWordEngine: 'openwakeword' paketi yüklü değil.")

        try:
            import pyaudio  # noqa: F401
            self._pyaudio_available = True
        except ImportError:
            log.warning("WakeWordEngine: 'pyaudio' paketi yüklü değil.")

    def _listen_loop(self) -> None:
        """Mikrofon dinleme döngüsü (ayrı thread)."""
        try:
            import pyaudio
            import openwakeword
            from openwakeword.model import Model

            # Model yükle
            try:
                oww_model = Model(wakeword_models=[_MODEL_NAME], inference_framework="onnx")
            except Exception:
                try:
                    oww_model = Model(inference_framework="onnx")
                except Exception as exc:
                    log.warning("WakeWordEngine: model yüklenemedi: %s", exc)
                    self._running = False
                    return

            pa = pyaudio.PyAudio()
            stream = pa.open(
                rate=_SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=_CHUNK_SIZE,
            )

            log.debug("WakeWordEngine: mikrofon akışı açıldı.")

            try:
                while not self._stop_event.is_set():
                    audio_chunk = stream.read(_CHUNK_SIZE, exception_on_overflow=False)
                    prediction = oww_model.predict(audio_chunk)

                    # Herhangi bir modelin skoru eşiği aşıyorsa tetikle
                    for model_name, score in prediction.items():
                        if score >= _WAKE_THRESHOLD:
                            log.info(
                                "WakeWordEngine: '%s' tetiklendi (skor=%.3f).",
                                model_name,
                                score,
                            )
                            self._trigger_wake()
                            # Kısa bekleme — çift tetiklemeyi önle
                            self._stop_event.wait(1.5)
                            break
            finally:
                stream.stop_stream()
                stream.close()
                pa.terminate()

        except Exception as exc:
            log.warning("WakeWordEngine: dinleme döngüsü hatası: %s", exc)
        finally:
            self._running = False

    def _trigger_wake(self) -> None:
        """Wake sinyalini işle."""
        # Privacy aktifse yok say (Req 19.4)
        if self._privacy is not None and self._privacy.is_active():
            log.debug("WakeWordEngine: Privacy aktif, wake sinyali yok sayıldı.")
            return

        if self._on_wake is None:
            return

        try:
            result = self._on_wake()
            # Coroutine ise asyncio loop'a gönder
            if asyncio.iscoroutine(result):
                if self._loop and self._loop.is_running():
                    asyncio.run_coroutine_threadsafe(result, self._loop)
                else:
                    log.warning("WakeWordEngine: asyncio loop yok, coroutine atlandı.")
        except Exception:
            log.exception("WakeWordEngine: on_wake callback hatası.")


__all__ = ["WakeWordEngine"]
