"""Productivity skill tool implementations.

İçerdiği handler'lar (tümü ``inline`` modda çalışır — tipik süre <2 sn):

- :func:`get_calendar_events` — Windows Outlook Calendar takviminden etkinlik
  özeti döner (PowerShell + Outlook COM interop).
- :func:`add_calendar_event` — Outlook Calendar'a yeni etkinlik ekler.
- :func:`delete_calendar_event` — Outlook Calendar'dan etkinlik siler.
- :func:`get_reminders` — Yerel JSON dosyasındaki hatırlatıcıları özetler.
- :func:`add_reminder` — Yeni bir hatırlatıcı ekler.
- :func:`get_weather_summary` — Uzaktaki ``wttr.in`` servisinden anlık hava
  durumu özeti döner. Bu handler ayrıca ``get_weather`` tool adı altında
  yayımlanır.

Her tool, Plugin_Host'un kayıt sırasında okuyacağı ``__tool__`` metadata
sözlüğünü dosyanın sonunda fonksiyona ekler. ``declaration`` alanı Gemini
function-calling şemasına bire bir uyar ve eski ``main.TOOL_DECLARATIONS``
listesinden taşınmıştır.

Tool sonuçları her zaman Türkçe ve voice-friendly tek paragraflık (veya
kısa satırlı liste) metindir.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import requests


# ---------------------------------------------------------------------------
# Ortak yardımcılar
# ---------------------------------------------------------------------------


TR_WEEKDAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
TR_MONTHS = [
    "",
    "Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran",
    "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım", "Aralık",
]

UNSUPPORTED_MESSAGE = "Bu özellik Windows'ta desteklenmiyor"

# Outlook bulunmadığında veya COM hatası raporladığında PowerShell çıktısında
# bu işaretçileri arıyoruz.
_OUTLOOK_MISSING_MARKERS = (
    "Outlook.Application",
    "0x80040154",
    "InteropServices.COMException",
    "GetComObject",
    "operable program",
    "is not recognized",
)


def _outlook_unavailable(detail: str = "") -> str:
    """Windows'ta Outlook erişimi sağlanamadığında dönen Türkçe mesaj."""
    if detail:
        return f"{UNSUPPORTED_MESSAGE}: {detail}"
    return f"{UNSUPPORTED_MESSAGE}."


def _looks_like_outlook_missing(*texts: str) -> bool:
    blob = " ".join(t for t in texts if t)
    return any(marker in blob for marker in _OUTLOOK_MISSING_MARKERS)


def _ps_single_quoted(value: str) -> str:
    """PowerShell tek tırnaklı güvenli string."""
    return "'" + str(value or "").replace("'", "''") + "'"


def _day_label(when: dt.datetime, now: dt.datetime) -> str:
    today = now.date()
    target = when.date()
    if target == today:
        return "bugün"
    if target == today + dt.timedelta(days=1):
        return "yarın"
    return f"{when.day} {TR_MONTHS[when.month]} {TR_WEEKDAYS[when.weekday()]}"


# ---------------------------------------------------------------------------
# get_calendar_events / add_calendar_event / delete_calendar_event
# ---------------------------------------------------------------------------


def _normalize_calendar_query(query: str) -> dict:
    q = (query or "today").strip().lower()

    month_match = re.search(r"(\d+)\s*(ay|month|months)", q)
    if "gelecek ay" in q or "önümüzdeki ay" in q or "next month" in q:
        return {"mode": "next_month", "days": 30}
    if "bu ay" in q or "this month" in q:
        return {"mode": "this_month", "days": 30}
    if month_match:
        months = max(1, min(12, int(month_match.group(1))))
        return {"mode": "months", "days": months * 30}
    if "yarın" in q or "tomorrow" in q:
        return {"mode": "tomorrow", "days": 1}
    if any(token in q for token in ("hafta", "week", "7 gun")):
        return {"mode": "week", "days": 7}
    if any(token in q for token in ("sıradaki", "sonraki", "next")):
        return {"mode": "next", "days": 1}
    return {"mode": "today", "days": 1}


