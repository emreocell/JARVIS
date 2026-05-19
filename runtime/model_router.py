"""Model_Router — sağlayıcı/model seçimi, fallback, retry, health gating ve log.

Bu modül Tool_Runtime ile gerçek HTTP istemcileri arasında konuşlanır.
Her ``route()`` çağrısı şu adımları izler:

1. ``select_route`` ile birincil rota ve fallback zinciri belirlenir.
2. ``RouteCache`` hit kontrolü yapılır; hit ise HTTP çağrısı yapılmaz.
3. Birincil rotaya çağrı yapılır; hata türüne göre fallback/retry uygulanır.
4. Sonuç debug log'una yazılır (Privacy_Mode aktifken gövde yazılmaz).
5. ``RouteResult`` döner.

Hata zinciri:
- 5xx / Timeout / ConnectionError → en fazla 2 retry, sonra fallback rotaya geç.
- 429 (Gemini) → yalnızca diğer Gemini rotasına bir kez sıçra; NVIDIA'ya düşme.
- 401/403 → ilgili sağlayıcıyı oturum boyunca devre dışı bırak.
- 408/504 NVIDIA → "NVIDIA servisi yanıt vermedi, lütfen tekrar deneyin".

Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8,
              2.1, 2.2, 2.3, 2.4, 2.6, 2.7,
              15.1, 15.2, 15.3, 15.4
"""

from __future__ import annotations

# Feature: jarvis-nvidia-skill-pack, Task 7.1 — runtime/model_router.py

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app_config import mask_secret
from runtime.clients.gemini_client import (
    GeminiAuthError,
    GeminiClient,
    GeminiRateLimitError,
    GeminiServerError,
)
from runtime.clients.nim_client import (
    NimAuthError,
    NimClient,
    NimConnectionError,
    NimServerError,
    NimTimeoutError,
)
from runtime.clients.groq_client import (
    GroqAuthError,
    GroqClient,
    GroqConnectionError,
    GroqRateLimitError,
    GroqServerError,
    GroqTimeoutError,
)
from runtime.clients.openrouter_client import (
    OpenRouterAuthError,
    OpenRouterClient,
    OpenRouterConnectionError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
    OpenRouterTimeoutError,
)
from runtime.privacy_mode import PrivacyMode
from runtime.route_cache import RouteCache, make_key
from runtime.route_selection import derive_tool_category, select_route
from runtime.types import (
    HealthState,
    Route,
    RouteProfile,
    RouteRequest,
    RouteResult,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_MAX_RETRIES: int = 3
"""5xx/Timeout/Connection hatalarında birincil rotaya toplam kaç deneme yapılır.
Design: 'en fazla 2 retry' = 1 ilk deneme + 2 retry = 3 toplam deneme."""

_GEMINI_RATE_LIMIT_MAX_JUMP: int = 1
"""429 durumunda diğer Gemini rotasına en fazla kaç kez sıçranır."""

# Türkçe kullanıcı mesajları
_MSG_NVIDIA_TIMEOUT = (
    "NVIDIA servisi yanıt vermedi, lütfen tekrar deneyin."
)
_MSG_GEMINI_BUSY = (
    "Gemini servisleri yoğun, lütfen birazdan tekrar deneyin."
)
_MSG_ALL_FAILED = (
    "İstek tüm sağlayıcılarda başarısız oldu. "
    "Lütfen internet bağlantınızı kontrol edip tekrar deneyin."
)
_MSG_PROVIDER_DISABLED = (
    "Sağlayıcı kimlik doğrulama hatası nedeniyle bu oturumda devre dışı bırakıldı."
)


_GEMINI_CHAT_MODEL = "models/gemini-3.1-flash-lite"
_GEMINI_TASK_MODELS: tuple[str, ...] = (
    "models/gemini-2.5-flash",
    "models/gemini-3.1-flash-lite",
    "models/gemini-2.5-flash-lite",
)
_GEMINI_POOL_PROVIDERS: tuple[str, ...] = (
    "gemini_primary",
    "gemini_secondary",
    "gemini_extra_1",
    "gemini_extra_2",
    "gemini_extra_3",
)

_PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "gemini_primary": _GEMINI_CHAT_MODEL,
    "gemini_secondary": _GEMINI_CHAT_MODEL,
    "gemini_extra_1": _GEMINI_CHAT_MODEL,
    "gemini_extra_2": _GEMINI_CHAT_MODEL,
    "gemini_extra_3": _GEMINI_CHAT_MODEL,
    "groq": "llama-3.1-8b-instant",
    "openrouter": "openai/gpt-oss-20b:free",
    "nvidia": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
}


