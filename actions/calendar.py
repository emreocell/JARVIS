"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/productivity/tools.py``.

Bu modül, görev 5.7 kapsamında ``actions/calendar.py`` içeriğini
``skills/productivity/`` paketi altına taşıdıktan sonra eski import yollarının
(``from actions.calendar import get_calendar_events`` vb.) bozulmaması için
bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.productivity.tools import (
    UNSUPPORTED_MESSAGE,
    add_calendar_event,
    delete_calendar_event,
    get_calendar_events,
)


__all__ = [
    "UNSUPPORTED_MESSAGE",
    "add_calendar_event",
    "delete_calendar_event",
    "get_calendar_events",
]
