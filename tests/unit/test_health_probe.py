"""Unit tests for runtime/clients/health.py — HealthProbe and compute_next_health_state.

Tests cover:
- compute_next_health_state: success/failure transitions, streak counting,
  unhealthy window enforcement, single-success reset
- HealthProbe: start/stop idempotency, state() returns deep copy,
  probe_once() updates state, failure streak → unhealthy, recovery
- _build_ping_request: correct request type per provider
"""

from __future__ import annotations

import threading
import time
from typing import Callable
from unittest.mock import MagicMock, patch

import pytest

from runtime.clients.health import (
    HealthProbe,
    _build_ping_request,
    compute_next_health_state,
)
from runtime.types import HealthState, RouteRequest, RouteResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_health_state(
    provider: str = "nvidia",
    healthy: bool = True,
    failure_streak: int = 0,
    last_checked_at: float = 0.0,
    last_error: str | None = None,
) -> HealthState:
    return HealthState(
        provider=provider,
        healthy=healthy,
        last_checked_at=last_checked_at,
        last_latency_ms=None,
        failure_streak=failure_streak,
        last_error=last_error,
    )


def _ok_result(provider: str = "nvidia") -> RouteResult:
    return RouteResult(ok=True, provider=provider, model="test-model", latency_ms=50)


def _fail_result(provider: str = "nvidia", error: str = "timeout") -> RouteResult:
    return RouteResult(
        ok=False,
        provider=provider,
        model="test-model",
        error_class="TimeoutError",
        error_message=error,
        user_message_tr="Hata oluştu.",
    )


class _FakeRouter:
    """Minimal router mock that satisfies _RouterProtocol."""

    def __init__(self, responses: dict[str, RouteResult] | None = None) -> None:
        # responses: provider_name → RouteResult
        self._responses: dict[str, RouteResult] = responses or {}
        self.calls: list[tuple[str, RouteRequest, object]] = []

    def route(
        self,
        tool_name: str,
        request: RouteRequest,
        *,
        prefer: object = None,
    ) -> RouteResult:
        self.calls.append((tool_name, request, prefer))
        # Determine provider from tool_name (e.g. "__health_probe_nvidia__")
        for provider, result in self._responses.items():
            if provider in tool_name:
                return result
        # Default: success
        return RouteResult(ok=True, provider="unknown", model="m", latency_ms=10)

    def health(self) -> dict[str, HealthState]:
        return {}


# ---------------------------------------------------------------------------
# compute_next_health_state tests
# ---------------------------------------------------------------------------


class TestComputeNextHealthState:
    def test_success_resets_streak_and_marks_healthy(self):
        current = _make_health_state(healthy=False, failure_streak=3)
        result = compute_next_health_state(
            current,
            success=True,
            latency_ms=42,
            error=None,
            now=100.0,
        )
        assert result.healthy is True
        assert result.failure_streak == 0
        assert result.last_error is None
        assert result.last_latency_ms == 42
        assert result.last_checked_at == 100.0

    def test_first_failure_increments_streak_stays_healthy(self):
        current = _make_health_state(healthy=True, failure_streak=0)
        result = compute_next_health_state(
            current,
            success=False,
            latency_ms=None,
            error="connection refused",
            now=200.0,
        )
        assert result.failure_streak == 1
        assert result.healthy is True  # only 1 failure, threshold is 2
        assert result.last_error == "connection refused"

    def test_second_failure_marks_unhealthy(self):
        current = _make_health_state(healthy=True, failure_streak=1)
        result = compute_next_health_state(
            current,
            success=False,
            latency_ms=None,
            error="timeout",
            now=300.0,
        )
        assert result.failure_streak == 2
        assert result.healthy is False

    def test_third_failure_stays_unhealthy(self):
        current = _make_health_state(healthy=False, failure_streak=2, last_checked_at=290.0)
        result = compute_next_health_state(
            current,
            success=False,
            latency_ms=None,
            error="timeout",
            now=300.0,  # only 10s elapsed, window=60s
            unhealthy_window_sec=60.0,
        )
        assert result.failure_streak == 3
        assert result.healthy is False

    def test_unhealthy_window_enforced_even_after_single_failure(self):
        """If already unhealthy and window hasn't elapsed, stays unhealthy."""
        current = _make_health_state(
            healthy=False, failure_streak=2, last_checked_at=0.0
        )
        result = compute_next_health_state(
            current,
            success=False,
            latency_ms=None,
            error="err",
            now=30.0,  # only 30s elapsed, window=60s
            unhealthy_window_sec=60.0,
        )
        assert result.healthy is False

    def test_success_immediately_resets_even_during_unhealthy_window(self):
        """Single success resets streak and marks healthy immediately."""
        current = _make_health_state(
            healthy=False, failure_streak=5, last_checked_at=0.0
        )
        result = compute_next_health_state(
            current,
            success=True,
            latency_ms=10,
            error=None,
            now=30.0,  # within unhealthy window
            unhealthy_window_sec=60.0,
        )
        assert result.healthy is True
        assert result.failure_streak == 0

    def test_provider_name_preserved(self):
        current = _make_health_state(provider="gemini_primary")
        result = compute_next_health_state(
            current, success=True, latency_ms=5, error=None, now=1.0
        )
        assert result.provider == "gemini_primary"

    def test_failure_with_no_latency(self):
        current = _make_health_state()
        result = compute_next_health_state(
            current, success=False, latency_ms=None, error="err", now=1.0
        )
        assert result.last_latency_ms is None

    def test_success_with_latency(self):
        current = _make_health_state()
        result = compute_next_health_state(
            current, success=True, latency_ms=123, error=None, now=1.0
        )
        assert result.last_latency_ms == 123


