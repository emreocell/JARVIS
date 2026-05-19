"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/vision/tools.py``.

Bu modül, görev 5.8 kapsamında ``actions/screen_vision.py`` içeriğini
``skills/vision/`` paketi altına taşıdıktan sonra eski import yollarının
(`from actions.screen_vision import analyze_screen`) bozulmaması için
bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.vision.tools import analyze_screen


__all__ = ["analyze_screen"]
