"""Privacy_Mode — observable on/off switch for privacy-sensitive subsystems.

JARVIS v2 collects an unusual amount of ambient signal (microphone,
clipboard, conversation transcripts, debug traces). Privacy_Mode is the
single, process-wide kill switch that lets the user pause every one of
those streams at once.

This module deliberately stays *small*: it only owns the on/off state and
notifies subscribers when it flips. The actual "stop listening" / "stop
logging" work lives in each subsystem (Voice_Core, Wake_Word_Engine,
Clipboard_Manager, Conversation_Logger, debug logger, Result_Announcer)
and is wired up by reading ``privacy.is_active()`` before any sensitive
write or by registering an ``on_change`` callback.

Public contract (design.md § Privacy_Mode):

* ``enable()`` / ``disable()`` flip the state and notify listeners.
* ``is_active()`` returns the current bool — cheap, thread-safe, lock-free
  reads via an atomic-ish ``threading.Event`` snapshot.
* ``on_change(cb)`` registers a listener invoked with the new bool after
  every state flip.

Design notes
------------
* **Idempotent**: ``enable()`` while already active (or ``disable()`` while
  already inactive) is a no-op and does **not** fire listeners. This
  matters because subsystems (e.g. Conversation_Logger) often rebuild
  state on every callback; firing redundant flips would needlessly wake
  log rotation, mic resume, etc.
* **Thread-safe**: enable/disable can be called from any thread (HUD
  toggle, tray agent, hotkey thread, voice core async). State mutation
  happens under a single lock; listener callbacks fire **outside** the
  lock with a snapshot of the listener list, so a slow or buggy
  subscriber cannot block other toggles or hold the manager's lock.
* **Listener errors swallowed**: a misbehaving subscriber must not be
  able to leave Privacy_Mode in a half-applied state. We log and move on,
  matching the policy ``TaskManager`` already applies to its own
  ``on_state_change`` listeners.
* **No subsystem references**: this class never imports Voice_Core,
  Clipboard_Manager, etc. Inversion of control keeps the module
  dependency-free and trivially testable, and avoids import cycles when
  privacy-sensitive components are wired together in ``main.py``.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)


PrivacyChangeListener = Callable[[bool], None]
"""Callback signature used by ``PrivacyMode.on_change``.

Receives the *new* active state (``True`` when privacy was just turned on,
``False`` when it was just turned off). The callback runs synchronously on
the thread that called ``enable``/``disable`` but **outside** the manager's
lock; subscribers should keep the work small or hand it off to their own
thread/event loop.
"""


class PrivacyMode:
    """Process-wide privacy toggle with a small observer API.

    Instances are cheap; production wiring creates one in ``main.py`` and
    shares it with every privacy-sensitive component. The constructor
    accepts an optional ``initial`` value so the bootstrap can honour
    ``app_config.privacy_mode_default`` without an extra ``enable()``
    call (which would fire listeners before they are even subscribed).
    """

    def __init__(self, initial: bool = False) -> None:
        # ``threading.Event`` gives us a lock-free, atomic boolean read
        # path for the hot ``is_active()`` checks scattered across the
        # codebase (each tool dispatch, each clipboard event, each log
        # write). All *mutations* still go through ``_lock`` so the
        # event flip and the listener notification stay consistent with
        # each other.
        self._active = threading.Event()
        if initial:
            self._active.set()
        self._lock = threading.RLock()
        self._listeners: list[PrivacyChangeListener] = []

    # ----------------------------------------------------------- mutation API

    def enable(self) -> bool:
        """Turn privacy mode on.

        Returns ``True`` if the state actually changed (and listeners were
        notified), ``False`` if privacy was already active. Idempotent on
        purpose so callers can safely "re-assert" the state without
        causing log spam or repeated mic-pause attempts.
        """
        return self._set(True)

    def disable(self) -> bool:
        """Turn privacy mode off.

        Returns ``True`` if the state actually changed (and listeners
        were notified), ``False`` if privacy was already inactive.
        """
        return self._set(False)

    def toggle(self) -> bool:
        """Flip the state and return the **new** active value.

        Convenience helper for the tray menu / hotkey wiring where the
        caller does not care about the previous state.
        """
        with self._lock:
            new_value = not self._active.is_set()
            self._set(new_value)
            return new_value

    # --------------------------------------------------------------- read API

    def is_active(self) -> bool:
        """Return the current state. Lock-free, safe from any thread."""
        return self._active.is_set()

    # ----------------------------------------------------------- listener API

    def on_change(self, cb: PrivacyChangeListener) -> None:
        """Register ``cb`` to be invoked after every state flip.

        The callback receives the new active value. Multiple callbacks are
        invoked in registration order. Exceptions raised by a callback are
        logged and otherwise ignored so one broken subscriber cannot
        block the rest. The same callback may be registered more than
        once; callers wanting deduplication should track that themselves.
        """
        if not callable(cb):
            raise TypeError("Privacy_Mode listener must be callable")
        with self._lock:
            self._listeners.append(cb)

    def remove_listener(self, cb: PrivacyChangeListener) -> bool:
        """Unregister a previously registered listener.

        Returns ``True`` if the listener was found and removed, ``False``
        otherwise. Useful in tests and when a subsystem (e.g. a
        Clipboard_Manager) is shut down before the rest of the app.
        """
        with self._lock:
            try:
                self._listeners.remove(cb)
                return True
            except ValueError:
                return False

    # ---------------------------------------------------------------- helpers

    def _set(self, value: bool) -> bool:
        """Internal: set the state and notify, only on real transitions."""
        with self._lock:
            current = self._active.is_set()
            if current == value:
                return False
            if value:
                self._active.set()
            else:
                self._active.clear()
            # Snapshot listeners *inside* the lock so a concurrent
            # ``on_change``/``remove_listener`` cannot mutate the list
            # under us, then fire them *outside* the lock so a slow
            # subscriber cannot stall further toggles.
            listeners = tuple(self._listeners)

        for cb in listeners:
            try:
                cb(value)
            except Exception:
                # A misbehaving subscriber must not corrupt privacy state.
                log.exception("Privacy_Mode listener raised; ignoring")
        return True


__all__ = ["PrivacyMode", "PrivacyChangeListener"]
