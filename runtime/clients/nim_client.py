"""NVIDIA NIM REST sarmalayıcı.

Bu modül, NVIDIA NIM API'sine yapılan HTTP çağrılarını kapsüller.
``requests.Session`` ile bağlantı havuzu paylaşılır; varsayılan timeout
60 saniyedir.

Skill kodu bu sınıfı doğrudan çağırmaz; ``Model_Router.route()``
üzerinden geçer. Sınıf yalnızca Model_Router tarafından kullanılmak
üzere tasarlanmıştır.

Desteklenen uç noktalar:

- :meth:`NimClient.chat` — ``POST /v1/chat/completions``
- :meth:`NimClient.embed` — ``POST /v1/embeddings``
- :meth:`NimClient.vision` — chat uç noktası üzerinden ``image_url``
  veya ``data:`` URL ile görsel anlama

Hata sınıfları:

- :exc:`NimAuthError` — 401/403 yanıtı; sağlayıcı oturum boyunca
  devre dışı bırakılmalıdır.
- :exc:`NimRateLimitError` — 429 yanıtı; kısa bekleme sonrası yeniden
  denenebilir.
- :exc:`NimServerError` — 5xx yanıtı; fallback rotaya geçilmelidir.
- :exc:`NimTimeoutError` — bağlantı veya okuma zaman aşımı.
- :exc:`NimConnectionError` — ağ düzeyinde bağlantı hatası.
- :exc:`NimClientError` — 4xx (401/403/429 dışı) istemci hatası.
- :exc:`NimError` — tüm NIM hatalarının taban sınıfı.

Requirements: 1.1, 1.7
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_BASE_URL = "https://integrate.api.nvidia.com/v1"
_CHAT_ENDPOINT = f"{_BASE_URL}/chat/completions"
_EMBED_ENDPOINT = f"{_BASE_URL}/embeddings"

_DEFAULT_TIMEOUT_SEC: float = 60.0

# Session düzeyinde yeniden deneme: yalnızca bağlantı hataları için;
# HTTP hata kodları Model_Router tarafından yönetilir.
_RETRY_CONFIG = Retry(
    total=0,          # HTTP hata kodlarında otomatik retry yok
    connect=1,        # bağlantı kurulamadığında bir kez daha dene
    read=0,
    backoff_factor=0.5,
    status_forcelist=(),  # boş: HTTP durum kodlarına göre retry yok
    raise_on_status=False,
)


# ---------------------------------------------------------------------------
# Özel istisna hiyerarşisi
# ---------------------------------------------------------------------------


class NimError(Exception):
    """Tüm NIM istemci hatalarının taban sınıfı."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NimAuthError(NimError):
    """401 veya 403 — geçersiz veya yetkisiz API anahtarı."""


class NimRateLimitError(NimError):
    """429 — hız limiti aşıldı."""


class NimServerError(NimError):
    """5xx — sunucu taraflı hata; fallback rotaya geçilmeli."""


class NimTimeoutError(NimError):
    """Bağlantı veya okuma zaman aşımı."""


class NimConnectionError(NimError):
    """Ağ düzeyinde bağlantı hatası."""


class NimClientError(NimError):
    """4xx (401/403/429 dışı) istemci hatası."""


# ---------------------------------------------------------------------------
# NimClient
# ---------------------------------------------------------------------------


