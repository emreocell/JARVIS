"""Translate skill manifest.

Yayınlanan tool'lar:

- ``translate_text`` — Verilen metni ``nvidia/riva-translate-4b-instruct-v1.1``
  modeli ile hedef dile çevirir. Kaynak dil belirtilmezse otomatik tespit
  edilir; hedef dil belirtilmezse ``config/api_keys.json`` içindeki
  ``translate.default_target`` değeri veya varsayılan ``"en"`` kullanılır.
  ``inline`` modda çalışır (Req 7.7).

- ``translate_screen`` — Aktif pencerenin ekran görüntüsünü alır, Gemini
  vision ile OCR uygular ve elde edilen metni
  ``nvidia/riva-translate-4b-instruct-v1.1`` ile çevirir. OCR boş metin
  döndürürse "Ekranda çevrilebilir metin bulunamadı" Türkçe paragrafı
  döner ve çeviri çağrısı yapılmaz (Req 7.6). ``background`` modda
  çalışır (Req 7.7).

Privacy_Mode aktifken clipboard kaynaklı ``translate_text`` çağrıları
durdurulur; kullanıcı doğrudan diktiklerin çevirisi devam eder (Req 7.8).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

``translate_screen`` background modda çalıştığından Tool_Runtime tarafından
Task_Manager'a delege edilir; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur (design.md § Tool_Runtime, Req 7.7).

``requires=["nvidia_api_key"]`` — NVIDIA anahtarı yoksa Plugin_Host bu
skill'i otomatik olarak devre dışı bırakır (Req 17.4).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="translate",
    version="1.0.0",
    enabled=True,
    entry_module="skills.translate.tools",
    tools=[
        "translate_text",
        "translate_screen",
    ],
    description=(
        "Translate skill'i: Metin ve ekran çevirisi (NVIDIA Riva). "
        "translate_text ile verilen metni, translate_screen ile ekrandaki "
        "metni hedef dile çevirir. Kaynak dil otomatik tespit edilir."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
