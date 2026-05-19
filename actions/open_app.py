"""Backwards-compatibility shim.

The application launcher now lives in :mod:`skills.system.tools`
(task 5.10). This module re-exports :func:`open_app` and
``APP_ALIASES`` so existing imports such as::

    from actions.open_app import open_app

continue to work during the v1 -> v2 migration. New code should import
the symbol from its canonical location instead::

    from skills.system.tools import open_app

This shim contains no logic of its own; please make changes only in
``skills/system/tools.py``.
"""

from __future__ import annotations

from skills.system.tools import APP_ALIASES, open_app


__all__ = ["open_app", "APP_ALIASES"]
