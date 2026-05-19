"""Doc_Intel skill manifest.

Yayınlanan tool'lar:

- ``doc_parse`` — PDF, fatura veya makbuz görselini ``nvidia/nemotron-parse``
  (fallback: ``nvidia/nemoretriever-parse``) modeli ile yapılandırılmış JSON'a
  çevirir. Başarılı çıktı Privacy_Mode kapalıysa ``logs/doc_intel/{timestamp}.json``
  dosyasına yazılır. ``background`` modda çalışır.

- ``chart_read`` — Grafik/tablo görselini ``google/deplot`` modeli ile
  tabloya çevirir ve Türkçe açıklamayla döner. ``background`` modda çalışır.

- ``screenshot_summarize`` — Uzun ekran görüntüsünü ``microsoft/kosmos-2``
  (fallback: ``adept/fuyu-8b``) modeli ile en fazla üç paragrafta Türkçe
  özetler. ``background`` modda çalışır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

Tüm tool'lar background modda çalışır; Tool_Runtime tarafından Task_Manager'a
delege edilir ve sonuçlar Result_Announcer üzerinden uygun Turn_Boundary'de
duyurulur (design.md § Tool_Runtime, Req 5.5).

Dosya yok/okunamaz → modele istek gönderilmez; Türkçe hata paragrafı döner
(Req 5.6). Görsel >4096 px uzun kenar ise gönderim öncesi resize yapılır
(Req 5.7). Privacy_Mode kapalıysa ``doc_parse`` çıktısı diske yazılır (Req 5.8).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="doc_intel",
    version="1.0.0",
    enabled=True,
    entry_module="skills.doc_intel.tools",
    tools=[
        "doc_parse",
        "chart_read",
        "screenshot_summarize",
    ],
    description=(
        "Doc_Intel skill'i: PDF, fatura ve makbuz çözümleme (nemotron-parse), "
        "grafik/tablo okuma (deplot) ve uzun ekran görüntüsü özetleme "
        "(kosmos-2). Tüm tool'lar background modda çalışır."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
