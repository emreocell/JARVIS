"""Backwards-compatibility shim.

The browser tool now lives in :mod:`skills.web.tools` (task 5.10). This
module re-exports :func:`browser_control` so existing imports such as::

    from actions.browser import browser_control

continue to work during the v1 -> v2 migration. New code should import
the symbol from its canonical location instead::

    from skills.web.tools import browser_control

This shim contains no logic of its own; please make changes only in
``skills/web/tools.py``.
"""

from __future__ import annotations

from skills.web.tools import browser_control


__all__ = ["browser_control"]
