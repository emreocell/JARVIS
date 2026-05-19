"""Backwards-compat shim for ``actions.shell`` (görev 5.5).

Eski ``actions/shell.py`` artık ``skills/system/tools.py`` içinde tanımlı.
Bu modül, taşımanın ``main.py`` ve diğer harici tüketicileri kırmaması
için yalnızca yeniden ihracat yapar.

Yeni kod doğrudan ``skills.system.tools.shell_run`` içinden import etmelidir.
"""

from __future__ import annotations

from skills.system.tools import shell_run

__all__ = ["shell_run"]
