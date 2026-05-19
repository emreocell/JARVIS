"""Unit tests for :mod:`runtime.clipboard`.

These tests exercise the platform-independent behaviour of the
Clipboard_Manager: layered ring buffers (RAM 30 / disk 100), JSON
persistence round-trip, Privacy_Mode pause/resume, recall ordering, and
consecutive-duplicate dedup. The Win32 ``AddClipboardFormatListener``
loop is covered by an integration smoke test that runs only on Windows.

Validates Requirements:
    22.1 — RAM ring buffer of 30 entries
    22.2 — disk archive of 100 entries
    22.4 — recall round-trip
    22.5 — Privacy_Mode pause
    27.3 — Clipboard_Manager honours Privacy_Mode
"""

# Feature: jarvis-v2-upgrade, Task 13.1 — runtime/clipboard.py

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Callable

import pytest

from runtime.clipboard import (
    DEFAULT_DISK_CAPACITY,
    DEFAULT_RAM_CAPACITY,
    ClipboardManager,
)
from runtime.privacy_mode import PrivacyMode
from runtime.types import ClipboardEntry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(
    tmp_path: Path,
    *,
    privacy: PrivacyMode | None = None,
    ram: int = DEFAULT_RAM_CAPACITY,
    disk: int = DEFAULT_DISK_CAPACITY,
) -> ClipboardManager:
    """Build a manager with persistence pointed at a per-test tmpdir."""
    return ClipboardManager(
        privacy=privacy,
        history_path=tmp_path / "memory" / "clipboard_history.json",
        ram_capacity=ram,
        disk_capacity=disk,
    )


def _record(mgr: ClipboardManager, text: str, *, ts: float | None = None) -> bool:
    """Inject an entry without going through Win32. The listener path is
    a thin wrapper around ``_record_entry`` so testing the core directly
    keeps these tests cross-platform."""
    return mgr._record_entry(text, source_app="test", now=ts)


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_default_capacities_match_design(tmp_path: Path) -> None:
    """RAM 30 / disk 100 are the canonical defaults from Req 22.1/22.2."""
    mgr = _make_manager(tmp_path)
    assert DEFAULT_RAM_CAPACITY == 30
    assert DEFAULT_DISK_CAPACITY == 100
    assert mgr._ram_capacity == 30
    assert mgr._disk_capacity == 100


