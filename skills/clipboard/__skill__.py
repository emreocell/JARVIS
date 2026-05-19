"""Clipboard skill manifest.

Plugin_Host bu modülü keşfeder, MANIFEST global'ını okur ve
tools.py içindeki handler'ları Tool_Runtime'a kaydeder.
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="clipboard",
    version="0.1.0",
    enabled=True,
    entry_module="skills.clipboard.tools",
    tools=[
        "clipboard_history",
        "clipboard_recall",
    ],
    description="Windows pano geçmişini listeler ve önceki kopyaları geri çağırır.",
    requires=["pyperclip"],
)


__all__ = ["MANIFEST"]
