"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/media/tools.py``.

Bu modül, görev 5.6 kapsamında ``actions/youtube_stats.py`` içeriğini
``skills/media/`` paketi altına taşıdıktan sonra eski import yollarının
(`from actions.youtube_stats import get_youtube_channel_report`) bozulmaması
için bırakılmıştır.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.media.tools import (
    DEFAULT_VIDEO_LIMIT,
    get_youtube_channel_report,
)


__all__ = ["DEFAULT_VIDEO_LIMIT", "get_youtube_channel_report"]
