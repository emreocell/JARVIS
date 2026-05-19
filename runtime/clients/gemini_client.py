"""Gemini istemci fabrikası — runtime/clients/gemini_client.py

Bu modül iki ayrı `genai.Client` örneği üretir:
  - GeminiClient_A (primary): Voice_Core intent ve kısa sohbet çağrıları
  - GeminiClient_B (secondary): Uzun bağlam, vision ve ağır Gemini görevleri

İstemciler arasında oturum durumu paylaşılmaz (Req 2.5).

Hata sınıflandırması:
  - 401/403 → GeminiAuthError   (oturum boyunca sağlayıcı devre dışı)
  - 429     → GeminiRateLimitError (ikinci Gemini'ye sıçra)
  - 5xx     → GeminiServerError  (fallback zinciri + retry)
  - Diğer   → GeminiClientError  (genel sarmalayıcı)

Tasarım: jarvis-nvidia-skill-pack design.md § "Gemini istemcileri"
Requirements: 1.7, 2.5, 2.7
"""
from __future__ import annotations

import logging
from typing import Any

from google import genai  # type: ignore[reportMissingImports]
from google.genai import errors as _genai_errors  # type: ignore[reportMissingImports]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Özel exception hiyerarşisi
# ---------------------------------------------------------------------------


