"""Google Vision API smoke client."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import requests

_BASE_URL = "https://vision.googleapis.com/v1/images:annotate"
_DEFAULT_TIMEOUT_SEC = 30.0


class GoogleVisionError(Exception):
    """Base Google Vision client error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GoogleVisionAuthError(GoogleVisionError):
    """401/403 or API/billing permission denial."""


class GoogleVisionRateLimitError(GoogleVisionError):
    """429 quota/rate-limit error."""


class GoogleVisionClientError(GoogleVisionError):
    """Other 4xx error."""


class GoogleVisionServerError(GoogleVisionError):
    """5xx error."""


class GoogleVisionClient:
    """Tiny REST wrapper for ``images:annotate``."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _BASE_URL,
        default_timeout: float = _DEFAULT_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key.strip() if api_key else ""
        self._base_url = base_url
        self._default_timeout = default_timeout

    def _redact(self, text: str) -> str:
        if self._api_key:
            return text.replace(self._api_key, "[REDACTED]")
        return text

    def _request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self._api_key:
            raise GoogleVisionAuthError(
                "Google Vision API anahtarı eksik. config/api_keys.json içine "
                "'google_vision_api_key' değerini ekleyin.",
                status_code=401,
            )
        try:
            response = requests.post(
                self._base_url,
                params={"key": self._api_key},
                json=payload,
                timeout=timeout,
            )
        except requests.exceptions.Timeout as exc:
            raise GoogleVisionError("Google Vision isteği zaman aşımına uğradı.") from exc
        except requests.exceptions.RequestException as exc:
            raise GoogleVisionError(
                f"Google Vision isteği başarısız: {self._redact(str(exc))}"
            ) from exc

        status = response.status_code
        body = self._redact(response.text[:500])
        if status in (401, 403):
            raise GoogleVisionAuthError(
                f"Google Vision kimlik/izin hatası ({status}): {body}",
                status_code=status,
            )
        if status == 429:
            raise GoogleVisionRateLimitError(
                f"Google Vision kota veya hız limiti hatası (429): {body}",
                status_code=429,
            )
        if status >= 500:
            raise GoogleVisionServerError(
                f"Google Vision sunucu hatası ({status}): {body}",
                status_code=status,
            )
        if status >= 400:
            raise GoogleVisionClientError(
                f"Google Vision istemci hatası ({status}): {body}",
                status_code=status,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise GoogleVisionError(f"Google Vision geçersiz JSON döndürdü: {exc}", status_code=status) from exc

        responses = data.get("responses") or []
        if responses and isinstance(responses[0], dict) and responses[0].get("error"):
            err = responses[0]["error"]
            code = int(err.get("code", status) or status)
            message = str(err.get("message", "Bilinmeyen Vision hatası"))
            if code in (401, 403):
                raise GoogleVisionAuthError(message, status_code=code)
            if code == 429:
                raise GoogleVisionRateLimitError(message, status_code=code)
            raise GoogleVisionClientError(message, status_code=code)
        return data

    def annotate_image(
        self,
        image_bytes: bytes,
        *,
        features: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        image_b64 = base64.b64encode(image_bytes).decode("ascii")
        feature_list = features or ["TEXT_DETECTION", "LABEL_DETECTION"]
        payload = {
            "requests": [
                {
                    "image": {"content": image_b64},
                    "features": [{"type": name, "maxResults": 5} for name in feature_list],
                }
            ]
        }
        return self._request(
            payload,
            timeout=timeout if timeout is not None else self._default_timeout,
        )

    def annotate_file(
        self,
        image_path: str | Path,
        *,
        features: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.annotate_image(
            Path(image_path).read_bytes(),
            features=features,
            timeout=timeout,
        )

    @staticmethod
    def summarize(data: dict[str, Any]) -> str:
        responses = data.get("responses") or []
        first = responses[0] if responses else {}
        text = (first.get("fullTextAnnotation") or {}).get("text", "")
        labels = first.get("labelAnnotations") or []
        label_names = [str(item.get("description", "")) for item in labels if item.get("description")]
        parts: list[str] = []
        if text:
            parts.append(f"OCR: {text.strip()[:160]}")
        if label_names:
            parts.append("Etiketler: " + ", ".join(label_names[:5]))
        return " | ".join(parts) if parts else "Vision API yanıt verdi ama metin/etiket bulunamadı."


__all__ = [
    "GoogleVisionClient",
    "GoogleVisionError",
    "GoogleVisionAuthError",
    "GoogleVisionRateLimitError",
    "GoogleVisionClientError",
    "GoogleVisionServerError",
]
