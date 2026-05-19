"""Common data types for the JARVIS v2 runtime.

This module hosts the dataclasses and enums that are shared across the
Task_Manager, Tool_Runtime, Plugin_Host, Result_Announcer, Routine_Engine,
Clipboard_Manager, Conversation Logger and HUD layers.

Keeping these types in one place lets every component speak the same
vocabulary without import cycles. The shapes here are the source of truth
referenced by `design.md` § "Data Models".
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Literal


# ---------------------------------------------------------------------------
# Task lifecycle
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    """Lifecycle states for a Background_Task.

    Allowed transitions (see design.md § Task_Manager):

    - ``queued -> running``
    - ``running -> succeeded``
    - ``running -> failed``
    - ``running -> cancelled``
    - ``succeeded -> announced``
    - ``failed -> announced``

    Any other transition is illegal and ``BackgroundTask.transition_to`` will
    raise ``RuntimeError``.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ANNOUNCED = "announced"


# Single source of truth for the legal state machine.
_ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.QUEUED: frozenset({TaskState.RUNNING}),
    TaskState.RUNNING: frozenset(
        {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
    ),
    TaskState.SUCCEEDED: frozenset({TaskState.ANNOUNCED}),
    TaskState.FAILED: frozenset({TaskState.ANNOUNCED}),
    TaskState.CANCELLED: frozenset(),
    TaskState.ANNOUNCED: frozenset(),
}

_TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.ANNOUNCED}
)


@dataclass
class BackgroundTask:
    """A single background unit of work tracked by the Task_Manager.

    Field invariants (enforced by ``transition_to`` and consumers):

    - Timestamps are non-decreasing: ``created_at <= started_at <= finished_at``
      for any pair of non-``None`` values.
    - ``state == SUCCEEDED`` => ``result_text`` is not ``None`` and
      ``error_text`` is ``None``.
    - ``state == FAILED`` => ``error_text`` is not ``None`` and
      ``result_text`` is ``None``.
    - ``state == CANCELLED`` => ``finished_at`` is not ``None`` and both
      ``result_text`` and ``error_text`` are ``None``.

    The ``cancel_event`` is a cooperative signal: long-running handlers
    should poll it (e.g. between chunks of an NVIDIA call) to honour
    ``Task_Manager.cancel``. It is excluded from ``repr`` to keep logs
    readable.
    """

    id: str
    name: str
    args: dict
    state: TaskState = TaskState.QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result_text: str | None = None
    error_text: str | None = None
    cancel_event: threading.Event = field(
        default_factory=threading.Event, repr=False, compare=False
    )
    skill_id: str = ""

    def transition_to(
        self,
        new_state: TaskState,
        *,
        result_text: str | None = None,
        error_text: str | None = None,
        now: float | None = None,
    ) -> None:
        """Move the task into ``new_state``, validating the transition.

        Updates timestamps and result/error fields atomically with the state
        change so that the invariants above always hold. Raises
        ``RuntimeError`` if the transition is not allowed from the current
        state.
        """
        allowed = _ALLOWED_TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise RuntimeError(
                f"Illegal task transition: {self.state} -> {new_state} "
                f"(task id={self.id})"
            )

        ts = time.time() if now is None else now

        if new_state is TaskState.RUNNING:
            self.started_at = ts
        elif new_state is TaskState.SUCCEEDED:
            if result_text is None:
                raise ValueError("SUCCEEDED transition requires result_text")
            self.result_text = result_text
            self.error_text = None
            self.finished_at = ts
        elif new_state is TaskState.FAILED:
            if error_text is None:
                raise ValueError("FAILED transition requires error_text")
            self.error_text = error_text
            self.result_text = None
            self.finished_at = ts
        elif new_state is TaskState.CANCELLED:
            self.result_text = None
            self.error_text = None
            self.finished_at = ts
            self.cancel_event.set()
        # ANNOUNCED is purely a bookkeeping flip; timestamps stay.

        self.state = new_state

    @property
    def is_terminal(self) -> bool:
        """True once the task has reached a state from which no work runs."""
        return self.state in _TERMINAL_STATES

    def elapsed_seconds(self, now: float | None = None) -> float:
        """Wall-clock seconds spent so far, capped at ``finished_at``."""
        ts = time.time() if now is None else now
        end = self.finished_at if self.finished_at is not None else ts
        start = self.started_at if self.started_at is not None else self.created_at
        return max(0.0, end - start)


