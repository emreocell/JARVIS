"""Audio_Structured skill manifest.

Yayınlanan tool'lar:

- ``meeting_to_actions`` — Toplantı ses kaydını transkribe edip
  ``{participants, action_items}`` JSON yapısına dönüştürür.
  ``background`` modda çalışır.
- ``call_to_crm`` — Telefon görüşmesi kaydını transkribe edip
  ``{customer, intent, next_step, summary}`` CRM şemasına dönüştürür.
  ``background`` modda çalışır.

Bu skill, mevcut ``skills/vision/audio_to_table`` tool'unu ve
``actions/nvidia_tools.py`` shim'ini **bozmaz** (Req 11.1). Her iki
tool da bağımsız paketler olarak çalışmaya devam eder.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

Background mode'da çalışan her iki tool da Tool_Runtime tarafından
Task_Manager'a delege edilir; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur (design.md § Tool_Runtime, Req 11.4).

60 dk üzeri kayıtlar otomatik olarak 10 dk parçalara bölünür (Req 11.5).
Transkripsiyon başarısız olursa 3x exponential backoff uygulanır (Req 11.8).
Privacy_Mode aktifken çıktı diske yazılmaz (Req 11.7).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="audio_structured",
    version="1.0.0",
    enabled=True,
    entry_module="skills.audio_structured.tools",
    tools=[
        "meeting_to_actions",
        "call_to_crm",
    ],
    description=(
        "Audio_Structured skill'i: Toplantı ve telefon görüşmesi ses "
        "kayıtlarını yapılandırılmış veriye dönüştürür. Toplantı kaydından "
        "katılımcı + aksiyon listesi, görüşme kaydından CRM girdisi üretir."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