# ---------------------------------------------------------------------------
# ModelRouterConfig
# ---------------------------------------------------------------------------


@dataclass
class ModelRouterConfig:
    """Model_Router'ın çalışma zamanı yapılandırması.

    ``app_config.py``'daki ``model_router`` bloğundan doldurulur.
    """

    default_routes: dict[str, Any] = field(default_factory=dict)
    fallback_chain: dict[str, list[str]] = field(default_factory=dict)
    health_check_interval_sec: float = 60.0
    disable_cache: bool = False
    gemini_chat_model: str = _GEMINI_CHAT_MODEL
    gemini_task_models: tuple[str, ...] = _GEMINI_TASK_MODELS
    gemini_pool_providers: tuple[str, ...] = _GEMINI_POOL_PROVIDERS

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ModelRouterConfig":
        """``model_router`` config bloğundan örnek oluştur."""
        task_models = d.get("gemini_task_models", _GEMINI_TASK_MODELS)
        if isinstance(task_models, str):
            task_models = [task_models]
        pool_providers = d.get("gemini_pool_providers", _GEMINI_POOL_PROVIDERS)
        if isinstance(pool_providers, str):
            pool_providers = [pool_providers]
        return cls(
            default_routes=d.get("default_routes", {}),
            fallback_chain=d.get("fallback_chain", {}),
            health_check_interval_sec=float(
                d.get("health_check_interval_sec", 60.0)
            ),
            disable_cache=bool(d.get("disable_cache", False)),
            gemini_chat_model=str(d.get("gemini_chat_model") or _GEMINI_CHAT_MODEL),
            gemini_task_models=tuple(str(m) for m in task_models if str(m).strip()),
            gemini_pool_providers=tuple(str(p) for p in pool_providers if str(p).strip()),
        )


# ---------------------------------------------------------------------------
# Yardımcı: hata sınıflandırması
# ---------------------------------------------------------------------------


def _is_auth_error(exc: Exception) -> bool:
    """401/403 kimlik doğrulama hatası mı?"""
    if isinstance(exc, (GeminiAuthError, NimAuthError, GroqAuthError, OpenRouterAuthError)):
        return True
    status = getattr(exc, "status_code", None)
    return status in (401, 403)


def _is_rate_limit_error(exc: Exception) -> bool:
    """429 rate-limit hatası mı?"""
    if isinstance(exc, (GeminiRateLimitError, GroqRateLimitError, OpenRouterRateLimitError)):
        return True
    status = getattr(exc, "status_code", None)
    return status == 429


def _is_server_error(exc: Exception) -> bool:
    """5xx / Timeout / Connection hatası mı?"""
    if isinstance(
        exc,
        (
            GeminiServerError,
            NimServerError,
            NimTimeoutError,
            NimConnectionError,
            GroqServerError,
            GroqTimeoutError,
            GroqConnectionError,
            OpenRouterServerError,
            OpenRouterTimeoutError,
            OpenRouterConnectionError,
        ),
    ):
        return True
    status = getattr(exc, "status_code", None)
    if status is not None and status >= 500:
        return True
    return False


def _is_nvidia_timeout(exc: Exception, provider: str) -> bool:
    """NVIDIA 408/504 zaman aşımı mı?"""
    if provider != "nvidia":
        return False
    if isinstance(exc, NimTimeoutError):
        return True
    status = getattr(exc, "status_code", None)
    return status in (408, 504)


def _error_class_name(exc: Exception) -> str:
    """İstisna sınıf adını döner."""
    return type(exc).__name__


