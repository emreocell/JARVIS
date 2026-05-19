"""Unit tests for ``runtime/conversation_logger.py``.

Covers Requirements 28.1 (JSONL persistence), 28.3 / 27.2 (Privacy_Mode
suppression), the 7-day retention cleanup described by Task 6.2, and PII
masking before write (Requirements 8.7, 8.8, 16.1 — Task 22.1).
"""

# Feature: jarvis-v2-upgrade, Conversation Logger unit tests (Task 6.2)
# Feature: jarvis-nvidia-skill-pack, PII pipeline wiring (Task 22.1)

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime.conversation_logger import ConversationLogger
from runtime.types import ConversationLogEntry
import skills.safety.pii as _pii_module


# ---------------------------------------------------------------------------
# Fixtures local to this module
# ---------------------------------------------------------------------------


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    """Isolated ``logs/conversation`` directory for the logger under test."""
    target = tmp_path / "logs" / "conversation"
    target.mkdir(parents=True)
    return target


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Construction & directory handling
# ---------------------------------------------------------------------------


def test_logger_creates_log_dir_when_missing(tmp_path: Path) -> None:
    target = tmp_path / "fresh" / "conversation"
    assert not target.exists()
    ConversationLogger(target)
    assert target.is_dir()


def test_negative_retention_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        ConversationLogger(tmp_path, retention_days=-1)


# ---------------------------------------------------------------------------
# Writing JSONL entries (Req 28.1)
# ---------------------------------------------------------------------------


def test_log_writes_jsonl_to_dated_file(log_dir: Path) -> None:
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    entry = ConversationLogEntry(
        ts="2025-03-04T10:11:12+00:00",
        role="user",
        text="merhaba",
        tool_name=None,
        task_id=None,
    )
    assert logger.log(entry) is True

    target = log_dir / "2025-03-04.jsonl"
    assert target.is_file()
    rows = _read_jsonl(target)
    assert rows == [
        {
            "ts": "2025-03-04T10:11:12+00:00",
            "role": "user",
            "text": "merhaba",
            "tool_name": None,
            "task_id": None,
        }
    ]


def test_log_message_appends_chronologically(log_dir: Path) -> None:
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.log_message("user", "ilk satır", ts="2025-03-04T10:00:00+00:00")
    logger.log_message(
        "assistant",
        "ikinci satır",
        ts="2025-03-04T10:00:01+00:00",
    )
    logger.log_message(
        "tool",
        "üçüncü satır",
        tool_name="sys_info",
        task_id="abc123",
        ts="2025-03-04T10:00:02+00:00",
    )

    rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
    assert [r["text"] for r in rows] == ["ilk satır", "ikinci satır", "üçüncü satır"]
    assert rows[2]["tool_name"] == "sys_info"
    assert rows[2]["task_id"] == "abc123"


def test_log_groups_by_date_prefix(log_dir: Path) -> None:
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.log_message("user", "gün bir", ts="2025-03-04T23:59:59+00:00")
    logger.log_message("user", "gün iki", ts="2025-03-05T00:00:01+00:00")

    assert (log_dir / "2025-03-04.jsonl").is_file()
    assert (log_dir / "2025-03-05.jsonl").is_file()
    assert _read_jsonl(log_dir / "2025-03-04.jsonl")[0]["text"] == "gün bir"
    assert _read_jsonl(log_dir / "2025-03-05.jsonl")[0]["text"] == "gün iki"


def test_log_preserves_unicode(log_dir: Path) -> None:
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.log_message("user", "şçğüöı — 你好", ts="2025-03-04T10:00:00+00:00")

    rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
    assert rows[0]["text"] == "şçğüöı — 你好"


# ---------------------------------------------------------------------------
# Privacy_Mode suppression (Req 27.2, 28.3)
# ---------------------------------------------------------------------------


class _StubPrivacy:
    """Minimal Privacy_Mode stub for the logger contract."""

    def __init__(self, active: bool = False) -> None:
        self.active = active

    def is_active(self) -> bool:
        return self.active


