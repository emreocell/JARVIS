"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/media/tools.py``.

Bu modül, görev 5.6 kapsamında ``actions/media.py`` içeriğini
``skills/media/`` paketi altına taşıdıktan sonra eski import yollarının
(`from actions.media import play_media`) bozulmaması için bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.media.tools import play_media


__all__ = ["play_media"]
