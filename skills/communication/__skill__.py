"""Communication skill manifest — WhatsApp_Bridge + Email_Skill.

Plugin_Host bu modülü keşfeder, MANIFEST global'ını okur ve
ilgili handler'ları Tool_Runtime'a kaydeder.

Not: Plugin_Host tek bir entry_module destekler; birden fazla modül için
main.py bootstrap'ta ek kayıt yapılır.
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST = SkillManifest(
    name="communication",
    version="0.2.0",
    enabled=True,
    entry_module="skills.communication.whatsapp_bridge",
    tools=[
        "send_whatsapp_message",
        "save_whatsapp_contact",
        "send_whatsapp_via_search",
    ],
    description=(
        "WhatsApp Desktop ve Web üzerinden mesaj gönderimi ile kişi yönetimi. "
        "Outlook e-posta araçları (read_emails, send_email) ayrıca yüklenir."
    ),
    requires=["pyperclip", "pyautogui"],
)


__all__ = ["MANIFEST"]
