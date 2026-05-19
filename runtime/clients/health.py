"""Health_Probe — periyodik sağlayıcı canlılık denetimi.

Her sağlayıcı (gemini_primary, gemini_secondary, nvidia) için hafif bir
ping gönderir ve sonuçları ``HealthState`` nesneleri olarak tutar.
Model_Router bu durumu okuyarak sağlıksız sağlayıcıları fallback zincirine
yönlendirir (Requirements 1.6, 2.6, 15.7).

Ping stratejisi
---------------
* ``gemini_primary`` / ``gemini_secondary`` → küçük bir chat isteği
  (``RouteRequest(kind="chat", messages=[{"role":"user","content":"ping"}],
  max_tokens=1)``).
* ``nvidia`` → küçük bir chat completion isteği
  (``RouteRequest(kind="chat", messages=[{"role":"user","content":"ping"}],
  max_tokens=1)``).

Sağlık durumu geçişleri
-----------------------
* Her başarılı ping → ``failure_streak = 0``, ``healthy = True``.
* Her başarısız ping → ``failure_streak += 1``.
* ``failure_streak >= 2`` → ``healthy = False``; bu durum en az
  ``unhealthy_window_sec`` (varsayılan 60 sn) boyunca korunur.
* Tek bir başarılı ping → ``failure_streak`` sıfırlanır ve ``healthy``
  hemen ``True`` olur (60 sn beklenmez).

Saat enjeksiyonu
----------------
``time_provider: Callable[[], float]`` parametresi sayesinde gerçek
``time.monotonic`` yerine sahte bir saat kullanılabilir; bu özellik
Property 3 (Health_Probe karar tutarlılığı ve interval saygısı) için
Hypothesis testlerinde kullanılır.

Tasarım notları
---------------
* ``HealthProbe`` arka planda bir ``threading.Thread`` çalıştırır.
  ``start()`` idempotent'tir: zaten çalışıyorsa ikinci kez başlatmaz.
* ``stop()`` thread'e durdurma sinyali gönderir ve ``join()`` ile
  tamamlanmasını bekler (en fazla ``interval_sec + 2`` sn).
* ``state()`` anlık bir kopya döndürür; çağıran taraf değiştiremez.
* Router'a bağımlılık ``_RouterProtocol`` üzerinden soyutlanmıştır;
  böylece ``ModelRouter`` henüz oluşturulmadan önce bu modül import
  edilebilir ve testlerde sahte bir router kullanılabilir.

Validates: Requirements 1.6, 2.6, 15.7 (jarvis-nvidia-skill-pack)
"""

from __future__ import annotations

# Feature: jarvis-nvidia-skill-pack, Task 5.3 — runtime/clients/health.py

import logging
import threading
import time
from copy import deepcopy
from typing import Callable, Protocol, runtime_checkable

from runtime.types import HealthState, Route, RouteRequest, RouteResult

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_UNHEALTHY_STREAK_THRESHOLD: int = 2
"""Kaç ardışık başarısızlık sonrası sağlayıcı unhealthy sayılır."""

_DEFAULT_UNHEALTHY_WINDOW_SEC: float = 60.0
"""Unhealthy durumunun minimum süresi (saniye)."""

_DEFAULT_INTERVAL_SEC: float = 60.0
"""Ping döngüsü aralığı (saniye)."""

# Ping için kullanılan hafif istek şablonları. Gemini tarafında embed yerine
# chat kullanıyoruz; bazı chat modelleri embedContent desteklemez.
_GEMINI_PING_REQUEST = RouteRequest(
    kind="chat",
    messages=[{"role": "user", "content": "ping"}],
    max_tokens=1,
    temperature=0.0,
    timeout_sec=10.0,
)

_NVIDIA_PING_REQUEST = RouteRequest(
    kind="chat",
    messages=[{"role": "user", "content": "ping"}],
    max_tokens=1,
    temperature=0.0,
    timeout_sec=10.0,
)

