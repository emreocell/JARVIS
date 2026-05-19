from __future__ import annotations

from runtime.model_router import ModelRouter, ModelRouterConfig
from runtime.types import Route, RouteRequest


class _FakeGemini:
    provider_id = "gemini_primary"

    def chat(self, **kwargs):  # noqa: ANN001
        return "gemini"


class _FakeGroq:
    provider_id = "groq"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def chat(self, **kwargs):  # noqa: ANN001
        self.calls.append(kwargs)
        return "groq routed"


class _Privacy:
    def is_active(self) -> bool:
        return False


def test_model_router_routes_chat_to_groq_prefer() -> None:
    groq = _FakeGroq()
    router = ModelRouter(
        _FakeGemini(),  # type: ignore[arg-type]
        None,
        None,
        groq,  # type: ignore[arg-type]
        None,
        ModelRouterConfig(),
        _Privacy(),  # type: ignore[arg-type]
    )

    result = router.route(
        "quick_intent",
        RouteRequest(
            kind="chat",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=16,
            temperature=0,
        ),
        prefer=Route(provider="groq", model="llama-3.1-8b-instant"),
    )

    assert result.ok is True
    assert result.provider == "groq"
    assert result.text == "groq routed"
    assert groq.calls[0]["model"] == "llama-3.1-8b-instant"
