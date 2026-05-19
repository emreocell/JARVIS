"""Unit tests for :mod:`runtime.privacy_mode`.

Covers the small contract documented in design.md § Privacy_Mode and the
acceptance criteria of Requirement 27:

* ``enable``/``disable`` flip the observable state.
* ``is_active()`` reflects the latest flip.
* ``on_change`` listeners fire **once** per real transition and never on
  redundant calls.
* Listener exceptions never corrupt the state or block other listeners.
* The class is safe to drive from multiple threads concurrently.
"""

# Feature: jarvis-v2-upgrade, Task 6.1 — runtime/privacy_mode.py

from __future__ import annotations

import threading
from typing import Any

import pytest

from runtime.privacy_mode import PrivacyMode


# ---------------------------------------------------------------------------
# Basic on/off behaviour
# ---------------------------------------------------------------------------


def test_default_state_is_inactive() -> None:
    """Privacy_Mode boots inactive so the rest of the app is unblocked."""
    privacy = PrivacyMode()
    assert privacy.is_active() is False


def test_initial_true_starts_active_without_firing_listeners() -> None:
    """``initial=True`` honours app_config without notifying yet-unsubscribed callbacks."""
    seen: list[bool] = []
    privacy = PrivacyMode(initial=True)
    privacy.on_change(seen.append)

    assert privacy.is_active() is True
    # Subscribing after the bootstrap state is set must not synthesise a flip.
    assert seen == []


def test_enable_flips_state_and_returns_true() -> None:
    privacy = PrivacyMode()
    changed = privacy.enable()

    assert changed is True
    assert privacy.is_active() is True


def test_disable_returns_to_inactive() -> None:
    privacy = PrivacyMode(initial=True)
    changed = privacy.disable()

    assert changed is True
    assert privacy.is_active() is False


def test_toggle_returns_new_state() -> None:
    privacy = PrivacyMode()

    assert privacy.toggle() is True
    assert privacy.is_active() is True
    assert privacy.toggle() is False
    assert privacy.is_active() is False


# ---------------------------------------------------------------------------
# Idempotence — redundant calls don't fire listeners
# ---------------------------------------------------------------------------


def test_enable_is_idempotent() -> None:
    """``enable()`` while already active is a no-op (returns False)."""
    privacy = PrivacyMode(initial=True)

    assert privacy.enable() is False
    assert privacy.is_active() is True


def test_disable_is_idempotent() -> None:
    privacy = PrivacyMode()

    assert privacy.disable() is False
    assert privacy.is_active() is False


def test_redundant_enable_does_not_notify_listeners() -> None:
    seen: list[bool] = []
    privacy = PrivacyMode()
    privacy.on_change(seen.append)

    privacy.enable()
    privacy.enable()
    privacy.enable()

    # Only the *real* transition should fire the listener.
    assert seen == [True]


def test_redundant_disable_does_not_notify_listeners() -> None:
    seen: list[bool] = []
    privacy = PrivacyMode(initial=True)
    privacy.on_change(seen.append)

    privacy.disable()
    privacy.disable()

    assert seen == [False]


# ---------------------------------------------------------------------------
# Listener fan-out semantics
# ---------------------------------------------------------------------------


def test_listener_receives_new_state_value() -> None:
    """Subscribers see the *new* boolean, not the previous one."""
    captured: list[bool] = []
    privacy = PrivacyMode()
    privacy.on_change(captured.append)

    privacy.enable()
    privacy.disable()
    privacy.enable()

    assert captured == [True, False, True]


def test_multiple_listeners_fire_in_registration_order() -> None:
    order: list[str] = []
    privacy = PrivacyMode()

    privacy.on_change(lambda _v: order.append("a"))
    privacy.on_change(lambda _v: order.append("b"))
    privacy.on_change(lambda _v: order.append("c"))

    privacy.enable()

    assert order == ["a", "b", "c"]


def test_listener_exception_does_not_block_other_listeners() -> None:
    """A raising subscriber must not stop later subscribers from running.

    Mirrors the policy ``TaskManager`` already applies to its listeners:
    one broken consumer should never stall the rest of the system.
    """
    seen: list[bool] = []
    privacy = PrivacyMode()

    def boom(_: bool) -> None:
        raise RuntimeError("listener failure simulated")

    privacy.on_change(boom)
    privacy.on_change(seen.append)

    privacy.enable()  # must not raise

    assert seen == [True]
    assert privacy.is_active() is True


