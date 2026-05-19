"""System skill tool implementations.

Bu modül, eski ``actions/system_control.py``, ``actions/sys_info.py``,
``actions/health.py`` ve ``actions/shell.py`` modüllerinin tek bir Plugin_Host
uyumlu skill paketi altında birleştirilmiş halidir (görev 5.5).

Yayınlanan handler'lar (tümü ``inline`` execution_mode'da çalışır):

- :func:`sys_info` — Pil, CPU, RAM, disk, saat, tarih, ağ özetleri.
- :func:`system_control` — Ses, kilit, masaüstü, görev yöneticisi, pano kısayolları.
- :func:`get_health_data` — iCloud for Windows ile senkronize HealthAutoExport
  dosyalarından sağlık özeti.
- :func:`shell_run` — Güvenlik filtreli PowerShell / CMD komutu çalıştırıcı.

Her tool, Plugin_Host'un kayıt sırasında okuyacağı ``__tool__`` metadata
sözlüğünü dosyanın sonunda fonksiyona ekler. Bu sözlüklerdeki
``declaration`` alanları eski ``main.TOOL_DECLARATIONS`` listesinden bire bir
taşınmıştır; sözleşme değişmedi.

Görev 5.10 ``open_app`` handler'larını bu dosyaya **append** edecek; bu yüzden
MANIFEST (``skills/system/__skill__.py``) sadece şu an kayıtlı tool'ları
listeler ve 5.10'da listeye yeni adlar eklenir.

Tool sonuçları her zaman Türkçe ve voice-friendly metindir.
"""

from __future__ import annotations

import ctypes
import datetime
import json
import os
import re
import subprocess
import time
from datetime import date, datetime as _dt, timedelta
from pathlib import Path

import pyautogui
import pyperclip

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:  # pragma: no cover — optional on minimal Windows envs
    HAS_PSUTIL = False


# ---------------------------------------------------------------------------
# system_control — Windows sistem kısayolları
# ---------------------------------------------------------------------------


VK_VOLUME_MUTE = 0xAD
VK_VOLUME_DOWN = 0xAE
VK_VOLUME_UP = 0xAF
KEYEVENTF_KEYUP = 0x0002


def _tap_vk(vk_code: int, presses: int = 1, interval: float = 0.06) -> None:
    user32 = ctypes.windll.user32
    for _ in range(max(1, presses)):
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        time.sleep(max(0.01, float(interval)))


