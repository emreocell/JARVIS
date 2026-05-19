from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from runtime.clients.google_vision_client import (
    GoogleVisionAuthError,
    GoogleVisionClient,
    GoogleVisionClientError,
    GoogleVisionRateLimitError,
    GoogleVisionServerError,
)


def _response(status: int, payload: dict | None = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload or {}
    return response


def test_empty_key_raises_auth_error() -> None:
    client = GoogleVisionClient("")
    with pytest.raises(GoogleVisionAuthError):
        client.annotate_image(b"fake")


def test_annotate_image_posts_base64_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GoogleVisionClient("key")
    captured: dict = {}

    def fake_post(url, params, json, timeout):  # noqa: ANN001
        captured.update({"url": url, "params": params, "json": json, "timeout": timeout})
        return _response(200, {"responses": [{"labelAnnotations": [{"description": "Text"}]}]})

    monkeypatch.setattr("runtime.clients.google_vision_client.requests.post", fake_post)
    data = client.annotate_image(b"abc", features=["TEXT_DETECTION"], timeout=5)
    assert data["responses"]
    assert captured["params"] == {"key": "key"}
    assert captured["json"]["requests"][0]["image"]["content"] == "YWJj"
    assert captured["timeout"] == 5


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (401, GoogleVisionAuthError),
        (403, GoogleVisionAuthError),
        (429, GoogleVisionRateLimitError),
        (500, GoogleVisionServerError),
        (400, GoogleVisionClientError),
    ],
)
def test_http_errors_are_classified(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    exc_type: type[Exception],
) -> None:
    client = GoogleVisionClient("key")
    monkeypatch.setattr(
        "runtime.clients.google_vision_client.requests.post",
        lambda *args, **kwargs: _response(status, text="error"),
    )
    with pytest.raises(exc_type):
        client.annotate_image(b"abc")


def test_embedded_response_error_is_classified(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GoogleVisionClient("key")
    monkeypatch.setattr(
        "runtime.clients.google_vision_client.requests.post",
        lambda *args, **kwargs: _response(200, {"responses": [{"error": {"code": 403, "message": "API disabled"}}]}),
    )
    with pytest.raises(GoogleVisionAuthError, match="API disabled"):
        client.annotate_image(b"abc")


def test_summarize_includes_ocr_and_labels() -> None:
    summary = GoogleVisionClient.summarize(
        {
            "responses": [
                {
                    "fullTextAnnotation": {"text": "Hello screen"},
                    "labelAnnotations": [{"description": "Screenshot"}],
                }
            ]
        }
    )
    assert "Hello screen" in summary
    assert "Screenshot" in summary
