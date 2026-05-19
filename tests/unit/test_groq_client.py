from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import requests

from runtime.clients.groq_client import (
    GroqAuthError,
    GroqClient,
    GroqClientError,
    GroqError,
    GroqRateLimitError,
    GroqServerError,
)


def _response(status: int, payload: dict | None = None, text: str = "") -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload or {}
    return response


def test_empty_key_raises_auth_error() -> None:
    client = GroqClient("")
    with pytest.raises(GroqAuthError):
        client.chat("llama-3.1-8b-instant", [{"role": "user", "content": "hi"}])


def test_chat_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GroqClient("key")
    posted: dict = {}

    def fake_post(url, headers, json, timeout):  # noqa: ANN001
        posted.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _response(
            200,
            {"choices": [{"message": {"content": "pong"}}]},
        )

    monkeypatch.setattr(client._session, "post", fake_post)  # noqa: SLF001
    result = client.chat("llama-3.1-8b-instant", [{"role": "user", "content": "ping"}], timeout=3)
    assert result == "pong"
    assert posted["url"].endswith("/v1/chat/completions")
    assert posted["headers"]["Authorization"] == "Bearer key"
    assert posted["json"]["stream"] is False
    assert posted["timeout"] == 3


@pytest.mark.parametrize(
    ("status", "exc_type"),
    [
        (401, GroqAuthError),
        (403, GroqAuthError),
        (429, GroqRateLimitError),
        (500, GroqServerError),
        (400, GroqClientError),
    ],
)
def test_chat_classifies_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    exc_type: type[Exception],
) -> None:
    client = GroqClient("key")
    monkeypatch.setattr(
        client._session,  # noqa: SLF001
        "post",
        lambda *args, **kwargs: _response(status, text="error"),
    )
    with pytest.raises(exc_type):
        client.chat("llama-3.1-8b-instant", [{"role": "user", "content": "hi"}])


def test_chat_wraps_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GroqClient("key")

    def fake_post(*args, **kwargs):  # noqa: ANN001
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(client._session, "post", fake_post)  # noqa: SLF001
    with pytest.raises(GroqError):
        client.chat("llama-3.1-8b-instant", [{"role": "user", "content": "hi"}])


def test_list_models_returns_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GroqClient("key")
    monkeypatch.setattr(
        client._session,  # noqa: SLF001
        "get",
        lambda *args, **kwargs: _response(200, {"data": [{"id": "a"}, {"id": "b"}]}),
    )
    assert client.list_models() == ["a", "b"]
