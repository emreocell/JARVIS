"""Embodied skill manifest.

Yayınlanan tool'lar:

- ``gui_next_action`` — Aktif pencerenin ekran görüntüsünü alıp
  ``nvidia/cosmos-reason2-8b`` modeli ile GUI agent reasoning yapar.
  Kullanıcıya tek paragraflık Türkçe yönerge döner; koordinat/bbox varsa
  parantez içinde sonda yer alır. Doğrudan tıklama eylemi **yapılmaz**.
  ``background`` modda çalışır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

Background mode'da çalışan ``gui_next_action`` Tool_Runtime tarafından
Task_Manager'a delege edilir; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur (design.md § Tool_Runtime, Req 12.4).

Privacy_Mode aktifken ekran görüntüsü diske yazılmaz, yalnızca bellekte
tutulur (Req 12.7).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="embodied",
    version="1.0.0",
    enabled=True,
    entry_module="skills.embodied.tools",
    tools=[
        "gui_next_action",
    ],
    description=(
        "Embodied skill'i: Aktif pencere ekran görüntüsünden GUI agent "
        "reasoning (NVIDIA Cosmos). Kullanıcıya hangi adımı atması "
        "gerektiğini Türkçe yönerge olarak açıklar."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