class GeminiClientError(Exception):
    """Tüm Gemini istemci hatalarının temel sınıfı."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GeminiAuthError(GeminiClientError):
    """HTTP 401 veya 403 — geçersiz/yetkisiz API anahtarı.

    Model_Router bu hatayı alınca ilgili sağlayıcıyı oturum boyunca
    devre dışı bırakır (Req 2.7).
    """


class GeminiRateLimitError(GeminiClientError):
    """HTTP 429 — kota veya hız limiti aşıldı.

    Model_Router bu hatayı alınca yalnızca diğer Gemini rotasına sıçrar;
    NVIDIA'ya düşmez (Req 2.3, 2.4).
    """


class GeminiServerError(GeminiClientError):
    """HTTP 5xx veya geçici servis hatası.

    Model_Router bu hatayı alınca fallback zincirinde en fazla 2 retry
    yapar (Req 1.4).
    """


# ---------------------------------------------------------------------------
# Hata sınıflandırma yardımcısı
# ---------------------------------------------------------------------------

# Mesaj içeriğine göre hata sınıflandırması için anahtar kelimeler.
_AUTH_MARKERS = ("401", "403", "unauthorized", "forbidden", "api key", "api_key", "invalid key")
_RATE_LIMIT_MARKERS = ("429", "quota", "rate limit", "resource exhausted", "too many requests",
                       "quota exceeded", "limit exceeded", "rateLimitExceeded")
_SERVER_ERROR_MARKERS = ("500", "502", "503", "504", "internal error", "service unavailable",
                         "backend error", "overloaded", "unavailable", "deadline exceeded",
                         "timed out", "timeout", "connection reset", "busy")


def _classify_genai_error(exc: Exception) -> GeminiClientError:
    """Bir `google.genai` hatasını uygun `GeminiClientError` alt sınıfına çevir.

    Önce `errors.ClientError` / `errors.ServerError` tip hiyerarşisine bakar;
    eşleşmezse mesaj içeriğini tarar.
    """
    # google-genai kütüphanesi status_code'u farklı yerlerde saklayabilir.
    status_code: int | None = None
    for attr in ("status_code", "code", "http_status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            status_code = val
            break

    message = str(exc or "").lower()

    # Tip tabanlı sınıflandırma (en güvenilir yol)
    if isinstance(exc, _genai_errors.ClientError):
        # ClientError genellikle 4xx'i kapsar
        if status_code in (401, 403) or any(m in message for m in _AUTH_MARKERS):
            return GeminiAuthError(str(exc), status_code=status_code)
        if status_code == 429 or any(m in message for m in _RATE_LIMIT_MARKERS):
            return GeminiRateLimitError(str(exc), status_code=status_code)
        return GeminiClientError(str(exc), status_code=status_code)

    if isinstance(exc, _genai_errors.ServerError):
        return GeminiServerError(str(exc), status_code=status_code)

    # Mesaj tabanlı sınıflandırma (fallback)
    if status_code in (401, 403) or any(m in message for m in _AUTH_MARKERS):
        return GeminiAuthError(str(exc), status_code=status_code)
    if status_code == 429 or any(m in message for m in _RATE_LIMIT_MARKERS):
        return GeminiRateLimitError(str(exc), status_code=status_code)
    if status_code is not None and status_code >= 500 or any(m in message for m in _SERVER_ERROR_MARKERS):
        return GeminiServerError(str(exc), status_code=status_code)

    return GeminiClientError(str(exc), status_code=status_code)


# ---------------------------------------------------------------------------
# GeminiClient — ortak arabirim
# ---------------------------------------------------------------------------


class GeminiClient:
    """Tek bir `genai.Client` örneğini saran ince sarmalayıcı.

    `chat()` metodu Model_Router'ın beklediği ortak arabirimi sağlar.
    İstemciler arasında oturum durumu paylaşılmaz; her `GeminiClient`
    kendi `genai.Client` örneğine sahiptir (Req 2.5).

    Attributes:
        provider_id: "gemini_primary" veya "gemini_secondary" — loglama için.
        _client: Altta yatan `genai.Client` örneği.
    """

    def __init__(self, api_key: str, provider_id: str = "gemini_primary") -> None:
        """Yeni bir GeminiClient oluştur.

        Args:
            api_key: Gemini API anahtarı.
            provider_id: Loglama ve hata mesajları için sağlayıcı kimliği.

        Raises:
            ValueError: api_key boş veya None ise.
        """
        if not api_key or not api_key.strip():
            raise ValueError(f"GeminiClient ({provider_id}): api_key boş olamaz.")
        self.provider_id = provider_id
        self._client = genai.Client(api_key=api_key.strip())

    # ------------------------------------------------------------------
    # Ortak arabirim
    # ------------------------------------------------------------------

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        timeout_sec: float = 60.0,
        system_instruction: str | None = None,
    ) -> str:
        """Gemini modeline sohbet isteği gönder ve metin yanıtı döndür.

        Args:
            model: Kullanılacak model adı (örn. "models/gemini-2.5-flash").
            messages: OpenAI-uyumlu mesaj listesi; her öğe
                      ``{"role": "user"|"model", "content": "..."}`` biçiminde.
            max_tokens: Maksimum çıktı token sayısı.
            temperature: Örnekleme sıcaklığı (0.0–2.0).
            timeout_sec: İstek zaman aşımı (saniye).
            system_instruction: Opsiyonel sistem talimatı.

        Returns:
            Modelin ürettiği metin yanıtı.

        Raises:
            GeminiAuthError: 401/403 yanıtı alındığında.
            GeminiRateLimitError: 429 yanıtı alındığında.
            GeminiServerError: 5xx veya geçici servis hatası.
            GeminiClientError: Diğer tüm Gemini hataları.
        """
        from google.genai import types as _types  # type: ignore[reportMissingImports]

        # Mesajları Gemini Content formatına çevir
        contents = _build_contents(messages)

        config_kwargs: dict[str, Any] = {
            "temperature": temperature,
            "max_output_tokens": max_tokens,
        }
        if system_instruction:
            config_kwargs["system_instruction"] = system_instruction

        generate_config = _types.GenerateContentConfig(**config_kwargs)

        try:
            response = self._client.models.generate_content(
                model=model,
                contents=contents,
                config=generate_config,
            )
        except Exception as exc:
            classified = _classify_genai_error(exc)
            logger.debug(
                "[%s] chat error: %s (status=%s)",
                self.provider_id,
                type(classified).__name__,
                classified.status_code,
            )
            raise classified from exc

        return _extract_text(response)

    def embed(
        self,
        model: str,
        inputs: list[str],
        *,
        timeout_sec: float = 60.0,
    ) -> list[list[float]]:
        """Gemini embedding modeli ile metin gömme üret.

        Args:
            model: Embedding modeli adı.
            inputs: Gömülecek metin listesi.
            timeout_sec: İstek zaman aşımı (saniye).

        Returns:
            Her girdi için float listesi içeren liste.

        Raises:
            GeminiAuthError: 401/403 yanıtı alındığında.
            GeminiRateLimitError: 429 yanıtı alındığında.
            GeminiServerError: 5xx veya geçici servis hatası.
            GeminiClientError: Diğer tüm Gemini hataları.
        """
        try:
            result = self._client.models.embed_content(
                model=model,
                contents=inputs,
            )
        except Exception as exc:
            classified = _classify_genai_error(exc)
            logger.debug(
                "[%s] embed error: %s (status=%s)",
                self.provider_id,
                type(classified).__name__,
                classified.status_code,
            )
            raise classified from exc

        # Yanıt yapısı: result.embeddings listesi, her biri .values içerir
        embeddings: list[list[float]] = []
        for emb in (result.embeddings or []):
            values = getattr(emb, "values", None) or []
            embeddings.append(list(values))
        return embeddings

    def raw_client(self) -> genai.Client:
        """Altta yatan `genai.Client` örneğini döndür.

        Yalnızca Voice_Core'un `aio.live.connect` gibi özel API'lere
        doğrudan erişmesi gerektiğinde kullanılır.
        """
        return self._client


# ---------------------------------------------------------------------------
# Fabrika fonksiyonu
# ---------------------------------------------------------------------------


def build_clients(
    primary_key: str,
    secondary_key: str | None,
) -> tuple[GeminiClient, GeminiClient | None]:
    """İki ayrı GeminiClient örneği üret.

    Args:
        primary_key: Birincil Gemini API anahtarı (Voice_Core intent).
        secondary_key: İkincil Gemini API anahtarı (ağır görevler).
                       Boş veya None ise ikinci istemci oluşturulmaz.

    Returns:
        ``(client_a, client_b)`` çifti. ``client_b`` ikincil anahtar
        yoksa ``None``'dır.

    Raises:
        ValueError: primary_key boş veya None ise.

    Notes:
        - İki istemci arasında oturum durumu paylaşılmaz (Req 2.5).
        - secondary_key yoksa Model_Router ikincil çağrıları birincil
          anahtara düşürür ve uyarı log'u üretir (Req 2.2).
    """
    client_a = GeminiClient(primary_key, provider_id="gemini_primary")

    client_b: GeminiClient | None = None
    secondary_stripped = (secondary_key or "").strip()
    if secondary_stripped:
        try:
            client_b = GeminiClient(secondary_stripped, provider_id="gemini_secondary")
        except ValueError as exc:
            logger.warning(
                "build_clients: ikincil Gemini istemcisi oluşturulamadı: %s. "
                "gemini_secondary çağrıları gemini_primary'ye düşürülecek.",
                exc,
            )
            client_b = None
    else:
        logger.warning(
            "build_clients: gemini_secondary_api_key boş. "
            "gemini_secondary çağrıları gemini_primary'ye düşürülecek."
        )

    return client_a, client_b


# ---------------------------------------------------------------------------
# Yardımcı dönüşüm fonksiyonları
# ---------------------------------------------------------------------------


def _build_contents(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI-uyumlu mesaj listesini Gemini `contents` formatına çevir.

    Gemini API'si "user" ve "model" rollerini kabul eder. "assistant" rolü
    "model"'e eşlenir; "system" rolü ise içerik olarak "user" mesajına
    dönüştürülür (Gemini Live API'de system_instruction ayrı bir alan).

    Args:
        messages: ``[{"role": "user"|"assistant"|"model"|"system", "content": "..."}]``

    Returns:
        Gemini API'sinin beklediği ``[{"role": "user"|"model", "parts": [...]}]``
    """
    contents: list[dict[str, Any]] = []
    for msg in messages:
        role = str(msg.get("role", "user")).lower()
        content = msg.get("content", "") or ""

        # Rol normalizasyonu
        if role == "assistant":
            role = "model"
        elif role == "system":
            # System mesajları user rolüyle eklenir; system_instruction
            # ayrıca GenerateContentConfig'e geçirilmeli.
            role = "user"
        elif role not in ("user", "model"):
            role = "user"

        contents.append({
            "role": role,
            "parts": [{"text": str(content)}],
        })
    return contents


def _extract_text(response: Any) -> str:
    """Gemini `GenerateContentResponse`'dan metin içeriğini çıkar.

    Birden fazla `Part` varsa hepsini birleştirir.
    """
    chunks: list[str] = []

    # response.text kısayolu (tek part için)
    try:
        text = response.text
        if text:
            return str(text).strip()
    except (AttributeError, ValueError):
        pass

    # Çok-part yanıt için candidates → content → parts yolunu izle
    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        if content is None:
            continue
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = str(getattr(part, "text", "") or "").strip()
            if part_text:
                chunks.append(part_text)

    return "\n".join(chunks).strip()


__all__ = [
    "GeminiClient",
    "GeminiClientError",
    "GeminiAuthError",
    "GeminiRateLimitError",
    "GeminiServerError",
    "build_clients",
]