# ---------------------------------------------------------------------------
# _build_ping_request tests
# ---------------------------------------------------------------------------


class TestBuildPingRequest:
    def test_gemini_primary_returns_chat(self):
        req = _build_ping_request("gemini_primary")
        assert req.kind == "chat"
        assert req.messages is not None
        assert req.messages[0]["content"] == "ping"

    def test_gemini_secondary_returns_chat(self):
        req = _build_ping_request("gemini_secondary")
        assert req.kind == "chat"

    def test_nvidia_returns_chat(self):
        req = _build_ping_request("nvidia")
        assert req.kind == "chat"
        assert req.messages is not None
        assert req.messages[0]["content"] == "ping"

    def test_unknown_provider_returns_chat(self):
        req = _build_ping_request("some_unknown_provider")
        assert req.kind == "chat"


# ---------------------------------------------------------------------------
# HealthProbe constructor tests
# ---------------------------------------------------------------------------


class TestHealthProbeConstructor:
    def test_default_interval(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        assert probe._interval_sec == 60.0

    def test_custom_interval(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=30.0)
        assert probe._interval_sec == 30.0

    def test_invalid_interval_raises(self):
        router = _FakeRouter()
        with pytest.raises(ValueError, match="interval_sec"):
            HealthProbe(router, interval_sec=0)

    def test_invalid_unhealthy_window_raises(self):
        router = _FakeRouter()
        with pytest.raises(ValueError, match="unhealthy_window_sec"):
            HealthProbe(router, unhealthy_window_sec=-1.0)

    def test_initial_state_all_healthy(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        states = probe.state()
        assert "gemini_primary" in states
        assert "gemini_secondary" in states
        assert "nvidia" in states
        for state in states.values():
            assert state.healthy is True
            assert state.failure_streak == 0

    def test_custom_time_provider(self):
        router = _FakeRouter()
        fake_time = [1000.0]
        probe = HealthProbe(router, time_provider=lambda: fake_time[0])
        states = probe.state()
        for state in states.values():
            assert state.last_checked_at == 1000.0


# ---------------------------------------------------------------------------
# HealthProbe.state() tests
# ---------------------------------------------------------------------------


class TestHealthProbeState:
    def test_state_returns_deep_copy(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        states1 = probe.state()
        states2 = probe.state()
        # Modifying one copy should not affect the other
        states1["nvidia"].healthy = False
        states3 = probe.state()
        assert states3["nvidia"].healthy is True  # original unchanged

    def test_state_contains_all_providers(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        states = probe.state()
        assert set(states.keys()) == {"gemini_primary", "gemini_secondary", "nvidia"}


# ---------------------------------------------------------------------------
# HealthProbe.probe_once() tests
# ---------------------------------------------------------------------------


class TestHealthProbeProbeOnce:
    def test_probe_once_calls_router_for_each_provider(self):
        router = _FakeRouter(
            responses={
                "gemini_primary": _ok_result("gemini_primary"),
                "gemini_secondary": _ok_result("gemini_secondary"),
                "nvidia": _ok_result("nvidia"),
            }
        )
        probe = HealthProbe(router)
        states = probe.probe_once()
        # Should have called router 3 times (one per provider)
        assert len(router.calls) == 3
        # All should be healthy after success
        for state in states.values():
            assert state.healthy is True

    def test_probe_once_pins_each_provider_route(self):
        router = _FakeRouter()
        probe = HealthProbe(router)

        probe.probe_once()

        preferred = {tool_name: prefer for tool_name, _request, prefer in router.calls}
        assert preferred["__health_probe_gemini_primary__"].provider == "gemini_primary"
        assert preferred["__health_probe_gemini_secondary__"].provider == "gemini_secondary"
        assert preferred["__health_probe_nvidia__"].provider == "nvidia"

    def test_probe_once_marks_unhealthy_after_two_failures(self):
        router = _FakeRouter(
            responses={
                "nvidia": _fail_result("nvidia"),
            }
        )
        probe = HealthProbe(router)
        # First probe: streak=1, still healthy
        probe.probe_once()
        assert probe.state()["nvidia"].failure_streak == 1
        assert probe.state()["nvidia"].healthy is True
        # Second probe: streak=2, unhealthy
        probe.probe_once()
        assert probe.state()["nvidia"].failure_streak == 2
        assert probe.state()["nvidia"].healthy is False

    def test_probe_once_resets_after_success(self):
        fail_router = _FakeRouter(responses={"nvidia": _fail_result("nvidia")})
        probe = HealthProbe(fail_router)
        probe.probe_once()
        probe.probe_once()
        assert probe.state()["nvidia"].healthy is False

        # Now switch to success
        ok_router = _FakeRouter(responses={"nvidia": _ok_result("nvidia")})
        probe._router = ok_router
        probe.probe_once()
        assert probe.state()["nvidia"].healthy is True
        assert probe.state()["nvidia"].failure_streak == 0

    def test_probe_once_handles_router_exception(self):
        """Router raising an exception should count as a failure."""

        class _ExceptionRouter:
            def route(self, tool_name, request, *, prefer=None):
                if "nvidia" in tool_name:
                    raise ConnectionError("network down")
                return RouteResult(ok=True, provider="gemini", model="m", latency_ms=5)

            def health(self):
                return {}

        probe = HealthProbe(_ExceptionRouter())
        probe.probe_once()
        assert probe.state()["nvidia"].failure_streak == 1
        assert probe.state()["nvidia"].last_error is not None
        assert "ConnectionError" in probe.state()["nvidia"].last_error

    def test_probe_once_returns_state_snapshot(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        result = probe.probe_once()
        assert isinstance(result, dict)
        assert "nvidia" in result


# ---------------------------------------------------------------------------
# HealthProbe.start() / stop() tests
# ---------------------------------------------------------------------------


class TestHealthProbeStartStop:
    def test_start_creates_daemon_thread(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=60.0)
        probe.start()
        try:
            assert probe._thread is not None
            assert probe._thread.is_alive()
            assert probe._thread.daemon is True
        finally:
            probe.stop()

    def test_start_is_idempotent(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=60.0)
        probe.start()
        thread1 = probe._thread
        probe.start()  # second call should be no-op
        thread2 = probe._thread
        assert thread1 is thread2
        probe.stop()

    def test_stop_terminates_thread(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=60.0)
        probe.start()
        assert probe._thread.is_alive()
        probe.stop()
        # Give thread a moment to finish
        probe._thread.join(timeout=3.0)
        assert not probe._thread.is_alive()

    def test_stop_without_start_is_safe(self):
        router = _FakeRouter()
        probe = HealthProbe(router)
        probe.stop()  # should not raise

    def test_stop_sets_stop_event(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=60.0)
        probe.start()
        probe.stop()
        assert probe._stop_event.is_set()

    def test_thread_name_is_health_probe(self):
        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=60.0)
        probe.start()
        try:
            assert probe._thread.name == "HealthProbe"
        finally:
            probe.stop()


# ---------------------------------------------------------------------------
# HealthProbe integration: background loop pings providers
# ---------------------------------------------------------------------------


class TestHealthProbeBackgroundLoop:
    def test_background_loop_updates_state(self):
        """Background thread should call router and update state."""
        call_event = threading.Event()
        original_ping = HealthProbe._ping_all_providers

        def _patched_ping(self):
            original_ping(self)
            call_event.set()

        router = _FakeRouter()
        probe = HealthProbe(router, interval_sec=0.05)  # very short interval

        with patch.object(HealthProbe, "_ping_all_providers", _patched_ping):
            probe.start()
            called = call_event.wait(timeout=2.0)
            probe.stop()

        assert called, "Background loop did not call _ping_all_providers within timeout"

    def test_background_loop_marks_unhealthy_after_two_failures(self):
        """Two consecutive failures in background loop → unhealthy."""
        fail_count = {"n": 0}

        class _CountingRouter:
            def route(self, tool_name, request, *, prefer=None):
                if "nvidia" in tool_name:
                    fail_count["n"] += 1
                    return _fail_result("nvidia")
                return _ok_result("gemini")

            def health(self):
                return {}

        probe = HealthProbe(_CountingRouter(), interval_sec=0.02)
        probe.start()
        # Wait until nvidia has been probed at least twice
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if fail_count["n"] >= 2:
                break
            time.sleep(0.01)
        probe.stop()

        assert probe.state()["nvidia"].healthy is False