def system_control(action: str, value: str = "") -> str:
    """Ses, kilit, pano gibi hızlı sistem eylemlerini yürüt."""
    action = (action or "").strip().lower()
    value = str(value or "").strip()

    if action in {"volume_up", "ses_arttir"}:
        step = int(value) if value.isdigit() else 3
        _tap_vk(VK_VOLUME_UP, presses=max(1, min(30, step)))
        return "Ses seviyesi artırıldı."

    if action in {"volume_down", "ses_azalt"}:
        step = int(value) if value.isdigit() else 3
        _tap_vk(VK_VOLUME_DOWN, presses=max(1, min(30, step)))
        return "Ses seviyesi azaltıldı."

    if action in {"volume_mute", "mute_toggle", "ses_kapat"}:
        _tap_vk(VK_VOLUME_MUTE, presses=1)
        return "Ses aç/kapa komutu gönderildi."

    if action in {"lock_screen", "ekrani_kilitle"}:
        try:
            result = subprocess.run(
                ["rundll32.exe", "user32.dll,LockWorkStation"],
                capture_output=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode == 0:
                return "Ekran kilitlendi."
        except Exception as exc:
            return f"Ekran kilitlenemedi: {exc}"
        return "Ekran kilitlenemedi."

    if action in {"show_desktop", "masaustu_goster", "minimize_all"}:
        try:
            pyautogui.hotkey("win", "d")
            return "Masaüstü gösterildi."
        except Exception as exc:
            return f"Masaüstü komutu gönderilemedi: {exc}"

    if action in {"open_task_manager", "task_manager"}:
        try:
            result = subprocess.run(
                ["start", "", "taskmgr"],
                shell=True,
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            if result.returncode == 0:
                return "Görev Yöneticisi açıldı."
            return "Görev Yöneticisi açılamadı."
        except Exception as exc:
            return f"Görev Yöneticisi açılamadı: {exc}"

    if action in {"copy_clipboard", "clipboard_copy"}:
        if not value:
            return "Panoya kopyalamak için value gerekli."
        try:
            pyperclip.copy(value)
            return "Metin panoya kopyalandı."
        except Exception as exc:
            return f"Panoya kopyalama başarısız: {exc}"

    if action in {"paste_clipboard", "clipboard_paste"}:
        try:
            pyautogui.hotkey("ctrl", "v")
            return "Pano yapıştırma komutu gönderildi."
        except Exception as exc:
            return f"Yapıştırma komutu gönderilemedi: {exc}"

    return f"Bilinmeyen system_control eylemi: {action}"


system_control.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "system_control",
        "description": (
            "Windows sistemini hızlıca kontrol eder: ses aç/kapa, ses artır/azalt, "
            "ekran kilitleme, masaüstü gösterme, görev yöneticisi açma ve pano işlemleri."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "volume_up | volume_down | volume_mute | lock_screen | show_desktop | "
                        "open_task_manager | copy_clipboard | paste_clipboard"
                    ),
                },
                "value": {
                    "type": "STRING",
                    "description": (
                        "Opsiyonel değer. Ses için adım sayısı (örn: 5), "
                        "copy_clipboard için kopyalanacak metin."
                    ),
                },
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# sys_info — pil, CPU, RAM, disk, saat, ağ
# ---------------------------------------------------------------------------


def sys_info(query: str) -> str:
    """Sistem bilgisi özeti döner: pil, CPU, RAM, disk, saat, tarih, ağ."""
    query = query.lower().strip()

    results: list[str] = []

    if query in ("battery", "pil", "all"):
        results.append(_battery())

    if query in ("cpu", "işlemci", "all"):
        results.append(_cpu())

    if query in ("ram", "bellek", "memory", "all"):
        results.append(_ram())

    if query in ("disk", "depolama", "all"):
        results.append(_disk())

    if query in ("time", "saat", "zaman", "all"):
        now = datetime.datetime.now()
        results.append(f"Saat: {now.strftime('%H:%M:%S')}")

    if query in ("date", "tarih", "all"):
        now = datetime.datetime.now()
        results.append(f"Tarih: {now.strftime('%d %B %Y, %A')}")

    if query in ("network", "ağ", "wifi", "all"):
        results.append(_network())

    if not results:
        results.append(
            f"Bilinmeyen sorgu: {query}. battery/cpu/ram/disk/time/date/network/all kullanın."
        )

    return "\n".join(r for r in results if r)


def _battery() -> str:
    if HAS_PSUTIL:
        bat = psutil.sensors_battery()
        if bat:
            status = "Şarj oluyor" if bat.power_plugged else "Pilde"
            percent = int(bat.percent)
            if bat.power_plugged and bat.percent == 100:
                status = "Tam şarjlı"
            elif bat.power_plugged:
                status = f"Şarj oluyor ({bat.percent:.0f}%)"
            return f"Pil: %{percent} — {status}"
    return "Pil bilgisi alınamadı (masaüstü bilgisayar olabilir)."


def _cpu() -> str:
    if HAS_PSUTIL:
        usage = psutil.cpu_percent(interval=0.5)
        count = psutil.cpu_count(logical=True)
        freq = psutil.cpu_freq()
        freq_str = f", {freq.current:.0f} MHz" if freq else ""
        return f"CPU: %{usage:.1f} kullanım — {count} çekirdek{freq_str}"
    return "CPU bilgisi alınamadı."


def _ram() -> str:
    if HAS_PSUTIL:
        vm = psutil.virtual_memory()
        total = vm.total / (1024**3)
        used = vm.used / (1024**3)
        pct = vm.percent
        return f"RAM: {used:.1f}GB / {total:.1f}GB kullanımda (%{pct:.0f})"
    return "RAM bilgisi alınamadı."


def _disk() -> str:
    if HAS_PSUTIL:
        # Windows'ta genellikle C: sürücüsü
        try:
            du = psutil.disk_usage("C:\\")
            total = du.total / (1024**3)
            used = du.used / (1024**3)
            free = du.free / (1024**3)
            return (
                f"Disk (C:): {used:.1f}GB kullanıldı, {free:.1f}GB boş "
                f"(toplam {total:.1f}GB)"
            )
        except Exception:
            pass
        # Tüm sürücüleri dene
        partitions = psutil.disk_partitions()
        rows: list[str] = []
        for p in partitions:
            try:
                du = psutil.disk_usage(p.mountpoint)
                total = du.total / (1024**3)
                used = du.used / (1024**3)
                rows.append(f"{p.device}: {used:.1f}GB/{total:.1f}GB")
            except Exception:
                pass
        if rows:
            return "Disk: " + " | ".join(rows)
    return "Disk bilgisi alınamadı."


def _network() -> str:
    # WiFi SSID (Windows netsh)
    try:
        out = subprocess.check_output(
            ["netsh", "wlan", "show", "interfaces"],
            text=True,
            timeout=5,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for line in out.splitlines():
            if "SSID" in line and ":" in line:
                ssid = line.split(":", 1)[1].strip()
                if ssid:
                    # Sinyal gücünü bul
                    signal = None
                    for l2 in out.splitlines():
                        if "Signal" in l2 and ":" in l2:
                            try:
                                signal = l2.split(":")[1].strip()
                            except Exception:
                                pass
                    if signal:
                        return f"WiFi: {ssid} bağlı (Sinyal: {signal})"
                    return f"WiFi: {ssid} bağlı"
    except Exception:
        pass

    # IP adresi fallback
    try:
        hostname = subprocess.check_output(
            ["hostname"],
            text=True,
            timeout=3,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).strip()
        # Yerel IP'yi bul
        if HAS_PSUTIL:
            for _iface, addrs in psutil.net_if_addrs().items():
                for addr in addrs:
                    if addr.family.name == "AF_INET":
                        ip = addr.address
                        if not ip.startswith("127."):
                            return f"Ağ: {hostname} - IP {ip}"
    except Exception:
        pass

    return "Ağ bağlantısı bilgisi alınamadı."


sys_info.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "sys_info",
        "description": (
            "Sistem bilgisi alır: pil durumu, CPU, RAM, disk, saat, tarih, ağ bağlantısı."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "battery | cpu | ram | disk | time | date | network | all",
                }
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# get_health_data — iCloud for Windows / HealthAutoExport
# ---------------------------------------------------------------------------


UNSUPPORTED_MESSAGE = "Bu özellik Windows'ta desteklenmiyor"

# Windows'ta iCloud for Windows aracılığıyla senkronize edilen sağlık verisi
# klasörleri. Tüm yollar pathlib.Path üzerinden kurulur.
ICLOUD_WINDOWS_PATHS: list[Path] = [
    Path.home() / "iCloudDrive" / "Health",
    Path.home() / "iCloudDrive" / "HealthAutoExport",
]

STALE_WARN_MINUTES = 120


def _normalize_query(text: str) -> str:
    text = (text or "").strip().lower()
    text = (
        text.replace("ı", "i")
        .replace("ğ", "g")
        .replace("ü", "u")
        .replace("ş", "s")
        .replace("ö", "o")
        .replace("ç", "c")
    )
    return text


def _extract_target_date(query: str) -> date | None:
    q = _normalize_query(query)
    today = date.today()

    if any(token in q for token in ("önceki gün", "evvelsi gün", "iki gün önce")):
        return today - timedelta(days=2)
    if any(token in q for token in ("dün", "yesterday")):
        return today - timedelta(days=1)
    if any(token in q for token in ("bugün", "today", "şimdi")):
        return today

    iso_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", q)
    if iso_match:
        try:
            return _dt.strptime(iso_match.group(1), "%Y-%m-%d").date()
        except ValueError:
            pass

    tr_match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d{2})\b", q)
    if tr_match:
        day_s, month_s, year_s = tr_match.groups()
        try:
            return date(int(year_s), int(month_s), int(day_s))
        except ValueError:
            pass

    return None


def _find_health_file(target_date: date | None = None) -> Path | None:
    """En güncel veya hedef tarihli sağlık dosyasını döndür."""
    for directory in ICLOUD_WINDOWS_PATHS:
        if not directory.exists():
            continue
        if target_date:
            target_name = f"HealthAutoExport-{target_date.isoformat()}.json"
            candidate = directory / target_name
            if candidate.exists():
                return candidate
        candidates = sorted(
            directory.glob("HealthAutoExport-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if candidates:
            return candidates[0]
    return None


def _resolve_file(target_date: date | None = None) -> Path | None:
    """En güncel sağlık dosyasını döndürür."""
    return _find_health_file(target_date)


def _age_str(ts: float) -> str:
    mins = (time.time() - ts) / 60
    if mins < 2:
        return "az önce"
    if mins < 60:
        return f"{int(mins)} dakika önce"
    hrs = mins / 60
    if hrs < 24:
        return f"{hrs:.1f} saat önce"
    return f"{hrs/24:.1f} gün önce"


def _v(d: dict, key: str, unit: str = "", dec: int = 0) -> str:
    val = d.get(key)
    if val is None:
        return "—"
    try:
        f = float(val)
        return f"{f:.{dec}f}{unit}" if dec else f"{int(round(f))}{unit}"
    except (ValueError, TypeError):
        return str(val)


def _unsupported(detail: str = "") -> str:
    """Windows'ta desteklenmeyen aksiyonlar için standart Türkçe mesaj."""
    if detail:
        return f"{UNSUPPORTED_MESSAGE}: {detail}"
    return f"{UNSUPPORTED_MESSAGE}."


def get_health_data(query: str = "all") -> str:
    """Windows'ta sağlık verisini özetle.

    Veri kaynağı iCloud for Windows tarafından senkronize edilen
    ``HealthAutoExport-*.json`` dosyalarıdır. Klasör veya dosya yoksa
    standart "Bu özellik Windows'ta desteklenmiyor" mesajı döner.
    """
    target_date = _extract_target_date(query)
    source_file = _resolve_file(target_date)

    if not source_file:
        return _unsupported(
            "iCloud Drive üzerinde HealthAutoExport dosyası bulunamadı"
        )

    try:
        raw = json.loads(source_file.read_text(encoding="utf-8"))
        file_mtime = source_file.stat().st_mtime
        age = _age_str(file_mtime)

        if "data" in raw and isinstance(raw["data"], dict):
            data = raw["data"]
        else:
            data = raw

        lines = ["── SAĞLIK ÖZETİ ──────────────────"]
        lines.append(f"Adım          : {_v(data, 'steps')}")
        lines.append(f"Aktif kalori  : {_v(data, 'calories', ' kcal')}")
        lines.append(f"Egzersiz süresi: {_v(data, 'exercise_min', ' dk')}")
        lines.append(f"Nabız         : {_v(data, 'heart_rate', ' bpm')}")
        lines.append(f"Uyku süresi   : {_v(data, 'sleep_hours', ' saat', 1)}")
        lines.append("──────────────────────────────────")
        lines.append(f"[güncelleme: {age}]")
        return "\n".join(lines)

    except Exception as exc:
        return f"Sağlık dosyası okunamadı: {exc}"


def get_welcome_health_summary() -> str:
    """Hoş geldin sağlık özeti.

    Plugin_Host bu fonksiyonu tool olarak yayınlamaz (``__tool__`` yok); UI
    tarafındaki hoş geldin akışı doğrudan import ederek çağırır.
    """
    source_file = _resolve_file()
    if not source_file:
        return "Sağlık verilerin şu anda alınamadı."

    try:
        raw = json.loads(source_file.read_text(encoding="utf-8"))
        file_mtime = source_file.stat().st_mtime

        if "data" in raw and isinstance(raw["data"], dict):
            data = raw["data"]
        else:
            data = raw

        steps = _v(data, "steps")
        exercise_min = _v(data, "exercise_min", " dakika")
        age = _age_str(file_mtime)

        parts: list[str] = []
        if steps != "—":
            parts.append(f"{steps} adım attın")
        if exercise_min != "—":
            parts.append(f"{exercise_min} egzersiz yaptın")

        if not parts:
            return "Sağlık verilerin şu anda alınamadı."

        summary = ", ".join(parts) + "."
        summary += f" (veri {age} güncellendi)"
        return summary

    except Exception:
        return "Sağlık verilerin şu anda alınamadı."


get_health_data.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "get_health_data",
        "description": (
            "Windows'taki sağlık export dosyasından günlük sağlık özetini çıkarır. "
            "Kullanıcı adım, kalori, nabız, uyku veya sağlık durumu sorduğunda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "all | today | yesterday | bugun | dun veya YYYY-MM-DD",
                }
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# shell_run — PowerShell / CMD command runner
# ---------------------------------------------------------------------------


# Tehlikeli komutları engelle
_BLOCKED_PATTERNS: list[str] = [
    "rm -rf /",
    "sudo rm -rf",
    "mkfs",
    "dd if=",
    ":(){ :|:& };:",
    "shutdown",
    "reboot",
    "halt",
    "diskutil",
    "rm -rf",
    "format ",
    "del /f /s /q",
    "rmdir /s /q",
    "icacls . /grant",
    "takeown /f",
]


def shell_run(command: str, timeout: int = 30) -> str:
    """Windows PowerShell/CMD üzerinden filtrelenmiş bir komut çalıştır."""
    if not command:
        return "Komut belirtilmedi."

    cmd_lower = command.lower()
    stripped = command.strip()

    # Güvenlik kontrolleri
    if stripped.startswith(("rm ", "del ", "format ", "rd ", "rmdir ")):
        return (
            "Güvenlik: Dosya silme veya format komutları doğrudan çalıştırılmıyor. "
            "Daha güvenli ve dar kapsamlı bir komut dene."
        )

    for blocked in _BLOCKED_PATTERNS:
        if blocked in cmd_lower:
            return f"Güvenlik: Bu komut engellendi → {blocked}"

    try:
        # Windows'ta PowerShell veya CMD kullan
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout + result.stderr).strip()
        if not output:
            return "Komut başarıyla çalıştı (çıktı yok)."
        # Çok uzun çıktıları kırp
        if len(output) > 800:
            output = output[:800] + "\n... (çıktı kısaltıldı)"
        return output
    except subprocess.TimeoutExpired:
        return f"Komut zaman aşımına uğradı ({timeout}s)."
    except Exception as exc:
        return f"Hata: {exc}"


shell_run.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "shell_run",
        "description": (
            "Windows PowerShell veya CMD komutu çalıştırır. Dosya işlemleri, sistem yönetimi."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "command": {
                    "type": "STRING",
                    "description": "Çalıştırılacak PowerShell veya CMD komutu",
                }
            },
            "required": ["command"],
        },
    },
    "execution_mode": "inline",
}


