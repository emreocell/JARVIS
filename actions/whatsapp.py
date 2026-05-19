"""
WhatsApp mesaj gönderme — Windows'ta WhatsApp Desktop veya Web üzerinden çalışır.
Alp Ünlü tarafından yapılmıştır — @alppunlu
Windows uyarlaması

Desteklenen akışlar:
- WhatsApp Desktop URL scheme ile numaraya sohbet açma
- WhatsApp Web üzerinden telefon numarasıyla taslak açma
- Sık kullanılan kişileri kalıcı belleğe kaydetme

Not:
- Otomatik gönderim için PyAutoGUI + clipboard kullanılır.
- Windows'ta WhatsApp Desktop veya Web kullanılabilir.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata
import urllib.parse
import time
from pathlib import Path
from typing import Optional

import pyperclip
import pyautogui

from memory.memory_manager import load_memory, update_memory


# ---------------------------------------------------------------------------
# Logging — logs/debug/whatsapp.log
# ---------------------------------------------------------------------------

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs" / "debug"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_wa_log = logging.getLogger("jarvis.whatsapp")
if not _wa_log.handlers:
    _fh = logging.FileHandler(_LOG_DIR / "whatsapp.log", encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    _wa_log.addHandler(_fh)
    _wa_log.setLevel(logging.DEBUG)


PREFERRED_BROWSERS = ["chrome", "msedge"]
AUTO_SEND_DELAY_SECONDS = 2.0
BASE_DIR = Path(__file__).resolve().parent.parent
PHONEBOOK_FILE = BASE_DIR / "memory" / "phone_book.json"

# WhatsApp Desktop arama akışı için bekleme süreleri (saniye).
WA_LAUNCH_WAIT = 1.5
WA_FOCUS_WAIT = 0.4
WA_SEARCH_OPEN_WAIT = 0.6
WA_SEARCH_TYPE_WAIT = 1.2
WA_CHAT_OPEN_WAIT = 1.0
WA_MESSAGE_BOX_WAIT = 0.6

# Aday isimleri ekrandan okumak için kullanılan UIA timeout (sn).
WA_UIA_LOOKUP_TIMEOUT = 1.5


def _normalize_phone(phone_number: str) -> str:
    digits = re.sub(r"\D+", "", phone_number or "")
    if len(digits) == 11 and digits.startswith("0"):
        digits = "90" + digits[1:]
    elif len(digits) == 10:
        digits = "90" + digits
    if len(digits) < 8 or len(digits) > 15:
        raise ValueError(
            "Telefon numarası uluslararası formatta olmalı. "
            "Örn: +905551112233"
        )
    return digits


def _normalize_lookup(text: str) -> str:
    text = (text or "").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ı", "i")
    text = re.sub(r"\s+", " ", text)
    return text


def _contact_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _normalize_lookup(name)).strip("_") or "contact"


def _load_contacts() -> dict:
    memory = load_memory()
    contacts = memory.get("whatsapp_contacts", {})
    return contacts if isinstance(contacts, dict) else {}


def _load_phone_book() -> dict:
    try:
        if PHONEBOOK_FILE.exists():
            return json.loads(PHONEBOOK_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_phone_book(phone_book: dict):
    PHONEBOOK_FILE.parent.mkdir(parents=True, exist_ok=True)
    PHONEBOOK_FILE.write_text(
        json.dumps(phone_book, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _contact_candidates() -> list[dict]:
    candidates = []
    for source_name, source in (("whatsapp", _load_contacts()), ("phone_book", _load_phone_book())):
        if not isinstance(source, dict):
            continue
        for key, entry in source.items():
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item.setdefault("display_name", key)
            item["_source"] = source_name
            item["_key"] = key
            candidates.append(item)
    return candidates


def _match_score(needle: str, candidate: str) -> int:
    candidate_norm = _normalize_lookup(candidate)
    if not candidate_norm:
        return 0
    if candidate_norm == needle:
        return 300
    if candidate_norm.startswith(needle) or needle.startswith(candidate_norm):
        return 220
    if needle in candidate_norm:
        return 160
    needle_parts = needle.split()
    if needle_parts and all(part in candidate_norm for part in needle_parts):
        return 120
    return 0


def _find_contact(recipient_name: str) -> dict | None:
    needle = _normalize_lookup(recipient_name)
    if not needle:
        return None

    best_match = None
    best_score = 0
    for entry in _contact_candidates():
        names = [entry.get("display_name", ""), entry.get("_key", "")]
        aliases = entry.get("aliases", [])
        if isinstance(aliases, list):
            names.extend(str(alias) for alias in aliases)
        elif aliases:
            names.append(str(aliases))

        for name in names:
            score = _match_score(needle, name)
            if score > best_score:
                best_score = score
                best_match = entry

    return best_match


def save_whatsapp_contact(display_name: str, phone_number: str, aliases: str = "") -> str:
    if not display_name or not display_name.strip():
        return "Kişi adı boş olamaz."

    try:
        normalized_phone = _normalize_phone(phone_number)
    except ValueError as exc:
        return str(exc)

    alias_list = []
    if aliases and aliases.strip():
        alias_list = [part.strip() for part in aliases.split(",") if part.strip()]

    key = _contact_key(display_name)
    update_memory(
        {
            "whatsapp_contacts": {
                key: {
                    "value": f"+{normalized_phone}",
                    "display_name": display_name.strip(),
                    "aliases": alias_list,
                }
            }
        }
    )

    if alias_list:
        return f"{display_name.strip()} WhatsApp kişilerine kaydedildi. Takma adlar: {', '.join(alias_list)}"
    return f"{display_name.strip()} WhatsApp kişilerine kaydedildi."


def _unfold_vcf_lines(text: str) -> list[str]:
    unfolded = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def import_phone_book_from_vcf(vcf_path: str) -> str:
    source = Path(vcf_path).expanduser()
    if not source.exists():
        return f"Rehber dosyası bulunamadı: {source}"

    try:
        text = source.read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        return f"Rehber dosyası okunamadı: {exc}"

    entries = {}
    current_lines = []
    imported = 0
    skipped = 0

    def _flush_card(lines: list[str]):
        nonlocal imported, skipped
        if not lines:
            return
        display_name = ""
        aliases = []
        numbers = []
        for line in lines:
            upper = line.upper()
            if upper.startswith("FN:"):
                display_name = line.split(":", 1)[1].strip()
            elif upper.startswith("N:") and not display_name:
                parts = [part.strip() for part in line.split(":", 1)[1].split(";") if part.strip()]
                if parts:
                    display_name = " ".join(reversed(parts[:2])).strip()
            elif "TEL" in upper and ":" in line:
                number = line.split(":", 1)[1].strip()
                if number:
                    numbers.append(number)

        if not display_name or not numbers:
            skipped += 1
            return

        normalized_numbers = []
        for raw_number in numbers:
            try:
                normalized_numbers.append("+" + _normalize_phone(raw_number))
            except ValueError:
                continue
        if not normalized_numbers:
            skipped += 1
            return

        if " " in display_name:
            aliases.extend(part for part in display_name.split() if len(part) > 1)
        key = _contact_key(display_name)
        entries[key] = {
            "display_name": display_name,
            "value": normalized_numbers[0],
            "numbers": normalized_numbers,
            "aliases": sorted({alias for alias in aliases if _normalize_lookup(alias) != _normalize_lookup(display_name)}),
            "source": "vcf_import",
        }
        imported += 1

    for line in _unfold_vcf_lines(text):
        if line.upper() == "BEGIN:VCARD":
            current_lines = []
        elif line.upper() == "END:VCARD":
            _flush_card(current_lines)
            current_lines = []
        else:
            current_lines.append(line)

    phone_book = _load_phone_book()
    phone_book.update(entries)
    _save_phone_book(phone_book)
    return f"{imported} rehber kişisi içe aktarıldı, {skipped} kayıt atlandı."


def _copy_to_clipboard(text: str) -> None:
    """Windows'ta panoya kopyala."""
    pyperclip.copy(text)


