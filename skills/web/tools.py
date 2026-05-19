"""Web skill tool implementations.

İçerdiği handler:

- :func:`browser_control` — Tarayıcı kontrolü: URL açma, arama, YouTube
  oynatma, sekme yönetimi ve video kontrolü (oynat/duraklat, reklam
  atlama vb.). ``inline`` modda çalışır; tipik süre <2 sn (uzun bir IO
  YouTube ilk-video aramasıdır ve 10 sn timeout ile sınırlandırılmıştır).

Plugin_Host bu modülü ``__skill__.py::MANIFEST`` üzerinden bulur ve
``__tool__`` metadata'sındaki Gemini declaration'ı runtime'a aktarır.
Tool sonuçları her zaman Türkçe ve voice-friendly tek paragraflık
metindir.
"""

from __future__ import annotations

import re
import subprocess
import time
import urllib.parse
import webbrowser

import pyautogui
import requests

from app_config import get_app_config_value


# ---------------------------------------------------------------------------
# Sabitler ve düşük seviyeli yardımcılar
# ---------------------------------------------------------------------------


_VIDEO_ID_RE = re.compile(r'"videoId":"([A-Za-z0-9_-]{11})"')


def _activate_browser_window() -> bool:
    """Bilinen bir tarayıcı penceresini öne getirmeyi dener."""
    ps_script = (
        "$ws=New-Object -ComObject WScript.Shell;"
        "$titles=@('Chrome','Edge','Firefox','Brave','Opera','Vivaldi','Yandex');"
        "$ok=$false;"
        "foreach($t in $titles){if($ws.AppActivate($t)){$ok=$true;break}};"
        "if($ok){exit 0}else{exit 1}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-Command", ps_script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0
    except Exception:
        return False


def _send_hotkey(*keys: str, settle: float = 0.12, focus_browser: bool = True) -> bool:
    try:
        if focus_browser:
            _activate_browser_window()
            time.sleep(0.08)
        pyautogui.hotkey(*keys)
        time.sleep(settle)
        return True
    except Exception:
        return False


def _press_key(
    key: str,
    presses: int = 1,
    interval: float = 0.08,
    settle: float = 0.08,
    focus_browser: bool = False,
) -> bool:
    try:
        if focus_browser:
            _activate_browser_window()
            time.sleep(0.08)
        pyautogui.press(key, presses=max(1, int(presses)), interval=max(0.01, float(interval)))
        time.sleep(settle)
        return True
    except Exception:
        return False


def _skip_youtube_ad_best_effort() -> bool:
    """YouTube reklamını klavye ile atlamayı dener (best-effort)."""
    if not _activate_browser_window():
        return False
    _press_key("esc")
    _press_key("tab", presses=6, interval=0.06)
    _press_key("enter")
    _press_key("l")
    return True


def _open(url: str) -> None:
    """URL'yi tarayıcıda aç."""
    preferred = str(get_app_config_value("preferred_browser", "") or "").strip().lower()
    if preferred:
        try:
            from skills.computer_control.tools import _open_url_in_browser

            _open_url_in_browser(url, preferred)
            return
        except Exception:
            pass
    try:
        webbrowser.open_new_tab(url)
    except Exception:
        webbrowser.open(url)


def _find_first_youtube_video(query: str) -> str | None:
    encoded = urllib.parse.quote_plus(query)
    response = requests.get(
        f"https://www.youtube.com/results?search_query={encoded}",
        headers={"User-Agent": "JARVIS/1.0"},
        timeout=10,
    )
    response.raise_for_status()

    seen: set[str] = set()
    for video_id in _VIDEO_ID_RE.findall(response.text):
        if video_id not in seen:
            seen.add(video_id)
            return video_id
    return None


# ---------------------------------------------------------------------------
# browser_control — tek tool, çoklu eylem
# ---------------------------------------------------------------------------


def browser_control(action: str, url: str | None = None, query: str | None = None) -> str:
    """Tarayıcı kontrolü: URL açma, arama, YouTube oynatma ve klavye kısayolları.

    Args:
        action: Eylem adı (örn. ``open_url``, ``play_youtube``, ``close_tab``).
        url: ``open_url`` için açılacak URL.
        query: ``search`` veya ``play_youtube`` için arama metni.

    Returns:
        Kullanıcıya gösterilecek voice-friendly Türkçe sonuç metni.
    """
    action = (action or "").strip().lower()

    if action == "open_url":
        if not url:
            return "URL belirtilmedi."
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        _open(url)
        return f"Açıldı: {url}"

    elif action == "search":
        if not query:
            return "Arama sorgusu belirtilmedi."
        encoded = urllib.parse.quote(query)
        search_url = f"https://www.google.com/search?q={encoded}"
        _open(search_url)
        return f"'{query}' için arama açıldı."

    elif action in ("play_youtube", "youtube_play", "play_music"):
        if not query:
            return "YouTube için arama sorgusu belirtilmedi."

        try:
            video_id = _find_first_youtube_video(query)
        except Exception as exc:
            encoded = urllib.parse.quote(query)
            fallback_url = f"https://www.youtube.com/results?search_query={encoded}"
            _open(fallback_url)
            return (
                f"YouTube ilk sonucu alınamadı ({exc}). "
                f"Arama sonuçları açıldı: {query}"
            )

        if not video_id:
            encoded = urllib.parse.quote(query)
            fallback_url = f"https://www.youtube.com/results?search_query={encoded}"
            _open(fallback_url)
            return f"YouTube'da doğrudan video bulunamadı. Arama sonuçları açıldı: {query}"

        watch_url = f"https://www.youtube.com/watch?v={video_id}&autoplay=1"
        _open(watch_url)
        return f"YouTube'da oynatılıyor: {query}"

    elif action in {"close_tab", "tab_close"}:
        if _send_hotkey("ctrl", "w"):
            return "Aktif sekme kapatıldı."
        return "Sekme kapatma başarısız oldu."

    elif action in {"new_tab", "tab_new"}:
        if _send_hotkey("ctrl", "t"):
            return "Yeni sekme açıldı."
        return "Yeni sekme açılamadı."

    elif action in {"reopen_tab", "undo_close_tab"}:
        if _send_hotkey("ctrl", "shift", "t"):
            return "Son kapatılan sekme geri açıldı."
        return "Sekme geri açılamadı."

    elif action in {"next_tab", "tab_next"}:
        if _send_hotkey("ctrl", "tab"):
            return "Sonraki sekmeye geçildi."
        return "Sekme değiştirilemedi."

    elif action in {"previous_tab", "prev_tab", "tab_prev"}:
        if _send_hotkey("ctrl", "shift", "tab"):
            return "Önceki sekmeye geçildi."
        return "Sekme değiştirilemedi."

    elif action in {"refresh", "reload"}:
        if _send_hotkey("ctrl", "r"):
            return "Sayfa yenilendi."
        return "Sayfa yenilenemedi."

    elif action == "hard_refresh":
        if _send_hotkey("ctrl", "shift", "r"):
            return "Sayfa önbellek temizlenerek yenilendi."
        return "Hard refresh başarısız oldu."

    elif action in {"play_pause", "pause_play_video", "toggle_play_pause"}:
        # Yalnızca tek bir tuş gönderiyoruz. Eskiden hem `k` hem `space`
        # gönderiliyordu ve bu YouTube'da videoyu önce durdurup hemen
        # tekrar oynatıyordu. `k` YouTube/Vimeo/Twitch dahil çoğu web
        # player'da evrensel oynat-duraklat kısayoludur.
        if _press_key("k", focus_browser=True):
            return "Video oynat/duraklat komutu gönderildi."
        return "Video kontrolü başarısız oldu."

    elif action in {"skip_ad", "youtube_skip_ad"}:
        if _skip_youtube_ad_best_effort():
            return "Reklam atlama komutu gönderildi."
        return "Reklam atlama denemesi başarısız oldu."

    elif action in {"mute_video", "mute_unmute_video"}:
        if _press_key("m", focus_browser=True):
            return "Video sesi aç/kapa komutu gönderildi."
        return "Video sesi kontrol edilemedi."

    elif action == "fullscreen_video":
        if _press_key("f", focus_browser=True):
            return "Video tam ekran komutu gönderildi."
        return "Tam ekran komutu gönderilemedi."

    return f"Bilinmeyen eylem: {action}"


browser_control.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "browser_control",
        "description": (
            "Tarayıcı kontrolü yapar: URL açma, arama, YouTube oynatma, sekme yönetimi "
            "ve video kontrolü (oynat/duraklat, reklam atlama vb.)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type": "STRING",
                    "description": (
                        "open_url | search | play_youtube | close_tab | new_tab | reopen_tab | "
                        "next_tab | previous_tab | refresh | hard_refresh | play_pause | "
                        "skip_ad | mute_video | fullscreen_video"
                    ),
                },
                "url": {
                    "type": "STRING",
                    "description": "Açılacak URL (open_url için)",
                },
                "query": {
                    "type": "STRING",
                    "description": "Arama sorgusu (search veya play_youtube için)",
                },
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


__all__ = ["browser_control"]
