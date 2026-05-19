"""Backwards-compat shim for ``actions.system_control`` (görev 5.5).

Eski ``actions/system_control.py`` artık ``skills/system/tools.py`` içinde
tanımlı. Bu modül, taşımanın ``main.py`` ve diğer harici tüketicileri
kırmaması için yalnızca yeniden ihracat yapar.

Yeni kod doğrudan ``skills.system.tools.system_control`` içinden import
etmelidir.
"""

from __future__ import annotations

from skills.system.tools import system_control

__all__ = ["system_control"]
