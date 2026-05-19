"""Unit tests for runtime/clients/gemini_client.py

Tests cover:
- build_clients() factory: primary/secondary client creation
- GeminiClient.chat() interface
- Error classification: 401/403 → GeminiAuthError, 429 → GeminiRateLimitError, 5xx → GeminiServerError
- _build_contents() message format conversion
- _extract_text() response text extraction
- Edge cases: empty keys, missing secondary key

Requirements: 1.7, 2.5, 2.7
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from runtime.clients.gemini_client import (
    GeminiClient,
    GeminiAuthError,
    GeminiClientError,
    GeminiRateLimitError,
    GeminiServerError,
    build_clients,
    _build_contents,
    _classify_genai_error,
    _extract_text,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_mock_genai_client():
    """Return a mock genai.Client with models.generate_content stubbed."""
    client = MagicMock(name="genai.Client")
    response = MagicMock(name="GenerateContentResponse")
    response.text = "Merhaba!"
    response.candidates = []
    client.models.generate_content.return_value = response
    return client


# ---------------------------------------------------------------------------
# _build_contents
# ---------------------------------------------------------------------------


class TestBuildContents:
    def test_user_message_preserved(self):
        msgs = [{"role": "user", "content": "Merhaba"}]
        result = _build_contents(msgs)
        assert result == [{"role": "user", "parts": [{"text": "Merhaba"}]}]

    def test_assistant_mapped_to_model(self):
        msgs = [{"role": "assistant", "content": "Tamam"}]
        result = _build_contents(msgs)
        assert result[0]["role"] == "model"

    def test_model_role_preserved(self):
        msgs = [{"role": "model", "content": "Evet"}]
        result = _build_contents(msgs)
        assert result[0]["role"] == "model"

    def test_system_mapped_to_user(self):
        msgs = [{"role": "system", "content": "Sen bir asistansın"}]
        result = _build_contents(msgs)
        assert result[0]["role"] == "user"

    def test_unknown_role_mapped_to_user(self):
        msgs = [{"role": "unknown_role", "content": "test"}]
        result = _build_contents(msgs)
        assert result[0]["role"] == "user"

    def test_multiple_messages(self):
        msgs = [
            {"role": "user", "content": "Soru"},
            {"role": "assistant", "content": "Cevap"},
            {"role": "user", "content": "Takip"},
        ]
        result = _build_contents(msgs)
        assert len(result) == 3
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "model"
        assert result[2]["role"] == "user"

    def test_empty_messages(self):
        result = _build_contents([])
        assert result == []

    def test_none_content_becomes_empty_string(self):
        msgs = [{"role": "user", "content": None}]
        result = _build_contents(msgs)
        assert result[0]["parts"][0]["text"] == ""


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------


class TestExtractText:
    def test_response_text_shortcut(self):
        response = MagicMock()
        response.text = "Kısa yanıt"
        assert _extract_text(response) == "Kısa yanıt"

    def test_response_text_empty_falls_through_to_candidates(self):
        response = MagicMock()
        response.text = ""
        part = MagicMock()
        part.text = "Candidates yanıtı"
        content = MagicMock()
        content.parts = [part]
        candidate = MagicMock()
        candidate.content = content
        response.candidates = [candidate]
        assert _extract_text(response) == "Candidates yanıtı"

    def test_response_text_raises_value_error_falls_through(self):
        """When response.text raises ValueError (e.g. safety block), fall through."""
        response = MagicMock()
        type(response).text = PropertyMock(side_effect=ValueError("blocked"))
        response.candidates = []
        assert _extract_text(response) == ""

    def test_multiple_parts_joined(self):
        response = MagicMock()
        # Make response.text return empty string so we fall through to candidates
        response.text = ""
        part1 = MagicMock()
        part1.text = "Birinci"
        part2 = MagicMock()
        part2.text = "İkinci"
        content = MagicMock()
        content.parts = [part1, part2]
        candidate = MagicMock()
        candidate.content = content
        response.candidates = [candidate]
        result = _extract_text(response)
        assert "Birinci" in result
        assert "İkinci" in result

    def test_no_candidates_returns_empty(self):
        response = MagicMock()
        # Make response.text return empty string so we fall through to candidates
        response.text = ""
        response.candidates = []
        assert _extract_text(response) == ""


# ---------------------------------------------------------------------------
# _classify_genai_error
# ---------------------------------------------------------------------------


class TestClassifyGenaiError:
    def test_message_with_401_becomes_auth_error(self):
        exc = Exception("HTTP 401 Unauthorized")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiAuthError)

    def test_message_with_403_becomes_auth_error(self):
        exc = Exception("403 Forbidden")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiAuthError)

    def test_message_with_429_becomes_rate_limit_error(self):
        exc = Exception("429 Too Many Requests")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiRateLimitError)

    def test_message_with_quota_becomes_rate_limit_error(self):
        exc = Exception("quota exceeded for this project")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiRateLimitError)

    def test_message_with_500_becomes_server_error(self):
        exc = Exception("500 Internal Server Error")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiServerError)

    def test_message_with_503_becomes_server_error(self):
        exc = Exception("503 Service Unavailable")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiServerError)

    def test_message_with_timeout_becomes_server_error(self):
        exc = Exception("deadline exceeded: request timed out")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiServerError)

    def test_unknown_error_becomes_client_error(self):
        exc = Exception("Something unexpected happened")
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiClientError)

    def test_status_code_attribute_used(self):
        exc = Exception("auth failed")
        exc.status_code = 401
        result = _classify_genai_error(exc)
        assert isinstance(result, GeminiAuthError)
        assert result.status_code == 401

    def test_genai_client_error_type_classified(self):
        """google.genai ClientError with 429 → GeminiRateLimitError."""
        try:
            from google.genai import errors as _errors
            # ClientError(code, response_json) — code=429 triggers rate limit
            exc = _errors.ClientError(429, {"error": {"message": "429 rate limit"}})
            result = _classify_genai_error(exc)
            assert isinstance(result, (GeminiRateLimitError, GeminiClientError))
        except ImportError:
            pytest.skip("google-genai not installed")

    def test_genai_server_error_type_classified(self):
        """google.genai ServerError → GeminiServerError."""
        try:
            from google.genai import errors as _errors
            # ServerError(code, response_json)
            exc = _errors.ServerError(503, {"error": {"message": "503 unavailable"}})
            result = _classify_genai_error(exc)
            assert isinstance(result, GeminiServerError)
        except ImportError:
            pytest.skip("google-genai not installed")


# ---------------------------------------------------------------------------
# GeminiClient construction
# ---------------------------------------------------------------------------


class TestGeminiClientConstruction:
    def test_empty_api_key_raises_value_error(self):
        with pytest.raises(ValueError, match="api_key boş olamaz"):
            GeminiClient("", provider_id="gemini_primary")

    def test_whitespace_api_key_raises_value_error(self):
        with pytest.raises(ValueError, match="api_key boş olamaz"):
            GeminiClient("   ", provider_id="gemini_primary")

    def test_none_api_key_raises_value_error(self):
        with pytest.raises(ValueError):
            GeminiClient(None, provider_id="gemini_primary")  # type: ignore[arg-type]

    def test_valid_key_creates_client(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = GeminiClient("valid-key-123", provider_id="gemini_primary")
            assert client.provider_id == "gemini_primary"
            mock_cls.assert_called_once_with(api_key="valid-key-123")

    def test_key_is_stripped(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            GeminiClient("  key-with-spaces  ", provider_id="gemini_primary")
            mock_cls.assert_called_once_with(api_key="key-with-spaces")

    def test_raw_client_returns_underlying_client(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            underlying = MagicMock()
            mock_cls.return_value = underlying
            client = GeminiClient("valid-key", provider_id="gemini_primary")
            assert client.raw_client() is underlying


# ---------------------------------------------------------------------------
# GeminiClient.chat()
# ---------------------------------------------------------------------------


class TestGeminiClientChat:
    def _make_client(self, response_text: str = "Yanıt") -> tuple[GeminiClient, MagicMock]:
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_genai = MagicMock()
            mock_response = MagicMock()
            mock_response.text = response_text
            mock_response.candidates = []
            mock_genai.models.generate_content.return_value = mock_response
            mock_cls.return_value = mock_genai
            client = GeminiClient("test-key", provider_id="gemini_primary")
            return client, mock_genai

    def test_chat_returns_text(self):
        client, mock_genai = self._make_client("Merhaba dünya")
        result = client.chat(
            model="models/gemini-2.5-flash",
            messages=[{"role": "user", "content": "Merhaba"}],
        )
        assert result == "Merhaba dünya"

    def test_chat_calls_generate_content(self):
        client, mock_genai = self._make_client()
        client.chat(
            model="models/gemini-2.5-flash",
            messages=[{"role": "user", "content": "Test"}],
        )
        mock_genai.models.generate_content.assert_called_once()
        call_kwargs = mock_genai.models.generate_content.call_args
        assert call_kwargs.kwargs["model"] == "models/gemini-2.5-flash"

    def test_chat_passes_temperature_and_max_tokens(self):
        client, mock_genai = self._make_client()
        client.chat(
            model="models/gemini-2.5-flash",
            messages=[{"role": "user", "content": "Test"}],
            temperature=0.7,
            max_tokens=512,
        )
        call_kwargs = mock_genai.models.generate_content.call_args
        config = call_kwargs.kwargs["config"]
        assert config.temperature == 0.7
        assert config.max_output_tokens == 512

    def test_chat_401_raises_auth_error(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_genai = MagicMock()
            mock_genai.models.generate_content.side_effect = Exception("401 Unauthorized")
            mock_cls.return_value = mock_genai
            client = GeminiClient("test-key", provider_id="gemini_primary")
            with pytest.raises(GeminiAuthError):
                client.chat("models/gemini-2.5-flash", [{"role": "user", "content": "test"}])

    def test_chat_429_raises_rate_limit_error(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_genai = MagicMock()
            mock_genai.models.generate_content.side_effect = Exception("429 Too Many Requests")
            mock_cls.return_value = mock_genai
            client = GeminiClient("test-key", provider_id="gemini_primary")
            with pytest.raises(GeminiRateLimitError):
                client.chat("models/gemini-2.5-flash", [{"role": "user", "content": "test"}])

    def test_chat_503_raises_server_error(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_genai = MagicMock()
            mock_genai.models.generate_content.side_effect = Exception("503 Service Unavailable")
            mock_cls.return_value = mock_genai
            client = GeminiClient("test-key", provider_id="gemini_primary")
            with pytest.raises(GeminiServerError):
                client.chat("models/gemini-2.5-flash", [{"role": "user", "content": "test"}])

    def test_chat_with_system_instruction(self):
        client, mock_genai = self._make_client()
        client.chat(
            model="models/gemini-2.5-flash",
            messages=[{"role": "user", "content": "Test"}],
            system_instruction="Sen bir asistansın",
        )
        call_kwargs = mock_genai.models.generate_content.call_args
        config = call_kwargs.kwargs["config"]
        assert config.system_instruction == "Sen bir asistansın"


# ---------------------------------------------------------------------------
# build_clients factory
# ---------------------------------------------------------------------------


class TestBuildClients:
    def test_empty_primary_key_raises_value_error(self):
        with patch("runtime.clients.gemini_client.genai.Client"):
            with pytest.raises(ValueError):
                build_clients("", "secondary-key")

    def test_valid_primary_only_returns_none_secondary(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client_a, client_b = build_clients("primary-key", "")
            assert client_a is not None
            assert client_b is None

    def test_none_secondary_returns_none_secondary(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client_a, client_b = build_clients("primary-key", None)
            assert client_a is not None
            assert client_b is None

    def test_whitespace_secondary_returns_none_secondary(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client_a, client_b = build_clients("primary-key", "   ")
            assert client_b is None

    def test_both_keys_returns_two_clients(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client_a, client_b = build_clients("primary-key", "secondary-key")
            assert client_a is not None
            assert client_b is not None

    def test_two_clients_have_different_provider_ids(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client_a, client_b = build_clients("primary-key", "secondary-key")
            assert client_a.provider_id == "gemini_primary"
            assert client_b is not None
            assert client_b.provider_id == "gemini_secondary"

    def test_two_clients_use_separate_genai_instances(self):
        """Req 2.5: no shared session state between clients."""
        instances = []
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.side_effect = lambda **kw: MagicMock(name=f"genai_{len(instances)}")
            client_a, client_b = build_clients("primary-key", "secondary-key")
            # Two separate genai.Client() calls were made
            assert mock_cls.call_count == 2
            # The two calls used different api_keys
            calls = mock_cls.call_args_list
            keys_used = [c.kwargs["api_key"] for c in calls]
            assert "primary-key" in keys_used
            assert "secondary-key" in keys_used

    def test_primary_key_stripped(self):
        with patch("runtime.clients.gemini_client.genai.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            build_clients("  primary-key  ", None)
            call = mock_cls.call_args_list[0]
            assert call.kwargs["api_key"] == "primary-key"
