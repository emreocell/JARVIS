"""Unit tests for runtime/route_selection.py.

Tests cover:
- derive_tool_category: prefix-based category derivation
- select_route: decision chain (prefer → tool_route → default_routes → fallback)
- health/blacklist filtering
- edge cases (empty health, all providers blacklisted, etc.)
"""

from __future__ import annotations

import pytest

from runtime.route_selection import derive_tool_category, select_route
from runtime.types import HealthState, Route, RouteProfile


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _healthy(provider: str) -> HealthState:
    return HealthState(
        provider=provider,
        healthy=True,
        last_checked_at=0.0,
        last_latency_ms=None,
        failure_streak=0,
        last_error=None,
    )


def _unhealthy(provider: str) -> HealthState:
    return HealthState(
        provider=provider,
        healthy=False,
        last_checked_at=0.0,
        last_latency_ms=None,
        failure_streak=2,
        last_error="timeout",
    )


def _route(provider: str, model: str = "m") -> Route:
    return Route(provider=provider, model=model)  # type: ignore[arg-type]


def _profile(primary: Route, *fallback: Route) -> RouteProfile:
    return RouteProfile(primary=primary, fallback=fallback)


_DEFAULT_ROUTES: dict = {
    "memory_rag.query": {"provider": "nvidia", "model": "nvidia/llama3-chatqa-1.5-70b"},
    "memory_rag.embed": {"provider": "nvidia", "model": "nvidia/nv-embedqa-e5-v5"},
    "doc_intel.parse": {"provider": "nvidia", "model": "nvidia/nemotron-parse"},
    "reasoning.plan": {"provider": "nvidia", "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5"},
    "translate.text": {"provider": "nvidia", "model": "nvidia/riva-translate-4b-instruct-v1.1"},
    "creative.write": {"provider": "nvidia", "model": "writer/palmyra-creative-122b"},
    "image_search.embed": {"provider": "nvidia", "model": "nvidia/nvclip"},
    "embodied.next_action": {"provider": "nvidia", "model": "nvidia/cosmos-reason2-8b"},
    "voice_core.intent": {"provider": "gemini_primary", "model": "models/gemini-2.5-flash"},
    "safety.*": {"provider": "nvidia", "model": "nvidia/llama-guard"},
}

_NO_HEALTH: dict = {}
_NO_BLACKLIST: set = set()


# ---------------------------------------------------------------------------
# derive_tool_category tests
# ---------------------------------------------------------------------------

class TestDeriveToolCategory:
    def test_memory_prefix(self):
        assert derive_tool_category("memory_rag_query") == "memory_rag.query"
        assert derive_tool_category("memory_index_add") == "memory_rag.query"

    def test_doc_prefix(self):
        assert derive_tool_category("doc_parse") == "doc_intel.parse"
        assert derive_tool_category("doc_intel_something") == "doc_intel.parse"

    def test_chart_prefix(self):
        assert derive_tool_category("chart_read") == "doc_intel.parse"

    def test_screenshot_prefix(self):
        assert derive_tool_category("screenshot_summarize") == "doc_intel.parse"

    def test_plan_prefix(self):
        assert derive_tool_category("plan_generate") == "reasoning.plan"
        assert derive_tool_category("plan_save") == "reasoning.plan"

    def test_translate_prefix(self):
        assert derive_tool_category("translate_text") == "translate.text"
        assert derive_tool_category("translate_screen") == "translate.text"

    def test_creative_prefix(self):
        assert derive_tool_category("creative_write") == "creative.write"

    def test_financial_prefix(self):
        assert derive_tool_category("financial_analyze") == "creative.write"

    def test_medical_prefix(self):
        assert derive_tool_category("medical_qa") == "creative.write"

    def test_image_prefix(self):
        assert derive_tool_category("image_search") == "image_search.embed"
        assert derive_tool_category("image_index_build") == "image_search.embed"

    def test_gui_prefix(self):
        assert derive_tool_category("gui_next_action") == "embodied.next_action"

    def test_pii_prefix(self):
        assert derive_tool_category("pii_mask") == "safety.*"

    def test_safety_check_suffix(self):
        assert derive_tool_category("content_safety_check") == "safety.*"
        assert derive_tool_category("topic_control_check") == "safety.*"

    def test_deepfake_prefix(self):
        assert derive_tool_category("deepfake_detect") == "safety.*"

    def test_unknown_falls_back_to_voice_core(self):
        assert derive_tool_category("some_random_tool") == "voice_core.intent"
        assert derive_tool_category("clipboard_copy") == "voice_core.intent"
        assert derive_tool_category("") == "voice_core.intent"


# ---------------------------------------------------------------------------
# select_route: decision chain tests
# ---------------------------------------------------------------------------

