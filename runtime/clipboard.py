"""Clipboard_Manager — Windows clipboard listener and history store.

Implements the runtime side of Task 13.1 in the JARVIS v2 upgrade spec.
The manager runs a dedicated Win32 message-pump thread that owns a hidden
message-only window; ``AddClipboardFormatListener`` registers that window
for ``WM_CLIPBOARDUPDATE`` notifications, so every clipboard write the OS
produces (Ctrl+C from any app, programmatic ``SetClipboardData`` calls,
clipboard manager utilities) reaches us with no polling.

Each event is normalised into a :class:`ClipboardEntry` (text, capture
timestamp, foreground exe name) and stored in two layered ring buffers:

* an in-memory ``deque`` capped at ``ram_capacity`` (default **30**, see
  Requirement 22.1) — this is what the ``clipboard_history`` skill tool
  reads;
* a disk-backed JSON file (``memory/clipboard_history.json``) capped at
  ``disk_capacity`` (default **100**, Requirement 22.2) — entries survive
  process restarts and feed the RAM buffer on bootstrap.

Privacy_Mode integration (Requirement 22.5)
-------------------------------------------
The manager subscribes to ``PrivacyMode.on_change`` so the user toggling
privacy from the tray, hotkey or HUD propagates here without any extra
plumbing. While privacy is active every clipboard event is *dropped*
before it ever lands in the RAM buffer, and no JSON file is rewritten —
matching the "stop recording **and reading**" wording of Req 22.5 and
the "kayıt yok" half of Req 27.3. When privacy turns off the listener
naturally resumes from the next OS event; we do not back-fill the gap.

The Win32 plumbing is loaded lazily inside :meth:`start` so the module
imports cleanly on non-Windows boxes (CI runners, developer laptops).
That keeps the buffer/persistence/privacy logic — which is plain
Python — directly unit-testable without monkey-patching ``ctypes``.
"""

# Feature: jarvis-v2-upgrade, Task 13.1 — runtime/clipboard.py

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterable, Protocol, runtime_checkable

from runtime.types import ClipboardEntry

# Lazy import so the module loads cleanly even when Safety_Skill is absent.
# When Safety_Skill is not loaded, ``skills.safety.pii.mask`` is a no-op
# (identity function), so callers never need to guard against ImportError.
try:
    from skills.safety import pii as _pii_module
except ImportError:  # pragma: no cover - safety skill not installed
    _pii_module = None  # type: ignore[assignment]

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public protocol — any object exposing ``is_active()`` + ``on_change(cb)``
# can be wired in. Keeping this as a Protocol avoids a hard dependency on
# ``runtime.privacy_mode.PrivacyMode`` and lets tests pass a tiny stub.
# ---------------------------------------------------------------------------


@runtime_checkable
class PrivacyGate(Protocol):
    """Minimal Privacy_Mode contract the clipboard manager needs."""

    def is_active(self) -> bool:  # pragma: no cover - protocol body
        ...

    def on_change(self, cb) -> None:  # pragma: no cover - protocol body
        ...


# ---------------------------------------------------------------------------
# Capacities and Win32 message constants. Constants are defined at module
# level so tests can introspect them without instantiating a manager.
# ---------------------------------------------------------------------------

DEFAULT_RAM_CAPACITY = 30
"""How many entries the live in-memory history holds (Req 22.1)."""

DEFAULT_DISK_CAPACITY = 100
"""How many entries the JSON archive retains (Req 22.2)."""

DEFAULT_HISTORY_PATH = Path("memory") / "clipboard_history.json"
"""Default on-disk location, relative to the JARVIS working dir."""

# Win32 message-pump constants. Re-declared here so the listener loop
# does not need pywin32 just for these integers; the broader Win32 API
# surface is loaded lazily inside ``_listen_loop``.
_WM_CLIPBOARDUPDATE = 0x031D
_WM_DESTROY = 0x0002
_WM_QUIT = 0x0012
_HWND_MESSAGE = -3


# ---------------------------------------------------------------------------
# PII masking helper
# ---------------------------------------------------------------------------


