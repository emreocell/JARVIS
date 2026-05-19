"""Creative skill manifest.

Yayınlanan tool'lar:

- ``creative_write`` — Blog, sosyal medya veya hikaye taslağı üretir.
  ``writer/palmyra-creative-122b`` modelini kullanır. ``background`` modda
  çalışır (Req 9.2, 9.5).
- ``financial_analyze`` — Finansal analiz üretir.
  ``writer/palmyra-fin-70b-32k`` modelini kullanır. Çıktının başında
  "Bu yatırım tavsiyesi değildir" Türkçe uyarısı zorunludur (Req 9.3).
  ``background`` modda çalışır (Req 9.5).
- ``medical_qa`` — Sağlık sorusu yanıtlar.
  ``writer/palmyra-med-70b`` modelini kullanır. Çıktının başında
  "Bu profesyonel tıbbi tavsiye yerine geçmez, bir doktora danışın"
  Türkçe uyarısı zorunludur (Req 9.4). ``background`` modda çalışır
  (Req 9.5).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

Background mode'da çalışan tüm tool'lar Tool_Runtime tarafından
Task_Manager'a delege edilir; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur (design.md § Tool_Runtime, Req 9.5).

Finansal ve tıbbi uyarılar kullanıcı ``suppress_disclaimer=True`` verse bile
kaldırılmaz; kalıcı yasal gerekliliktir (Req 9.6).

30 sn timeout: Task_Manager üzerinden iptal sinyali yayılır ve Türkçe
zaman aşımı paragrafı döner (Req 9.7).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="creative",
    version="1.0.0",
    enabled=True,
    entry_module="skills.creative.tools",
    tools=[
        "creative_write",
        "financial_analyze",
        "medical_qa",
    ],
    description=(
        "Creative skill'i: Yaratıcı yazım (blog/sosyal medya/hikaye), "
        "finansal analiz ve sağlık bilgisi yanıtları. "
        "Her tool için özelleşmiş NVIDIA Palmyra modelleri kullanılır. "
        "Finansal ve tıbbi çıktılarda zorunlu Türkçe yasal uyarılar eklenir."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