class NimClient:
    """NVIDIA NIM REST API istemcisi.

    Parameters
    ----------
    api_key:
        NVIDIA NIM API anahtarı. Boş bırakılırsa tüm çağrılar
        :exc:`NimAuthError` fırlatır.
    base_url:
        Uç nokta kök URL'i. Varsayılan ``https://integrate.api.nvidia.com/v1``.
        Test ortamında mock sunucuya yönlendirmek için geçersiz kılınabilir.
    default_timeout:
        Saniye cinsinden varsayılan istek zaman aşımı. Her çağrıda
        ``timeout`` parametresiyle geçersiz kılınabilir.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        default_timeout: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key.strip() if api_key else ""
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout

        # Bağlantı havuzu: her NimClient örneği kendi Session'ını taşır.
        self._session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=_RETRY_CONFIG,
            pool_connections=4,
            pool_maxsize=10,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ------------------------------------------------------------------
    # Dahili yardımcılar
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise NimAuthError(
                "NVIDIA API anahtarı eksik. Lütfen config/api_keys.json "
                "dosyasına 'nvidia_api_key' değerini ekleyin.",
                status_code=401,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        """``endpoint``'e POST isteği gönderir; hataları sınıflandırır."""
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        try:
            response = self._session.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise NimTimeoutError(
                f"NVIDIA NIM isteği zaman aşımına uğradı ({timeout:.0f}s): {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise NimConnectionError(
                f"NVIDIA NIM'e bağlanılamadı: {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise NimError(
                f"NVIDIA NIM isteği başarısız: {exc}",
                status_code=None,
            ) from exc

        status = response.status_code
        if status == 401 or status == 403:
            raise NimAuthError(
                f"NVIDIA NIM kimlik doğrulama hatası ({status}): "
                f"{response.text[:200]}",
                status_code=status,
            )
        if status == 429:
            raise NimRateLimitError(
                f"NVIDIA NIM hız limiti aşıldı (429): {response.text[:200]}",
                status_code=status,
            )
        if status >= 500:
            raise NimServerError(
                f"NVIDIA NIM sunucu hatası ({status}): {response.text[:400]}",
                status_code=status,
            )
        if status >= 400:
            raise NimClientError(
                f"NVIDIA NIM istemci hatası ({status}): {response.text[:400]}",
                status_code=status,
            )

        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise NimError(
                f"NVIDIA NIM geçersiz JSON yanıtı döndürdü: {exc}",
                status_code=status,
            ) from exc

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        """``/v1/chat/completions`` yanıtından metin içeriğini çıkarır."""
        choices = data.get("choices") or []
        if not choices:
            raise NimError("NVIDIA NIM boş 'choices' listesi döndürdü.")

        message = (choices[0] or {}).get("message", {})
        content = message.get("content", "")

        if isinstance(content, list):
            # Multimodal yanıt: metin parçalarını birleştir.
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text", "") or "").strip()
                    if text:
                        parts.append(text)
            return "\n".join(parts).strip()

        text = str(content or "").strip()
        if not text:
            raise NimError("NVIDIA NIM modeli boş metin döndürdü.")
        return text

    # ------------------------------------------------------------------
    # Genel API
    # ------------------------------------------------------------------

    def chat(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        timeout: float | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        stream: bool = False,
    ) -> str:
        """``POST /v1/chat/completions`` — metin tamamlama.

        Parameters
        ----------
        model:
            NVIDIA NIM model adı. Örnek: ``"meta/llama-3.1-70b-instruct"``.
        messages:
            OpenAI uyumlu mesaj listesi.
            Örnek: ``[{"role": "user", "content": "Merhaba"}]``.
        timeout:
            Saniye cinsinden istek zaman aşımı. ``None`` ise
            ``default_timeout`` kullanılır.
        max_tokens:
            Üretilecek maksimum token sayısı.
        temperature:
            Örnekleme sıcaklığı (0.0–1.0).
        stream:
            ``True`` ise streaming modu etkinleştirilir. Şu an
            desteklenmemektedir; ``False`` olarak bırakın.

        Returns
        -------
        str
            Modelin ürettiği metin.

        Raises
        ------
        NimAuthError
            API anahtarı geçersiz veya eksik.
        NimRateLimitError
            Hız limiti aşıldı.
        NimServerError
            Sunucu taraflı hata (5xx).
        NimTimeoutError
            İstek zaman aşımına uğradı.
        NimConnectionError
            Ağ bağlantısı kurulamadı.
        """
        if stream:
            raise NotImplementedError(
                "Streaming modu henüz desteklenmiyor. stream=False kullanın."
            )

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": False,
        }
        data = self._post(
            "v1/chat/completions",
            payload,
            timeout=timeout if timeout is not None else self._default_timeout,
        )
        return self._extract_text(data)

    def embed(
        self,
        model: str,
        inputs: list[str],
        *,
        timeout: float | None = None,
        input_type: str = "query",
        encoding_format: str = "float",
        truncate: str = "NONE",
    ) -> list[list[float]]:
        """``POST /v1/embeddings`` — metin gömme vektörleri.

        Parameters
        ----------
        model:
            NVIDIA NIM embedding model adı.
            Örnek: ``"nvidia/nv-embedqa-e5-v5"``.
        inputs:
            Gömme üretilecek metin listesi. Boş liste geçilirse boş liste
            döner.
        timeout:
            Saniye cinsinden istek zaman aşımı.
        input_type:
            NIM embedding modeli için girdi türü. Genellikle ``"query"``
            veya ``"passage"``.
        encoding_format:
            Vektör kodlama formatı. ``"float"`` veya ``"base64"``.
        truncate:
            Uzun metinleri kırpma stratejisi. ``"NONE"``, ``"START"``
            veya ``"END"``.

        Returns
        -------
        list[list[float]]
            Her girdi için bir gömme vektörü; ``inputs`` ile aynı sırada.

        Raises
        ------
        NimAuthError, NimRateLimitError, NimServerError,
        NimTimeoutError, NimConnectionError
            İlgili HTTP/ağ hataları.
        NimError
            Yanıt ayrıştırma hatası.
        """
        if not inputs:
            return []

        payload: dict[str, Any] = {
            "model": model,
            "input": inputs,
            "input_type": input_type,
            "encoding_format": encoding_format,
            "truncate": truncate,
        }
        data = self._post(
            "v1/embeddings",
            payload,
            timeout=timeout if timeout is not None else self._default_timeout,
        )

        embedding_data = data.get("data") or []
        if not embedding_data:
            raise NimError(
                "NVIDIA NIM embedding yanıtında 'data' alanı boş veya eksik."
            )

        # Yanıt sırası girdi sırasıyla eşleşmeyebilir; 'index' alanına göre
        # sırala.
        try:
            sorted_data = sorted(embedding_data, key=lambda x: x.get("index", 0))
        except (TypeError, AttributeError):
            sorted_data = embedding_data

        embeddings: list[list[float]] = []
        for item in sorted_data:
            vec = item.get("embedding")
            if not isinstance(vec, list):
                raise NimError(
                    f"NVIDIA NIM embedding öğesi beklenen formatta değil: {item!r}"
                )
            embeddings.append([float(v) for v in vec])

        return embeddings

    def vision(
        self,
        model: str,
        prompt: str,
        image_b64: str,
        *,
        timeout: float | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        media_type: str = "image/jpeg",
        use_data_url: bool = True,
    ) -> str:
        """Görsel anlama — chat uç noktası üzerinden ``image_url`` ile.

        Görsel, ``data:<media_type>;base64,<image_b64>`` formatında
        ``image_url`` içeriği olarak gönderilir.

        Parameters
        ----------
        model:
            NVIDIA NIM vision model adı.
            Örnek: ``"meta/llama-3.2-90b-vision-instruct"``.
        prompt:
            Görsel hakkında kullanıcı sorusu veya yönergesi.
        image_b64:
            Base64 kodlanmış görsel verisi (ham base64; ``data:`` öneki
            olmadan).
        timeout:
            Saniye cinsinden istek zaman aşımı.
        max_tokens:
            Üretilecek maksimum token sayısı.
        temperature:
            Örnekleme sıcaklığı.
        media_type:
            Görselin MIME türü. Örnek: ``"image/jpeg"``, ``"image/png"``.
        use_data_url:
            ``True`` ise ``data:`` URL formatı kullanılır (varsayılan).
            ``False`` ise ``image_b64`` doğrudan ``url`` alanına yazılır
            (bazı NIM modelleri ham URL bekler).

        Returns
        -------
        str
            Modelin ürettiği metin yanıtı.

        Raises
        ------
        NimAuthError, NimRateLimitError, NimServerError,
        NimTimeoutError, NimConnectionError, NimError
            İlgili HTTP/ağ/ayrıştırma hataları.
        """
        if use_data_url:
            image_url = f"data:{media_type};base64,{image_b64}"
        else:
            image_url = image_b64

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": image_url},
                    },
                ],
            }
        ]
        return self.chat(
            model,
            messages,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def close(self) -> None:
        """Bağlantı havuzunu kapat ve kaynakları serbest bırak."""
        self._session.close()

    def __enter__(self) -> "NimClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        masked = (self._api_key[:4] + "****") if self._api_key else "<empty>"
        return (
            f"NimClient(base_url={self._base_url!r}, "
            f"api_key={masked!r}, "
            f"default_timeout={self._default_timeout}s)"
        )


__all__ = [
    "NimClient",
    "NimError",
    "NimAuthError",
    "NimRateLimitError",
    "NimServerError",
    "NimTimeoutError",
    "NimConnectionError",
    "NimClientError",
]