def _get_outlook_events(days: int = 1) -> tuple[list[dict], str | None]:
    """Windows Outlook'tan etkinlikleri al (PowerShell ile)."""
    try:
        ps_script = f'''
        Add-Type -AssemblyName Microsoft.Office.Interop.Outlook
        $outlook = New-Object -ComObject Outlook.Application
        $namespace = $outlook.GetNamespace("MAPI")
        $calendar = $namespace.GetDefaultFolder(9)
        $start = (Get-Date).Date
        $end = (Get-Date).Date.AddDays({days})
        $start_str = $start.ToString("yyyy-MM-dd")
        $end_str = $end.ToString("yyyy-MM-dd")
        $filter = "[Start] >= '$start_str' AND [Start] <= '$end_str 23:59'"
        $items = $calendar.Items
        $items.Sort("[Start]")
        $items.IncludeRecurrences = $true
        $filtered = $items.Restrict($filter)
        $results = @()
        foreach ($item in $filtered) {{
            $results += [PSCustomObject]@{{
                Title = $item.Subject
                Start = $item.Start
                End = $item.End
                Location = $item.Location
                AllDay = $item.AllDayEvent
            }}
        }}
        $results | ConvertTo-Json -Compress
        '''
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
            encoding='utf-8',
            errors='replace',
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()

        if result.returncode != 0 or _looks_like_outlook_missing(stdout, stderr):
            return [], _outlook_unavailable(
                "Outlook bulunamadı veya COM erişimi sağlanamadı"
            )

        if stdout:
            try:
                data = json.loads(stdout)
                if isinstance(data, dict):
                    return [data], None
                return (data if isinstance(data, list) else []), None
            except json.JSONDecodeError:
                pass
    except FileNotFoundError:
        return [], _outlook_unavailable("PowerShell çalıştırılamadı")
    except Exception:
        # Beklenmeyen hatalarda boş liste dönüyor; çağıran tarafta doğal mesaj üretilir.
        pass
    return [], None


def _format_event_line(event: dict, now: dt.datetime) -> str:
    title = event.get("title", event.get("Title", "Adsız etkinlik"))
    start = event.get("start", event.get("Start", ""))
    end = event.get("end", event.get("End", ""))
    location = event.get("location", event.get("Location", ""))
    all_day = event.get("all_day", event.get("AllDay", False))

    if isinstance(start, str):
        try:
            start_dt = dt.datetime.fromisoformat(start.replace(" ", "T"))
        except Exception:
            start_dt = now
    else:
        start_dt = start

    if isinstance(end, str):
        try:
            end_dt = dt.datetime.fromisoformat(end.replace(" ", "T"))
        except Exception:
            end_dt = start_dt
    else:
        end_dt = end

    day_label = _day_label(start_dt, now)

    if all_day:
        time_str = f"{day_label} tüm gün"
    else:
        time_str = f"{day_label} {start_dt.strftime('%H:%M')}-{end_dt.strftime('%H:%M')}"

    parts = [f"{time_str} - {title}"]
    if location:
        parts.append(f"@ {location}")
    return " ".join(parts)


def get_calendar_events(query: str = "today", limit: int = 6) -> str:
    window = _normalize_calendar_query(query)
    events, error = _get_outlook_events(days=window["days"])

    if error:
        return error

    if not events:
        return "Takvimde etkinlik bulunamadı."

    selected = events[: max(1, int(limit or 6))]
    header = f"{len(selected)} etkinlik bulundu:"

    now = dt.datetime.now()
    lines = [header]
    for event in selected:
        lines.append(f"- {_format_event_line(event, now)}")
    return "\n".join(lines)


