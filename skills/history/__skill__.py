"""History skill manifest.

Yayınlanan tool'lar (tümü ``inline`` execution_mode'da çalışır):

- ``search_history`` — ``logs/conversation/{YYYY-MM-DD}.jsonl`` dosyalarını
  tarar; ``query`` anahtar kelimesini ``text`` veya ``tool_name`` alanlarında
  arar ve eşleşen ilk 10 girdiyi tarih + kısa özet olarak Türkçe metinle
  döner. Opsiyonel ``since`` / ``until`` (YYYY-MM-DD) ve ``role`` filtreleri
  desteklenir. Privacy_Mode aktifken yapılan atlamalar (Req 28.3)
  ConversationLogger'ın ``privacy_skip_count`` sayacı üzerinden sonuç
  metninde açıklanır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__``
metadata'sını okur (bkz. ``runtime/plugin_host.py`` görev 5.1).

JarvisLive başlatma sırasında (görev 20.1) çalışan ``ConversationLogger``
instance'ını tool ile paylaşmak için ``skills.history.tools.set_logger(...)``
çağrılır; aksi halde tool varsayılan ``logs/conversation/`` dizinini
salt-okunur kipte tarar.

Requirements: 28.2, 28.3.
"""

# Feature: jarvis-v2-upgrade, search_history skill manifest (Task 6.3)

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="history",
    version="1.0.0",
    enabled=True,
    entry_module="skills.history.tools",
    tools=[
        "search_history",
    ],
    description=(
        "History skill'i: Geçmiş konuşma günlüklerinde anahtar kelime, "
        "tarih aralığı ve role göre arama yapar; Privacy_Mode boyunca "
        "atlanan satırları sonuç metninde belirtir."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
