"""Pure, side-effect-free helpers for the Safety skill.

This module contains only deterministic, I/O-free helpers so they can be
property-tested independently of NIM HTTP clients, file I/O, or the rest of
the runtime.

Public surface:

- :func:`mask_pii` — apply ``[PII:label]`` masking to a text given a list of
  spans returned by ``nvidia/gliner-pii``. Idempotent and deterministic.
- :func:`should_fail_closed` — pure decision function that maps a Safety
  endpoint failure to either *fail closed* (reject the call) or *fail open*
  (warn and pass through), per Requirement 8.10.

The user-facing ``safety.pii.mask`` wrapper used by
``runtime.conversation_logger`` and ``runtime.clipboard`` lives next to
this module in :mod:`skills.safety.pii`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence, Tuple

# A PII span as returned by the gliner-pii model: ``(start, end, label)``.
# ``start`` is inclusive, ``end`` is exclusive, both are character offsets.
PIISpan = Tuple[int, int, str]


# ---------------------------------------------------------------------------
# mask_pii
# ---------------------------------------------------------------------------


_PLACEHOLDER_RE = re.compile(r"\[PII:[^\[\]]*\]")


@dataclass(frozen=True)
class _NormalizedSpan:
    """A span after clipping, deduping, and merging."""

    start: int
    end: int
    label: str


def _coerce_span(raw: object) -> PIISpan | None:
    """Best-effort coercion of a span-like input into ``(start, end, label)``.

    Returns ``None`` if the input cannot be interpreted as a valid span.
    Pure: does not raise.
    """

    if isinstance(raw, dict):
        start = raw.get("start")
        end = raw.get("end")
        label = raw.get("label") or raw.get("type") or raw.get("entity")
        seq: Sequence[object] = (start, end, label)
    elif isinstance(raw, (list, tuple)) and len(raw) >= 3:
        seq = (raw[0], raw[1], raw[2])
    else:
        return None

    start_v, end_v, label_v = seq
    # ``bool`` is a subclass of ``int`` — exclude it explicitly so a True/False
    # offset doesn't slip through.
    if isinstance(start_v, bool) or not isinstance(start_v, int):
        return None
    if isinstance(end_v, bool) or not isinstance(end_v, int):
        return None
    if not isinstance(label_v, str):
        return None
    return (start_v, end_v, label_v)


def _find_existing_placeholders(text: str) -> list[tuple[int, int]]:
    """Return ``(start, end)`` byte ranges of existing ``[PII:*]`` regions.

    Returned ranges are non-overlapping and sorted by start offset because
    they come from a single regex pass over the text.
    """

    return [(m.start(), m.end()) for m in _PLACEHOLDER_RE.finditer(text)]


def _overlaps_any(start: int, end: int, ranges: Sequence[tuple[int, int]]) -> bool:
    """``True`` if ``[start, end)`` intersects any range in ``ranges``."""

    for r_start, r_end in ranges:
        if start < r_end and r_start < end:
            return True
    return False


def _normalize_spans(
    text: str,
    spans: Iterable[object],
    existing: Sequence[tuple[int, int]],
) -> list[_NormalizedSpan]:
    """Clip, drop, and merge spans into a sorted, non-overlapping list.

    - Spans with ``start >= end`` are discarded (empty/invalid).
    - Spans are clipped to ``[0, len(text)]``.
    - Spans that intersect an existing ``[PII:*]`` placeholder in the input
      are dropped — that region is already masked, so re-masking it would
      break idempotence.
    - Overlapping or adjacent spans are merged. The earliest-start label
      wins; ties are broken by label string ordering for determinism.
    """

    text_len = len(text)
    cleaned: list[_NormalizedSpan] = []
    for raw in spans:
        coerced = _coerce_span(raw)
        if coerced is None:
            continue
        start, end, label = coerced
        # Clip to text bounds.
        if start < 0:
            start = 0
        if end > text_len:
            end = text_len
        if start >= end:
            continue
        if _overlaps_any(start, end, existing):
            continue
        cleaned.append(_NormalizedSpan(start=start, end=end, label=label))

    if not cleaned:
        return []

    # Sort by (start, end, label) for determinism.
    cleaned.sort(key=lambda s: (s.start, s.end, s.label))

    merged: list[_NormalizedSpan] = []
    for span in cleaned:
        if not merged:
            merged.append(span)
            continue
        last = merged[-1]
        # Adjacent or overlapping spans merge into one block.
        if span.start <= last.end:
            new_end = max(last.end, span.end)
            # Earliest-start label wins (deterministic).
            new_label = last.label
            merged[-1] = _NormalizedSpan(
                start=last.start, end=new_end, label=new_label
            )
        else:
            merged.append(span)

    return merged


def mask_pii(text: str, spans: Iterable[object]) -> str:
    """Mask PII spans in ``text`` using ``[PII:label]`` placeholders.

    Contract (Property 11 in design.md):

    1. **Idempotent.** ``mask_pii(mask_pii(text, spans), spans) ==
       mask_pii(text, spans)``. Spans that fall inside an existing
       ``[PII:*]`` placeholder in the input are skipped, so re-running the
       function on its own output with the same spans is a no-op.
    2. **Format.** Every masked region in the output is exactly
       ``[PII:<label>]``.
    3. **Leak-free.** A span's original substring does not appear in the
       output (unless that substring was already present elsewhere in the
       input outside any span).
    4. **Merging.** Overlapping or adjacent spans collapse into a single
       ``[PII:label]`` block; the earliest-start label wins.
    5. **Outside-span preservation.** Characters outside every span are
       preserved bit-for-bit.

    Pure, deterministic, and never raises on malformed model input — bad
    spans (non-int offsets, ``start >= end``, non-str label) are silently
    dropped.

    Parameters
    ----------
    text:
        Original text the model analyzed.
    spans:
        Iterable of ``(start, end, label)`` triples (or dicts with the same
        keys). Offsets are character indices into ``text``.

    Returns
    -------
    str
        The masked text.
    """

    if not isinstance(text, str):
        # Fail safe: never raise on malformed input from the model.
        return ""

    existing = _find_existing_placeholders(text)
    normalized = _normalize_spans(text, spans, existing)
    if not normalized:
        return text

    out: list[str] = []
    cursor = 0
    for span in normalized:
        if span.start > cursor:
            out.append(text[cursor : span.start])
        out.append(f"[PII:{span.label}]")
        cursor = span.end
    if cursor < len(text):
        out.append(text[cursor:])
    return "".join(out)


# ---------------------------------------------------------------------------
# should_fail_closed (Req 8.10)
# ---------------------------------------------------------------------------


def should_fail_closed(exc: BaseException | None, fail_closed_flag: bool) -> bool:
    """Decide whether a Safety endpoint failure should *fail closed*.

    Per Requirement 8.10: when a Safety NIM endpoint fails, the skill
    behaves according to the ``safety.fail_closed`` config flag — when
    ``True`` the call is rejected, when ``False`` the call is allowed
    through with only a warning log.

    Parameters
    ----------
    exc:
        The exception raised by the Safety call, or ``None`` if the call
        succeeded. When ``None`` this function always returns ``False``
        (nothing to fail-close on).
    fail_closed_flag:
        The ``safety.fail_closed`` config value, coerced to a strict bool.

    Returns
    -------
    bool
        ``True`` when the caller MUST reject the request; ``False`` when
        the caller SHOULD pass the request through with only a warning.

    Notes
    -----
    Pure decision function: no logging, no I/O. Truthiness of
    ``fail_closed_flag`` is normalized via :class:`bool` so non-bool inputs
    such as ``1`` or ``"true"`` behave consistently.
    """

    if exc is None:
        return False
    return bool(fail_closed_flag)