# Sağlayıcı adlarının sıralı listesi — ping sırayla yapılır
_PROVIDER_ORDER: tuple[str, ...] = (
    "gemini_primary",
    "gemini_secondary",
    "nvidia",
)

_PROVIDER_PING_ROUTES: dict[str, Route] = {
    "gemini_primary": Route(provider="gemini_primary", model="models/gemini-3.1-flash-lite"),
    "gemini_secondary": Route(provider="gemini_secondary", model="models/gemini-3.1-flash-lite"),
    "nvidia": Route(provider="nvidia", model="nvidia/llama-3.3-nemotron-super-49b-v1.5"),
}


# ---------------------------------------------------------------------------
# Router protokolü (duck-typing; ModelRouter henüz oluşturulmamış olabilir)
# ---------------------------------------------------------------------------


@runtime_checkable
class _RouterProtocol(Protocol):
    """HealthProbe'un ihtiyaç duyduğu ModelRouter arayüzü.

    Gerçek ``ModelRouter`` bu protokolü otomatik olarak karşılar;
    testlerde sahte bir nesne kullanılabilir.
    """

    def route(
        self,
        tool_name: str,
        request: RouteRequest,
        *,
        prefer: object = None,
    ) -> RouteResult:
        """Verilen isteği ilgili sağlayıcıya yönlendirir."""
        ...

    def health(self) -> dict[str, HealthState]:
        """Mevcut sağlık durumunu döndürür."""
        ...


# ---------------------------------------------------------------------------
# Saf yardımcı: sağlık durumu geçiş mantığı
# ---------------------------------------------------------------------------


def compute_next_health_state(
    current: HealthState,
    *,
    success: bool,
    latency_ms: int | None,
    error: str | None,
    now: float,
    unhealthy_window_sec: float = _DEFAULT_UNHEALTHY_WINDOW_SEC,
) -> HealthState:
    """Bir ping sonucuna göre yeni ``HealthState`` hesaplar.

    Bu fonksiyon **saf** (pure) ve yan etkisizdir; aynı girdilerle her
    zaman aynı çıktıyı üretir. Property 3 bu fonksiyonu doğrudan test
    eder.

    Parameters
    ----------
    current:
        Önceki sağlık durumu.
    success:
        Ping başarılı mıydı?
    latency_ms:
        Ping süresi (ms); başarısızlıkta ``None`` olabilir.
    error:
        Başarısızlık mesajı; başarıda ``None``.
    now:
        Mevcut zaman (saniye, ``time_provider()`` çıktısı).
    unhealthy_window_sec:
        Unhealthy durumunun minimum süresi.

    Returns
    -------
    HealthState
        Güncellenmiş sağlık durumu.
    """
    if success:
        return HealthState(
            provider=current.provider,
            healthy=True,
            last_checked_at=now,
            last_latency_ms=latency_ms,
            failure_streak=0,
            last_error=None,
        )

    # Başarısız ping
    new_streak = current.failure_streak + 1
    # 2 veya daha fazla ardışık başarısızlık → unhealthy
    is_healthy = new_streak < _UNHEALTHY_STREAK_THRESHOLD

    # Eğer zaten unhealthy ise ve unhealthy_window_sec henüz dolmadıysa
    # healthy=False durumunu koru (streak ne olursa olsun).
    if not current.healthy:
        elapsed_since_last_check = now - current.last_checked_at
        if elapsed_since_last_check < unhealthy_window_sec:
            is_healthy = False

    return HealthState(
        provider=current.provider,
        healthy=is_healthy,
        last_checked_at=now,
        last_latency_ms=latency_ms,
        failure_streak=new_streak,
        last_error=error,
    )


# ---------------------------------------------------------------------------
# HealthProbe
# ---------------------------------------------------------------------------