__all__ = [
    "system_control",
    "sys_info",
    "get_health_data",
    "get_welcome_health_summary",
    "shell_run",
]


# ---------------------------------------------------------------------------
# open_app — kısa isimden Windows uygulama yoluna eşleme (görev 5.10)
# ---------------------------------------------------------------------------


# Kısa isimden uygulama yoluna eşleme (Türkçe ve İngilizce alias'lar dahil).
APP_ALIASES: dict[str, str] = {
    "chrome":              "chrome",
    "google chrome":       "chrome",
    "firefox":             "firefox",
    "edge":                "msedge",
    "terminal":            "cmd",
    "cmd":                 "cmd",
    "powershell":          "powershell",
    "spotify":             "spotify",
    "vscode":              "code",
    "vs code":             "code",
    "code":                "code",
    "notion":              "Notion",
    "slack":               "slack",
    "discord":             "Discord",
    "whatsapp":            "WhatsApp",
    "telegram":            "Telegram",
    "zoom":                "zoom",
    "mail":                "outlook",
    "outlook":             "outlook",
    "takvim":              "outlook",
    "excel":               "excel",
    "word":                "winword",
    "photos":              "ms-photos",
    "fotoğraflar":         "ms-photos",
    "calculator":          "calc",
    "hesap makinesi":      "calc",
    "settings":            "ms-settings",
    "ayarlar":             "ms-settings",
    "task manager":        "taskmgr",
    "görev yöneticisi":    "taskmgr",
    "file explorer":       "explorer",
    "dosya gezgini":       "explorer",
    "figma":               "Figma",
    "postman":             "Postman",
    "docker":              "Docker Desktop",
    "obs":                 "obs64",
    "steam":               "steam://open/main",
    "epic":                "EpicGamesLauncher",
    "epic games":          "EpicGamesLauncher",
    "epic games launcher": "EpicGamesLauncher",
    "battle net":          "Battle.net",
    "battle.net":          "Battle.net",
    "riot":                "Riot Client",
    "riot client":         "Riot Client",
    "xbox":                "Xbox",
}