def _status_code(exc: Exception) -> int | None:
    """İstisna üzerindeki HTTP durum kodunu döner."""
    return getattr(exc, "status_code", None)


# ---------------------------------------------------------------------------
# Yardımcı: debug log girdisi
# ---------------------------------------------------------------------------


def _write_debug_log(
    *,
    tool: str,
    provider: str,
    model: str,
    latency_ms: int,
    status_code: int | None,
    error_class: str | None,
    error_message: str | None,
    tokens_in: int | None,
    tokens_out: int | None,
    privacy_active: bool,
    request: RouteRequest | None = None,
    response_text: str | None = None,
) -> None:
    """Debug log'una tek bir satır yazar.

    Privacy_Mode aktifken request/response gövdelerini yazmaz;
    sadece sayım alanlarını yazar (Req 15.3, Property 7).
    """
    entry: dict[str, Any] = {
        "tool": tool,
        "provider": provider,
        "model": model,
        "latency_ms": latency_ms,
        "status_code": status_code,
        "error_class": error_class,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
    }

    if not privacy_active:
        # Gövde alanlarını yalnızca Privacy_Mode kapalıyken ekle.
        if request is not None:
            if request.messages:
                entry["request_messages_count"] = len(request.messages)
            if request.inputs:
                entry["request_inputs_count"] = len(request.inputs)
        if response_text is not None:
            entry["response_preview"] = response_text[:120]

    if error_class:
        log.debug(
            "[ModelRouter] FAIL tool=%s provider=%s model=%s "
            "latency_ms=%d status=%s error_class=%s error=%s",
            tool, provider, model, latency_ms,
            status_code, error_class,
            (error_message or "")[:200],
        )
    else:
        log.debug(
            "[ModelRouter] OK tool=%s provider=%s model=%s "
            "latency_ms=%d tokens_in=%s tokens_out=%s",
            tool, provider, model, latency_ms, tokens_in, tokens_out,
        )


# ---------------------------------------------------------------------------
# ModelRouter
# ---------------------------------------------------------------------------


