"""Backwards-compat shim for ``actions.sys_info`` (görev 5.5).

Eski ``actions/sys_info.py`` artık ``skills/system/tools.py`` içinde
tanımlı. Bu modül, taşımanın ``main.py`` ve diğer harici tüketicileri
kırmaması için yalnızca yeniden ihracat yapar.

Yeni kod doğrudan ``skills.system.tools.sys_info`` içinden import etmelidir.
"""

from __future__ import annotations

from skills.system.tools import sys_info

__all__ = ["sys_info"]
