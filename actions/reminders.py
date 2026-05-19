"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/productivity/tools.py``.

Bu modül, görev 5.7 kapsamında ``actions/reminders.py`` içeriğini
``skills/productivity/`` paketi altına taşıdıktan sonra eski import yollarının
(``from actions.reminders import get_reminders, add_reminder``) bozulmaması için
bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.productivity.tools import (
    UNSUPPORTED_MESSAGE,
    add_reminder,
    get_reminders,
)


__all__ = [
    "UNSUPPORTED_MESSAGE",
    "add_reminder",
    "get_reminders",
]
