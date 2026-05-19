"""Clipboard skill tools — pano geçmişi ve geri çağırma.

Design.md § 10 ve Requirements § 22'ye karşılık gelir.

Sorumluluklar
-------------
* clipboard_history(): son 10 girdi indeksli özet (Req 22.3).
* clipboard_recall(index): pyperclip.copy(...) ile metni geri yazar (Req 22.4).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# ClipboardManager referansı — main.py bootstrap'ta set_clipboard_manager() ile atanır
_clipboard_manager = None


def set_clipboard_manager(manager) -> None:
    """ClipboardManager referansını ata (bootstrap sırasında çağrılır)."""
    global _clipboard_manager
    _clipboard_manager = manager


def clipboard_history() -> str:
    """Son 10 pano girişini indeksli özet olarak döner.

    Returns
    -------
    str
        Numaralı liste formatında pano geçmişi.
    """
    if _clipboard_manager is None:
        return "Pano yöneticisi başlatılmamış."

    try:
        entries = _clipboard_manager.history(count=10)
    except Exception as exc:
        log.warning("clipboard_history hatası: %s", exc)
        return f"Pano geçmişi alınamadı: {exc}"

    if not entries:
        return "Pano geçmişi boş."

    lines: list[str] = []
    for i, entry in enumerate(entries):
        preview = entry.text[:80].replace("\n", " ")
        if len(entry.text) > 80:
            preview += "…"
        app = f" [{entry.source_app}]" if entry.source_app else ""
        lines.append(f"{i}. {preview}{app}")

    return "Pano geçmişi:\n" + "\n".join(lines)


def clipboard_recall(index: int) -> str:
    """Belirtilen indeksteki pano girişini panoya geri yazar.

    Parameters
    ----------
    index:
        clipboard_history() çıktısındaki sıra numarası.

    Returns
    -------
    str
        Başarı veya hata mesajı.
    """
    if _clipboard_manager is None:
        return "Pano yöneticisi başlatılmamış."

    try:
        text = _clipboard_manager.recall(int(index))
    except IndexError:
        return f"Geçersiz indeks: {index}. clipboard_history ile mevcut girişleri kontrol edin."
    except Exception as exc:
        log.warning("clipboard_recall hatası: %s", exc)
        return f"Pano geri çağırma başarısız: {exc}"

    try:
        import pyperclip
        pyperclip.copy(text)
    except Exception as exc:
        log.warning("clipboard_recall pyperclip hatası: %s", exc)
        return f"Metin panoya yazılamadı: {exc}"

    preview = text[:60].replace("\n", " ")
    if len(text) > 60:
        preview += "…"
    return f"Pano güncellendi: {preview}"


# Tool metadata
clipboard_history.__tool__ = {
    "declaration": {
        "name": "clipboard_history",
        "description": (
            "Son 10 pano girişini indeksli özet olarak listeler. "
            "Kullanıcı 'panoda ne vardı?' veya 'kopyaladıklarımı göster' dediğinde kullan."
        ),
    },
    "execution_mode": "inline",
}

clipboard_recall.__tool__ = {
    "declaration": {
        "name": "clipboard_recall",
        "description": (
            "Pano geçmişindeki belirtilen indeksteki metni panoya geri yazar. "
            "clipboard_history ile önce listeyi göster, sonra kullanıcı indeks seçsin."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "index": {
                    "type": "NUMBER",
                    "description": "clipboard_history çıktısındaki sıra numarası (0'dan başlar).",
                },
            },
            "required": ["index"],
        },
    },
    "execution_mode": "inline",
}

__all__ = ["clipboard_history", "clipboard_recall", "set_clipboard_manager"]