get_calendar_events.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "get_calendar_events",
        "description": (
            "Windows Outlook Calendar takvimini okur. "
            "Bugun, yarin, siradaki etkinlik veya yaklasan ajandayi ozetler. "
            "Kullanici toplanti, takvim, ajanda, etkinlik veya gunluk programini sordugunda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "today | tomorrow | next | agenda | week veya dogal dilde "
                        "'onumuzdeki 30 gun', '2 hafta', 'bu ay', 'gelecek ay'"
                    ),
                },
                "limit": {
                    "type": "NUMBER",
                    "description": "Maksimum etkinlik sayisi",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


def add_calendar_event(
    title: str,
    start_iso: str = "",
    end_iso: str = "",
    notes: str = "",
    location: str = "",
    calendar_name: str = "",
    all_day: bool = False,
) -> str:
    """Windows Outlook'a etkinlik ekle."""
    title = (title or "").strip()
    if not title:
        return "Takvime eklemek için etkinlik başlığı gerekli."

    try:
        start_dt = dt.datetime.now()
        if start_iso:
            try:
                start_dt = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            except ValueError:
                try:
                    start_dt = dt.datetime.strptime(start_iso, "%Y-%m-%d %H:%M")
                except ValueError:
                    return "Başlangıç tarihi formatı geçersiz. ISO veya yyyy-MM-dd HH:mm kullan."

        if end_iso:
            try:
                end_dt = dt.datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
            except ValueError:
                try:
                    end_dt = dt.datetime.strptime(end_iso, "%Y-%m-%d %H:%M")
                except ValueError:
                    return "Bitiş tarihi formatı geçersiz. ISO veya yyyy-MM-dd HH:mm kullan."
        else:
            end_dt = start_dt + (dt.timedelta(days=1) if all_day else dt.timedelta(hours=1))

        if end_dt <= start_dt:
            end_dt = start_dt + dt.timedelta(minutes=30)

        ps_script = f'''
        try {{
            Add-Type -AssemblyName Microsoft.Office.Interop.Outlook
            $outlook = New-Object -ComObject Outlook.Application
            $namespace = $outlook.GetNamespace("MAPI")
            $calendar = $namespace.GetDefaultFolder(9)
            $appointment = $calendar.Items.Add(1)
            $appointment.Subject = {_ps_single_quoted(title)}
            $appointment.Start = [datetime]{_ps_single_quoted(start_dt.isoformat())}
            $appointment.End = [datetime]{_ps_single_quoted(end_dt.isoformat())}
            $appointment.Location = {_ps_single_quoted(location or "")}
            $appointment.Body = {_ps_single_quoted(notes or "")}
            $appointment.AllDayEvent = {'$true' if all_day else '$false'}
            $appointment.ReminderSet = $false
            $appointment.Save()
            Write-Output "OK"
        }} catch {{
            Write-Output ("ERR:" + $_.Exception.Message)
            exit 1
        }}
        '''
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=30,
            encoding='utf-8',
            errors='replace',
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (result.stdout or "").strip()
        err = (result.stderr or "").strip()
        if "OK" in out:
            return f"Takvime eklendi: {title}"
        if _looks_like_outlook_missing(out, err):
            return _outlook_unavailable(
                "Outlook bulunamadı veya COM erişimi sağlanamadı"
            )
        if out.startswith("ERR:"):
            return f"Takvim etkinliği eklenemedi: {out[4:].strip()}"
        return f"Takvim etkinliği eklenemedi: {err or out or 'Bilinmeyen hata'}"
    except FileNotFoundError:
        return _outlook_unavailable("PowerShell çalıştırılamadı")
    except Exception as exc:
        return f"Takvim etkinliği eklenemedi: {exc}"


add_calendar_event.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "add_calendar_event",
        "description": (
            "Windows Outlook Calendar takvimine yeni etkinlik ekler. "
            "Kullanici toplanti, randevu, takvime ekleme veya etkinlik olusturma isterse kullan. "
            "Baslangic tarihini gercek tarih/saat olarak ver; bitis verilmezse varsayilan sure kullanilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {
                    "type": "STRING",
                    "description": "Etkinlik basligi. Ornek: 'Disci Randevusu'",
                },
                "start_iso": {
                    "type": "STRING",
                    "description": "Baslangic tarih/saat. ISO veya yyyy-MM-dd HH:mm formatinda.",
                },
                "end_iso": {
                    "type": "STRING",
                    "description": "Bitis tarih/saat. Opsiyonel.",
                },
                "location": {
                    "type": "STRING",
                    "description": "Etkinlik konumu. Opsiyonel.",
                },
                "notes": {
                    "type": "STRING",
                    "description": "Etkinlik notlari. Opsiyonel.",
                },
                "calendar_name": {
                    "type": "STRING",
                    "description": "Eklenecek takvim adi. Opsiyonel.",
                },
                "all_day": {
                    "type": "BOOLEAN",
                    "description": "true ise tum gun etkinligi olusturur.",
                },
            },
            "required": ["title", "start_iso"],
        },
    },
    "execution_mode": "inline",
}