# Windows ``start`` shell komutu ile başlatılması güvenli olan hedeflerin
# kümesi (PATH'te bulunan veya Windows protokol/AppX hedefleri).
_START_TARGETS: frozenset[str] = frozenset(
    {
        "chrome",
        "firefox",
        "msedge",
        "spotify",
        "discord",
        "slack",
        "telegram",
        "zoom",
        "whatsapp",
        "WhatsApp",
        "notion",
        "calc",
        "cmd",
        "powershell",
        "Figma",
        "Postman",
        "Docker Desktop",
        "obs64",
        "EpicGamesLauncher",
        "Battle.net",
        "Riot Client",
    }
)

_APP_EXE_NAMES: dict[str, tuple[str, ...]] = {
    "steam://open/main": ("steam.exe",),
    "EpicGamesLauncher": ("EpicGamesLauncher.exe",),
    "Battle.net": ("Battle.net.exe", "Battle.net Launcher.exe"),
    "Riot Client": ("RiotClientServices.exe",),
}

_START_MENU_ROOTS: tuple[Path, ...] = tuple(
    Path(p)
    for p in (
        os.environ.get("APPDATA", "") + r"\Microsoft\Windows\Start Menu\Programs",
        os.environ.get("PROGRAMDATA", "") + r"\Microsoft\Windows\Start Menu\Programs",
    )
    if p and Path(p).exists()
)


