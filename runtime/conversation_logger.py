"""Conversation logger for the JARVIS v2 runtime.

Writes one JSONL line per turn to ``logs/conversation/{YYYY-MM-DD}.jsonl``.
Each line is a serialised ``ConversationLogEntry`` (see ``runtime/types.py``)
and contains the role (``user``/``assistant``/``tool``/``system``), text,
optional ``tool_name`` / ``task_id``, and an ISO-8601 timestamp.

Behaviour summary (design.md § "Conversation Logger" + tasks.md 6.2):

- One file per UTC date; appended atomically under a lock.
- On construction the logger removes files older than ``retention_days``
  (default 7) so the conversation archive does not grow unbounded.
- When Privacy_Mode is active no new entries are written; ``log()`` returns
  ``False`` and the in-memory ``privacy_skip_count`` increments so the
  ``search_history`` tool (Task 6.3) can surface the gap to the user
  (Req 28.3).
- Before each write the entry text is passed through
  :func:`skills.safety.pii.mask` so PII never lands on disk when the
  Safety_Skill is loaded. When the skill is absent the wrapper is a
  no-op (identity), preserving the keyless behaviour. The masking is
  applied *after* the Privacy_Mode gate so it only runs on writes that
  will actually persist (jarvis-nvidia-skill-pack design § "Conversation
  Logger ve Clipboard_Manager ile etkileşim", Task 22.1).

Requirements: 28.1, 28.3, 27.2 (jarvis-v2-upgrade) and 8.7, 8.8, 16.1
(jarvis-nvidia-skill-pack).
"""

# Feature: jarvis-v2-upgrade, Conversation Logger (Task 6.2)
# Feature: jarvis-nvidia-skill-pack, PII pipeline wiring (Task 22.1)

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from runtime.types import ConversationLogEntry, ConversationRole
from skills.safety import pii as _safety_pii


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class PrivacyGate(Protocol):
    """Minimal Privacy_Mode contract the logger depends on.

    Only ``is_active()`` is required so the logger stays decoupled from the
    full Privacy_Mode implementation in ``runtime/privacy_mode.py``.
    """

    def is_active(self) -> bool:  # pragma: no cover - protocol body
        ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    """UTC ``datetime`` with timezone info attached.

    Wrapped in a function so tests can monkeypatch ``utcnow`` without
    touching the standard library.
    """
    return datetime.now(tz=timezone.utc)


def _iso_now() -> str:
    """ISO-8601 timestamp string (microsecond precision, UTC)."""
    return _utc_now().isoformat()


def _date_part_from_ts(ts: str) -> str:
    """Return the ``YYYY-MM-DD`` prefix of an ISO-8601 timestamp.

    Falls back to today's UTC date when ``ts`` does not start with a valid
    date prefix; this keeps malformed entries grouped instead of crashing.
    """
    if len(ts) >= 10:
        candidate = ts[:10]
        try:
            datetime.strptime(candidate, "%Y-%m-%d")
            return candidate
        except ValueError:
            pass
    return _utc_now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# ConversationLogger
# ---------------------------------------------------------------------------


