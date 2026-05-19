"""UAC error translation and per-tool rate limiting.

When a tool handler raises an exception that signals "this operation needs
elevated privileges" on Windows, the Tool_Runtime asks this module to turn
the raw exception into a friendly Turkish message that the user can hear or
read in the HUD.

Recognised error shapes (see design.md § Hata Çevirisi, Req 15.1):

- :class:`PermissionError` (Python's :pep:`3151` mapping of EACCES).
- :class:`OSError` with ``winerror`` ``5`` (``ERROR_ACCESS_DENIED``) or
  ``1314`` (``ERROR_PRIVILEGE_NOT_HELD``).
- ``pywintypes.error`` with the same Win32 error codes in its first arg.
  ``pywintypes`` is an optional dependency, so we degrade gracefully when
  the package is not installed.

Rate limiting (Req 15.3, Property 30): for any single ``tool_name``, no
more than one warning is emitted in a sliding 10-minute window. Suppressed
calls return ``None`` so the caller can fall back to its default error
handling without bothering the user a second time. The tracking dict is
guarded by a :class:`threading.Lock` because Tool_Runtime can dispatch
inline tools from worker threads.
"""

from __future__ import annotations

import threading
import time
from typing import Final

# pywintypes ships with pywin32 and is only available on Windows. Tests and
# non-Windows imports must still succeed, so we treat its absence as "no
# pywintypes errors will ever be raised here".
try:  # pragma: no cover - exercised on Windows installs.
    import pywintypes  # type: ignore[import-not-found]

    _PYWINTYPES_ERROR: type[BaseException] | None = pywintypes.error
except ImportError:  # pragma: no cover - exercised on Linux/CI.
    _PYWINTYPES_ERROR = None


#: Sliding window in seconds for the per-tool warning quota (Req 15.3).
RATE_LIMIT_SECONDS: Final[int] = 600

#: User-facing message returned for every UAC-translated error (Req 15.1, 15.2).
UAC_MESSAGE: Final[str] = (
    "Bu işlem yönetici izni gerektiriyor. "
    "JARVIS'i yönetici olarak çalıştırın."
)

# Win32 error codes that signal "needs elevation".
_ELEVATION_WINERRORS: Final[frozenset[int]] = frozenset({5, 1314})

# Maps tool_name -> timestamp of last warning emitted for that tool. Guarded
# by ``_LOCK`` for thread safety.
_LAST_WARN: dict[str, float] = {}
_LOCK = threading.Lock()


def _is_elevation_error(error: BaseException) -> bool:
    """Return True iff ``error`` represents a Windows elevation failure.

    The check is intentionally narrow: a plain ``OSError`` without a
    ``winerror`` attribute (e.g. POSIX ``EACCES`` mapped to ``OSError``) is
    only treated as elevation-related when it is a :class:`PermissionError`.
    """
    if isinstance(error, PermissionError):
        return True

    if isinstance(error, OSError):
        winerror = getattr(error, "winerror", None)
        if winerror in _ELEVATION_WINERRORS:
            return True

    if _PYWINTYPES_ERROR is not None and isinstance(error, _PYWINTYPES_ERROR):
        # pywintypes.error.args is typically (winerror, funcname, message).
        args = getattr(error, "args", ())
        if args and args[0] in _ELEVATION_WINERRORS:
            return True
        # Some call sites surface the code via a ``winerror`` attribute too.
        winerror = getattr(error, "winerror", None)
        if winerror in _ELEVATION_WINERRORS:
            return True

    return False


def translate(
    error: BaseException,
    *,
    tool_name: str = "",
    now: float | None = None,
) -> str | None:
    """Translate a UAC-related exception into a Turkish user message.

    Parameters
    ----------
    error:
        The exception raised by a tool handler.
    tool_name:
        Identifier used for rate limiting. The default empty string places
        every "anonymous" caller in a single shared bucket, which is the
        conservative choice when the caller cannot supply a name.
    now:
        Optional override for the current time, in seconds since the epoch.
        Tests use this to drive the sliding window deterministically; in
        production, leave it unset and we fall back to :func:`time.time`.

    Returns
    -------
    str | None
        The Turkish warning message when the error is elevation-related and
        the per-tool rate limit allows another emission. ``None`` when the
        error is unrelated to UAC, or when a warning for ``tool_name`` was
        already emitted within the last :data:`RATE_LIMIT_SECONDS`.
    """
    if not _is_elevation_error(error):
        return None

    ts = time.time() if now is None else now

    with _LOCK:
        last = _LAST_WARN.get(tool_name)
        if last is not None and (ts - last) < RATE_LIMIT_SECONDS:
            # Inside the suppression window; honour the quota.
            return None
        _LAST_WARN[tool_name] = ts

    return UAC_MESSAGE


def reset() -> None:
    """Clear the per-tool rate-limit cache.

    Intended for tests that want a clean slate between cases. Production
    code should not need to call this; the sliding window naturally expires
    entries as time passes.
    """
    with _LOCK:
        _LAST_WARN.clear()


__all__ = [
    "RATE_LIMIT_SECONDS",
    "UAC_MESSAGE",
    "translate",
    "reset",
]