# ---------------------------------------------------------------------------
# Model_Router: routes, requests, results, health
# ---------------------------------------------------------------------------


# The set of providers Model_Router can dispatch to. Extended in lockstep
# with the dual-Gemini + NVIDIA NIM design (see design.md § Architecture).
ProviderId = Literal[
    "gemini_primary",
    "gemini_secondary",
    "gemini_extra_1",
    "gemini_extra_2",
    "gemini_extra_3",
    "nvidia",
    "groq",
    "openrouter",
]


@dataclass(frozen=True)
class Route:
    """A single ``(provider, model)`` target for a Model_Router call.

    Frozen so routes can be safely used as dict keys, set members and
    shared between the Plugin_Host (where tool metadata is parsed) and
    the Model_Router runtime without defensive copying.
    """

    provider: ProviderId
    model: str


@dataclass(frozen=True)
class RouteProfile:
    """A tool's preferred ``Route`` plus an ordered fallback chain.

    ``fallback`` is a tuple (not a list) to keep the dataclass hashable
    and to communicate that the order is part of the contract: the
    Model_Router walks ``chain()`` from index 0 upward.
    """

    primary: Route
    fallback: tuple[Route, ...] = ()

    def chain(self) -> tuple[Route, ...]:
        """Return ``(primary, *fallback)`` as a single ordered tuple.

        This is the canonical sequence the Model_Router consults when
        routing a request; deduplication and health-gating happen on top
        of this ordering inside ``select_route``.
        """
        return (self.primary, *self.fallback)


@dataclass
class RouteRequest:
    """Provider-agnostic call payload handed to ``ModelRouter.route``.

    Only the field set required by the chosen ``kind`` is used; the
    remaining fields are ignored. Defaults match the design's "small
    chat" preset (1024 tokens, low temperature, 60 s timeout) so most
    callers can omit them.
    """

    kind: Literal["chat", "embed", "vision"]
    messages: list[dict] | None = None
    inputs: list[str] | None = None
    image_b64: str | None = None
    max_tokens: int = 1024
    temperature: float = 0.2
    timeout_sec: float = 60.0


@dataclass
class RouteResult:
    """Outcome of a Model_Router dispatch.

    ``ok`` is the single source of truth: when ``False``, ``error_class``
    and ``error_message`` are populated and ``user_message_tr`` carries
    the Turkish single-paragraph message that should be surfaced to the
    user. When ``True``, ``text`` (chat/vision) or ``embeddings`` (embed)
    is populated depending on the originating ``RouteRequest.kind``.

    ``fallback_chain`` is the tuple of ``"<provider>:<model>"`` strings
    actually attempted, in order, including the final attempt that
    produced this result.
    """

    ok: bool
    provider: str
    model: str
    text: str | None = None
    embeddings: list[list[float]] | None = None
    latency_ms: int = 0
    tokens_in: int | None = None
    tokens_out: int | None = None
    error_class: str | None = None
    error_message: str | None = None
    user_message_tr: str | None = None
    fallback_chain: tuple[str, ...] = ()


@dataclass
class HealthState:
    """Snapshot of a single provider's health from the Health_Probe.

    ``failure_streak`` is reset to 0 on any successful probe; the
    Model_Router treats a provider as unhealthy when ``healthy`` is
    ``False`` and ``last_checked_at`` is within
    ``model_router.health_check_interval_sec``.
    """

    provider: str
    healthy: bool
    last_checked_at: float
    last_latency_ms: int | None = None
    failure_streak: int = 0
    last_error: str | None = None


# ---------------------------------------------------------------------------
# Tool & plugin metadata
# ---------------------------------------------------------------------------


ExecutionMode = Literal["inline", "background"]


