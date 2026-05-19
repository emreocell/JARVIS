"""Project-internal ``safety.pii.mask`` wrapper.

This module exposes a single plain-Python function, :func:`mask`, that other
runtime modules (``runtime.conversation_logger``, ``runtime.clipboard``)
call synchronously to mask PII before persisting or displaying text.

When the Safety_Skill is loaded and configured with a NIM-backed
``pii_mask`` provider, the runtime registers it via :func:`set_provider`.
Until then — and whenever no provider is registered — :func:`mask` is a
no-op (identity) so that callers do not have to know whether the skill is
available. This keeps the keyless / NVIDIA-unavailable path completely
non-disruptive.

The wrapper intentionally never raises: a provider exception is swallowed
and the original ``text`` is returned unchanged so a Safety failure cannot
break the host call path. The fail-closed decision lives in
:func:`skills.safety._internal.should_fail_closed` and is applied by the
skill's tool layer, not by this passive helper.
"""

from __future__ import annotations

from typing import Callable, Optional

# A provider takes the raw text and returns the masked text. It is provided
# by the Safety_Skill at load time and resets to ``None`` when the skill is
# disabled or absent.
_Provider = Callable[[str], str]
_provider: Optional[_Provider] = None


def set_provider(provider: Optional[_Provider]) -> None:
    """Register (or clear) the active ``mask`` provider.

    Passing ``None`` reverts the wrapper to identity behavior. The runtime
    calls this on Safety_Skill load and again on shutdown / disable so the
    no-op invariant is restored.
    """

    global _provider
    _provider = provider


def get_provider() -> Optional[_Provider]:
    """Return the currently registered provider, if any."""

    return _provider


def mask(text: str) -> str:
    """Mask PII in ``text`` using the registered provider.

    Returns ``text`` unchanged when:

    - no provider is registered (Safety_Skill not loaded);
    - ``text`` is not a non-empty string;
    - the provider raises any exception (we never propagate so a Safety
      hiccup can't break logging or clipboard flows).
    """

    if not isinstance(text, str) or not text:
        return text if isinstance(text, str) else ""
    provider = _provider
    if provider is None:
        return text
    try:
        result = provider(text)
    except Exception:
        return text
    if not isinstance(result, str):
        return text
    return result