def _registry_app_paths(exe_names: tuple[str, ...]) -> list[str]:
    try:
        import winreg
    except Exception:
        return []

    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\App Paths"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths"),
    )
    found: list[str] = []
    for root, base in roots:
        for exe in exe_names:
            try:
                with winreg.OpenKey(root, base + "\\" + exe) as key:
                    value, _ = winreg.QueryValueEx(key, "")
                    if value:
                        found.append(str(value))
            except OSError:
                continue
    return found


def _known_install_paths(resolved: str) -> list[str]:
    candidates: list[str] = []
    program_files = [os.environ.get("ProgramFiles", ""), os.environ.get("ProgramFiles(x86)", "")]
    if resolved == "steam://open/main":
        try:
            import winreg

            for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(root, r"Software\Valve\Steam") as key:
                        install_path, _ = winreg.QueryValueEx(key, "SteamPath")
                        candidates.append(str(Path(install_path) / "steam.exe"))
                except OSError:
                    continue
        except Exception:
            pass
        candidates.extend(str(Path(base) / "Steam" / "steam.exe") for base in program_files if base)
    elif resolved == "EpicGamesLauncher":
        candidates.extend(
            str(Path(base) / "Epic Games" / "Launcher" / "Portal" / "Binaries" / "Win64" / "EpicGamesLauncher.exe")
            for base in program_files
            if base
        )
    elif resolved == "Battle.net":
        candidates.extend(str(Path(base) / "Battle.net" / "Battle.net.exe") for base in program_files if base)
    elif resolved == "Riot Client":
        candidates.extend(
            str(Path(base) / "Riot Games" / "Riot Client" / "RiotClientServices.exe")
            for base in program_files
            if base
        )
    return candidates


