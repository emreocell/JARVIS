"""Web skill manifest — tarayıcı kontrol araçları.

Yayınlanan tool (``inline`` execution_mode):

- ``browser_control`` — URL açma, arama, YouTube oynatma, sekme yönetimi
  ve video kontrol kısayolları.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve
``entry_module`` içindeki ``__tool__`` metadata'sını okur.
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="web",
    version="1.0.0",
    enabled=True,
    entry_module="skills.web.tools",
    tools=[
        "browser_control",
    ],
    description=(
        "Web skill'i: varsayılan tarayıcı üzerinden URL açma, arama, "
        "YouTube oynatma ve sekme/video kontrol kısayolları."
    ),
    requires=["pyautogui", "requests"],
)


__all__ = ["MANIFEST"]