class TestSelectRouteDecisionChain:
    def test_prefer_route_takes_priority_over_tool_route(self):
        prefer = _route("gemini_primary", "models/gemini-2.5-flash")
        tool_route = _profile(_route("nvidia", "nvidia/some-model"))
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=prefer,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_primary"
        assert primary.model == "models/gemini-2.5-flash"

    def test_prefer_profile_takes_priority(self):
        prefer = _profile(
            _route("gemini_secondary", "models/gemini-2.5-pro"),
            _route("gemini_primary", "models/gemini-2.5-flash"),
        )
        tool_route = _profile(_route("nvidia", "nvidia/some-model"))
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=prefer,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_secondary"
        assert len(fallback) == 1
        assert fallback[0].provider == "gemini_primary"

    def test_tool_route_takes_priority_over_default_routes(self):
        tool_route = _profile(_route("nvidia", "nvidia/custom-model"))
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"
        assert primary.model == "nvidia/custom-model"

    def test_default_routes_used_when_no_prefer_or_tool_route(self):
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"
        assert primary.model == "nvidia/llama3-chatqa-1.5-70b"

    def test_fallback_route_when_no_match(self):
        """Unknown tool with no default route → gemini_primary fallback."""
        primary, fallback = select_route(
            tool_name="some_unknown_tool",
            tool_route=None,
            prefer=None,
            default_routes={},
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_primary"
        assert primary.model == "models/gemini-3.1-flash-lite"
        assert fallback == ()

    def test_voice_core_intent_category_uses_gemini_primary(self):
        primary, _ = select_route(
            tool_name="some_unknown_tool",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_primary"

    def test_translate_tool_uses_nvidia(self):
        primary, _ = select_route(
            tool_name="translate_text",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"
        assert "riva" in primary.model

    def test_safety_tool_uses_wildcard_category(self):
        primary, _ = select_route(
            tool_name="pii_mask",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"


# ---------------------------------------------------------------------------
# select_route: health/blacklist filtering tests
# ---------------------------------------------------------------------------

class TestSelectRouteFiltering:
    def test_blacklisted_provider_skipped(self):
        """If nvidia is blacklisted, prefer nvidia route should be skipped."""
        prefer = _route("nvidia", "nvidia/some-model")
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=prefer,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist={"nvidia"},
        )
        # prefer was nvidia (blacklisted), falls through to default_routes
        # memory_rag.query → nvidia (also blacklisted) → falls to voice_core.intent → gemini_primary
        assert primary.provider == "gemini_primary"

    def test_unhealthy_provider_skipped(self):
        """Unhealthy nvidia → skip to next in chain."""
        tool_route = _profile(
            _route("nvidia", "nvidia/model-a"),
            _route("gemini_secondary", "models/gemini-2.5-pro"),
        )
        health = {"nvidia": _unhealthy("nvidia")}
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=health,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_secondary"

    def test_all_prefer_routes_unhealthy_falls_to_tool_route(self):
        prefer = _profile(
            _route("nvidia", "nvidia/model-a"),
        )
        tool_route = _profile(_route("gemini_primary", "models/gemini-2.5-flash"))
        health = {"nvidia": _unhealthy("nvidia")}
        primary, _ = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=prefer,
            default_routes=_DEFAULT_ROUTES,
            health=health,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "gemini_primary"

    def test_all_routes_unhealthy_returns_fallback_route(self):
        """When all providers are unhealthy, last-resort route is returned."""
        health = {
            "nvidia": _unhealthy("nvidia"),
            "gemini_primary": _unhealthy("gemini_primary"),
            "gemini_secondary": _unhealthy("gemini_secondary"),
        }
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=health,
            blacklist=_NO_BLACKLIST,
        )
        # Last resort: gemini_primary + default model (no filter applied)
        assert primary.provider == "gemini_primary"
        assert primary.model == "models/gemini-3.1-flash-lite"

    def test_healthy_provider_not_filtered(self):
        health = {"nvidia": _healthy("nvidia")}
        primary, _ = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=health,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"

    def test_blacklist_takes_precedence_over_health(self):
        """Even if health says healthy, blacklist wins."""
        health = {"nvidia": _healthy("nvidia")}
        primary, _ = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=health,
            blacklist={"nvidia"},
        )
        # nvidia blacklisted → falls to voice_core.intent → gemini_primary
        assert primary.provider == "gemini_primary"

    def test_fallback_chain_deduplication(self):
        """Same route appearing twice in chain should only appear once."""
        nvidia_route = _route("nvidia", "nvidia/model-a")
        tool_route = _profile(nvidia_route, nvidia_route)
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary == nvidia_route
        assert len(fallback) == 0  # duplicate removed

    def test_fallback_chain_preserves_order(self):
        """Fallback chain order must be preserved."""
        tool_route = _profile(
            _route("nvidia", "nvidia/model-a"),
            _route("gemini_secondary", "models/gemini-2.5-pro"),
            _route("gemini_primary", "models/gemini-2.5-flash"),
        )
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=tool_route,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"
        assert fallback[0].provider == "gemini_secondary"
        assert fallback[1].provider == "gemini_primary"


# ---------------------------------------------------------------------------
# select_route: RouteProfile as default_routes value
# ---------------------------------------------------------------------------

class TestSelectRouteWithRouteProfileDefaults:
    def test_default_routes_accepts_route_profile_objects(self):
        """default_routes values can be RouteProfile instances."""
        profile = _profile(
            _route("nvidia", "nvidia/custom"),
            _route("gemini_primary", "models/gemini-2.5-flash"),
        )
        default_routes = {"memory_rag.query": profile}
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=default_routes,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert primary.provider == "nvidia"
        assert len(fallback) == 1
        assert fallback[0].provider == "gemini_primary"


# ---------------------------------------------------------------------------
# select_route: always returns at least one route
# ---------------------------------------------------------------------------

class TestSelectRouteGuarantees:
    def test_always_returns_route_even_with_empty_defaults(self):
        primary, fallback = select_route(
            tool_name="anything",
            tool_route=None,
            prefer=None,
            default_routes={},
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert isinstance(primary, Route)

    def test_always_returns_route_with_all_blacklisted(self):
        primary, fallback = select_route(
            tool_name="memory_rag_query",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist={"nvidia", "gemini_primary", "gemini_secondary"},
        )
        # Last resort is returned without filter
        assert isinstance(primary, Route)
        assert primary.provider == "gemini_primary"

    def test_return_type_is_route_and_tuple(self):
        primary, fallback = select_route(
            tool_name="translate_text",
            tool_route=None,
            prefer=None,
            default_routes=_DEFAULT_ROUTES,
            health=_NO_HEALTH,
            blacklist=_NO_BLACKLIST,
        )
        assert isinstance(primary, Route)
        assert isinstance(fallback, tuple)
