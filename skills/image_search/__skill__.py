"""Image_Search skill manifest.

Yayınlanan tool'lar:

- ``image_index_build`` — Verilen klasördeki desteklenen görselleri
  (`.jpg`, `.jpeg`, `.png`, `.webp`) ``nvidia/nvclip`` modeli ile
  embedding'e çevirir ve ``Vector_Store`` içindeki ``image_search``
  namespace'ine yazar. Hash tabanlı dedupe ile daha önce indekslenmiş
  ve değişmemiş görseller atlanır. 5000 üzerinde görsel içeren
  klasörlerde her 500 görselde bir Türkçe ilerleme duyurusu yapılır.
  ``background`` modda çalışır.

- ``image_search`` — Doğal dil sorgusu için NVCLIP text embedding'i
  üretir ve ``Vector_Store``'da top-k (varsayılan k=10) en yakın
  görselin tam yollarını skorlarıyla birlikte döner.
  ``background`` modda çalışır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__``
metadata'sını okur (bkz. ``runtime/plugin_host.py``).

Privacy_Mode aktifken yeni indeksleme görevleri durdurulur; mevcut
indeks üzerinde arama açıktır (Req 10.8).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="image_search",
    version="1.0.0",
    enabled=True,
    entry_module="skills.image_search.tools",
    tools=[
        "image_index_build",
        "image_search",
    ],
    description=(
        "Image_Search skill'i: Yerel klasördeki görselleri NVCLIP ile "
        "indeksler ve doğal dil sorgusuyla sıfır-shot görsel arama yapar. "
        "Hash tabanlı dedupe ile değişmeyen görseller yeniden işlenmez."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
