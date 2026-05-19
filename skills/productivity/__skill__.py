"""Productivity skill manifest.

Yayınlanan tool'lar (tümü ``inline`` execution_mode'da çalışır):

- ``get_calendar_events`` — Windows Outlook Calendar etkinliklerini özetler.
- ``add_calendar_event`` — Outlook Calendar'a yeni etkinlik ekler.
- ``delete_calendar_event`` — Outlook Calendar'dan etkinlik siler.
- ``get_reminders`` — Yerel hatırlatıcı listesini özetler.
- ``add_reminder`` — Yeni bir hatırlatıcı ekler.
- ``get_weather`` — Anlık hava durumu özeti döner (handler: ``get_weather_summary``).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools`` listesindeki
her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını okur (bkz.
``runtime/plugin_host.py`` görev 5.1).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="productivity",
    version="1.0.0",
    enabled=True,
    entry_module="skills.productivity.tools",
    tools=[
        "get_calendar_events",
        "add_calendar_event",
        "delete_calendar_event",
        "get_reminders",
        "add_reminder",
        "get_weather_summary",
    ],
    description=(
        "Productivity skill'i: Outlook Calendar takvim okuma/ekleme/silme, "
        "yerel hatırlatıcı yönetimi ve anlık hava durumu özeti."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