def delete_calendar_event(
    title: str,
    start_iso: str = "",
    calendar_name: str = "",
    delete_all_matches: bool = False,
) -> str:
    """Windows Outlook'tan etkinlik sil."""
    title = (title or "").strip()
    start_iso = (start_iso or "").strip()
    if not title and not start_iso:
        return "Silme için en az etkinlik başlığı veya başlangıç tarihi gerekli."

    start_date_filter = ""
    if start_iso:
        try:
            parsed = dt.datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
            start_date_filter = parsed.strftime("%Y-%m-%d")
        except ValueError:
            try:
                parsed = dt.datetime.strptime(start_iso, "%Y-%m-%d %H:%M")
                start_date_filter = parsed.strftime("%Y-%m-%d")
            except ValueError:
                return "Başlangıç tarihi formatı geçersiz. ISO veya yyyy-MM-dd HH:mm kullan."

    ps_script = f'''
    try {{
        Add-Type -AssemblyName Microsoft.Office.Interop.Outlook
        $outlook = New-Object -ComObject Outlook.Application
        $namespace = $outlook.GetNamespace("MAPI")
        $calendar = $namespace.GetDefaultFolder(9)
        $items = $calendar.Items
        $items.Sort("[Start]")
        $items.IncludeRecurrences = $true
        $titleFilter = {_ps_single_quoted(title)}
        $dateFilter = {_ps_single_quoted(start_date_filter)}
        $deleteAll = {'$true' if delete_all_matches else '$false'}
        $matched = @()

        foreach ($item in $items) {{
            if (-not ($item -is [Microsoft.Office.Interop.Outlook.AppointmentItem])) {{ continue }}

            $titleOk = $true
            if ($titleFilter -ne "") {{
                $subject = [string]($item.Subject)
                $titleOk = $subject -like ("*" + $titleFilter + "*")
            }}

            $dateOk = $true
            if ($dateFilter -ne "") {{
                try {{
                    $itemDate = ([datetime]$item.Start).ToString("yyyy-MM-dd")
                    $dateOk = $itemDate -eq $dateFilter
                }} catch {{
                    $dateOk = $false
                }}
            }}

            if ($titleOk -and $dateOk) {{
                $matched += $item
            }}
        }}

        if ($matched.Count -eq 0) {{
            Write-Output "NOT_FOUND"
            exit 0
        }}

        if (-not $deleteAll) {{
            $matched = @($matched[0])
        }}

        $deleted = 0
        foreach ($entry in $matched) {{
            $entry.Delete()
            $deleted += 1
        }}
        Write-Output ("DELETED:" + $deleted)
    }} catch {{
        Write-Output ("ERR:" + $_.Exception.Message)
        exit 1
    }}
    '''

    try:
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=35,
            encoding='utf-8',
            errors='replace',
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return _outlook_unavailable("PowerShell çalıştırılamadı")
    except Exception as exc:
        return f"Takvim etkinliği silinemedi: {exc}"

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    if out == "NOT_FOUND":
        return "Silinecek etkinlik bulunamadı."
    if out.startswith("DELETED:"):
        count = out.split(":", 1)[1].strip() or "0"
        return f"Takvimden {count} etkinlik silindi."
    if _looks_like_outlook_missing(out, err):
        return _outlook_unavailable(
            "Outlook bulunamadı veya COM erişimi sağlanamadı"
        )
    if out.startswith("ERR:"):
        return f"Takvim etkinliği silinemedi: {out[4:].strip()}"
    return f"Takvim etkinliği silinemedi: {err or out or 'Bilinmeyen hata'}"


