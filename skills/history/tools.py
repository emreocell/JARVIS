"""History skill tools — konuşma geçmişinde arama.

`logs/conversation/{YYYY-MM-DD}.jsonl` dosyalarını tarar; query'yi text
veya tool_name alanında arar; ilk 10 eşleşmeyi döner.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

# ConversationLogger referansı (main.py bootstrap'ta atanır)
_logger = None


def set_logger(logger) -> None:
    """ConversationLogger referansını ata."""
    global _logger
    _logger = logger


def _default_log_dir() -> Path:
    return Path(__file__).resolve().parent.parent.parent / "logs" / "conversation"


def search_history(
    query: str,
    since: Optional[str] = None,
    until: Optional[str] = None,
    role: Optional[str] = None,
) -> str:
    """Konuşma geçmişinde arama yap.

    Parameters
    ----------
    query:
        Aranacak metin (text veya tool_name içinde geçmeli).
    since:
        Başlangıç tarihi (YYYY-MM-DD). Opsiyonel.
    until:
        Bitiş tarihi (YYYY-MM-DD). Opsiyonel.
    role:
        Filtre: user/assistant/tool/system. Opsiyonel.

    Returns
    -------
    str
        İlk 10 eşleşme veya bilgi mesajı.
    """
    if not query or not query.strip():
        return "Arama için bir kelime girin."

    q = query.strip().lower()

    # Log dizinini al
    log_dir: Path
    if _logger is not None:
        try:
            log_dir = Path(_logger.log_dir)
        except Exception:
            log_dir = _default_log_dir()
    else:
        log_dir = _default_log_dir()

    if not log_dir.exists():
        return "Henüz konuşma geçmişi kaydı yok."

    # Tarih sınırlarını parse et
    since_dt = None
    until_dt = None
    if since:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").date()
        except ValueError:
            return f"Geçersiz tarih formatı 'since' için: {since} (YYYY-MM-DD bekleniyor)."
    if until:
        try:
            until_dt = datetime.strptime(until, "%Y-%m-%d").date()
        except ValueError:
            return f"Geçersiz tarih formatı 'until' için: {until} (YYYY-MM-DD bekleniyor)."

    matches: list[str] = []

    for jsonl_file in sorted(log_dir.glob("*.jsonl")):
        # Tarih aralığı kontrolü
        try:
            file_date = datetime.strptime(jsonl_file.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if since_dt and file_date < since_dt:
            continue
        if until_dt and file_date > until_dt:
            continue

        try:
            with jsonl_file.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    text = str(data.get("text", "")).lower()
                    tool_name = str(data.get("tool_name", "") or "").lower()
                    entry_role = data.get("role", "")

                    if role and entry_role != role:
                        continue

                    if q in text or q in tool_name:
                        ts = data.get("ts", "")[:19].replace("T", " ")
                        r = data.get("role", "?")
                        snippet = data.get("text", "")[:120]
                        matches.append(f"[{ts}] {r}: {snippet}")
                        if len(matches) >= 10:
                            break
        except OSError:
            continue

        if len(matches) >= 10:
            break

    # Privacy skip count
    privacy_note = ""
    if _logger is not None:
        try:
            skips = _logger.privacy_skip_count
            if skips > 0:
                privacy_note = (
                    f"\n\nNot: {skips} kayıt Privacy Mode aktifken atlandı; "
                    "o aralıklara ait kayıt yok."
                )
        except Exception:
            pass

    if not matches:
        return f"'{query}' için eşleşme bulunamadı.{privacy_note}"

    return f"'{query}' için {len(matches)} sonuç:\n\n" + "\n".join(matches) + privacy_note


search_history.__tool__ = {
    "declaration": {
        "name": "search_history",
        "description": (
            "Konuşma geçmişinde anahtar kelime arar. "
            "Kullanıcı 'geçmişte X dedi mi?', 'şu konuyu bulabilir misin?' "
            "gibi sorularda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Aranacak anahtar kelime veya kelime grubu.",
                },
                "since": {
                    "type": "STRING",
                    "description": "Başlangıç tarihi YYYY-MM-DD (opsiyonel).",
                },
                "until": {
                    "type": "STRING",
                    "description": "Bitiş tarihi YYYY-MM-DD (opsiyonel).",
                },
                "role": {
                    "type": "STRING",
                    "description": "Filtre: user, assistant, tool, system (opsiyonel).",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


__all__ = ["search_history", "set_logger"]