class ConversationLogger:
    """Thread-safe JSONL writer for conversation history.

    The logger is instantiated once per ``JarvisLive`` session and shared
    across Voice_Core, Tool_Runtime and Result_Announcer.
    """

    DEFAULT_LOG_DIR = Path("logs") / "conversation"
    DEFAULT_RETENTION_DAYS = 7

    def __init__(
        self,
        log_dir: Path | str | None = None,
        *,
        privacy: PrivacyGate | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
        cleanup_on_start: bool = True,
    ) -> None:
        self._log_dir = Path(log_dir) if log_dir is not None else self.DEFAULT_LOG_DIR
        self._privacy = privacy
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._privacy_skip_count = 0

        self._log_dir.mkdir(parents=True, exist_ok=True)

        if cleanup_on_start:
            self.cleanup_old_logs()

    # -- public properties --------------------------------------------------

    @property
    def log_dir(self) -> Path:
        """Directory where dated ``.jsonl`` files live."""
        return self._log_dir

    @property
    def retention_days(self) -> int:
        """Number of days to keep on disk."""
        return self._retention_days

    @property
    def privacy_skip_count(self) -> int:
        """How many ``log()`` calls were dropped because Privacy was active.

        ``search_history`` (Task 6.3) can read this and surface a hint in
        its result text so the user knows recordings were paused
        (Req 28.3).
        """
        return self._privacy_skip_count

    def reset_privacy_skip_count(self) -> None:
        """Clear the skip counter (used by tests and by ``search_history``
        once the gap has been reported)."""
        self._privacy_skip_count = 0

    # -- write API ----------------------------------------------------------

    def log(self, entry: ConversationLogEntry) -> bool:
        """Append ``entry`` to today's JSONL file.

        Returns:
            ``True`` if the line was written, ``False`` if Privacy_Mode is
            active and the entry was skipped (Req 27.2, 28.3).
        """
        if self._privacy is not None and self._privacy.is_active():
            self._privacy_skip_count += 1
            return False

        # Mask PII before persisting. ``safety.pii.mask`` is a no-op when
        # the Safety_Skill has not registered a provider, so the keyless
        # path is unchanged. We only swap ``entry.text`` when the masked
        # value actually differs to keep dataclass identity stable for
        # callers who reuse the entry afterwards.
        masked_text = _safety_pii.mask(entry.text)
        if masked_text != entry.text:
            entry = ConversationLogEntry(
                ts=entry.ts,
                role=entry.role,
                text=masked_text,
                tool_name=entry.tool_name,
                task_id=entry.task_id,
            )

        date_part = _date_part_from_ts(entry.ts)
        path = self._log_dir / f"{date_part}.jsonl"
        payload = json.dumps(asdict(entry), ensure_ascii=False)

        with self._lock:
            # ``a`` mode + a single ``write`` keeps the JSONL line atomic
            # for typical entry sizes; the lock prevents interleaving with
            # cleanup.
            with path.open("a", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
        return True

    def log_message(
        self,
        role: ConversationRole,
        text: str,
        *,
        tool_name: str | None = None,
        task_id: str | None = None,
        ts: str | None = None,
    ) -> bool:
        """Convenience wrapper that builds a ``ConversationLogEntry`` and
        forwards it to :meth:`log`."""
        entry = ConversationLogEntry(
            ts=ts if ts is not None else _iso_now(),
            role=role,
            text=text,
            tool_name=tool_name,
            task_id=task_id,
        )
        return self.log(entry)

    # -- read helpers (used later by search_history, but useful here too) ---

    def iter_entries(
        self, *, dates: Iterable[str] | None = None
    ) -> Iterable[ConversationLogEntry]:
        """Yield entries from the log directory.

        ``dates`` filters by ``YYYY-MM-DD`` file stem. When ``None`` every
        ``.jsonl`` file in the directory is read in lexicographic (i.e.
        chronological) order.
        """
        if dates is None:
            files = sorted(self._log_dir.glob("*.jsonl"))
        else:
            files = [self._log_dir / f"{d}.jsonl" for d in dates]

        for path in files:
            if not path.is_file():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        # A corrupt line should not stop iteration.
                        continue
                    yield ConversationLogEntry(
                        ts=str(data.get("ts", "")),
                        role=data.get("role", "user"),
                        text=str(data.get("text", "")),
                        tool_name=data.get("tool_name"),
                        task_id=data.get("task_id"),
                    )

    # -- maintenance --------------------------------------------------------

    def cleanup_old_logs(self, *, now: datetime | None = None) -> int:
        """Delete ``.jsonl`` files older than ``retention_days``.

        Returns the number of files deleted. Files whose stem cannot be
        parsed as ``YYYY-MM-DD`` are left in place; this protects unrelated
        artefacts from accidental removal.
        """
        if self._retention_days == 0:
            # 0 means "no retention window" — keep everything.
            return 0

        cutoff = (now if now is not None else _utc_now()) - timedelta(
            days=self._retention_days
        )
        deleted = 0

        with self._lock:
            for path in self._log_dir.glob("*.jsonl"):
                try:
                    file_date = datetime.strptime(path.stem, "%Y-%m-%d").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue
                if file_date < cutoff:
                    try:
                        path.unlink()
                        deleted += 1
                    except OSError:
                        # Best-effort cleanup; another process may have
                        # the file open. Don't surface the failure.
                        continue
        return deleted


__all__ = [
    "ConversationLogger",
    "PrivacyGate",
]
