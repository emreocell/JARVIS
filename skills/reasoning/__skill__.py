"""Reasoning skill manifest.

Yayınlanan tool'lar:

- ``plan_generate`` — Doğal dil hedefini ``nvidia/llama-3.3-nemotron-super-49b-v1.5``
  (varsayılan) veya ultra modele (``nvidia/llama-3.1-nemotron-ultra-253b-v1`` ya da
  ``qwen/qwen3-next-80b-a3b-thinking``) göndererek Routine_Engine uyumlu adım listesi
  üretir. Hedef metinde "derin düşün" veya "ultra reasoning" ifadesi geçiyorsa ultra
  model devreye girer. ``background`` modda çalışır.

- ``plan_explain`` — Mevcut planı doğal dilde Türkçe açıklar. ``inline`` modda
  çalışır.

- ``plan_save`` — Dinamik planı ``routines.json`` dosyasına kalıcı olarak yazar.
  Mevcut rutin adıyla çakışırsa onay bekler. ``background`` modda çalışır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

``plan_generate`` ve ``plan_save`` background modda çalışır; Tool_Runtime
tarafından Task_Manager'a delege edilir ve sonuçlar Result_Announcer
üzerinden uygun Turn_Boundary'de duyurulur (design.md § Tool_Runtime).

``plan_explain`` inline modda çalışır; Voice_Core akışını engellemeden
anında yanıt döner.

NVIDIA API anahtarı yoksa Plugin_Host bu skill'i otomatik olarak devre
dışı bırakır (``requires=["nvidia_api_key"]``, Req 17.4).

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.7, 6.8
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="reasoning",
    version="1.0.0",
    enabled=True,
    entry_module="skills.reasoning.tools",
    tools=[
        "plan_generate",
        "plan_explain",
        "plan_save",
    ],
    description=(
        "Reasoning skill'i: Doğal dil hedefini Routine_Engine uyumlu "
        "adım listesine çevirir (NVIDIA Nemotron / Qwen). "
        "Orta seviye için llama-3.3-nemotron-super-49b-v1.5, "
        "'derin düşün' komutuyla ultra model devreye girer. "
        "Üretilen plan açıklanabilir ve routines.json'a kaydedilebilir."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