def _open_in_browser(url: str) -> str:
    """URL'yi varsayılan tarayıcıda aç."""
    try:
        subprocess.Popen(
            ["start", "", url],
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return "default browser"
    except Exception:
        return "unknown"


def _auto_send_with_pyautogui() -> tuple[bool, str]:
    """PyAutoGUI ile Enter tuşu gönder (WhatsApp'ta mesaj gönder)."""
    try:
        time.sleep(AUTO_SEND_DELAY_SECONDS)
        pyautogui.press('enter')
        return True, "Mesaj gönderildi."
    except Exception as exc:
        return False, f"PyAutoGUI hatası: {exc}"


def _open_whatsapp_desktop_via_scheme(phone_number: str, message: str) -> tuple[bool, str]:
    """WhatsApp Desktop URL scheme ile aç."""
    encoded_message = urllib.parse.quote(message.strip())
    url = f"whatsapp://send?phone={phone_number}&text={encoded_message}"
    try:
        subprocess.Popen(
            ["start", "", url],
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        return True, "WhatsApp Desktop sohbeti açıldı."
    except Exception as exc:
        return False, f"WhatsApp Desktop açılamadı: {exc}"


def _open_whatsapp_web(phone_number: str, message: str) -> tuple[bool, str]:
    """WhatsApp Web'i tarayıcıda aç."""
    encoded_message = urllib.parse.quote(message.strip())
    url = f"https://web.whatsapp.com/send?phone={phone_number}&text={encoded_message}"
    try:
        _open_in_browser(url)
        return True, "WhatsApp Web"
    except Exception as exc:
        return False, f"WhatsApp Web açılamadı: {exc}"


def send_whatsapp_message(
    message: str,
    phone_number: str = "",
    recipient_name: str = "",
    send_now: bool = False,
    app_target: str = "auto",
) -> str:
    if not message or not message.strip():
        return "Mesaj boş olamaz."

    app_target = (app_target or "auto").strip().lower()
    if app_target not in {"auto", "desktop", "web"}:
        app_target = "auto"

    normalized_phone = ""
    if phone_number and phone_number.strip():
        try:
            normalized_phone = _normalize_phone(phone_number)
        except ValueError as exc:
            return str(exc)

    resolved_name = recipient_name.strip() if recipient_name else ""
    contact = _find_contact(resolved_name) if resolved_name else None

    if contact and not normalized_phone:
        stored_phone = str(contact.get("value", "")).strip()
        try:
            normalized_phone = _normalize_phone(stored_phone)
        except ValueError:
            normalized_phone = ""
        resolved_name = str(contact.get("display_name", resolved_name)).strip() or resolved_name
        contact_source = contact.get("_source", "")
    else:
        contact_source = ""

    if resolved_name and normalized_phone and (contact is None or contact.get("_source") == "phone_book"):
        alias_list = contact.get("aliases", []) if isinstance(contact, dict) else []
        aliases = ", ".join(str(alias) for alias in alias_list) if alias_list else ""
        save_whatsapp_contact(resolved_name, normalized_phone, aliases=aliases)

    # WhatsApp Desktop URL scheme dene
    if app_target in {"auto", "desktop"}:
        if normalized_phone:
            ok, detail = _open_whatsapp_desktop_via_scheme(normalized_phone, message)
            if ok:
                source_note = " (rehberden bulundu)" if contact_source == "phone_book" else ""
                if not send_now:
                    label = resolved_name or f"+{normalized_phone}"
                    return f"WhatsApp Desktop içinde {label}{source_note} için taslak mesaj açıldı."
                ok_send, send_detail = _auto_send_with_pyautogui()
                if ok_send:
                    label = resolved_name or f"+{normalized_phone}"
                    return f"WhatsApp Desktop üzerinden {label}{source_note} kişisine mesaj gönderildi."
                return (
                    "WhatsApp Desktop sohbeti açıldı ama otomatik gönderim tamamlanamadı. "
                    f"{send_detail}"
                )

    # Numara yoksa: WhatsApp Desktop iç araması ile sohbet/grup bul.
    if not normalized_phone:
        if resolved_name and app_target in {"auto", "desktop"}:
            return send_whatsapp_via_search(
                recipient_name=resolved_name,
                message=message,
                send_now=send_now,
            )
        if resolved_name:
            return (
                f"'{resolved_name}' için kayıtlı bir telefon numarası bulamadım. "
                "WhatsApp Desktop araması yapmak için app_target='desktop' kullan."
            )
        return "WhatsApp mesajı için kişi adı veya telefon numarası gerekli."

    ok, detail = _open_whatsapp_web(normalized_phone, message)
    if not ok:
        return detail

    if not send_now:
        source_note = " (rehberden bulundu)" if contact_source == "phone_book" else ""
        return (
            f"WhatsApp sohbeti {detail} içinde {resolved_name or f'+{normalized_phone}'}{source_note} için taslak mesajla açıldı. "
            "Göndermek için Enter'a bas."
        )

    ok_send, send_detail = _auto_send_with_pyautogui()
    if ok_send:
        label = resolved_name or f"+{normalized_phone}"
        source_note = " (rehberden bulundu)" if contact_source == "phone_book" else ""
        return f"WhatsApp Web üzerinden {label}{source_note} kişisine mesaj gönderildi."

    return (
        "WhatsApp Web sohbeti açıldı ama otomatik gönderim tamamlanamadı. "
        f"{send_detail}"
    )



# ---------------------------------------------------------------------------
# WhatsApp Desktop in-app arama akışı (rehbere kayıtlı olmayan kişi/grup)
# ---------------------------------------------------------------------------
#
# Bu akış WhatsApp Desktop'ın kendi sohbet listesindeki arama kutusunu
# (Ctrl+F) kullanır; numara veya rehber kaydı gerektirmez. Sohbet listesinde
# adı geçen herhangi bir kişi veya grup üzerinden mesaj gönderilebilir.
#
# Akış:
#   1. WhatsApp Desktop'ı aç ve foreground'a getir.
#   2. Ctrl+F ile arama kutusunu odakla.
#   3. Aranan adı pano üzerinden yapıştır, sonuçların oturmasını bekle.
#   4. UIA ile görünür sonuç adlarını oku.
#       - Tam eşleşme varsa Enter ile sohbet aç ve mesaj gönder.
#       - Yoksa adayları döner, ana akış kullanıcıya onay sorar.
#   5. Mesaj kutusuna mesajı yapıştır, send_now=True ise Enter.

# Ad arama için son ekleri temizleme listesi.
_WA_NAME_SUFFIXES: tuple[str, ...] = (
    " grubuna",
    " grubu",
    " grubundan",
    " kişisine",
    " kisisine",
    " ile",
    " sohbetine",
    " sohbetinden",
    " kişisinden",
    " kisisinden",
    " olan kişiye",
    " olan kisiye",
)


def _strip_name_suffix(name: str) -> str:
    """LLM'in 'X grubuna' gibi yazdığı ekleri temizler."""
    cleaned = (name or "").strip()
    if not cleaned:
        return cleaned
    lowered = cleaned.lower()
    for suffix in sorted(_WA_NAME_SUFFIXES, key=len, reverse=True):
        if lowered.endswith(suffix):
            return cleaned[: -len(suffix)].strip()
    return cleaned


def _open_whatsapp_desktop() -> tuple[bool, str]:
    """WhatsApp Desktop'ı aç (zaten açıksa öne getir)."""
    try:
        subprocess.Popen(
            ["start", "", "whatsapp:"],
            shell=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        time.sleep(WA_LAUNCH_WAIT)
        return True, "WhatsApp Desktop açıldı."
    except Exception as exc:
        return False, f"WhatsApp Desktop açılamadı: {exc}"


def _focus_whatsapp_window() -> tuple[bool, str]:
    """WhatsApp penceresini foreground'a getir."""
    window = _resolve_whatsapp_window()
    if window is None:
        return False, "WhatsApp penceresi bulunamadı."
    try:
        window.SetActive()
    except Exception:
        pass
    try:
        window.SetFocus()
    except Exception:
        pass
    time.sleep(WA_FOCUS_WAIT)
    return True, "WhatsApp odaklandı."


def _resolve_whatsapp_window():
    """WhatsApp / WhatsApp Beta / Business penceresini bul, yoksa None."""
    try:
        import uiautomation as auto  # type: ignore

        for title in ("WhatsApp", "WhatsApp Beta", "WhatsApp Business"):
            window = auto.WindowControl(searchDepth=1, Name=title)
            if window.Exists(maxSearchSeconds=1.0):
                return window
    except Exception:
        return None
    return None


def _read_search_results(query: str, limit: int = 8) -> list[str]:
    """Aramadan sonra sohbet listesinde görünen ListItem adlarını topla.

    Sadece ``ListItemControl`` adlarını okuruz. Sohbet listesi WhatsApp'ın
    UIA ağacında bu kontrol türü olarak görünür; "bilgi çubuğu" gibi panel
    etiketleri bu türden değildir, bu yüzden yanlış pozitif vermez.

    Boş liste döndüğünde "okunamadı" anlamına gelir; üst akış bu durumu
    "tek sonuç" varsayar ve Enter ile sohbeti açar.
    """
    out: list[str] = []
    seen: set[str] = set()
    window = _resolve_whatsapp_window()
    if window is None:
        return out

    def visit(node, depth: int = 0) -> None:
        if len(out) >= limit or depth > 14:
            return
        try:
            ctrl = getattr(node, "ControlTypeName", "")
            name = (getattr(node, "Name", "") or "").strip()
        except Exception:
            ctrl, name = "", ""
        if ctrl == "ListItemControl" and name and name not in seen:
            seen.add(name)
            out.append(name)
            if len(out) >= limit:
                return
        try:
            children = node.GetChildren() or []
        except Exception:
            children = []
        for child in children:
            if len(out) >= limit:
                return
            visit(child, depth + 1)

    try:
        visit(window)
    except Exception:
        pass

    return out


def _open_search_and_paste(query: str) -> tuple[bool, str]:
    """Ctrl+F ile arama kutusunu aç ve adı pano üzerinden yapıştır."""
    cleaned = (query or "").strip()
    if not cleaned:
        return False, "Boş arama."
    try:
        pyautogui.hotkey("ctrl", "f")
        time.sleep(WA_SEARCH_OPEN_WAIT)
        pyautogui.hotkey("ctrl", "a")
        pyautogui.press("delete")
        time.sleep(0.1)
        pyperclip.copy(cleaned)
        time.sleep(0.1)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(WA_SEARCH_TYPE_WAIT)
        return True, "Arama yapıldı."
    except Exception as exc:
        return False, f"Arama kutusu kullanılamadı: {exc}"


def _find_message_edit():
    """Açık sohbetteki mesaj giriş kutusunu UIA üzerinden bul.

    WhatsApp Desktop yeni sürümlerinde (Electron) mesaj kutusu
    ``EditControl`` olarak görünmeyebilir. Bu durumda None döner ve
    koordinat tabanlı fallback devreye girer.
    """
    window = _resolve_whatsapp_window()
    if window is None:
        _wa_log.warning("_find_message_edit: WhatsApp penceresi bulunamadı.")
        return None

    needles = (
        "bir mesaj yazın",
        "bir mesaj yaz",
        "type a message",
        "mesaj yaz",
    )
    found = []
    all_edits: list[str] = []

    def visit(node, depth: int = 0) -> None:
        if depth > 16 or len(found) >= 3:
            return
        try:
            ctrl = getattr(node, "ControlTypeName", "")
            name = (getattr(node, "Name", "") or "").strip()
        except Exception:
            ctrl, name = "", ""
        if ctrl == "EditControl":
            all_edits.append(repr(name))
            name_lower = name.lower()
            for n in needles:
                if n in name_lower:
                    found.append(node)
                    return
        try:
            children = node.GetChildren() or []
        except Exception:
            children = []
        for child in children:
            visit(child, depth + 1)

    try:
        visit(window)
    except Exception as exc:
        _wa_log.error("_find_message_edit: UIA tarama hatası: %s", exc)
        return None

    _wa_log.debug(
        "_find_message_edit: tüm EditControl adları: %s | eşleşen: %d",
        ", ".join(all_edits) or "(yok)",
        len(found),
    )
    return found[0] if found else None


def _get_whatsapp_window_rect() -> Optional[tuple]:
    """WhatsApp penceresinin (left, top, right, bottom) koordinatlarını döner."""
    window = _resolve_whatsapp_window()
    if window is None:
        return None
    try:
        rect = window.BoundingRectangle
        left = getattr(rect, "left", None)
        top = getattr(rect, "top", None)
        right = getattr(rect, "right", None)
        bottom = getattr(rect, "bottom", None)
        if None in (left, top, right, bottom):
            return None
        if right <= left or bottom <= top:
            return None
        return (left, top, right, bottom)
    except Exception:
        return None


def _click_message_box_by_coords() -> bool:
    """Mesaj kutusunu koordinat hesabıyla tıkla.

    WhatsApp Desktop'ta mesaj kutusu her zaman pencerenin alt kısmında,
    yatayda ortada yer alır. Pencere koordinatlarından hesaplanır:
      - X: pencerenin yatay ortası
      - Y: pencerenin alt kenarından ~40 px yukarı
    """
    rect = _get_whatsapp_window_rect()
    if rect is None:
        _wa_log.warning("_click_message_box_by_coords: pencere rect alınamadı.")
        return False
    left, top, right, bottom = rect
    cx = (left + right) // 2
    cy = bottom - 40  # mesaj kutusu alt kenardan ~40px yukarıda
    _wa_log.debug(
        "_click_message_box_by_coords: pencere=(%d,%d,%d,%d) tık=(%d,%d)",
        left, top, right, bottom, cx, cy,
    )
    try:
        pyautogui.click(cx, cy)
        time.sleep(0.2)
        return True
    except Exception as exc:
        _wa_log.error("_click_message_box_by_coords: tık hatası: %s", exc)
        return False


def _click_center(ctrl) -> bool:
    """UIA kontrolünün merkezine fare tıklaması yap. Odak garantilemek için."""
    try:
        rect = ctrl.BoundingRectangle
    except Exception:
        return False
    try:
        # uiautomation Rect: left, top, right, bottom
        left = getattr(rect, "left", None)
        top = getattr(rect, "top", None)
        right = getattr(rect, "right", None)
        bottom = getattr(rect, "bottom", None)
        if None in (left, top, right, bottom):
            return False
        if right <= left or bottom <= top:
            return False
        cx = (left + right) // 2
        cy = (top + bottom) // 2
        pyautogui.click(cx, cy)
        time.sleep(0.15)
        return True
    except Exception:
        return False


def _send_message_in_open_chat(message: str, send_now: bool) -> tuple[bool, str]:
    """Açık sohbet penceresine mesajı yapıştır ve istenirse gönder.

    Odak stratejisi (sırayla dener):
      1. UIA ile EditControl bul → SetFocus + merkeze tık.
      2. UIA bulamazsa (Electron/yeni WhatsApp) → pencere koordinatından
         mesaj kutusu konumunu hesapla ve tıkla.
    """
    try:
        time.sleep(WA_MESSAGE_BOX_WAIT)

        focused = False

        # 1) UIA yolu.
        edit_ctrl = _find_message_edit()
        if edit_ctrl is not None:
            try:
                edit_ctrl.SetFocus()
                _wa_log.debug("_send_message_in_open_chat: UIA SetFocus OK")
            except Exception as exc:
                _wa_log.warning("_send_message_in_open_chat: SetFocus hatası: %s", exc)
            time.sleep(0.1)
            clicked = _click_center(edit_ctrl)
            _wa_log.debug("_send_message_in_open_chat: UIA _click_center=%s", clicked)
            focused = True
        else:
            # 2) Koordinat fallback.
            _wa_log.debug(
                "_send_message_in_open_chat: EditControl yok, koordinat fallback."
            )
            clicked = _click_message_box_by_coords()
            _wa_log.debug(
                "_send_message_in_open_chat: koordinat tık=%s", clicked
            )
            focused = clicked

        if not focused:
            msg = "Mesaj kutusuna odaklanılamadı (UIA ve koordinat yöntemi başarısız)."
            _wa_log.error("_send_message_in_open_chat: %s", msg)
            return False, msg

        time.sleep(0.2)

        # Paste.
        pyperclip.copy(message)
        time.sleep(0.15)
        pyautogui.hotkey("ctrl", "v")
        _wa_log.debug("_send_message_in_open_chat: paste yapıldı, mesaj=%r", message)
        time.sleep(0.4)

        if send_now:
            pyautogui.press("enter")
            time.sleep(0.25)
            _wa_log.info("_send_message_in_open_chat: mesaj gönderildi.")
            return True, "Mesaj gönderildi."
        _wa_log.info("_send_message_in_open_chat: taslak yazıldı.")
        return True, "Mesaj taslağa yazıldı."
    except Exception as exc:
        _wa_log.exception("_send_message_in_open_chat: beklenmeyen hata")
        return False, f"Mesaj kutusu kullanılamadı: {exc}"


def send_whatsapp_via_search(
    recipient_name: str,
    message: str,
    send_now: bool = True,
    confirmed_match: str = "",
) -> str:
    """
    WhatsApp Desktop'ın iç arama kutusuyla sohbet aç ve mesaj gönder.

    Numara/rehber kaydı gerektirmez. Sohbet listesinde adı geçen kişi veya
    grup üzerinden çalışır.

    Akış:
      1. WhatsApp Desktop'ı aç + foreground.
      2. Ctrl+F → ``recipient_name`` arama kutusuna yapıştırılır.
         (``confirmed_match`` aramada KULLANILMAZ; sadece "kullanıcı zaten
         onayladı, ilk sonucu sor sorma aç" anlamına gelir.)
      3. ListItemControl adlarını UIA ile oku.
         - Tek aday VEYA tam eşleşme varsa Enter ile aç ve mesajı gönder.
         - Birden fazla aday varsa ve onay yoksa adayları döndür, onay iste.
         - Aday okunamadıysa (UIA boş) "tek sonuç" varsay ve gönder.
      4. ``confirmed_match`` boş değilse adayların ne olduğuna bakmadan
         ilk sonucu kabul et ve gönder.
    """
    if not message or not message.strip():
        return "Mesaj boş olamaz."

    name = _strip_name_suffix(recipient_name)
    if not name:
        return "WhatsApp araması için kişi/grup adı gerekli."

    is_confirmed = bool((confirmed_match or "").strip())

    ok, detail = _open_whatsapp_desktop()
    if not ok:
        return detail

    ok, detail = _focus_whatsapp_window()
    if not ok:
        return detail

    # Aramada her zaman kullanıcının orijinal istediği adı kullan; "confirmed_match"
    # sadece bir izin bayrağıdır, arama metni değil.
    ok, detail = _open_search_and_paste(name)
    if not ok:
        _wa_log.error("send_whatsapp_via_search: arama başarısız: %s", detail)
        return f"WhatsApp araması başarısız: {detail}"

    candidates = _read_search_results(name, limit=5)
    _wa_log.debug("send_whatsapp_via_search: adaylar=%s", candidates)
    needle = _normalize_lookup(name)

    exact_match = None
    for candidate_name in candidates:
        if _normalize_lookup(candidate_name) == needle:
            exact_match = candidate_name
            break

    # Onay yokken birden fazla aday → kullanıcıdan seçim iste, gönderme yapma.
    if not is_confirmed and exact_match is None and len(candidates) > 1:
        top = ", ".join(candidates[:3])
        return (
            f"'{name}' için tam eşleşme bulamadım. "
            f"En yakın eşleşmeler: {top}. "
            "Hangisine göndereyim?"
        )

    # Enter ile ilk sonucu aç.
    try:
        pyautogui.press("enter")
        time.sleep(WA_CHAT_OPEN_WAIT)
    except Exception as exc:
        return f"Sohbet açılamadı: {exc}"

    label = exact_match or (candidates[0] if candidates else name)

    if not send_now:
        ok_msg, detail_msg = _send_message_in_open_chat(message, send_now=False)
        if not ok_msg:
            return detail_msg
        return f"WhatsApp Desktop'ta {label} sohbeti açıldı, mesaj taslakta."

    ok_msg, detail_msg = _send_message_in_open_chat(message, send_now=True)
    if not ok_msg:
        return f"Sohbet açıldı ama mesaj gönderilemedi: {detail_msg}"
    return f"WhatsApp Desktop üzerinden {label} kişisine/grubuna mesaj gönderildi."
