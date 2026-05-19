"""Task_Manager — Background_Task lifecycle and execution.

Implements the asynchronous core described in ``design.md`` § Task_Manager.
The manager owns a ``ThreadPoolExecutor`` (default ``max_workers=4``, never
fewer than 3 to satisfy Requirement 1.6) and is the single source of truth
for every Background_Task's state and timestamps.

Responsibilities
----------------
- Accept ``submit(...)`` calls and schedule the handler on a worker thread.
- Drive the legal state machine (``queued → running → {succeeded | failed |
  cancelled} → announced``) via ``BackgroundTask.transition_to``; any other
  transition raises ``RuntimeError`` from the dataclass itself.
- Provide cooperative cancellation through ``BackgroundTask.cancel_event``;
  long NVIDIA REST loops should poll the event and abort early.
- Notify ``on_state_change`` listeners after every successful transition
  (including the initial submission). Listener errors are logged and
  swallowed so a buggy HUD never breaks the queue.
- Hand out a shared ``requests.Session`` configured for long, abortable
  NVIDIA calls.

The manager is fully thread-safe: every mutation of the task table or the
listener list happens under a single re-entrant lock. Listener callbacks
fire *outside* the lock to avoid deadlocks and slow updates blocking the
worker pool.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable

import requests

from runtime.types import BackgroundTask, TaskState

log = logging.getLogger(__name__)

# Public type aliases ------------------------------------------------------

TaskHandler = Callable[[BackgroundTask], Any]
"""Worker callable. Receives the BackgroundTask (so it can poll
``cancel_event`` and read ``args``) and returns a result that is stringified
into ``result_text``. Raising any exception transitions the task to
``failed`` (or ``cancelled`` when ``cancel_event`` is set)."""

StateChangeListener = Callable[[BackgroundTask], None]


# Default concurrency: design requires ≥3 parallel tasks (Req 1.6); we use 4
# to leave one slot for short bursts (e.g. a quick `list_background_tasks`).
DEFAULT_MAX_WORKERS = 4
MIN_PARALLEL_TASKS = 3

# How long a network read/connect should hang by default. Background NVIDIA
# calls are long, so handlers can override this; the floor here just keeps
# us from leaking sockets when a handler forgets to set anything.
DEFAULT_HTTP_TIMEOUT_SEC = 60.0


class TaskManager:
    """Schedule, track, and cancel Background_Tasks.

    Parameters
    ----------
    max_workers:
        ThreadPoolExecutor pool size. Must be at least :data:`MIN_PARALLEL_TASKS`
        (3) so we can guarantee Requirement 1.6 (≥3 concurrent tasks).
    """

    def __init__(self, max_workers: int = DEFAULT_MAX_WORKERS) -> None:
        if max_workers < MIN_PARALLEL_TASKS:
            raise ValueError(
                f"max_workers must be >= {MIN_PARALLEL_TASKS} to satisfy "
                f"the ≥3 parallel task requirement (got {max_workers})"
            )
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="jarvis-task"
        )
        self._tasks: dict[str, BackgroundTask] = {}
        self._futures: dict[str, Future[None]] = {}
        self._listeners: list[StateChangeListener] = []
        self._lock = threading.RLock()
        self._session: requests.Session | None = None
        self._closed = False

    # ------------------------------------------------------------------ submit

    def submit(
        self,
        name: str,
        handler: TaskHandler,
        args: dict | None = None,
        *,
        skill_id: str = "",
    ) -> BackgroundTask:
        """Register a new Background_Task and dispatch it to the pool.

        Returns the freshly-created :class:`BackgroundTask` (in ``queued``
        state). State change listeners are notified immediately so the
        Task_Dock can render the queued row before the worker picks it up.
        """
        if self._closed:
            raise RuntimeError("TaskManager is shut down; cannot submit new tasks")

        task = BackgroundTask(
            id=uuid.uuid4().hex[:12],
            name=name,
            args=dict(args or {}),
            skill_id=skill_id,
        )

        with self._lock:
            self._tasks[task.id] = task
            future = self._executor.submit(self._run_task, task, handler)
            self._futures[task.id] = future

        # Treat the submission as the "queued" lifecycle event so HUD/Task_Dock
        # listeners can paint the row before _run_task acquires the lock.
        self._notify(task)
        return task

    # ------------------------------------------------------------------ cancel

    def cancel(self, task_id: str) -> bool:
        """Signal cancellation of a non-terminal task.

        Returns ``True`` if the task existed and was non-terminal at the
        time of the call (i.e. a cancel signal was raised). Returns
        ``False`` if the task is unknown or already in a terminal state
        (Req 4.5).

        Behaviour:

        * The task's ``cancel_event`` is always set first so any handler
          currently polling sees the request as soon as possible.
        * If the task is still ``queued`` and the underlying ``Future``
          has not started, we cancel the future and drive the task
          through ``running → cancelled`` ourselves (the state machine
          forbids ``queued → cancelled`` directly). The ``running`` step
          is bookkeeping only; the handler never executes.
        * If the worker is already in flight, we rely on the worker's
          exit path to transition ``running → cancelled`` once the
          handler unwinds.
        """
        promote_to_cancelled = False
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or task.is_terminal:
                return False

            task.cancel_event.set()

            if task.state is TaskState.QUEUED:
                future = self._futures.get(task_id)
                if future is not None and future.cancel():
                    # Executor will skip this future entirely, so the
                    # manager owns the rest of the lifecycle. We have to
                    # walk through RUNNING because the state machine
                    # forbids QUEUED → CANCELLED directly.
                    task.transition_to(TaskState.RUNNING)
                    promote_to_cancelled = True

        if promote_to_cancelled:
            # Notify the brief RUNNING flip first, then the terminal CANCELLED.
            self._notify(task)
            with self._lock:
                task.transition_to(TaskState.CANCELLED)
            self._notify(task)

        return True

    # --------------------------------------------------------------------- get

    def get(self, task_id: str) -> BackgroundTask | None:
        """Return the live BackgroundTask record, or ``None`` if unknown."""
        with self._lock:
            return self._tasks.get(task_id)

    # ------------------------------------------------------------- list_recent

    def list_recent(
        self, minutes: int = 30, *, now: float | None = None
    ) -> list[BackgroundTask]:
        """Tasks created within the last ``minutes`` minutes.

        Sorted newest-first by ``created_at``. Negative or zero values of
        ``minutes`` collapse to ``0`` (only return tasks created at or
        after ``now``); this keeps the function defensive against caller
        typos without raising. The default of 30 minutes mirrors
        Requirement 4.2.
        """
        threshold_now = time.time() if now is None else now
        threshold = threshold_now - max(0, minutes) * 60.0
        with self._lock:
            recent = [t for t in self._tasks.values() if t.created_at >= threshold]
        recent.sort(key=lambda t: t.created_at, reverse=True)
        return recent

    # ----------------------------------------------------------- listener API

    def on_state_change(self, cb: StateChangeListener) -> None:
        """Register a listener invoked after every successful transition.

        Listeners are called outside the manager's lock with the affected
        task. Any exception raised by a listener is logged and swallowed
        so a single broken consumer cannot stall the queue.
        """
        with self._lock:
            self._listeners.append(cb)

    # ------------------------------------------------------------- announced

    def mark_announced(self, task_id: str) -> bool:
        """Flip a succeeded/failed task to ``announced``.

        Returns ``True`` on success, ``False`` if the task is unknown or
        not currently in ``succeeded``/``failed``. Used by Result_Announcer
        once a task's outcome has been read out to the user (Req 3.4).
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if task.state not in (TaskState.SUCCEEDED, TaskState.FAILED):
                return False
            task.transition_to(TaskState.ANNOUNCED)
        self._notify(task)
        return True

    # ----------------------------------------------------- HTTP helpers

    def shared_session(self) -> requests.Session:
        """Return the manager's shared ``requests.Session``.

        The session has retries disabled on purpose: long NVIDIA calls
        should be aborted via the task's ``cancel_event`` rather than
        retried opaquely. Handlers are expected to pass an explicit
        ``timeout`` (or use :data:`DEFAULT_HTTP_TIMEOUT_SEC`) and to poll
        ``task.cancel_event.is_set()`` between requests so a user-issued
        ``cancel_background_task`` can short-circuit the loop.
        """
        with self._lock:
            if self._session is None:
                self._session = requests.Session()
            return self._session

    # ---------------------------------------------------------------- shutdown

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = True) -> None:
        """Stop accepting new work and tear down the pool.

        ``cancel_pending=True`` signals every non-terminal task and asks the
        executor to drop queued futures. ``wait=True`` blocks until the
        currently running workers exit so callers (e.g. Tray_Agent's "Çıkış")
        can rely on a clean shutdown.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if cancel_pending:
                for tid, t in self._tasks.items():
                    if not t.is_terminal:
                        t.cancel_event.set()
                        fut = self._futures.get(tid)
                        if fut is not None:
                            fut.cancel()

        # cancel_futures only exists since Python 3.9 but we target 3.11+.
        self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)

        with self._lock:
            if self._session is not None:
                try:
                    self._session.close()
                except Exception:  # pragma: no cover - defensive cleanup
                    log.exception("TaskManager: failed to close shared session")
                self._session = None

    # ============================================================== internals

    def _notify(self, task: BackgroundTask) -> None:
        """Fan out a state change to every registered listener.

        Snapshots the listener list under the lock and then dispatches
        outside the lock so listeners can call back into the manager
        without deadlocking, and so a slow listener does not stall the
        worker thread that triggered the transition.
        """
        with self._lock:
            listeners = list(self._listeners)
        for cb in listeners:
            try:
                cb(task)
            except Exception:
                log.exception(
                    "TaskManager listener raised for task %s (%s -> %s)",
                    task.id,
                    task.name,
                    task.state,
                )

    def _run_task(self, task: BackgroundTask, handler: TaskHandler) -> None:
        """Worker entry point invoked by the ThreadPoolExecutor."""

        # 1. Cancel-before-start: cancel() may have set the event and tried
        #    to cancel the future, but the executor still scheduled us
        #    (race). Walk through RUNNING → CANCELLED so the listener gets
        #    a coherent story. We must hold the lock to evaluate
        #    `is_terminal` because cancel() may have already promoted us.
        if task.cancel_event.is_set():
            self._finalise_cancelled_before_run(task)
            return

        # 2. Start running.
        with self._lock:
            if task.is_terminal:
                # cancel() promoted us to CANCELLED while we were entering.
                return
            task.transition_to(TaskState.RUNNING)
        self._notify(task)

        # 3. Execute the handler outside the lock so concurrent submits
        #    and queries are not blocked by long-running work.
        try:
            result = handler(task)
        except BaseException as exc:
            self._finalise_failure(task, exc)
            return

        self._finalise_success(task, result)

    def _finalise_cancelled_before_run(self, task: BackgroundTask) -> None:
        with self._lock:
            if task.is_terminal:
                return
            task.transition_to(TaskState.RUNNING)
        self._notify(task)
        with self._lock:
            if task.is_terminal:
                return
            task.transition_to(TaskState.CANCELLED)
        self._notify(task)

    def _finalise_failure(self, task: BackgroundTask, exc: BaseException) -> None:
        with self._lock:
            if task.is_terminal:
                # Some other path (cancel + future.cancel) already terminated us.
                return
            if task.cancel_event.is_set():
                # An exception raised while cancellation is pending is
                # treated as a cooperative cancel, not a real failure.
                task.transition_to(TaskState.CANCELLED)
            else:
                task.transition_to(
                    TaskState.FAILED,
                    error_text=f"{type(exc).__name__}: {exc}",
                )
        self._notify(task)

    def _finalise_success(self, task: BackgroundTask, result: Any) -> None:
        result_text = "" if result is None else str(result)
        with self._lock:
            if task.is_terminal:
                return
            if task.cancel_event.is_set():
                # Cancel signalled mid-flight; honour it even if the handler
                # ignored the event and produced a result.
                task.transition_to(TaskState.CANCELLED)
            else:
                task.transition_to(TaskState.SUCCEEDED, result_text=result_text)
        self._notify(task)


__all__ = [
    "TaskManager",
    "TaskHandler",
    "StateChangeListener",
    "DEFAULT_MAX_WORKERS",
    "MIN_PARALLEL_TASKS",
    "DEFAULT_HTTP_TIMEOUT_SEC",
]
