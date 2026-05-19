"""Vision skill manifest.

Yayınlanan tool'lar:

- ``analyze_screen`` — Aktif pencerenin ekran görüntüsünü Gemini vision ile
  analiz eder. ``inline`` modda çalışır; tek Gemini çağrısı genelde 2-4 sn.
- ``click_on_screen`` — Ekran görüntüsünde doğal dille tarif edilen bir
  objeyi Gemini vision ile bulur ve pyautogui ile üzerine tıklar.
  ``inline`` modda çalışır.
- ``video_object_detect`` — Bir videodan kareler örnekleyip NVIDIA vision
  modeli ile obje tespiti yapar. ``background`` (yavaş çoklu çağrı).
- ``audio_to_table`` — Ses kaydını metne çevirip NVIDIA modeli ile markdown
  tabloya dönüştürür. ``background`` (STT + LLM zinciri).
- ``nvidia_text_task`` — NVIDIA text modellerini genel amaçlı görevlerde
  kullanır. ``background`` (uzun NVIDIA REST çağrısı).
- ``nvidia_image_analyze`` — Yerel bir görseli NVIDIA vision modeliyle
  analiz eder. ``background`` (uzun NVIDIA REST çağrısı).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py`` görev 5.1).

Background mode'da çalışan dört NVIDIA tool'u Tool_Runtime tarafından
Task_Manager'a delege edilir; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur (design.md § Tool_Runtime, Req 2.2).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="vision",
    version="1.0.0",
    enabled=True,
    entry_module="skills.vision.tools",
    tools=[
        "analyze_screen",
        "click_on_screen",
        "video_object_detect",
        "audio_to_table",
        "nvidia_text_task",
        "nvidia_image_analyze",
    ],
    description=(
        "Vision skill'i: Aktif pencere ekran analizi (Gemini), ekran "
        "objesine doğal dille tıklama ve NVIDIA çoklu-modal araç kümesi "
        "(video obje tespiti, sesten tablo, metin görevi, görsel analiz)."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