delete_calendar_event.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "delete_calendar_event",
        "description": (
            "Windows Outlook Calendar takviminden etkinlik siler. "
            "Kullanici bir toplantiyi, randevuyu veya takvim kaydini silmek istediginde kullan. "
            "Ayni ada birden fazla etkinlik varsa dogru kaydi bulmak icin baslangic tarihini gercek tarih/saat olarak ver."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {
                    "type": "STRING",
                    "description": "Silinecek etkinlik basligi. Ornek: 'Disci Randevusu'",
                },
                "start_iso": {
                    "type": "STRING",
                    "description": "Opsiyonel tarih/saat. Ayni isimli birden fazla etkinligi ayirt etmek icin kullan.",
                },
                "calendar_name": {
                    "type": "STRING",
                    "description": "Opsiyonel takvim adi",
                },
                "delete_all_matches": {
                    "type": "BOOLEAN",
                    "description": "true ise eslesen tum etkinlikleri siler",
                },
            },
            "required": ["title"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# get_reminders / add_reminder
# ---------------------------------------------------------------------------


BASE_DIR = Path(__file__).resolve().parent.parent.parent
REMINDERS_FILE = BASE_DIR / "memory" / "reminders.json"


def _load_reminders() -> list[dict]:
    """Hatırlatıcıları dosyadan yükle."""
    try:
        if REMINDERS_FILE.exists():
            data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
            return data.get("reminders", [])
    except Exception:
        pass
    return []


def _save_reminders(reminders: list[dict]) -> None:
    """Hatırlatıcıları dosyaya kaydet."""
    REMINDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    REMINDERS_FILE.write_text(
        json.dumps({"reminders": reminders}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _normalize_reminder_query(query: str) -> tuple[str, int]:
    q = (query or "").strip().lower()
    if any(token in q for token in ("bugün", "today")):
        return "today", 8
    if any(token in q for token in ("geciken", "geçmiş", "overdue")):
        return "overdue", 8
    if any(token in q for token in ("sıradaki", "next")):
        return "next", 1
    if any(token in q for token in ("hepsi", "tum", "tüm", "all", "listele")):
        return "all", 10
    return "upcoming", 8


def _format_due(item: dict, now: dt.datetime) -> str:
    due_str = item.get("due", "")
    if not due_str:
        return "zaman atanmamış"
    try:
        due = dt.datetime.fromisoformat(due_str)
        return f"{_day_label(due, now)} {due.strftime('%H:%M')}"
    except Exception:
        return due_str


def _format_reminder_line(item: dict, now: dt.datetime) -> str:
    parts = [f"{_format_due(item, now)} - {item.get('title', 'Adsız hatırlatıcı')}"]
    if item.get("list_name"):
        parts.append(f"[{item['list_name']}]")
    if item.get("priority") == 1:
        parts.append("(yüksek öncelik)")
    return " ".join(parts)


def get_reminders(query: str = "upcoming", limit: int = 8, list_name: str = "") -> str:
    mode, default_limit = _normalize_reminder_query(query)
    limit = max(1, min(20, int(limit or default_limit)))

    reminders = _load_reminders()
    now = dt.datetime.now()

    if mode == "today":
        reminders = [r for r in reminders if not r.get("completed")]
    elif mode == "overdue":
        reminders = [r for r in reminders if not r.get("completed") and r.get("due")]
    elif mode == "next":
        reminders = [r for r in reminders if not r.get("completed")][:1]
    elif mode == "all":
        reminders = [r for r in reminders if not r.get("completed")]
    else:  # upcoming
        reminders = [r for r in reminders if not r.get("completed")]

    if not reminders:
        if mode == "today":
            return "Bugün için hatırlatıcı görünmüyor."
        if mode == "overdue":
            return "Geciken hatırlatıcı görünmüyor."
        if mode == "next":
            return "Sıradaki hatırlatıcıyı bulamadım."
        if mode == "all":
            return "Kayıtlı açık hatırlatıcı görünmüyor."
        return "Yaklaşan hatırlatıcı görünmüyor."

    if mode == "next":
        return f"Sıradaki hatırlatıcı: {_format_reminder_line(reminders[0], now)}."

    selected = reminders[:limit]
    if mode == "today":
        header = f"Bugün için {len(selected)} hatırlatıcı buldum:"
    elif mode == "overdue":
        header = f"Gecikmiş {len(selected)} hatırlatıcı buldum:"
    elif mode == "all":
        header = f"Açık {len(selected)} hatırlatıcı buldum:"
    else:
        header = f"Yaklaşan {len(selected)} hatırlatıcı buldum:"

    lines = [header]
    for item in selected:
        lines.append(f"- {_format_reminder_line(item, now)}")
    return "\n".join(lines)


get_reminders.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "get_reminders",
        "description": (
            "Windows hatırlatıcı listesini okur. "
            "Bugunku, yaklasan, geciken veya tum acik hatirlatmalari ozetler. "
            "Kullanici hatirlatma, reminder veya yapilacaklar listesini sordugunda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "today | upcoming | overdue | all | next",
                },
                "limit": {
                    "type": "NUMBER",
                    "description": "Maksimum hatirlatici sayisi",
                },
                "list_name": {
                    "type": "STRING",
                    "description": "Istenirse belirli bir hatirlatıcı listesi adi",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


def add_reminder(
    title: str,
    due_iso: str = "",
    notes: str = "",
    list_name: str = "",
    priority: str = "",
    all_day: bool = False,
) -> str:
    if not title or not title.strip():
        return "Hatırlatıcı başlığı boş olamaz."

    reminder = {
        "title": title.strip(),
        "due": due_iso,
        "notes": notes,
        "list_name": list_name,
        "priority": 1 if priority == "high" else 0,
        "all_day": all_day,
        "completed": False,
        "created": dt.datetime.now().isoformat(),
    }

    reminders = _load_reminders()
    reminders.append(reminder)
    _save_reminders(reminders)

    when = _format_due(reminder, dt.datetime.now())
    list_suffix = f" [{list_name}]" if list_name else ""
    return f"Hatırlatıcı eklendi: {when} - {title}{list_suffix}"


add_reminder.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "add_reminder",
        "description": (
            "Windows hatırlatıcı listesine yeni bir hatırlatıcı ekler. "
            "Kullanici 'hatirlat', 'hatirlatıcı ekle', 'reminder kur' dediginde kullan. "
            "Goreli zaman ifadelerini bugunku tarih baglamina gore due_iso alanina ISO formatinda cevir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "title": {
                    "type": "STRING",
                    "description": "Hatirlatıcı basligi",
                },
                "due_iso": {
                    "type": "STRING",
                    "description": "Opsiyonel tarih/saat. Ornek: 2026-04-13T09:00 veya tum gun icin 2026-04-13",
                },
                "notes": {
                    "type": "STRING",
                    "description": "Opsiyonel not",
                },
                "list_name": {
                    "type": "STRING",
                    "description": "Opsiyonel hatirlatıcı listesi",
                },
                "priority": {
                    "type": "STRING",
                    "description": "low | medium | high",
                },
                "all_day": {
                    "type": "BOOLEAN",
                    "description": "Tum gun hatirlatıcı ise true",
                },
            },
            "required": ["title"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# get_weather_summary — uzak hava durumu servisi
# ---------------------------------------------------------------------------


def get_weather_summary(location: str | None = None) -> str:
    """``wttr.in`` üzerinden anlık hava durumu özeti döner."""
    target = (location or os.environ.get("JARVIS_WEATHER_LOCATION") or "Istanbul").strip()
    try:
        response = requests.get(
            f"https://wttr.in/{target}",
            params={"format": "j1"},
            timeout=10,
            headers={"User-Agent": "JARVIS Windows"},
        )
        response.raise_for_status()
        payload = response.json()
        current = (payload.get("current_condition") or [{}])[0]
        temp_c = current.get("temp_C")
        feels_like = current.get("FeelsLikeC")
        weather_desc = ((current.get("weatherDesc") or [{}])[0]).get("value", "")
        humidity = current.get("humidity")

        parts = []
        if temp_c:
            parts.append(f"{temp_c} derece")
        if weather_desc:
            parts.append(weather_desc.lower())
        if feels_like and feels_like != temp_c:
            parts.append(f"hissedilen {feels_like} derece")
        if humidity:
            parts.append(f"nem yüzde {humidity}")

        if not parts:
            return "Hava durumu bilgisi şu anda alınamadı."

        return f"{target} için hava durumu: " + ", ".join(parts) + "."
    except Exception:
        return "Hava durumu bilgisi şu anda alınamadı."


get_weather_summary.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "get_weather",
        "description": (
            "Anlik hava durumunu ozetler. Varsayilan konum Istanbul'dur. "
            "Kullanici hava durumunu, sicakligi veya yagmur durumunu sordugunda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "location": {
                    "type": "STRING",
                    "description": "Sehir veya konum. Bos birakilirsa Istanbul kullanilir.",
                },
            },
        },
    },
    "execution_mode": "inline",
}


__all__ = [
    "get_calendar_events",
    "add_calendar_event",
    "delete_calendar_event",
    "get_reminders",
    "add_reminder",
    "get_weather_summary",
]