def _mask_text(text: str) -> str:
    """Apply ``safety.pii.mask`` to ``text`` if the provider is registered.

    Falls back to identity when Safety_Skill is not loaded or when the
    import failed, so the clipboard module never raises due to a missing
    skill.
    """
    if _pii_module is None:
        return text
    try:
        return _pii_module.mask(text)
    except Exception:  # pragma: no cover - defensive
        log.exception("ClipboardManager: safety.pii.mask raised; returning original text")
        return text


# ---------------------------------------------------------------------------
# ClipboardManager
# ---------------------------------------------------------------------------


class ClipboardManager:
    """Listens for clipboard updates and exposes a recall-friendly history.

    The class is safe to construct on any platform; ``start()`` is the
    only call that requires Windows. All buffer operations are guarded by
    a re-entrant lock so callers from the message-pump thread, the
    Privacy_Mode change thread and the main asyncio loop can all read or
    mutate state without racing.
    """

    def __init__(
        self,
        *,
        privacy: PrivacyGate | None = None,
        history_path: Path | str | None = None,
        ram_capacity: int = DEFAULT_RAM_CAPACITY,
        disk_capacity: int = DEFAULT_DISK_CAPACITY,
        deduplicate_consecutive: bool = True,
    ) -> None:
        if ram_capacity <= 0:
            raise ValueError("ram_capacity must be positive")
        if disk_capacity < ram_capacity:
            raise ValueError(
                "disk_capacity must be >= ram_capacity "
                f"(got disk={disk_capacity}, ram={ram_capacity})"
            )

        self._privacy = privacy
        self._privacy_listener_attached = False
        self._manual_paused = False
        self._dedup = deduplicate_consecutive

        self._ram_capacity = ram_capacity
        self._disk_capacity = disk_capacity

        # Single backing deque holds up to ``disk_capacity`` entries; the
        # public ``history()`` view trims to ``ram_capacity``. This keeps
        # one source of truth for both layers while still honouring the
        # design's "RAM 30 / disk 100" split.
        self._buffer: deque[ClipboardEntry] = deque(maxlen=disk_capacity)

        self._history_path = (
            Path(history_path) if history_path is not None else DEFAULT_HISTORY_PATH
        )

        self._lock = threading.RLock()

        # Listener thread state — populated by ``start()``.
        self._listener_thread: threading.Thread | None = None
        self._listener_thread_id: int | None = None
        self._listener_hwnd: int | None = None
        self._wndproc_ref = None  # keep WNDPROC alive (otherwise GC kills it)
        self._stop_event = threading.Event()

        self._load_from_disk()

    # ------------------------------------------------------------------ start

    def start(self) -> None:
        """Spin up the Win32 message-pump thread.

        Idempotent: a second call while a listener is already running is a
        no-op. On non-Windows hosts the call returns immediately so unit
        tests and Linux dev environments can still construct the manager
        and exercise the buffer/privacy logic.
        """
        if os.name != "nt":
            log.debug(
                "ClipboardManager.start: skipping listener — non-Windows host (%s)",
                os.name,
            )
            return

        with self._lock:
            if self._listener_thread is not None and self._listener_thread.is_alive():
                return

            # Subscribe to Privacy_Mode exactly once. The handler is
            # intentionally a no-op: the gate is consulted on every event
            # via ``_is_paused``, so we don't need to drain or back-fill
            # anything when privacy flips. Keeping the subscription means
            # other Privacy_Mode subscribers (HUD, logger) don't need to
            # know about us.
            if (
                self._privacy is not None
                and not self._privacy_listener_attached
            ):
                try:
                    self._privacy.on_change(self._on_privacy_change)
                    self._privacy_listener_attached = True
                except Exception:  # pragma: no cover - defensive
                    log.exception(
                        "ClipboardManager: failed to subscribe to Privacy_Mode"
                    )

            self._stop_event.clear()
            t = threading.Thread(
                target=self._listen_loop,
                name="jarvis-clipboard-listener",
                daemon=True,
            )
            self._listener_thread = t
            t.start()

    # ------------------------------------------------------------------- stop

    def stop(self) -> None:
        """Tear down the message-pump thread cleanly.

        Posts ``WM_QUIT`` to the listener thread so ``GetMessageW``
        returns 0 and the thread can run its cleanup ``finally`` (remove
        clipboard listener, destroy the message-only window, unregister
        the class). Safe to call multiple times.
        """
        with self._lock:
            self._stop_event.set()
            thread = self._listener_thread
            tid = self._listener_thread_id

        if tid is not None and os.name == "nt":
            try:
                import ctypes

                ctypes.windll.user32.PostThreadMessageW(tid, _WM_QUIT, 0, 0)
            except Exception:  # pragma: no cover - defensive
                log.exception(
                    "ClipboardManager: PostThreadMessageW(WM_QUIT) failed"
                )

        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

        with self._lock:
            self._listener_thread = None
            self._listener_thread_id = None
            self._listener_hwnd = None

    # ----------------------------------------------------------- read API

    def history(self, count: int = 10) -> list[ClipboardEntry]:
        """Return the most recent entries, newest first, with PII masked.

        ``count`` is clamped to ``ram_capacity`` because Req 22.1 caps the
        live history at 30; callers who want the full disk archive should
        iterate via :meth:`iter_archive` instead.

        The raw text stored in the buffer is **never** modified; PII masking
        is applied only to the view returned here (Req 8.7, 8.8). When
        Safety_Skill is not loaded ``safety.pii.mask`` is a no-op so the
        behaviour is identical to the unmasked path.
        """
        if count <= 0:
            return []
        with self._lock:
            window = list(self._buffer)
        # Most recent ``ram_capacity`` entries, then newest-first ordering
        # for human consumption (matches how ``clipboard_history`` indexes
        # things in Task 13.2).
        window = window[-self._ram_capacity:]
        window.reverse()
        window = window[:count]

        # Apply PII mask to the display view only — the backing buffer is
        # untouched so recall() still returns the original text.
        masked = []
        for entry in window:
            masked_text = _mask_text(entry.text)
            if masked_text == entry.text:
                masked.append(entry)
            else:
                masked.append(
                    ClipboardEntry(
                        text=masked_text,
                        created_at=entry.created_at,
                        source_app=entry.source_app,
                    )
                )
        return masked

    def iter_archive(self) -> Iterable[ClipboardEntry]:
        """Yield every retained entry, oldest-first. Used by tests and by
        future audit tooling; not exposed as a tool."""
        with self._lock:
            return list(self._buffer)

    def recall(self, index: int) -> str:
        """Return the text of the entry at ``index`` and copy it back to
        the OS clipboard.

        The index is taken against the same newest-first view that
        :meth:`history` exposes, so ``recall(0)`` reproduces the most
        recent entry, ``recall(1)`` the one before it, and so on. Raises
        :class:`IndexError` for any out-of-range index.

        Re-copying the text *will* trigger our own listener once the OS
        delivers the resulting ``WM_CLIPBOARDUPDATE``; the consecutive
        deduplication guard skips that echo so the buffer is not
        polluted.
        """
        if index < 0:
            raise IndexError(f"clipboard index must be >= 0 (got {index})")

        with self._lock:
            window = list(self._buffer)
        window = window[-self._ram_capacity:]
        window.reverse()

        if index >= len(window):
            raise IndexError(
                f"clipboard index {index} out of range (have {len(window)})"
            )

        text = window[index].text
        self._write_clipboard_text(text)
        return text

    # --------------------------------------------------------- privacy API

    def set_privacy(self, active: bool) -> None:
        """Manual override for the privacy gate.

        Useful in tests and as a fallback when no Privacy_Mode instance
        is wired in. When a Privacy_Mode is present the call is forwarded
        so the rest of the app stays in sync; otherwise the manager keeps
        a local flag.
        """
        if self._privacy is not None:
            if active:
                self._privacy.enable()
            else:
                self._privacy.disable()
            return
        self._manual_paused = bool(active)

    def is_paused(self) -> bool:
        """True when new clipboard events would be dropped."""
        return self._is_paused()

    # ----------------------------------------------------------- core ingest

    def _record_entry(self, text: str, *, source_app: str = "", now: float | None = None) -> bool:
        """Insert a new entry into the layered buffer.

        Returns ``True`` if the entry was accepted. Returns ``False`` when
        the manager is paused (Privacy_Mode active or manually paused),
        when the text is empty, or when the consecutive-deduplication
        guard rejects an immediate duplicate.

        Persistence is fire-and-forget: any disk error is logged but does
        not propagate, mirroring the policy used by the other v2 runtime
        components — losing the *next* persisted snapshot is preferable
        to crashing the listener thread.
        """
        if self._is_paused():
            return False
        if not text:
            return False

        ts = time.time() if now is None else now
        entry = ClipboardEntry(
            text=text,
            created_at=ts,
            source_app=source_app,
        )

        with self._lock:
            if self._dedup and self._buffer and self._buffer[-1].text == text:
                # Echoes from our own ``recall()`` re-copy and from apps
                # that fire multiple WM_CLIPBOARDUPDATE notifications for
                # a single user action both land here. Updating the
                # timestamp would lie about when the user actually copied
                # it, so we just drop the duplicate.
                return False
            self._buffer.append(entry)

        self._persist_to_disk()
        return True

    # ----------------------------------------------------------- persistence

    def _load_from_disk(self) -> None:
        """Populate the buffer from a previous JSON snapshot, if any.

        Errors (missing file, corrupt JSON, partial schema) are
        intentionally silent: a missing/broken history is not a fatal
        condition — the next clipboard event will rebuild the file.
        """
        if not self._history_path.exists():
            return
        try:
            with self._history_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            log.warning(
                "ClipboardManager: history file %s is unreadable; ignoring",
                self._history_path,
            )
            return
        if not isinstance(data, list):
            return

        for raw in data:
            if not isinstance(raw, dict):
                continue
            text = raw.get("text")
            if not isinstance(text, str) or not text:
                continue
            created_at = raw.get("created_at", 0.0)
            try:
                created_at = float(created_at)
            except (TypeError, ValueError):
                created_at = 0.0
            source_app = raw.get("source_app", "")
            if not isinstance(source_app, str):
                source_app = ""
            self._buffer.append(
                ClipboardEntry(
                    text=text,
                    created_at=created_at,
                    source_app=source_app,
                )
            )

    def _persist_to_disk(self) -> None:
        """Write the buffer to ``history_path`` atomically.

        Honours the privacy gate: while privacy is active we never touch
        the disk, even if a stray event slipped through. Writes go to a
        ``.tmp`` sibling first and then ``Path.replace`` swaps it in
        atomically so a crashed write cannot leave a half-written JSON.
        """
        if self._is_paused():
            return

        with self._lock:
            payload = [
                {
                    "text": e.text,
                    "created_at": e.created_at,
                    "source_app": e.source_app,
                }
                for e in self._buffer
            ]

        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._history_path.with_suffix(
                self._history_path.suffix + ".tmp"
            )
            with tmp.open("w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            tmp.replace(self._history_path)
        except OSError:
            log.exception(
                "ClipboardManager: failed to persist history to %s",
                self._history_path,
            )

    # ----------------------------------------------------------- privacy gate

    def _is_paused(self) -> bool:
        if self._manual_paused:
            return True
        if self._privacy is not None:
            try:
                return bool(self._privacy.is_active())
            except Exception:  # pragma: no cover - defensive
                log.exception("ClipboardManager: privacy.is_active() raised")
                return False
        return False

    def _on_privacy_change(self, active: bool) -> None:
        """Privacy_Mode subscriber.

        We don't need to do anything here: ``_is_paused`` is consulted on
        every event, so toggling privacy on or off naturally pauses or
        resumes ingestion. The hook exists so other observers wiring
        through ``runtime/privacy_mode.py`` see a consistent fan-out and
        so we can log the transition for debugging.
        """
        log.debug(
            "ClipboardManager: privacy %s",
            "activated — events will be dropped" if active else "deactivated — resuming",
        )

    # ----------------------------------------------------------- Win32 layer

    def _on_clipboard_change(self) -> None:
        """Called from the message-pump thread for every WM_CLIPBOARDUPDATE.

        Reads the clipboard text and forwards it to ``_record_entry``.
        Non-text clipboard payloads (images, files, custom formats) are
        silently ignored — Req 22 is scoped to text recall.
        """
        if self._is_paused():
            return
        text = self._read_clipboard_text()
        if text is None:
            return
        source_app = self._foreground_app_name()
        self._record_entry(text, source_app=source_app)

    def _read_clipboard_text(self) -> str | None:
        """Pull plain text off the OS clipboard, retrying briefly.

        Other apps occasionally hold the clipboard open while we receive
        ``WM_CLIPBOARDUPDATE``; a short retry loop avoids racing with
        them. Returns ``None`` for non-text formats and on any failure
        — the listener treats that the same as "nothing to record".
        """
        try:
            import win32clipboard  # type: ignore[import-untyped]
            import win32con  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover - dependency missing
            return None

        for _ in range(3):
            try:
                win32clipboard.OpenClipboard()
            except Exception:
                time.sleep(0.05)
                continue
            try:
                if win32clipboard.IsClipboardFormatAvailable(
                    win32con.CF_UNICODETEXT
                ):
                    data = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
                    if isinstance(data, str):
                        return data
                elif win32clipboard.IsClipboardFormatAvailable(win32con.CF_TEXT):
                    raw = win32clipboard.GetClipboardData(win32con.CF_TEXT)
                    if isinstance(raw, bytes):
                        return raw.decode("utf-8", errors="replace")
                    if isinstance(raw, str):
                        return raw
                return None
            except Exception:
                return None
            finally:
                try:
                    win32clipboard.CloseClipboard()
                except Exception:  # pragma: no cover - defensive
                    pass
        return None

    def _write_clipboard_text(self, text: str) -> None:
        """Place ``text`` on the OS clipboard for ``recall``.

        Uses ``pyperclip`` because it already handles the OpenClipboard /
        retry dance and is the same library the rest of the v2 codebase
        (Command_Palette, WhatsApp_Bridge) standardises on. Failures are
        logged but not raised — a recall that cannot reach the clipboard
        still returns the text to the caller, which is the contract.
        """
        try:
            import pyperclip  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover - dependency missing
            log.warning("ClipboardManager.recall: pyperclip not installed")
            return
        try:
            pyperclip.copy(text)
        except Exception:  # pragma: no cover - depends on host
            log.exception("ClipboardManager.recall: pyperclip.copy failed")

    def _foreground_app_name(self) -> str:
        """Best-effort foreground exe name (e.g. ``Code.exe``).

        Returns ``""`` whenever any required dependency is missing or any
        Win32 call fails, so the listener never crashes over the
        diagnostic field.
        """
        try:
            import win32gui  # type: ignore[import-untyped]
            import win32process  # type: ignore[import-untyped]
        except ImportError:  # pragma: no cover - dependency missing
            return ""
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if not pid:
                return ""
        except Exception:
            return ""

        try:
            import psutil  # type: ignore[import-untyped]

            return psutil.Process(pid).name()
        except Exception:
            return ""

    def _listen_loop(self) -> None:
        """Win32 message-pump that owns a hidden message-only window.

        The window is registered with a unique class name so multiple
        managers (test isolation, feature flag rollout) can coexist. The
        ``WNDPROC`` callback dispatches ``WM_CLIPBOARDUPDATE`` to
        :meth:`_on_clipboard_change` and lets ``DefWindowProcW`` handle
        everything else. Cleanup in the ``finally`` block tears the
        listener and window down even if the pump exits unexpectedly.
        """
        if os.name != "nt":  # pragma: no cover - guarded by start()
            return

        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:  # pragma: no cover - non-Windows
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # ---- function prototypes (declared locally so the module imports
        # ---- cleanly on platforms without these DLLs) ------------------

        WNDPROCTYPE = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", ctypes.c_uint),
                ("lpfnWndProc", WNDPROCTYPE),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.RegisterClassW.restype = wintypes.ATOM

        user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        user32.UnregisterClassW.restype = wintypes.BOOL

        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.CreateWindowExW.restype = wintypes.HWND

        user32.DestroyWindow.argtypes = [wintypes.HWND]
        user32.DestroyWindow.restype = wintypes.BOOL

        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_long

        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG),
            wintypes.HWND,
            wintypes.UINT,
            wintypes.UINT,
        ]
        user32.GetMessageW.restype = wintypes.BOOL

        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.TranslateMessage.restype = wintypes.BOOL

        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.restype = ctypes.c_long

        user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.AddClipboardFormatListener.restype = wintypes.BOOL

        user32.RemoveClipboardFormatListener.argtypes = [wintypes.HWND]
        user32.RemoveClipboardFormatListener.restype = wintypes.BOOL

        user32.PostQuitMessage.argtypes = [ctypes.c_int]
        user32.PostQuitMessage.restype = None

        kernel32.GetCurrentThreadId.argtypes = []
        kernel32.GetCurrentThreadId.restype = wintypes.DWORD

        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE

        # ---- WNDPROC callback ----------------------------------------

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == _WM_CLIPBOARDUPDATE:
                try:
                    self._on_clipboard_change()
                except Exception:
                    log.exception(
                        "ClipboardManager: WM_CLIPBOARDUPDATE handler raised"
                    )
                return 0
            if msg == _WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        # ctypes is happy to GC our callback the moment the local variable
        # goes out of scope, which would crash the Win32 dispatch later.
        # Stash it on the instance so its lifetime matches the manager.
        self._wndproc_ref = WNDPROCTYPE(_wndproc)

        hinst = kernel32.GetModuleHandleW(None)
        class_name = f"JarvisClipboardListener_{id(self):x}"

        wc = WNDCLASSW()
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.lpszClassName = class_name

        atom = user32.RegisterClassW(ctypes.byref(wc))
        if not atom:
            log.error(
                "ClipboardManager: RegisterClassW failed (last_error=%d)",
                ctypes.get_last_error(),
            )
            return

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            "",
            0,
            0,
            0,
            0,
            0,
            _HWND_MESSAGE,
            None,
            hinst,
            None,
        )
        if not hwnd:
            log.error(
                "ClipboardManager: CreateWindowExW failed (last_error=%d)",
                ctypes.get_last_error(),
            )
            user32.UnregisterClassW(class_name, hinst)
            return

        if not user32.AddClipboardFormatListener(hwnd):
            log.error(
                "ClipboardManager: AddClipboardFormatListener failed (last_error=%d)",
                ctypes.get_last_error(),
            )
            user32.DestroyWindow(hwnd)
            user32.UnregisterClassW(class_name, hinst)
            return

        with self._lock:
            self._listener_hwnd = hwnd
            self._listener_thread_id = kernel32.GetCurrentThreadId()

        log.debug(
            "ClipboardManager: listener thread up (hwnd=%s tid=%s)",
            hwnd,
            self._listener_thread_id,
        )

        msg = wintypes.MSG()
        try:
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret == 0:  # WM_QUIT — stop() posted this for us
                    break
                if ret == -1:  # GetMessageW error; nothing safe to do
                    log.error(
                        "ClipboardManager: GetMessageW returned -1 "
                        "(last_error=%d); aborting listener",
                        ctypes.get_last_error(),
                    )
                    break
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            try:
                user32.RemoveClipboardFormatListener(hwnd)
            except Exception:  # pragma: no cover
                pass
            try:
                user32.DestroyWindow(hwnd)
            except Exception:  # pragma: no cover
                pass
            try:
                user32.UnregisterClassW(class_name, hinst)
            except Exception:  # pragma: no cover
                pass


__all__ = [
    "ClipboardManager",
    "ClipboardEntry",
    "PrivacyGate",
    "DEFAULT_RAM_CAPACITY",
    "DEFAULT_DISK_CAPACITY",
    "DEFAULT_HISTORY_PATH",
]
