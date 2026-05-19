"""System skill manifest.

Yayınlanan tool'lar (tümü ``inline`` execution_mode'da çalışır):

- ``system_control`` — Ses, kilit, masaüstü, görev yöneticisi, pano kısayolları.
- ``sys_info`` — Pil, CPU, RAM, disk, saat, tarih, ağ özetleri.
- ``get_health_data`` — iCloud for Windows ile senkronize HealthAutoExport
  dosyalarından sağlık özeti.
- ``shell_run`` — Güvenlik filtreli PowerShell / CMD komutu çalıştırıcı.
- ``open_app`` — Windows uygulama başlatıcı (görev 5.10'da
  ``actions/open_app.py``'den taşındı).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools`` listesindeki
her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını okur (bkz.
``runtime/plugin_host.py`` görev 5.1).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="system",
    version="1.1.0",
    enabled=True,
    entry_module="skills.system.tools",
    tools=[
        "system_control",
        "sys_info",
        "get_health_data",
        "shell_run",
        "open_app",
    ],
    description=(
        "System skill'i: Windows uygulama başlatma, ses/kilit/pano kısayolları, "
        "donanım bilgisi, sağlık verisi ve güvenlik filtreli kabuk komutu çalıştırma."
    ),
    requires=[],
)


__all__ = ["MANIFEST"]