class HealthProbe:
    """Arka planda çalışan periyodik sağlayıcı canlılık denetimi.

    Parameters
    ----------
    router:
        ``_RouterProtocol``'ü karşılayan bir nesne (genellikle
        ``ModelRouter``). Ping istekleri bu nesne üzerinden gönderilir.
    interval_sec:
        Ping döngüsü aralığı (saniye). Varsayılan 60.
    time_provider:
        Mevcut zamanı saniye olarak döndüren çağrılabilir. ``None``
        verilirse ``time.monotonic`` kullanılır. Hypothesis testleri
        sahte bir saat ile Property 3'ü ilerletir.
    unhealthy_window_sec:
        Unhealthy durumunun minimum süresi (saniye). Varsayılan 60.

    Examples
    --------
    >>> probe = HealthProbe(router, interval_sec=30)
    >>> probe.start()
    >>> states = probe.state()
    >>> probe.stop()
    """

    def __init__(
        self,
        router: _RouterProtocol,
        interval_sec: float = _DEFAULT_INTERVAL_SEC,
        *,
        time_provider: Callable[[], float] | None = None,
        unhealthy_window_sec: float = _DEFAULT_UNHEALTHY_WINDOW_SEC,
    ) -> None:
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be positive, got {interval_sec!r}")
        if unhealthy_window_sec <= 0:
            raise ValueError(
                f"unhealthy_window_sec must be positive, got {unhealthy_window_sec!r}"
            )

        self._router = router
        self._interval_sec = float(interval_sec)
        self._time_provider: Callable[[], float] = (
            time_provider if time_provider is not None else time.monotonic
        )
        self._unhealthy_window_sec = float(unhealthy_window_sec)

        # Başlangıç durumu: tüm sağlayıcılar healthy=True, streak=0
        now = self._time_provider()
        self._states: dict[str, HealthState] = {
            provider: HealthState(
                provider=provider,
                healthy=True,
                last_checked_at=now,
                last_latency_ms=None,
                failure_streak=0,
                last_error=None,
            )
            for provider in _PROVIDER_ORDER
        }
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # Kamuya açık API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Arka plan ping döngüsünü başlatır.

        İdempotent: zaten çalışıyorsa ikinci kez başlatmaz.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                log.debug("HealthProbe already running; ignoring start()")
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop,
                name="HealthProbe",
                daemon=True,
            )
            self._thread.start()
            log.debug(
                "HealthProbe started (interval=%.1fs, unhealthy_window=%.1fs)",
                self._interval_sec,
                self._unhealthy_window_sec,
            )

    def stop(self) -> None:
        """Arka plan ping döngüsünü durdurur ve thread'in bitmesini bekler.

        ``start()`` çağrılmamışsa veya zaten durmuşsa sessizce döner.
        """
        self._stop_event.set()
        with self._lock:
            thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._interval_sec + 2.0)
            if thread.is_alive():
                log.warning("HealthProbe thread did not stop within timeout")
        log.debug("HealthProbe stopped")

    def state(self) -> dict[str, HealthState]:
        """Mevcut sağlık durumunun anlık kopyasını döndürür.

        Döndürülen sözlük ve içindeki ``HealthState`` nesneleri
        değiştirilemez (derin kopya); çağıran taraf iç durumu bozamaz.

        Returns
        -------
        dict[str, HealthState]
            Sağlayıcı adından ``HealthState``'e eşleme.
        """
        with self._lock:
            return deepcopy(self._states)

    # ------------------------------------------------------------------
    # Dahili: ping döngüsü
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """Arka plan thread'inin ana döngüsü.

        Her ``interval_sec`` saniyede bir tüm sağlayıcıları sırayla
        ping'ler. ``_stop_event`` set edildiğinde döngüden çıkar.
        """
        log.debug("HealthProbe loop started")
        while not self._stop_event.is_set():
            self._ping_all_providers()
            # interval_sec boyunca bekle; stop_event set edilirse erken çık
            self._stop_event.wait(timeout=self._interval_sec)
        log.debug("HealthProbe loop exited")

    def _ping_all_providers(self) -> None:
        """Tüm sağlayıcıları sırayla ping'ler ve durumu günceller."""
        for provider in _PROVIDER_ORDER:
            if self._stop_event.is_set():
                break
            self._ping_provider(provider)

    def _ping_provider(self, provider: str) -> None:
        """Tek bir sağlayıcıya ping gönderir ve ``HealthState``'i günceller.

        Parameters
        ----------
        provider:
            Ping gönderilecek sağlayıcı adı
            (``"gemini_primary"``, ``"gemini_secondary"``, ``"nvidia"``).
        """
        request = _build_ping_request(provider)
        now = self._time_provider()
        t_start = self._time_provider()

        try:
            result: RouteResult = self._router.route(
                f"__health_probe_{provider}__",
                request,
                prefer=_PROVIDER_PING_ROUTES.get(provider),
            )
            t_end = self._time_provider()
            latency_ms = int((t_end - t_start) * 1000)

            if result.ok:
                success = True
                error = None
                log.debug(
                    "HealthProbe ping OK: provider=%s latency_ms=%d",
                    provider,
                    latency_ms,
                )
            else:
                success = False
                error = result.error_message or result.error_class or "unknown error"
                log.debug(
                    "HealthProbe ping FAILED: provider=%s error=%s",
                    provider,
                    error,
                )

        except Exception as exc:  # noqa: BLE001
            t_end = self._time_provider()
            latency_ms = int((t_end - t_start) * 1000)
            success = False
            error = f"{type(exc).__name__}: {exc}"
            log.debug(
                "HealthProbe ping EXCEPTION: provider=%s error=%s",
                provider,
                error,
            )

        with self._lock:
            current = self._states[provider]
            next_state = compute_next_health_state(
                current,
                success=success,
                latency_ms=latency_ms if success else None,
                error=error,
                now=now,
                unhealthy_window_sec=self._unhealthy_window_sec,
            )
            self._states[provider] = next_state

            if not next_state.healthy and current.healthy:
                log.warning(
                    "HealthProbe: provider '%s' marked UNHEALTHY "
                    "(streak=%d, last_error=%s)",
                    provider,
                    next_state.failure_streak,
                    next_state.last_error,
                )
            elif next_state.healthy and not current.healthy:
                log.info(
                    "HealthProbe: provider '%s' recovered (HEALTHY)",
                    provider,
                )

    # ------------------------------------------------------------------
    # Test yardımcısı: tek seferlik manuel ping
    # ------------------------------------------------------------------

    def probe_once(self) -> dict[str, HealthState]:
        """Tüm sağlayıcıları hemen bir kez ping'ler ve güncel durumu döndürür.

        Arka plan thread'i başlatılmadan kullanılabilir; açılış log'u ve
        testler için kullanışlıdır (Req 2.6).

        Returns
        -------
        dict[str, HealthState]
            Ping sonrası güncel sağlık durumunun kopyası.
        """
        self._ping_all_providers()
        return self.state()


# ---------------------------------------------------------------------------
# Yardımcı: sağlayıcıya göre ping isteği seç
# ---------------------------------------------------------------------------


def _build_ping_request(provider: str) -> RouteRequest:
    """Sağlayıcıya uygun hafif ping isteği döndürür.

    * Gemini sağlayıcıları → küçük chat isteği.
    * NVIDIA → küçük chat completion isteği.

    Parameters
    ----------
    provider:
        Sağlayıcı adı.

    Returns
    -------
    RouteRequest
        Ping için kullanılacak istek nesnesi.
    """
    if provider.startswith("gemini"):
        return _GEMINI_PING_REQUEST
    # nvidia veya bilinmeyen → chat ping
    return _NVIDIA_PING_REQUEST


__all__ = [
    "HealthProbe",
    "compute_next_health_state",
]