@dataclass
class ToolDescriptor:
    """Runtime metadata describing a single Gemini tool.

    ``declaration`` follows the Gemini function-calling schema (a dict with
    ``name``, ``description``, ``parameters``). ``handler`` is the Python
    callable the Tool_Runtime will dispatch to.

    ``route_profile`` is optional metadata read from a tool's ``__tool__``
    dict by the Plugin_Host. When present, the Model_Router uses it
    instead of the config-level default route for this tool.
    """

    name: str
    declaration: dict
    handler: Callable[..., Any]
    execution_mode: ExecutionMode
    skill_id: str
    timeout_sec: float = 30.0
    route_profile: RouteProfile | None = None


@dataclass
class SkillManifest:
    """Manifest contract loaded by the Plugin_Host for each skill package.

    Sourced from either ``skill.yaml`` or a ``__skill__.py`` module exporting
    ``MANIFEST: SkillManifest``. ``tools`` lists the names of callables
    inside ``entry_module`` that publish a ``__tool__`` metadata dict.
    """

    name: str
    version: str
    enabled: bool = True
    entry_module: str = ""
    tools: list[str] = field(default_factory=list)
    description: str = ""
    requires: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# WhatsApp Contact_Search
# ---------------------------------------------------------------------------


@dataclass
class ContactSearchResult:
    """Outcome of a WhatsApp Desktop Contact_Search probe.

    - ``matches``: visible names returned by the search box (max 5).
    - ``selected``: the name we automatically opened, when unambiguous.
    - ``ambiguous``: more than one visible match; auto-send must abort.
    - ``not_found``: the search box yielded no results within the deadline.
    - ``note``: free-form diagnostic message for tool output / logs.
    """

    matches: list[str] = field(default_factory=list)
    selected: str | None = None
    ambiguous: bool = False
    not_found: bool = False
    note: str = ""


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipboardEntry:
    """Immutable snapshot of a single clipboard event.

    Frozen so entries can be safely shared between the Clipboard_Manager
    listener thread and consumers (HUD, ``clipboard_history`` tool).
    """

    text: str
    created_at: float
    source_app: str = ""


# ---------------------------------------------------------------------------
# Routines
# ---------------------------------------------------------------------------


OnErrorPolicy = Literal["continue", "stop"]


@dataclass
class RoutineStep:
    """A single tool invocation inside a Routine."""

    tool: str
    args: dict = field(default_factory=dict)
    on_error: OnErrorPolicy = "continue"
    name: str = ""


@dataclass
class Routine:
    """A user-defined sequence of tool calls bound to trigger phrases."""

    name: str
    triggers: list[str]
    steps: list[RoutineStep] = field(default_factory=list)


@dataclass
class RoutineRunReport:
    """Summary of a Routine execution, suitable for HUD/voice readout."""

    routine: str
    completed: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    duration_sec: float = 0.0


# ---------------------------------------------------------------------------
# Conversation logging
# ---------------------------------------------------------------------------


ConversationRole = Literal["user", "assistant", "tool", "system"]


@dataclass
class ConversationLogEntry:
    """A single line of the JSONL conversation log."""

    ts: str  # ISO-8601 timestamp
    role: ConversationRole
    text: str
    tool_name: str | None = None
    task_id: str | None = None


# ---------------------------------------------------------------------------
# HUD theming
# ---------------------------------------------------------------------------


@dataclass
class Theme:
    """Color palette applied by the Theme_Engine to the HUD.

    All color fields are hex strings (``#RRGGBB``). ``halo_alpha`` is in
    ``[0.0, 1.0]`` and drives the intensity of glow / particle effects.
    """

    name: str
    bg: str
    primary: str
    accent: str
    danger: str
    text: str
    gradient_start: str
    gradient_end: str
    halo_alpha: float = 0.6


__all__ = [
    "TaskState",
    "BackgroundTask",
    "ProviderId",
    "Route",
    "RouteProfile",
    "RouteRequest",
    "RouteResult",
    "HealthState",
    "ToolDescriptor",
    "ExecutionMode",
    "SkillManifest",
    "ContactSearchResult",
    "ClipboardEntry",
    "RoutineStep",
    "Routine",
    "RoutineRunReport",
    "ConversationLogEntry",
    "ConversationRole",
    "OnErrorPolicy",
    "Theme",
]