def test_privacy_active_suppresses_writes(log_dir: Path) -> None:
    privacy = _StubPrivacy(active=True)
    logger = ConversationLogger(log_dir, privacy=privacy, cleanup_on_start=False)

    written = logger.log_message("user", "gizli", ts="2025-03-04T10:00:00+00:00")

    assert written is False
    assert list(log_dir.iterdir()) == []
    assert logger.privacy_skip_count == 1


def test_privacy_skip_counter_resets(log_dir: Path) -> None:
    privacy = _StubPrivacy(active=True)
    logger = ConversationLogger(log_dir, privacy=privacy, cleanup_on_start=False)

    logger.log_message("user", "a", ts="2025-03-04T10:00:00+00:00")
    logger.log_message("user", "b", ts="2025-03-04T10:00:01+00:00")
    assert logger.privacy_skip_count == 2

    logger.reset_privacy_skip_count()
    assert logger.privacy_skip_count == 0


def test_privacy_resumes_on_disable(log_dir: Path) -> None:
    privacy = _StubPrivacy(active=True)
    logger = ConversationLogger(log_dir, privacy=privacy, cleanup_on_start=False)

    assert logger.log_message("user", "gizli", ts="2025-03-04T10:00:00+00:00") is False
    privacy.active = False
    assert logger.log_message("user", "açık", ts="2025-03-04T10:00:01+00:00") is True

    rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
    assert [r["text"] for r in rows] == ["açık"]


# ---------------------------------------------------------------------------
# 7-day retention cleanup
# ---------------------------------------------------------------------------


def _seed_log(log_dir: Path, date: str, payload: str = "x") -> Path:
    path = log_dir / f"{date}.jsonl"
    path.write_text(payload + "\n", encoding="utf-8")
    return path


def test_cleanup_removes_files_older_than_retention(log_dir: Path) -> None:
    fresh = _seed_log(log_dir, "2025-03-10")
    edge = _seed_log(log_dir, "2025-03-04")  # exactly 7 days before
    stale = _seed_log(log_dir, "2025-03-01")
    unrelated = log_dir / "notes.txt"
    unrelated.write_text("keep me", encoding="utf-8")

    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    now = datetime(2025, 3, 11, 12, 0, 0, tzinfo=timezone.utc)
    deleted = logger.cleanup_old_logs(now=now)

    assert deleted == 2
    assert fresh.exists()
    assert not edge.exists()
    assert not stale.exists()
    assert unrelated.exists()