def test_disk_capacity_must_be_at_least_ram(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_manager(tmp_path, ram=10, disk=5)


def test_ram_capacity_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _make_manager(tmp_path, ram=0, disk=10)


# ---------------------------------------------------------------------------
# RAM ring buffer (Req 22.1)
# ---------------------------------------------------------------------------


def test_ram_history_returns_newest_first(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    for i in range(5):
        _record(mgr, f"entry-{i}", ts=1000.0 + i)

    history = mgr.history(count=10)
    assert [e.text for e in history] == [
        "entry-4",
        "entry-3",
        "entry-2",
        "entry-1",
        "entry-0",
    ]


def test_history_count_is_clamped_to_ram_capacity(tmp_path: Path) -> None:
    """Even if the disk archive holds more, the live view is capped at RAM."""
    mgr = _make_manager(tmp_path, ram=5, disk=20)

    for i in range(20):
        _record(mgr, f"item-{i}", ts=1000.0 + i)

    history = mgr.history(count=100)
    # Only the most recent 5 should surface.
    assert len(history) == 5
    assert [e.text for e in history] == [
        "item-19",
        "item-18",
        "item-17",
        "item-16",
        "item-15",
    ]


def test_history_count_zero_or_negative_returns_empty(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "x")
    assert mgr.history(count=0) == []
    assert mgr.history(count=-1) == []


def test_ram_buffer_evicts_oldest_when_full(tmp_path: Path) -> None:
    """Ring buffer behaviour — newer entries push older ones out."""
    mgr = _make_manager(tmp_path, ram=3, disk=3)

    for i in range(10):
        _record(mgr, f"v-{i}", ts=1000.0 + i)

    history = mgr.history(count=10)
    assert [e.text for e in history] == ["v-9", "v-8", "v-7"]


# ---------------------------------------------------------------------------
# Disk archive (Req 22.2)
# ---------------------------------------------------------------------------


def test_disk_persists_between_instances(tmp_path: Path) -> None:
    history_path = tmp_path / "memory" / "clipboard_history.json"

    mgr1 = ClipboardManager(history_path=history_path)
    _record(mgr1, "alpha", ts=1000.0)
    _record(mgr1, "beta", ts=1001.0)

    # New instance reads back what the first wrote.
    mgr2 = ClipboardManager(history_path=history_path)
    history = mgr2.history()
    assert [e.text for e in history] == ["beta", "alpha"]


def test_disk_archive_caps_at_disk_capacity(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path, ram=5, disk=10)

    for i in range(25):
        _record(mgr, f"d-{i}", ts=1000.0 + i)

    history_path = tmp_path / "memory" / "clipboard_history.json"
    with history_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)

    assert len(payload) == 10
    assert [row["text"] for row in payload] == [f"d-{i}" for i in range(15, 25)]


def test_persistence_handles_unreadable_history_file(tmp_path: Path) -> None:
    """A corrupt JSON snapshot must not break startup."""
    history_path = tmp_path / "memory" / "clipboard_history.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_text("{not valid json", encoding="utf-8")

    mgr = ClipboardManager(history_path=history_path)
    assert mgr.history() == []

    # And the next legitimate write should still succeed.
    _record(mgr, "fresh")
    assert mgr.history()[0].text == "fresh"


def test_persistence_creates_parent_directories(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "nested" / "history.json"
    mgr = ClipboardManager(history_path=nested)
    _record(mgr, "first")
    assert nested.is_file()


# ---------------------------------------------------------------------------
# Privacy_Mode integration (Req 22.5, 27.3)
# ---------------------------------------------------------------------------


def test_recording_paused_while_privacy_active(tmp_path: Path) -> None:
    privacy = PrivacyMode()
    mgr = _make_manager(tmp_path, privacy=privacy)

    privacy.enable()
    accepted = _record(mgr, "secret")

    assert accepted is False
    assert mgr.history() == []
    assert mgr.is_paused() is True


def test_recording_resumes_after_privacy_disabled(tmp_path: Path) -> None:
    privacy = PrivacyMode()
    mgr = _make_manager(tmp_path, privacy=privacy)

    privacy.enable()
    _record(mgr, "muted")  # dropped
    privacy.disable()
    _record(mgr, "audible")

    assert [e.text for e in mgr.history()] == ["audible"]
    assert mgr.is_paused() is False


def test_no_disk_writes_while_privacy_active(tmp_path: Path) -> None:
    privacy = PrivacyMode()
    history_path = tmp_path / "memory" / "clipboard_history.json"
    mgr = ClipboardManager(privacy=privacy, history_path=history_path)

    privacy.enable()
    _record(mgr, "should not persist")

    assert not history_path.exists()


def test_set_privacy_with_explicit_gate_forwards_to_privacy(
    tmp_path: Path,
) -> None:
    privacy = PrivacyMode()
    mgr = _make_manager(tmp_path, privacy=privacy)

    mgr.set_privacy(True)
    assert privacy.is_active() is True
    assert mgr.is_paused() is True

    mgr.set_privacy(False)
    assert privacy.is_active() is False
    assert mgr.is_paused() is False


def test_set_privacy_without_gate_uses_local_flag(tmp_path: Path) -> None:
    """Without a Privacy_Mode instance the manual flag is honoured."""
    mgr = _make_manager(tmp_path)

    mgr.set_privacy(True)
    accepted = _record(mgr, "still secret")
    assert accepted is False
    assert mgr.history() == []
    assert mgr.is_paused() is True

    mgr.set_privacy(False)
    _record(mgr, "now ok")
    assert [e.text for e in mgr.history()] == ["now ok"]


# ---------------------------------------------------------------------------
# Recall (Req 22.4)
# ---------------------------------------------------------------------------


def test_recall_returns_text_at_index(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "a", ts=1000.0)
    _record(mgr, "b", ts=1001.0)
    _record(mgr, "c", ts=1002.0)

    captured: list[str] = []
    monkeypatch.setattr(
        ClipboardManager,
        "_write_clipboard_text",
        lambda self, text: captured.append(text),
    )

    assert mgr.recall(0) == "c"
    assert mgr.recall(1) == "b"
    assert mgr.recall(2) == "a"
    assert captured == ["c", "b", "a"]


def test_recall_out_of_range_raises(tmp_path: Path, monkeypatch) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "only")

    monkeypatch.setattr(
        ClipboardManager,
        "_write_clipboard_text",
        lambda self, text: None,
    )

    with pytest.raises(IndexError):
        mgr.recall(5)


def test_recall_negative_raises(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "x")
    with pytest.raises(IndexError):
        mgr.recall(-1)


def test_recall_empty_history_raises(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    with pytest.raises(IndexError):
        mgr.recall(0)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_text_is_not_recorded(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    assert _record(mgr, "") is False
    assert mgr.history() == []


def test_consecutive_duplicates_are_dropped(tmp_path: Path) -> None:
    """Echoes from ``recall()`` and noisy app-side WM_CLIPBOARDUPDATE
    bursts should not pollute the buffer."""
    mgr = _make_manager(tmp_path)
    _record(mgr, "same", ts=1000.0)
    _record(mgr, "same", ts=1001.0)
    _record(mgr, "same", ts=1002.0)

    history = mgr.history()
    assert len(history) == 1
    assert history[0].text == "same"
    # Original timestamp preserved — duplicate did not refresh it.
    assert history[0].created_at == 1000.0


def test_non_consecutive_duplicates_are_kept(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "alpha", ts=1000.0)
    _record(mgr, "beta", ts=1001.0)
    _record(mgr, "alpha", ts=1002.0)

    assert [e.text for e in mgr.history()] == ["alpha", "beta", "alpha"]


def test_dedup_can_be_disabled(tmp_path: Path) -> None:
    mgr = ClipboardManager(
        history_path=tmp_path / "memory" / "history.json",
        deduplicate_consecutive=False,
    )
    mgr._record_entry("same", now=1000.0)
    mgr._record_entry("same", now=1001.0)

    assert len(mgr.history()) == 2


def test_load_skips_malformed_rows(tmp_path: Path) -> None:
    """Old / forward-compatible JSON shapes must not break the load."""
    history_path = tmp_path / "memory" / "history.json"
    history_path.parent.mkdir(parents=True)
    history_path.write_text(
        json.dumps(
            [
                {"text": "good", "created_at": 1.0, "source_app": "ok"},
                {"text": ""},  # empty — drop
                {"created_at": 2.0},  # missing text — drop
                "not a dict",  # wrong shape — drop
                {
                    "text": "stringy ts",
                    "created_at": "not-a-number",
                    "source_app": 5,  # wrong type
                },
            ]
        ),
        encoding="utf-8",
    )

    mgr = ClipboardManager(history_path=history_path)
    history = mgr.history()
    texts = [e.text for e in history]
    assert "good" in texts
    assert "stringy ts" in texts
    # Coerced values still produce a valid ClipboardEntry.
    bad_ts = next(e for e in history if e.text == "stringy ts")
    assert bad_ts.created_at == 0.0
    assert bad_ts.source_app == ""


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_stop_is_safe_when_never_started(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    # Should not raise; nothing to tear down.
    mgr.stop()


@pytest.mark.skipif(os.name != "nt", reason="Win32 listener only runs on Windows")
def test_listener_starts_and_stops_on_windows(tmp_path: Path) -> None:
    """Smoke test the real Win32 message pump on Windows hosts.

    We can't deterministically generate WM_CLIPBOARDUPDATE here without
    risking flake from other clipboard apps, so this is intentionally a
    "does the thread come up and shut down cleanly" check.
    """
    mgr = _make_manager(tmp_path)
    try:
        mgr.start()
        # Give the pump a beat to register its hwnd.
        import time as _time

        for _ in range(20):
            if mgr._listener_hwnd is not None:
                break
            _time.sleep(0.05)
        assert mgr._listener_hwnd is not None
        assert mgr._listener_thread is not None
        assert mgr._listener_thread.is_alive()
    finally:
        mgr.stop()
        assert mgr._listener_thread is None or not mgr._listener_thread.is_alive()


def test_start_is_no_op_off_windows(tmp_path: Path) -> None:
    """On Linux/macOS dev boxes ``start()`` must not raise."""
    if os.name == "nt":
        pytest.skip("Behaviour only meaningful off Windows")
    mgr = _make_manager(tmp_path)
    mgr.start()  # must not raise
    assert mgr._listener_thread is None


# ---------------------------------------------------------------------------
# ClipboardEntry shape
# ---------------------------------------------------------------------------


def test_recorded_entry_carries_metadata(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)
    _record(mgr, "metadata-check", ts=1234.5)

    entry = mgr.history()[0]
    assert isinstance(entry, ClipboardEntry)
    assert entry.text == "metadata-check"
    assert entry.created_at == 1234.5
    assert entry.source_app == "test"


# ---------------------------------------------------------------------------
# PII masking in history() view (Req 8.7, 8.8)
# ---------------------------------------------------------------------------


def test_history_applies_pii_mask_to_view(tmp_path: Path, monkeypatch) -> None:
    """history() must return masked text when a PII provider is registered.

    The backing buffer must remain unchanged so recall() still returns
    the original text.
    """
    import skills.safety.pii as pii_mod

    # Register a simple provider that replaces digits with [PII:NUMBER].
    import re

    pii_mod.set_provider(lambda t: re.sub(r"\d+", "[PII:NUMBER]", t))
    try:
        mgr = _make_manager(tmp_path)
        _record(mgr, "call 12345 now", ts=1000.0)

        history = mgr.history()
        assert len(history) == 1
        # View is masked.
        assert history[0].text == "call [PII:NUMBER] now"
        # Backing buffer is untouched — recall returns original.
        assert mgr._buffer[-1].text == "call 12345 now"
    finally:
        pii_mod.set_provider(None)


def test_history_is_noop_when_no_pii_provider(tmp_path: Path) -> None:
    """Without a registered provider history() returns text unchanged."""
    import skills.safety.pii as pii_mod

    # Ensure no provider is set.
    pii_mod.set_provider(None)

    mgr = _make_manager(tmp_path)
    _record(mgr, "plain text 999", ts=1000.0)

    history = mgr.history()
    assert history[0].text == "plain text 999"


def test_record_entry_does_not_modify_buffer_text(tmp_path: Path) -> None:
    """_record_entry() must store the raw text regardless of PII provider."""
    import skills.safety.pii as pii_mod

    pii_mod.set_provider(lambda t: "[MASKED]")
    try:
        mgr = _make_manager(tmp_path)
        _record(mgr, "sensitive data", ts=1000.0)

        # Buffer holds original.
        assert mgr._buffer[-1].text == "sensitive data"
        # history() view is masked.
        assert mgr.history()[0].text == "[MASKED]"
    finally:
        pii_mod.set_provider(None)
