"""Backwards-compat shim for ``actions.health`` (görev 5.5).

Eski ``actions/health.py`` artık ``skills/system/tools.py`` içinde tanımlı.
Bu modül, taşımanın ``main.py``, ``ui.py`` (welcome akışı) ve diğer harici
tüketicileri kırmaması için yalnızca yeniden ihracat yapar.

Yeni kod doğrudan ``skills.system.tools`` içinden import etmelidir.
"""

from __future__ import annotations

from skills.system.tools import (
    ICLOUD_WINDOWS_PATHS,
    UNSUPPORTED_MESSAGE,
    get_health_data,
    get_welcome_health_summary,
)

__all__ = [
    "ICLOUD_WINDOWS_PATHS",
    "UNSUPPORTED_MESSAGE",
    "get_health_data",
    "get_welcome_health_summary",
]
