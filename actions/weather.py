"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/productivity/tools.py``.

Bu modül, görev 5.7 kapsamında ``actions/weather.py`` içeriğini
``skills/productivity/`` paketi altına taşıdıktan sonra eski import yollarının
(``from actions.weather import get_weather_summary``) bozulmaması için
bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.productivity.tools import get_weather_summary


__all__ = ["get_weather_summary"]
