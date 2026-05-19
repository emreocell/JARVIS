"""WhatsApp_Bridge skill — communication / WhatsApp tool entry points.

This module is the v2 home for the WhatsApp tool surface. For task 5.9 it
is intentionally a thin **skeleton** that:

- Preserves the current v1 WhatsApp Desktop URL-scheme + WhatsApp Web
  fallback behaviour by delegating to ``actions.whatsapp`` (the legacy
  implementation) so nothing regresses while ``main.py`` still wires
  tools through ``actions/``.
- Publishes the ``__tool__`` metadata that the Plugin_Host and
  Tool_Runtime will consume once task 5.12 retires the static
  ``TOOL_DECLARATIONS`` list in ``main.py``.

The richer Desktop UI-Automation Contact_Search flow (open_desktop,
fallback_to_web, ContactSearch, message-box automation, send_now policy)
lands in task 9 — see design.md § "WhatsApp_Bridge". Until then the
existing Web fallback path remains the single source of truth.
"""

from __future__ import annotations

from typing import Any

# Delegate to the legacy implementation. Once the Desktop UIA flow is
# implemented (task 9) this module will own the logic directly and
# ``actions/whatsapp.py`` can be deleted.
from actions.whatsapp import (
    save_whatsapp_contact as _legacy_save_whatsapp_contact,
    send_whatsapp_message as _legacy_send_whatsapp_message,
    send_whatsapp_via_search as _legacy_send_whatsapp_via_search,
)


__all__ = [
    "send_whatsapp_message",
    "save_whatsapp_contact",
    "send_whatsapp_via_search",
]


# ---------------------------------------------------------------------------
# Tool: send_whatsapp_message
# ---------------------------------------------------------------------------


def send_whatsapp_message(
    message: str,
    phone_number: str = "",
    recipient_name: str = "",
    send_now: bool = False,
    app_target: str = "auto",
) -> str:
    """Open or send a WhatsApp message via Desktop URL scheme or Web fallback.

    Skeleton wrapper around the v1 implementation; behaviour is identical
    until task 9 adds the in-app Contact_Search and message-box UIA flow.
    """
    return _legacy_send_whatsapp_message(
        message=message,
        phone_number=phone_number,
        recipient_name=recipient_name,
        send_now=send_now,
        app_target=app_target,
    )


