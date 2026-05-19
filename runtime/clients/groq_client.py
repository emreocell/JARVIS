"""Groq OpenAI-compatible REST client for low-latency JARVIS routing."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_BASE_URL = "https://api.groq.com/openai"
_DEFAULT_TIMEOUT_SEC = 30.0
_RETRY_CONFIG = Retry(
    total=0,
    connect=1,
    read=0,
    backoff_factor=0.3,
    status_forcelist=(),
    raise_on_status=False,
)


class GroqError(Exception):
    """Base class for Groq client errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GroqAuthError(GroqError):
    """401 or 403 from Groq."""


class GroqRateLimitError(GroqError):
    """429 from Groq."""


class GroqServerError(GroqError):
    """5xx from Groq."""


class GroqTimeoutError(GroqError):
    """Network timeout."""


class GroqConnectionError(GroqError):
    """Network connection failure."""


class GroqClientError(GroqError):
    """Other 4xx client errors."""


class GroqClient:
    """Small OpenAI-compatible Groq client."""

    provider_id = "groq"

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
        self._session = requests.Session()
        adapter = HTTPAdapter(
            max_retries=_RETRY_CONFIG,
            pool_connections=4,
            pool_maxsize=10,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise GroqAuthError(
                "Groq API anahtarı eksik. config/api_keys.json içine "
                "'groq_api_key' değerini ekleyin.",
                status_code=401,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _post(self, endpoint: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        try:
            response = self._session.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise GroqTimeoutError(
                f"Groq isteği zaman aşımına uğradı ({timeout:.0f}s): {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise GroqConnectionError(
                f"Groq'a bağlanılamadı: {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise GroqError(f"Groq isteği başarısız: {exc}", status_code=None) from exc

        status = response.status_code
        body = response.text[:400]
        if status in (401, 403):
            raise GroqAuthError(f"Groq kimlik doğrulama hatası ({status}): {body}", status_code=status)
        if status == 429:
            raise GroqRateLimitError(f"Groq hız limiti aşıldı (429): {body}", status_code=status)
        if status >= 500:
            raise GroqServerError(f"Groq sunucu hatası ({status}): {body}", status_code=status)
        if status >= 400:
            raise GroqClientError(f"Groq istemci hatası ({status}): {body}", status_code=status)

        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise GroqError(f"Groq geçersiz JSON yanıtı döndürdü: {exc}", status_code=status) from exc

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise GroqError("Groq boş 'choices' listesi döndürdü.")
        message = (choices[0] or {}).get("message", {})
        text = str(message.get("content", "") or "").strip()
        if not text:
            raise GroqError("Groq modeli boş metin döndürdü.")
        return text

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
        if stream:
            raise NotImplementedError("Groq streaming henüz bu istemcide desteklenmiyor.")
        payload = {
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

    def list_models(self, *, timeout: float | None = None) -> list[str]:
        url = f"{self._base_url}/v1/models"
        try:
            response = self._session.get(
                url,
                headers=self._headers(),
                timeout=timeout if timeout is not None else self._default_timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise GroqTimeoutError("Groq model listesi zaman aşımına uğradı.", status_code=None) from exc
        except requests.exceptions.ConnectionError as exc:
            raise GroqConnectionError(f"Groq'a bağlanılamadı: {exc}", status_code=None) from exc

        if response.status_code in (401, 403):
            raise GroqAuthError("Groq model listesi için kimlik doğrulama başarısız.", status_code=response.status_code)
        if response.status_code == 429:
            raise GroqRateLimitError("Groq model listesi hız limitine takıldı.", status_code=429)
        if response.status_code >= 400:
            raise GroqClientError(
                f"Groq model listesi alınamadı ({response.status_code}).",
                status_code=response.status_code,
            )
        data = response.json()
        return [str(item.get("id", "")) for item in data.get("data", []) if item.get("id")]

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "GroqClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        masked = (self._api_key[:4] + "****") if self._api_key else "<empty>"
        return f"GroqClient(base_url={self._base_url!r}, api_key={masked!r})"


__all__ = [
    "GroqClient",
    "GroqError",
    "GroqAuthError",
    "GroqRateLimitError",
    "GroqServerError",
    "GroqTimeoutError",
    "GroqConnectionError",
    "GroqClientError",
]