def test_cleanup_runs_on_construction(log_dir: Path) -> None:
    _seed_log(log_dir, "1999-01-01")
    _seed_log(log_dir, datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"))

    ConversationLogger(log_dir)

    remaining = sorted(p.name for p in log_dir.glob("*.jsonl"))
    assert "1999-01-01.jsonl" not in remaining
    assert len(remaining) == 1


def test_cleanup_zero_retention_keeps_all(log_dir: Path) -> None:
    _seed_log(log_dir, "1999-01-01")
    _seed_log(log_dir, "2000-01-01")

    logger = ConversationLogger(log_dir, retention_days=0, cleanup_on_start=False)
    deleted = logger.cleanup_old_logs(now=datetime(2030, 1, 1, tzinfo=timezone.utc))

    assert deleted == 0
    assert sorted(p.name for p in log_dir.glob("*.jsonl")) == [
        "1999-01-01.jsonl",
        "2000-01-01.jsonl",
    ]


def test_cleanup_ignores_unparseable_filenames(log_dir: Path) -> None:
    weird = log_dir / "not-a-date.jsonl"
    weird.write_text("noise\n", encoding="utf-8")

    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.cleanup_old_logs(
        now=datetime(2030, 1, 1, tzinfo=timezone.utc)
    )

    assert weird.exists()


def test_iter_entries_returns_chronological_jsonl(log_dir: Path) -> None:
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.log_message("user", "a", ts="2025-03-04T10:00:00+00:00")
    logger.log_message("assistant", "b", ts="2025-03-05T10:00:00+00:00")

    entries = list(logger.iter_entries())
    assert [e.text for e in entries] == ["a", "b"]
    assert [e.role for e in entries] == ["user", "assistant"]


def test_iter_entries_skips_corrupt_lines(log_dir: Path) -> None:
    target = log_dir / "2025-03-04.jsonl"
    target.write_text(
        '{"ts":"2025-03-04T10:00:00+00:00","role":"user","text":"good"}\n'
        "this is not json\n"
        '{"ts":"2025-03-04T10:00:01+00:00","role":"assistant","text":"also good"}\n',
        encoding="utf-8",
    )

    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    entries = list(logger.iter_entries())

    assert [e.text for e in entries] == ["good", "also good"]


# ---------------------------------------------------------------------------
# PII masking before write (Req 8.7, 8.8, 16.1 — Task 22.1)
# ---------------------------------------------------------------------------


def test_pii_mask_applied_before_write(log_dir: Path) -> None:
    """When a PII provider is registered, entry.text is masked on disk."""
    def _fake_mask(text: str) -> str:
        return text.replace("John", "[PII:NAME]")

    _pii_module.set_provider(_fake_mask)
    try:
        logger = ConversationLogger(log_dir, cleanup_on_start=False)
        logger.log_message("user", "My name is John", ts="2025-03-04T10:00:00+00:00")

        rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
        assert rows[0]["text"] == "My name is [PII:NAME]"
    finally:
        _pii_module.set_provider(None)


def test_pii_mask_noop_when_no_provider(log_dir: Path) -> None:
    """When no PII provider is registered, text is written unchanged (no-op)."""
    _pii_module.set_provider(None)
    logger = ConversationLogger(log_dir, cleanup_on_start=False)
    logger.log_message("user", "My name is John", ts="2025-03-04T10:00:00+00:00")

    rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
    assert rows[0]["text"] == "My name is John"


def test_pii_mask_not_applied_when_privacy_active(log_dir: Path) -> None:
    """When Privacy_Mode is active, log() returns False and PII mask is never called."""
    call_count = {"n": 0}

    def _counting_mask(text: str) -> str:
        call_count["n"] += 1
        return text

    _pii_module.set_provider(_counting_mask)
    try:
        privacy = _StubPrivacy(active=True)
        logger = ConversationLogger(log_dir, privacy=privacy, cleanup_on_start=False)
        result = logger.log_message("user", "secret", ts="2025-03-04T10:00:00+00:00")

        assert result is False
        # PII mask must NOT be called — privacy gate fires first
        assert call_count["n"] == 0
    finally:
        _pii_module.set_provider(None)


def test_pii_mask_exception_does_not_break_write(log_dir: Path) -> None:
    """If the PII provider raises, the original text is written (fail-open)."""
    def _broken_mask(text: str) -> str:
        raise RuntimeError("NIM unavailable")

    _pii_module.set_provider(_broken_mask)
    try:
        logger = ConversationLogger(log_dir, cleanup_on_start=False)
        result = logger.log_message("user", "hello", ts="2025-03-04T10:00:00+00:00")

        assert result is True
        rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
        assert rows[0]["text"] == "hello"
    finally:
        _pii_module.set_provider(None)


def test_pii_mask_unchanged_text_keeps_entry_identity(log_dir: Path) -> None:
    """When mask returns the same text, the entry object is not replaced."""
    def _identity_mask(text: str) -> str:
        return text  # no change

    _pii_module.set_provider(_identity_mask)
    try:
        logger = ConversationLogger(log_dir, cleanup_on_start=False)
        logger.log_message("assistant", "no pii here", ts="2025-03-04T10:00:00+00:00")

        rows = _read_jsonl(log_dir / "2025-03-04.jsonl")
        assert rows[0]["text"] == "no pii here"
    finally:
        _pii_module.set_provider(None)
