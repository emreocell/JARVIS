from __future__ import annotations

from runtime.model_router import ModelRouter, ModelRouterConfig
from runtime.types import Route, RouteRequest


class _FakeGemini:
    provider_id = "gemini_primary"

    def chat(self, **kwargs):  # noqa: ANN001
        return "gemini"


class _FakeOpenRouter:
    provider_id = "openrouter"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        return "openrouter routed"


class _Privacy:
    def is_active(self) -> bool:
        return False


def test_model_router_routes_chat_to_openrouter_prefer() -> None:
    openrouter = _FakeOpenRouter()
    router = ModelRouter(
        _FakeGemini(),  # type: ignore[arg-type]
        None,
        None,
        None,
        openrouter,  # type: ignore[arg-type]
        ModelRouterConfig(),
        _Privacy(),  # type: ignore[arg-type]
    )

    result = router.route(
        "reasoning_plan",
        RouteRequest(
            kind="chat",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            temperature=0,
        ),
        prefer=Route(provider="openrouter", model="openai/gpt-oss-20b:free"),
    )

    assert result.ok is True
    assert result.provider == "openrouter"
    assert result.text == "openrouter routed"
    assert openrouter.calls[0]["model"] == "openai/gpt-oss-20b:free"


def test_model_router_expands_configured_provider_fallback_to_openrouter() -> None:
    openrouter = _FakeOpenRouter()
    router = ModelRouter(
        _FakeGemini(),  # type: ignore[arg-type]
        None,
        None,
        None,
        openrouter,  # type: ignore[arg-type]
        ModelRouterConfig(
            default_routes={
                "voice_core.intent": {
                    "provider": "groq",
                    "model": "llama-3.1-8b-instant",
                },
                "openrouter.fast": {
                    "provider": "openrouter",
                    "model": "openai/gpt-oss-20b:free",
                },
            },
            fallback_chain={"groq": ["openrouter", "gemini_primary"]},
        ),
        _Privacy(),  # type: ignore[arg-type]
    )

    result = router.route(
        "unknown_tool",
        RouteRequest(
            kind="chat",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            temperature=0,
        ),
    )

    assert result.ok is True
    assert result.provider == "openrouter"
    assert "groq:llama-3.1-8b-instant" in result.fallback_chain
    assert "openrouter:openai/gpt-oss-20b:free" in result.fallback_chain
