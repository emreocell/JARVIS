"""Media skill manifest.

Yayınlanan tool'lar (tümü ``inline`` execution_mode'da çalışır):

- ``play_media`` — Spotify Desktop ya da YouTube üzerinden müzik / video oynatır.
- ``get_youtube_channel_report`` — public YouTube Data API üzerinden kanal
  istatistikleri ve son video performans özetini döner.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools`` listesindeki
her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını okur (bkz.
``runtime/plugin_host.py`` görev 5.1).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="media",
    version="1.0.0",
    enabled=True,
    entry_module="skills.media.tools",
    tools=[
        "play_media",
        "get_youtube_channel_report",
    ],
    description=(
        "Medya skill'i: Spotify Desktop / YouTube üzerinden oynatma ve "
        "YouTube kanal istatistik raporu."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