class ModelRouter:
    """Sağlayıcı/model seçimi, fallback, retry, health gating ve log.

    Parameters
    ----------
    gemini_a:
        Birincil Gemini istemcisi (``gemini_primary``).
    gemini_b:
        İkincil Gemini istemcisi (``gemini_secondary``). ``None`` ise
        ``gemini_secondary`` çağrıları ``gemini_primary``'ye düşürülür.
    nvidia:
        NVIDIA NIM istemcisi. ``None`` ise NVIDIA rotaları başarısız döner.
    config:
        ``ModelRouterConfig`` örneği.
    privacy:
        ``PrivacyMode`` örneği; log gövde kararı için kullanılır.
    """

    def __init__(
        self,
        gemini_a: GeminiClient,
        gemini_b: GeminiClient | None,
        nvidia: NimClient | None,
        groq: GroqClient | None,
        openrouter: OpenRouterClient | None,
        config: ModelRouterConfig,
        privacy: PrivacyMode,
        gemini_pool: dict[str, GeminiClient] | None = None,
    ) -> None:
        self._gemini_a = gemini_a
        self._gemini_b = gemini_b
        self._gemini_clients: dict[str, GeminiClient] = {"gemini_primary": gemini_a}
        if gemini_b is not None:
            self._gemini_clients["gemini_secondary"] = gemini_b
        if gemini_pool:
            self._gemini_clients.update(gemini_pool)
        self._nvidia = nvidia
        self._groq = groq
        self._openrouter = openrouter
        self._config = config
        self._privacy = privacy

        # Oturum boyunca devre dışı bırakılan sağlayıcılar.
        self._blacklist: set[str] = set()

        # Sağlık durumu — HealthProbe tarafından dışarıdan güncellenebilir.
        # Başlangıçta tüm sağlayıcılar healthy=True.
        _now = time.monotonic()
        self._health: dict[str, HealthState] = {
            "gemini_primary": HealthState(
                provider="gemini_primary",
                healthy=True,
                last_checked_at=_now,
            ),
            "gemini_secondary": HealthState(
                provider="gemini_secondary",
                healthy=True,
                last_checked_at=_now,
            ),
            "nvidia": HealthState(
                provider="nvidia",
                healthy=True,
                last_checked_at=_now,
            ),
            "groq": HealthState(
                provider="groq",
                healthy=True,
                last_checked_at=_now,
            ),
            "openrouter": HealthState(
                provider="openrouter",
                healthy=True,
                last_checked_at=_now,
            ),
        }
        for provider in self._gemini_clients:
            self._health.setdefault(
                provider,
                HealthState(provider=provider, healthy=True, last_checked_at=_now),
            )

        # LRU + TTL cache.
        self._cache: RouteCache[RouteResult] = RouteCache(
            capacity=32,
            ttl_sec=30.0,
            disabled=config.disable_cache,
        )

    # ------------------------------------------------------------------
    # Kamuya açık API
    # ------------------------------------------------------------------

    def route(
        self,
        tool_name: str,
        request: RouteRequest,
        *,
        prefer: Route | RouteProfile | None = None,
    ) -> RouteResult:
        """Verilen isteği uygun sağlayıcıya yönlendir ve sonucu döndür.

        Parameters
        ----------
        tool_name:
            Çağrılan tool'un adı (loglama ve rota seçimi için).
        request:
            Sağlayıcıdan bağımsız çağrı paketi.
        prefer:
            Çağıranın açık rota tercihi (opsiyonel).

        Returns
        -------
        RouteResult
            Başarılı veya başarısız sonuç; ``ok`` alanı tek kaynak.
        """
        # Cache kontrolü.
        cache_key = make_key(tool_name, request)
        cached = self._cache.get(cache_key)
        if cached is not None:
            log.debug(
                "[ModelRouter] CACHE HIT tool=%s provider=%s model=%s",
                tool_name, cached.provider, cached.model,
            )
            return cached

        # Rota seçimi.
        primary, fallbacks = select_route(
            tool_name=tool_name,
            tool_route=None,  # Tool_Runtime tarafından doldurulabilir; şimdilik None.
            prefer=prefer,
            default_routes=self._config.default_routes,
            health=self._health,
            blacklist=self._blacklist,
        )
        fallbacks = self._expand_provider_fallbacks(primary, fallbacks)
        if request.kind == "chat" and not tool_name.startswith("__health_probe_"):
            fallbacks = self._expand_gemini_pool(tool_name, primary, fallbacks)

        result = self._execute_with_fallback(
            tool_name=tool_name,
            request=request,
            primary=primary,
            fallbacks=fallbacks,
        )

        # Başarılı sonucu cache'e yaz.
        if result.ok:
            self._cache.put(cache_key, result)

        return result

    def health(self) -> dict[str, HealthState]:
        """Mevcut sağlık durumunun anlık kopyasını döndür."""
        return dict(self._health)

    def disable_provider(self, name: str, *, reason: str) -> None:
        """Bir sağlayıcıyı oturum boyunca devre dışı bırak.

        Parameters
        ----------
        name:
            Devre dışı bırakılacak sağlayıcı adı
            (``"gemini_primary"``, ``"gemini_secondary"``, ``"nvidia"``).
        reason:
            Devre dışı bırakma sebebi (log için).
        """
        self._blacklist.add(name)
        log.warning(
            "[ModelRouter] Provider '%s' disabled for this session. Reason: %s",
            name, reason,
        )
        # Sağlık durumunu da güncelle.
        if name in self._health:
            current = self._health[name]
            self._health[name] = HealthState(
                provider=name,
                healthy=False,
                last_checked_at=time.monotonic(),
                last_latency_ms=current.last_latency_ms,
                failure_streak=current.failure_streak + 1,
                last_error=reason,
            )

    def update_health(self, provider: str, state: HealthState) -> None:
        """HealthProbe tarafından çağrılır; sağlık durumunu günceller."""
        self._health[provider] = state

    # ------------------------------------------------------------------
    # Dahili: fallback orkestrasyonu
    # ------------------------------------------------------------------

    def _execute_with_fallback(
        self,
        *,
        tool_name: str,
        request: RouteRequest,
        primary: Route,
        fallbacks: tuple[Route, ...],
    ) -> RouteResult:
        """Birincil rota + fallback zincirini yönetir.

        Hata zinciri kuralları:
        - 5xx/Timeout/Connection → max 2 retry, sonra fallback rotaya geç.
        - 429 (Gemini) → yalnızca diğer Gemini rotasına bir kez sıçra.
        - 401/403 → sağlayıcıyı devre dışı bırak, fallback'e geç.
        """
        attempted: list[str] = []
        all_routes = (primary,) + fallbacks

        # Gemini 429 durumunda NVIDIA'ya düşmemek için Gemini-only fallback.
        gemini_rate_limited: set[str] = set()

        last_error_result: RouteResult | None = None

        for route in all_routes:
            provider = route.provider
            model = route.model

            # Kara listede veya sağlıksızsa atla.
            if provider in self._blacklist:
                log.debug(
                    "[ModelRouter] Skipping blacklisted provider '%s'", provider
                )
                continue

            health_state = self._health.get(provider)
            if health_state is not None and not health_state.healthy:
                log.debug(
                    "[ModelRouter] Skipping unhealthy provider '%s'", provider
                )
                continue

            # 429 durumunda NVIDIA'ya düşme kuralı.
            if provider == "nvidia" and gemini_rate_limited:
                log.debug(
                    "[ModelRouter] Skipping nvidia fallback due to Gemini 429 rule"
                )
                continue

            # Birincil rotaya max 2 retry; fallback rotalar için 1 deneme.
            is_primary = (route == primary)
            max_attempts = _MAX_RETRIES if is_primary else 1

            for attempt in range(1, max_attempts + 1):
                route_key = f"{provider}:{model}"
                if route_key not in attempted:
                    attempted.append(route_key)

                t_start = time.monotonic()
                try:
                    result = self._call_provider(
                        tool_name=tool_name,
                        request=request,
                        route=route,
                    )
                    latency_ms = int((time.monotonic() - t_start) * 1000)

                    _write_debug_log(
                        tool=tool_name,
                        provider=provider,
                        model=model,
                        latency_ms=latency_ms,
                        status_code=200,
                        error_class=None,
                        error_message=None,
                        tokens_in=result.tokens_in,
                        tokens_out=result.tokens_out,
                        privacy_active=self._privacy.is_active(),
                        request=request,
                        response_text=result.text,
                    )

                    result = RouteResult(
                        ok=True,
                        provider=provider,
                        model=model,
                        text=result.text,
                        embeddings=result.embeddings,
                        latency_ms=latency_ms,
                        tokens_in=result.tokens_in,
                        tokens_out=result.tokens_out,
                        fallback_chain=tuple(attempted),
                    )
                    return result

                except Exception as exc:  # noqa: BLE001
                    latency_ms = int((time.monotonic() - t_start) * 1000)
                    err_class = _error_class_name(exc)
                    err_msg = str(exc)
                    status = _status_code(exc)

                    _write_debug_log(
                        tool=tool_name,
                        provider=provider,
                        model=model,
                        latency_ms=latency_ms,
                        status_code=status,
                        error_class=err_class,
                        error_message=err_msg,
                        tokens_in=None,
                        tokens_out=None,
                        privacy_active=self._privacy.is_active(),
                        request=request,
                    )

                    # 401/403 → sağlayıcıyı devre dışı bırak.
                    if _is_auth_error(exc):
                        self.disable_provider(
                            provider,
                            reason=f"{err_class}: {err_msg[:200]}",
                        )
                        last_error_result = self._make_error_result(
                            provider=provider,
                            model=model,
                            attempted=attempted,
                            error_class=err_class,
                            error_message=err_msg,
                            user_message_tr=_MSG_PROVIDER_DISABLED,
                        )
                        break  # Bu rotadan çık, fallback'e geç.

                    # 429 (Gemini) → yalnızca diğer Gemini rotasına sıçra.
                    if _is_rate_limit_error(exc) and provider.startswith("gemini"):
                        gemini_rate_limited.add(provider)
                        last_error_result = self._make_error_result(
                            provider=provider,
                            model=model,
                            attempted=attempted,
                            error_class=err_class,
                            error_message=err_msg,
                            user_message_tr=_MSG_GEMINI_BUSY,
                        )
                        break  # Bu rotadan çık, fallback'e geç.

                    # NVIDIA 408/504 özel mesajı.
                    if _is_nvidia_timeout(exc, provider):
                        last_error_result = self._make_error_result(
                            provider=provider,
                            model=model,
                            attempted=attempted,
                            error_class=err_class,
                            error_message=err_msg,
                            user_message_tr=_MSG_NVIDIA_TIMEOUT,
                        )
                        # Retry devam edebilir; son denemede fallback'e geç.
                        if attempt >= max_attempts:
                            break
                        continue

                    # 5xx / Timeout / Connection → retry veya fallback.
                    if _is_server_error(exc):
                        last_error_result = self._make_error_result(
                            provider=provider,
                            model=model,
                            attempted=attempted,
                            error_class=err_class,
                            error_message=err_msg,
                            user_message_tr=_MSG_ALL_FAILED,
                        )
                        if attempt < max_attempts:
                            log.debug(
                                "[ModelRouter] Retry %d/%d for %s:%s",
                                attempt, max_attempts, provider, model,
                            )
                            continue
                        break  # Max retry doldu, fallback'e geç.

                    # Diğer hatalar (4xx vb.) → fallback'e geç.
                    last_error_result = self._make_error_result(
                        provider=provider,
                        model=model,
                        attempted=attempted,
                        error_class=err_class,
                        error_message=err_msg,
                        user_message_tr=_MSG_ALL_FAILED,
                    )
                    break

        # Tüm rotalar tükendi.
        if last_error_result is not None:
            # Tüm Gemini'ler 429 döndüyse özel mesaj.
            if gemini_rate_limited and not any(
                r.provider == "nvidia" for r in all_routes
                if r.provider not in self._blacklist
            ):
                return RouteResult(
                    ok=False,
                    provider=last_error_result.provider,
                    model=last_error_result.model,
                    error_class=last_error_result.error_class,
                    error_message=last_error_result.error_message,
                    user_message_tr=_MSG_GEMINI_BUSY,
                    fallback_chain=tuple(attempted),
                )
            return RouteResult(
                ok=False,
                provider=last_error_result.provider,
                model=last_error_result.model,
                error_class=last_error_result.error_class,
                error_message=last_error_result.error_message,
                user_message_tr=last_error_result.user_message_tr or _MSG_ALL_FAILED,
                fallback_chain=tuple(attempted),
            )

        # Hiç rota denenemedi (hepsi blacklist/unhealthy).
        return RouteResult(
            ok=False,
            provider="",
            model="",
            error_class="NoAvailableProvider",
            error_message="Tüm sağlayıcılar devre dışı veya sağlıksız.",
            user_message_tr=_MSG_ALL_FAILED,
            fallback_chain=tuple(attempted),
        )

    # ------------------------------------------------------------------
    # Dahili: tek bir sağlayıcıya çağrı
    # ------------------------------------------------------------------

    def _call_provider(
        self,
        *,
        tool_name: str,
        request: RouteRequest,
        route: Route,
    ) -> RouteResult:
        """Tek bir sağlayıcıya HTTP çağrısı yapar; ham sonucu döner.

        Başarısızlıkta ilgili istisna fırlatılır; çağıran taraf yakalar.
        """
        provider = route.provider
        model = route.model

        if provider.startswith("gemini_"):
            client = self._gemini_clients.get(provider)
            if client is None:
                if provider == "gemini_secondary":
                    log.warning(
                        "[ModelRouter] gemini_secondary not configured; "
                        "falling back to gemini_primary"
                    )
                    client = self._gemini_a
                else:
                    from runtime.clients.gemini_client import GeminiAuthError as _GeminiAuthError
                    raise _GeminiAuthError(
                        f"Gemini provider yapilandirilmamis: {provider}",
                        status_code=401,
                    )
            return self._call_gemini(client, model, request)

        if provider == "nvidia":
            if self._nvidia is None:
                from runtime.clients.nim_client import NimAuthError as _NimAuthError
                raise _NimAuthError(
                    "NVIDIA istemcisi yapılandırılmamış.",
                    status_code=401,
                )
            return self._call_nvidia(self._nvidia, model, request)

        if provider == "groq":
            if self._groq is None:
                from runtime.clients.groq_client import GroqAuthError as _GroqAuthError
                raise _GroqAuthError(
                    "Groq istemcisi yapılandırılmamış.",
                    status_code=401,
                )
            return self._call_groq(self._groq, model, request)

        if provider == "openrouter":
            if self._openrouter is None:
                from runtime.clients.openrouter_client import OpenRouterAuthError as _OpenRouterAuthError
                raise _OpenRouterAuthError(
                    "OpenRouter istemcisi yapilandirilmamis.",
                    status_code=401,
                )
            return self._call_openrouter(self._openrouter, model, request)

        raise ValueError(f"Bilinmeyen sağlayıcı: {provider!r}")

    def _call_openrouter(
        self,
        client: OpenRouterClient,
        model: str,
        request: RouteRequest,
    ) -> RouteResult:
        """OpenRouter istemcisine chat istegi yapar."""
        if request.kind != "chat":
            raise ValueError("OpenRouter provider simdilik yalnizca chat isteklerini destekler.")
        text = client.chat(
            model=model,
            messages=request.messages or [],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            timeout=request.timeout_sec,
        )
        return RouteResult(
            ok=True,
            provider=client.provider_id,
            model=model,
            text=text,
        )

    def _call_groq(
        self,
        client: GroqClient,
        model: str,
        request: RouteRequest,
    ) -> RouteResult:
        """Groq istemcisine chat isteği yapar."""
        if request.kind != "chat":
            raise ValueError("Groq provider şimdilik yalnızca chat isteklerini destekler.")
        text = client.chat(
            model=model,
            messages=request.messages or [],
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            timeout=request.timeout_sec,
        )
        return RouteResult(
            ok=True,
            provider=client.provider_id,
            model=model,
            text=text,
        )

    def _call_gemini(
        self,
        client: GeminiClient,
        model: str,
        request: RouteRequest,
    ) -> RouteResult:
        """Gemini istemcisine çağrı yapar."""
        kind = request.kind

        if kind == "chat":
            messages = request.messages or []
            text = client.chat(
                model=model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout_sec=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider=client.provider_id,
                model=model,
                text=text,
            )

        if kind == "embed":
            inputs = request.inputs or []
            embeddings = client.embed(
                model=model,
                inputs=inputs,
                timeout_sec=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider=client.provider_id,
                model=model,
                embeddings=embeddings,
            )

        if kind == "vision":
            # Gemini vision: image_b64 + messages[0].content olarak gönder.
            messages = request.messages or []
            if request.image_b64:
                # Görsel içerikli mesaj oluştur.
                vision_messages = list(messages) + [
                    {
                        "role": "user",
                        "content": f"[image_b64:{request.image_b64[:32]}...]",
                    }
                ]
            else:
                vision_messages = messages
            text = client.chat(
                model=model,
                messages=vision_messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout_sec=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider=client.provider_id,
                model=model,
                text=text,
            )

        raise ValueError(f"Desteklenmeyen kind: {kind!r}")

    def _call_nvidia(
        self,
        client: NimClient,
        model: str,
        request: RouteRequest,
    ) -> RouteResult:
        """NVIDIA NIM istemcisine çağrı yapar."""
        kind = request.kind

        if kind == "chat":
            messages = request.messages or []
            text = client.chat(
                model=model,
                messages=messages,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider="nvidia",
                model=model,
                text=text,
            )

        if kind == "embed":
            inputs = request.inputs or []
            embeddings = client.embed(
                model=model,
                inputs=inputs,
                timeout=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider="nvidia",
                model=model,
                embeddings=embeddings,
            )

        if kind == "vision":
            messages = request.messages or []
            prompt = ""
            if messages:
                prompt = str(messages[-1].get("content", ""))
            image_b64 = request.image_b64 or ""
            text = client.vision(
                model=model,
                prompt=prompt,
                image_b64=image_b64,
                max_tokens=request.max_tokens,
                temperature=request.temperature,
                timeout=request.timeout_sec,
            )
            return RouteResult(
                ok=True,
                provider="nvidia",
                model=model,
                text=text,
            )

        raise ValueError(f"Desteklenmeyen kind: {kind!r}")

    # ------------------------------------------------------------------
    # Yardımcı: hata RouteResult oluştur
    # ------------------------------------------------------------------

    def _expand_provider_fallbacks(
        self,
        primary: Route,
        explicit_fallbacks: tuple[Route, ...],
    ) -> tuple[Route, ...]:
        """Config'deki provider fallback zincirini somut Route listesine cevir."""
        chain: list[Route] = list(explicit_fallbacks)
        seen: set[str] = {primary.provider}
        seen.update(route.provider for route in explicit_fallbacks)

        providers = (primary.provider,) + tuple(route.provider for route in explicit_fallbacks)
        for provider in providers:
            for fb_provider in self._config.fallback_chain.get(provider, []):
                if fb_provider in seen:
                    continue
                model = self._default_model_for_provider(fb_provider)
                if not model:
                    continue
                chain.append(Route(provider=fb_provider, model=model))  # type: ignore[arg-type]
                seen.add(fb_provider)
        return tuple(chain)

    def _expand_gemini_pool(
        self,
        tool_name: str,
        primary: Route,
        fallbacks: tuple[Route, ...],
    ) -> tuple[Route, ...]:
        """Gemini chat/task calls across configured key and model pools.

        Chat keeps one fixed model and rotates accounts. Task-like Gemini
        calls try each account on 2.5 Flash first, then move to cheaper/lighter
        fallback models. Non-Gemini fallbacks remain after the Gemini pool.
        """
        if not primary.provider.startswith("gemini_"):
            return fallbacks

        category = derive_tool_category(tool_name)
        chat_like = category == "voice_core.intent" and primary.model == self._config.gemini_chat_model
        models = (
            (self._config.gemini_chat_model,)
            if chat_like
            else self._config.gemini_task_models
        )
        if not models:
            return fallbacks

        providers = [
            provider for provider in self._config.gemini_pool_providers
            if provider in self._gemini_clients or provider in {"gemini_primary", "gemini_secondary"}
        ]
        if primary.provider in providers:
            providers.remove(primary.provider)
        providers.insert(0, primary.provider)

        expanded: list[Route] = []
        seen: set[tuple[str, str]] = {(primary.provider, primary.model)}

        for model in models:
            for provider in providers:
                key = (provider, model)
                if key in seen:
                    continue
                seen.add(key)
                expanded.append(Route(provider=provider, model=model))  # type: ignore[arg-type]

        for route in fallbacks:
            key = (route.provider, route.model)
            if key in seen:
                continue
            seen.add(key)
            expanded.append(route)
        return tuple(expanded)

    def _default_model_for_provider(self, provider: str) -> str:
        """Provider icin config'den veya sabitten kullanilabilir varsayilan model bul."""
        if provider.startswith("gemini_"):
            return self._config.gemini_chat_model
        for route_entry in self._config.default_routes.values():
            if isinstance(route_entry, RouteProfile):
                if route_entry.primary.provider == provider:
                    return route_entry.primary.model
                continue
            if not isinstance(route_entry, dict):
                continue
            if route_entry.get("provider") == provider and route_entry.get("model"):
                return str(route_entry["model"])
        return _PROVIDER_DEFAULT_MODELS.get(provider, "")

    @staticmethod
    def _make_error_result(
        *,
        provider: str,
        model: str,
        attempted: list[str],
        error_class: str,
        error_message: str,
        user_message_tr: str,
    ) -> RouteResult:
        return RouteResult(
            ok=False,
            provider=provider,
            model=model,
            error_class=error_class,
            error_message=error_message,
            user_message_tr=user_message_tr,
            fallback_chain=tuple(attempted),
        )


__all__ = [
    "ModelRouter",
    "ModelRouterConfig",
]
