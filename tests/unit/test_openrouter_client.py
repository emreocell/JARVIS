from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from runtime.clients.openrouter_client import (
    OpenRouterAuthError,
    OpenRouterClient,
    OpenRouterClientError,
    OpenRouterError,
    OpenRouterRateLimitError,
    OpenRouterServerError,
)


def _response(status: int, payload: dict | None = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload or {}
    return response


def test_empty_key_raises_auth_error() -> None:
    client = OpenRouterClient("")
    with pytest.raises(OpenRouterAuthError):
        client.chat("openai/gpt-oss-20b:free", [{"role": "user", "content": "hi"}])


def test_chat_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenRouterClient("key")
    posted: dict = {}

    def fake_post(url, headers, json, timeout):  # noqa: ANN001
        posted.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _response(
            200,
            {"choices": [{"message": {"content": "pong"}}]},
        )

    monkeypatch.setattr(client._session, "post", fake_post)  # noqa: SLF001
    result = client.chat("openai/gpt-oss-20b:free", [{"role": "user", "content": "ping"}], timeout=3)
    assert result == "pong"
    assert posted["url"].endswith("/v1/chat/completions")
    assert posted["headers"]["Authorization"] == "Bearer key"
    assert posted["headers"]["HTTP-Referer"]
    assert posted["headers"]["X-Title"]
    assert posted["json"]["stream"] is False
    assert posted["timeout"] == 3


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (401, OpenRouterAuthError),
        (403, OpenRouterAuthError),
        (429, OpenRouterRateLimitError),
        (500, OpenRouterServerError),
        (400, OpenRouterClientError),
    ],
)
def test_chat_classifies_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    exc_type: type[Exception],
) -> None:
    client = OpenRouterClient("key")
    monkeypatch.setattr(
        client._session,  # noqa: SLF001
        "post",
        lambda *args, **kwargs: _response(status, text="error"),
    )
    with pytest.raises(exc_type):
        client.chat("openai/gpt-oss-20b:free", [{"role": "user", "content": "hi"}])


def test_chat_wraps_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenRouterClient("key")

    def fake_post(*args, **kwargs):  # noqa: ANN001
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(client._session, "post", fake_post)  # noqa: SLF001
    with pytest.raises(OpenRouterError):
        client.chat("openai/gpt-oss-20b:free", [{"role": "user", "content": "hi"}])


def test_list_models_returns_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    client = OpenRouterClient("key")
    monkeypatch.setattr(
        client._session,  # noqa: SLF001
        "get",
        lambda *args, **kwargs: _response(200, {"data": [{"id": "a"}, {"id": "b"}]}),
    )
    assert client.list_models() == ["a", "b"]
