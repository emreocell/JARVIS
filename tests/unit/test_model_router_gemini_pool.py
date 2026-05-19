from __future__ import annotations

from runtime.clients.gemini_client import GeminiRateLimitError
from runtime.model_router import ModelRouter, ModelRouterConfig
from runtime.types import Route, RouteRequest


class _Gemini:
    def __init__(self, provider_id: str, *, fail: bool = False) -> None:
        self.provider_id = provider_id
        self.fail = fail
        self.calls: list[str] = []

    def chat(self, *, model: str, **_kwargs):  # noqa: ANN001
        self.calls.append(model)
        if self.fail:
            raise GeminiRateLimitError("quota", status_code=429)
        return f"{self.provider_id}:{model}"


class _Privacy:
    def is_active(self) -> bool:
        return False


def _router(primary: _Gemini, secondary: _Gemini, **kwargs) -> ModelRouter:
    config = ModelRouterConfig(
        gemini_chat_model="models/gemini-3.1-flash-lite",
        gemini_task_models=(
            "models/gemini-2.5-flash",
            "models/gemini-3.1-flash-lite",
            "models/gemini-2.5-flash-lite",
        ),
        gemini_pool_providers=("gemini_primary", "gemini_secondary"),
    )
    return ModelRouter(
        primary,  # type: ignore[arg-type]
        secondary,  # type: ignore[arg-type]
        None,
        None,
        None,
        config,
        _Privacy(),  # type: ignore[arg-type]
        **kwargs,
    )


def test_gemini_task_pool_rotates_accounts_before_next_model() -> None:
    primary = _Gemini("gemini_primary", fail=True)
    secondary = _Gemini("gemini_secondary")
    router = _router(primary, secondary)

    result = router.route(
        "heavy_task",
        RouteRequest(kind="chat", messages=[{"role": "user", "content": "ping"}]),
        prefer=Route(provider="gemini_primary", model="models/gemini-2.5-flash"),
    )

    assert result.ok is True
    assert result.provider == "gemini_secondary"
    assert result.model == "models/gemini-2.5-flash"
    assert primary.calls == ["models/gemini-2.5-flash"]
    assert secondary.calls == ["models/gemini-2.5-flash"]


def test_gemini_chat_pool_keeps_fixed_chat_model() -> None:
    primary = _Gemini("gemini_primary", fail=True)
    secondary = _Gemini("gemini_secondary")
    router = _router(primary, secondary)

    result = router.route(
        "chat",
        RouteRequest(kind="chat", messages=[{"role": "user", "content": "ping"}]),
    )

    assert result.ok is True
    assert result.provider == "gemini_secondary"
    assert result.model == "models/gemini-3.1-flash-lite"
    assert primary.calls == ["models/gemini-3.1-flash-lite"]
    assert secondary.calls == ["models/gemini-3.1-flash-lite"]