def _start_menu_shortcuts(app_name: str, resolved: str) -> list[str]:
    needles = {
        app_name.lower().strip(),
        resolved.lower().replace("://open/main", "").strip(),
    }
    matches: list[str] = []
    for root in _START_MENU_ROOTS:
        try:
            for shortcut in root.rglob("*.lnk"):
                stem = shortcut.stem.lower()
                if any(needle and needle in stem for needle in needles):
                    matches.append(str(shortcut))
        except OSError:
            continue
    return matches[:8]


def _open_candidate(target: str, *, creationflags: int) -> bool:
    if not target:
        return False
    try:
        if "://" in target or Path(target).exists():
            os.startfile(target)  # type: ignore[attr-defined]
            return True
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["start", "", target],
            shell=True,
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=creationflags,
        )
        return result.returncode == 0
    except Exception:
        return False


def _open_app_resolved(app_name: str) -> str:
    if not app_name:
        return "Uygulama adi belirtilmedi."

    normalized = app_name.lower().strip()
    resolved = APP_ALIASES.get(normalized, app_name)
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        candidates: list[str] = [str(resolved)]
        candidates.extend(_registry_app_paths(_APP_EXE_NAMES.get(str(resolved), ())))
        candidates.extend(_known_install_paths(str(resolved)))
        candidates.extend(_start_menu_shortcuts(app_name, str(resolved)))
        if str(resolved) in _START_TARGETS:
            candidates.append(str(resolved))

        seen: set[str] = set()
        for candidate in candidates:
            key = candidate.lower()
            if key in seen:
                continue
            seen.add(key)
            if _open_candidate(candidate, creationflags=creationflags):
                return f"{resolved} acildi."

        if str(resolved) in _START_TARGETS:
            return f"'{app_name}' acma komutu gonderildi."
        if str(resolved).startswith(("steam://", "com.epicgames.")):
            return f"{resolved} acildi."
        return f"'{app_name}' bulunamadi veya acilamadi."

    except subprocess.TimeoutExpired:
        return f"'{app_name}' acilirken zaman asimi."
    except Exception as exc:
        return f"Hata: {exc}"


