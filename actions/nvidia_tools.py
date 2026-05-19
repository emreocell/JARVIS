"""Geriye uyumluluk shim'i — gerçek implementasyon ``skills/vision/tools.py``.

Bu modül, görev 5.8 kapsamında ``actions/nvidia_tools.py`` içeriğini
``skills/vision/`` paketi altına taşıdıktan sonra eski import yollarının
(`from actions.nvidia_tools import detect_objects_in_video`) bozulmaması
için bırakılmıştır.

Yeni canonical adlar Plugin_Host manifesto'sundakilerle aynıdır:
``video_object_detect``, ``audio_to_table``, ``nvidia_text_task`` ve
``nvidia_image_analyze``. Eski snake_case fonksiyon adları
(``detect_objects_in_video``, ``create_table_from_audio``,
``run_nvidia_text_task``, ``analyze_image_with_nvidia``) bu shim üzerinden
hâlâ erişilebilir; ``main.py`` bu adları doğrudan import ediyor.

Görev 5.12 (`main.py`'nin Plugin_Host'a delege edilmesi) tamamlandığında
bu shim de silinebilir.
"""

from __future__ import annotations

from skills.vision.tools import (
    DEFAULT_IMAGE_MODEL,
    DEFAULT_TABLE_MODEL,
    DEFAULT_TEXT_MODEL,
    DEFAULT_VIDEO_MODEL,
    audio_to_table as create_table_from_audio,
    nvidia_image_analyze as analyze_image_with_nvidia,
    nvidia_text_task as run_nvidia_text_task,
    video_object_detect as detect_objects_in_video,
)


__all__ = [
    "DEFAULT_IMAGE_MODEL",
    "DEFAULT_TABLE_MODEL",
    "DEFAULT_TEXT_MODEL",
    "DEFAULT_VIDEO_MODEL",
    "analyze_image_with_nvidia",
    "create_table_from_audio",
    "detect_objects_in_video",
    "run_nvidia_text_task",
]