send_whatsapp_message.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "send_whatsapp_message",
        "description": (
            "WhatsApp Desktop veya WhatsApp Web üzerinden mesaj taslağı açar veya mesajı gönderir. "
            "Kişi adı veya telefon numarasıyla çalışabilir. "
            "Telefon numarası verilmemişse kişi adını önce kayıtlı WhatsApp kişileri ve içe aktarılan telefon rehberinde ara. "
            "Kullanıcı 'gönder', 'yolla', 'ile', 'hemen gönder' gibi açık bir gönderme niyeti söylüyorsa "
            "ekstra onay istemeden send_now=true kullan. "
            "Yalnızca 'hazırla', 'taslak aç', 'yaz ama gönderme' diyorsa send_now=false kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "recipient_name": {
                    "type": "STRING",
                    "description": "Kişi adı. Örn: 'Anne', 'Ahmet', 'Ece'",
                },
                "phone_number": {
                    "type": "STRING",
                    "description": "Uluslararası telefon numarası. Örn: +905551112233",
                },
                "message": {
                    "type": "STRING",
                    "description": "Gönderilecek mesaj içeriği",
                },
                "app_target": {
                    "type": "STRING",
                    "description": "desktop | web | auto. Varsayılan auto, tercihen desktop.",
                },
                "send_now": {
                    "type": "BOOLEAN",
                    "description": "true ise sohbet açıldıktan sonra mesajı otomatik gönderir",
                },
            },
            "required": ["message"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# Tool: save_whatsapp_contact
# ---------------------------------------------------------------------------


def save_whatsapp_contact(
    display_name: str,
    phone_number: str,
    aliases: str = "",
) -> str:
    """Persist a frequently used WhatsApp contact to long-term memory."""
    return _legacy_save_whatsapp_contact(
        display_name=display_name,
        phone_number=phone_number,
        aliases=aliases,
    )


save_whatsapp_contact.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "save_whatsapp_contact",
        "description": (
            "Sık kullanılan bir WhatsApp kişisini adı ve telefon numarasıyla kalıcı belleğe kaydeder. "
            "Kullanıcı bir kişiyi 'annem', 'Ahmet', 'iş ortağım' gibi tekrar kullanılacak şekilde tanımladığında kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "display_name": {
                    "type": "STRING",
                    "description": "Kaydedilecek kişi adı. Örn: 'Annem', 'Ahmet'",
                },
                "phone_number": {
                    "type": "STRING",
                    "description": "Uluslararası telefon numarası. Örn: +905551112233",
                },
                "aliases": {
                    "type": "STRING",
                    "description": "Virgülle ayrılmış alternatif hitaplar. Örn: 'anne, annem, mom'",
                },
            },
            "required": ["display_name", "phone_number"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# Tool: send_whatsapp_via_search
# ---------------------------------------------------------------------------


def send_whatsapp_via_search(
    recipient_name: str,
    message: str,
    send_now: bool = True,
    confirmed_match: str = "",
) -> str:
    """Open WhatsApp Desktop, search for a chat/group by name and send."""
    return _legacy_send_whatsapp_via_search(
        recipient_name=recipient_name,
        message=message,
        send_now=send_now,
        confirmed_match=confirmed_match,
    )


send_whatsapp_via_search.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "send_whatsapp_via_search",
        "description": (
            "WhatsApp Desktop'ın iç arama kutusunu (Ctrl+F) kullanarak sohbet listesindeki "
            "bir kişiye veya gruba mesaj gönderir. Numara veya rehber kaydı gerektirmez. "
            "Kullanıcı 'X grubuna ... yaz', 'X'e WhatsApp'tan ... at' gibi adı verdiğinde ve "
            "rehberde numara yoksa BU TOOL'u kullan. "
            "Tam eşleşme varsa otomatik gönderir; birden fazla aday varsa adayları döndürür ve "
            "kullanıcıdan onay ister. Onay sonrası confirmed_match parametresiyle aynı tool'u "
            "yeniden çağır."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "recipient_name": {
                    "type": "STRING",
                    "description": "Sohbet listesindeki kişi veya grup adı. Örn: 'Notlarım', 'DERİN DEVLET'",
                },
                "message": {
                    "type": "STRING",
                    "description": "Gönderilecek mesaj içeriği",
                },
                "send_now": {
                    "type": "BOOLEAN",
                    "description": "true ise sohbet açıldıktan sonra mesajı otomatik gönderir. Varsayılan true.",
                },
                "confirmed_match": {
                    "type": "STRING",
                    "description": (
                        "Kullanıcı önceki çağrıda gösterilen adaylardan birini onayladıysa 'evet' "
                        "veya seçilen ad yazılabilir. Bu alan SADECE onay bayrağı olarak kullanılır, "
                        "arama kutusuna yazılmaz. İlk çağrıda boş bırak."
                    ),
                },
            },
            "required": ["recipient_name", "message"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# Placeholders for the upcoming Desktop UIA flow (task 9)
# ---------------------------------------------------------------------------


def open_desktop(timeout_sec: float = 6.0) -> bool:
    """Bring WhatsApp Desktop to the foreground; raises NotImplementedError.

    Implemented in task 9. The skeleton keeps the public symbol so the
    Plugin_Host can already import this module.
    """
    raise NotImplementedError(
        "open_desktop is implemented in task 9 (WhatsApp_Bridge full flow)."
    )


def fallback_to_web(phone: str, message: str) -> str:
    """Web fallback entry point reserved for the task 9 Desktop UIA flow."""
    raise NotImplementedError(
        "fallback_to_web is implemented in task 9; v1 Web fallback runs "
        "inside send_whatsapp_message until then."
    )


class ContactSearch:
    """WhatsApp Desktop in-app Contact_Search probe.

    The full UI-Automation implementation (Ctrl+F search box, 1500 ms
    settle, ambiguous/multi-match handling) ships in task 9.
    """

    def search(self, name: str) -> Any:  # pragma: no cover - skeleton
        raise NotImplementedError(
            "ContactSearch.search is implemented in task 9."
        )
