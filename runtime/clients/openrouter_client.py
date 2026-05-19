"""OpenRouter OpenAI-compatible REST client for JARVIS model routing."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_BASE_URL = "https://openrouter.ai/api"
_DEFAULT_TIMEOUT_SEC = 45.0
_DEFAULT_REFERER = "https://localhost/jarvis"
_DEFAULT_TITLE = "JARVIS Windows"
_RETRY_CONFIG = Retry(
    total=0,
    connect=1,
    read=0,
    backoff_factor=0.3,
    status_forcelist=(),
    raise_on_status=False,
)


class OpenRouterError(Exception):
    """Base class for OpenRouter client errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class OpenRouterAuthError(OpenRouterError):
    """401 or 403 from OpenRouter."""


class OpenRouterRateLimitError(OpenRouterError):
    """429 from OpenRouter."""


class OpenRouterServerError(OpenRouterError):
    """5xx from OpenRouter."""


class OpenRouterTimeoutError(OpenRouterError):
    """Network timeout."""


class OpenRouterConnectionError(OpenRouterError):
    """Network connection failure."""


class OpenRouterClientError(OpenRouterError):
    """Other 4xx client errors."""


class OpenRouterClient:
    """Small OpenAI-compatible OpenRouter client."""

    provider_id = "openrouter"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        default_timeout: float = _DEFAULT_TIMEOUT_SEC,
        referer: str = _DEFAULT_REFERER,
        app_title: str = _DEFAULT_TITLE,
    ) -> None:
        self._api_key = api_key.strip() if api_key else ""
        self._base_url = base_url.rstrip("/")
        self._default_timeout = default_timeout
        self._referer = referer
        self._app_title = app_title
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
            raise OpenRouterAuthError(
                "OpenRouter API anahtari eksik. config/api_keys.json icine "
                "'openrouter_api_key' degerini ekleyin.",
                status_code=401,
            )
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "HTTP-Referer": self._referer,
            "X-Title": self._app_title,
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
            raise OpenRouterTimeoutError(
                f"OpenRouter istegi zaman asimina ugradi ({timeout:.0f}s): {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise OpenRouterConnectionError(
                f"OpenRouter'a baglanilamadi: {exc}",
                status_code=None,
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise OpenRouterError(f"OpenRouter istegi basarisiz: {exc}", status_code=None) from exc

        status = response.status_code
        body = response.text[:400]
        if status in (401, 403):
            raise OpenRouterAuthError(
                f"OpenRouter kimlik dogrulama hatasi ({status}): {body}",
                status_code=status,
            )
        if status == 429:
            raise OpenRouterRateLimitError(
                f"OpenRouter hiz limiti asildi (429): {body}",
                status_code=status,
            )
        if status >= 500:
            raise OpenRouterServerError(
                f"OpenRouter sunucu hatasi ({status}): {body}",
                status_code=status,
            )
        if status >= 400:
            raise OpenRouterClientError(
                f"OpenRouter istemci hatasi ({status}): {body}",
                status_code=status,
            )

        try:
            return response.json()  # type: ignore[no-any-return]
        except ValueError as exc:
            raise OpenRouterError(
                f"OpenRouter gecersiz JSON yaniti dondurdu: {exc}",
                status_code=status,
            ) from exc

    @staticmethod
    def _extract_text(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise OpenRouterError("OpenRouter bos 'choices' listesi dondurdu.")
        message = (choices[0] or {}).get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            text = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        else:
            text = str(content or "")
        text = text.strip()
        if not text:
            raise OpenRouterError("OpenRouter modeli bos metin dondurdu.")
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
            raise NotImplementedError("OpenRouter streaming henuz bu istemcide desteklenmiyor.")
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
            raise OpenRouterTimeoutError("OpenRouter model listesi zaman asimina ugradi.", status_code=None) from exc
        except requests.exceptions.ConnectionError as exc:
            raise OpenRouterConnectionError(f"OpenRouter'a baglanilamadi: {exc}", status_code=None) from exc

        if response.status_code in (401, 403):
            raise OpenRouterAuthError(
                "OpenRouter model listesi icin kimlik dogrulama basarisiz.",
                status_code=response.status_code,
            )
        if response.status_code == 429:
            raise OpenRouterRateLimitError("OpenRouter model listesi hiz limitine takildi.", status_code=429)
        if response.status_code >= 400:
            raise OpenRouterClientError(
                f"OpenRouter model listesi alinamadi ({response.status_code}).",
                status_code=response.status_code,
            )
        data = response.json()
        return [str(item.get("id", "")) for item in data.get("data", []) if item.get("id")]

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "OpenRouterClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        masked = (self._api_key[:4] + "****") if self._api_key else "<empty>"
        return f"OpenRouterClient(base_url={self._base_url!r}, api_key={masked!r})"


__all__ = [
    "OpenRouterClient",
    "OpenRouterError",
    "OpenRouterAuthError",
    "OpenRouterRateLimitError",
    "OpenRouterServerError",
    "OpenRouterTimeoutError",
    "OpenRouterConnectionError",
    "OpenRouterClientError",
]