def open_app(app_name: str) -> str:
    return _open_app_resolved(app_name)
    """Verilen isimle Windows uygulamasını açar; sonuç metni döner.

    İlk olarak ``APP_ALIASES`` üzerinden normalize ettikten sonra:

    1. Hedef ``_START_TARGETS`` içindeyse Windows ``start`` komutu çağrılır.
    2. Aksi halde ``os.startfile`` denenir (kayıtlı protokol/dosya türleri).
    3. Son çare olarak yine ``start`` komutuna düşülür.

    Subprocess çağrılarına ``CREATE_NEW_PROCESS_GROUP`` bayrağı verilir
    (Req 13.2): JARVIS kapatıldığında child uygulamayı sürüklemeden
    bağımsız bırakır.
    """
    import os  # local — open_app is the only consumer in this module

    if not app_name:
        return "Uygulama adı belirtilmedi."

    normalized = app_name.lower().strip()
    resolved = APP_ALIASES.get(normalized, app_name)

    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    try:
        if resolved in _START_TARGETS:
            result = subprocess.run(
                ["start", "", resolved],
                shell=True,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=creationflags,
            )
        else:
            try:
                os.startfile(resolved)  # type: ignore[attr-defined]
                return f"{resolved} açıldı."
            except OSError:
                result = subprocess.run(
                    ["start", "", resolved],
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    creationflags=creationflags,
                )

        if result.returncode == 0:
            return f"{resolved} açıldı."
        return f"'{app_name}' bulunamadı veya açılamadı."

    except subprocess.TimeoutExpired:
        return f"'{app_name}' açılırken zaman aşımı."
    except Exception as exc:
        return f"Hata: {exc}"


open_app.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "open_app",
        "description": (
            "Windows'ta herhangi bir uygulamayı açar. Spotify, Chrome, "
            "Terminal, File Explorer, VS Code vb."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": (
                        "Uygulama adı (örn. 'Spotify', 'Chrome', 'Terminal')"
                    ),
                },
            },
            "required": ["app_name"],
        },
    },
    "execution_mode": "inline",
}


__all__ += ["open_app", "APP_ALIASES"]
