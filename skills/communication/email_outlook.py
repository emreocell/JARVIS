"""Email_Skill — Outlook COM üzerinden e-posta okuma ve gönderme.

Design.md § 9 ve Requirements § 21'e karşılık gelir.

Sorumluluklar
-------------
* read_emails(count, folder): son N e-postayı sender, subject, özet, unread
  bayrağıyla döner (Req 21.1, 21.2).
* send_email(to, subject, body, cc): MailItem oluştur ve .Send() (Req 21.3).
* pywintypes.com_error → Türkçe hata mesajı (Req 21.4).
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_OUTLOOK_NOT_FOUND = (
    "Outlook bulunamadı veya yapılandırılmamış. "
    "Lütfen Microsoft Outlook'un yüklü ve bir hesapla yapılandırılmış olduğundan emin olun."
)
_SUMMARY_CHARS = 200


def _get_outlook():
    """Outlook.Application COM nesnesini döner."""
    try:
        import win32com.client
        return win32com.client.Dispatch("Outlook.Application")
    except ImportError:
        raise RuntimeError("pywin32 yüklü değil. 'pip install pywin32' çalıştırın.")
    except Exception as exc:
        # pywintypes.com_error dahil tüm COM hatalarını yakala
        raise RuntimeError(_OUTLOOK_NOT_FOUND) from exc


def read_emails(count: int = 10, folder: str = "Inbox") -> str:
    """Son N e-postayı oku.

    Parameters
    ----------
    count:
        Okunacak e-posta sayısı (Req 21.2).
    folder:
        Klasör adı (varsayılan: "Inbox").

    Returns
    -------
    str
        Okunabilir e-posta özeti.
    """
    count = max(1, min(count, 50))  # 1–50 arası sınırla

    try:
        outlook = _get_outlook()
        namespace = outlook.GetNamespace("MAPI")
        inbox = namespace.GetDefaultFolder(6)  # 6 = olInbox

        # Farklı klasör isteniyorsa bul
        if folder.lower() not in ("inbox", "gelen kutusu"):
            try:
                inbox = namespace.Folders.Item(1).Folders[folder]
            except Exception:
                return f"'{folder}' klasörü bulunamadı."

        items = inbox.Items
        items.Sort("[ReceivedTime]", True)  # en yeni önce

        results: list[str] = []
        for i in range(min(count, items.Count)):
            try:
                mail = items.Item(i + 1)
                sender = getattr(mail, "SenderName", "Bilinmiyor")
                subject = getattr(mail, "Subject", "(Konu yok)")
                body = getattr(mail, "Body", "") or ""
                summary = body.strip()[:_SUMMARY_CHARS].replace("\n", " ")
                unread = getattr(mail, "UnRead", False)
                unread_tag = " [OKUNMADI]" if unread else ""
                results.append(
                    f"{i+1}. {sender} — {subject}{unread_tag}\n   {summary}"
                )
            except Exception as exc:
                log.debug("read_emails: öğe %d okunamadı: %s", i + 1, exc)

        if not results:
            return f"'{folder}' klasöründe e-posta bulunamadı."

        return f"Son {len(results)} e-posta ({folder}):\n\n" + "\n\n".join(results)

    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        log.warning("read_emails hatası: %s", exc)
        return _OUTLOOK_NOT_FOUND


def send_email(to: str, subject: str, body: str, cc: str = "") -> str:
    """E-posta gönder.

    Parameters
    ----------
    to:
        Alıcı e-posta adresi.
    subject:
        Konu.
    body:
        Mesaj gövdesi.
    cc:
        CC adresi (opsiyonel).

    Returns
    -------
    str
        Başarı veya hata mesajı.
    """
    try:
        outlook = _get_outlook()
        mail = outlook.CreateItem(0)  # 0 = olMailItem
        mail.To = to
        mail.Subject = subject
        mail.Body = body
        if cc:
            mail.CC = cc
        mail.Send()
        log.info("send_email: '%s' adresine gönderildi.", to)
        return f"E-posta '{to}' adresine başarıyla gönderildi."
    except RuntimeError as exc:
        return str(exc)
    except Exception as exc:
        log.warning("send_email hatası: %s", exc)
        return f"E-posta gönderilemedi: {exc}"


# Tool metadata
read_emails.__tool__ = {
    "declaration": {
        "name": "read_emails",
        "description": (
            "Outlook gelen kutusundaki son e-postaları okur. "
            "Gönderen, konu, özet ve okunmamış durumunu gösterir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "count": {
                    "type": "NUMBER",
                    "description": "Okunacak e-posta sayısı (varsayılan: 10, max: 50).",
                },
                "folder": {
                    "type": "STRING",
                    "description": "Klasör adı (varsayılan: Inbox).",
                },
            },
        },
    },
    "execution_mode": "background",
}

send_email.__tool__ = {
    "declaration": {
        "name": "send_email",
        "description": "Outlook üzerinden e-posta gönderir.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "to": {
                    "type": "STRING",
                    "description": "Alıcı e-posta adresi.",
                },
                "subject": {
                    "type": "STRING",
                    "description": "E-posta konusu.",
                },
                "body": {
                    "type": "STRING",
                    "description": "E-posta gövdesi.",
                },
                "cc": {
                    "type": "STRING",
                    "description": "CC adresi (opsiyonel).",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    "execution_mode": "background",
}

__all__ = ["read_emails", "send_email"]