def test_listener_exception_does_not_corrupt_state() -> None:
    """Even when a subscriber crashes, the state flip is still observable."""
    privacy = PrivacyMode()

    def boom(_: bool) -> None:
        raise ValueError("ignore me")

    privacy.on_change(boom)

    privacy.enable()
    assert privacy.is_active() is True

    privacy.disable()
    assert privacy.is_active() is False


def test_non_callable_listener_rejected() -> None:
    privacy = PrivacyMode()

    with pytest.raises(TypeError):
        privacy.on_change("not callable")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Listener removal
# ---------------------------------------------------------------------------


def test_remove_listener_returns_true_when_present() -> None:
    privacy = PrivacyMode()
    cb: Any = lambda _v: None  # noqa: E731 — short test stub
    privacy.on_change(cb)

    assert privacy.remove_listener(cb) is True


def test_remove_listener_returns_false_when_unknown() -> None:
    privacy = PrivacyMode()
    cb: Any = lambda _v: None  # noqa: E731
    assert privacy.remove_listener(cb) is False


def test_removed_listener_no_longer_fires() -> None:
    seen: list[bool] = []
    privacy = PrivacyMode()
    privacy.on_change(seen.append)

    privacy.enable()  # captured
    privacy.remove_listener(seen.append)
    privacy.disable()  # not captured

    assert seen == [True]


def test_same_listener_can_be_registered_multiple_times() -> None:
    """Documented behaviour: dedupe is the caller's job."""
    seen: list[bool] = []
    privacy = PrivacyMode()
    privacy.on_change(seen.append)
    privacy.on_change(seen.append)

    privacy.enable()

    assert seen == [True, True]


# ---------------------------------------------------------------------------
# Subsystem integration sketch — Req 27.1/27.2/27.3 wiring
# ---------------------------------------------------------------------------


def test_subsystems_can_gate_on_is_active() -> None:
    """Privacy_Mode is a *signal*; consumers gate their own writes on it.

    This test plays the role of Conversation_Logger / Clipboard_Manager /
    Wake_Word_Engine: each peeks at ``is_active()`` before doing its
    sensitive work and skips when privacy is on. The acceptance criteria
    in Requirement 27 are satisfied by this pattern at the subsystem
    layer; here we just validate the signal we hand them is correct.
    """
    privacy = PrivacyMode()

    def conversation_logger_write(line: str, sink: list[str]) -> None:
        if privacy.is_active():
            return  # Req 27.2 / 28.3
        sink.append(line)

    sink: list[str] = []

    conversation_logger_write("hello before", sink)
    privacy.enable()
    conversation_logger_write("muted line", sink)
    privacy.disable()
    conversation_logger_write("hello after", sink)

    assert sink == ["hello before", "hello after"]


def test_subsystems_can_react_to_changes_via_callback() -> None:
    """Voice_Core / Wake_Word_Engine subscribe to flip events.

    When privacy turns *on*, they must pause; when it turns *off*, they
    must resume. The callback contract is the only mechanism the privacy
    module exposes for that, so we lock it in here.
    """
    voice_state = {"listening": True}

    def voice_handler(active: bool) -> None:
        voice_state["listening"] = not active

    privacy = PrivacyMode()
    privacy.on_change(voice_handler)

    privacy.enable()
    assert voice_state["listening"] is False  # Req 27.1: mic paused.

    privacy.disable()
    assert voice_state["listening"] is True  # Req 27.4: mic resumes.


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_toggles_keep_state_consistent() -> None:
    """Hammering enable/disable from many threads must never deadlock or
    leave the state out of sync with the listener fan-out.

    We don't assert a specific listener count (toggles can race past each
    other); we only assert that the final ``is_active()`` value matches
    the deterministic final write we issue at the end, and that no thread
    raised.
    """
    privacy = PrivacyMode()
    errors: list[BaseException] = []
    counter = {"calls": 0}
    counter_lock = threading.Lock()

    def listener(_: bool) -> None:
        with counter_lock:
            counter["calls"] += 1

    privacy.on_change(listener)

    def worker(flip_to: bool) -> None:
        try:
            for _ in range(50):
                if flip_to:
                    privacy.enable()
                else:
                    privacy.disable()
        except BaseException as exc:  # pragma: no cover - safety net
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i % 2 == 0,))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), "Privacy_Mode toggle deadlocked"

    assert errors == []

    # Final deterministic write so we can assert end state exactly.
    privacy.disable()
    assert privacy.is_active() is False
