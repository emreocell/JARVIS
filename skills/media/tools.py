"""Media skill tool implementations.

İçerdiği handler'lar:

- :func:`play_media` — Spotify Desktop / YouTube oynatma. ``inline`` modda
  çalışır; tipik süre <2 sn.
- :func:`get_youtube_channel_report` — Public YouTube Data API üzerinden
  kanal istatistikleri. ``inline`` modda çalışır (tek REST çağrısı, hızlı
  yanıt verir).

Her tool, Plugin_Host'un kayıt sırasında okuyacağı ``__tool__`` metadata
sözlüğünü dosyanın sonunda fonksiyona ekler. ``declaration`` alanı Gemini
function-calling şemasına bire bir uyar ve eski ``main.TOOL_DECLARATIONS``
listesinden taşınmıştır.

Tool sonuçları her zaman Türkçe ve voice-friendly tek paragraflık metindir.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import time
import urllib.parse
from urllib.parse import urlparse

import requests

from actions.browser import browser_control
from app_config import get_app_config_value


# ---------------------------------------------------------------------------
# play_media — Spotify Desktop / YouTube oynatma
# ---------------------------------------------------------------------------


def _play_youtube(query: str) -> str:
    return browser_control("play_youtube", query=query)


def _focus_spotify_window() -> bool:
    """Spotify Desktop penceresini öne getirmeye çalış."""
    ps_script = (
        "$ws=New-Object -ComObject WScript.Shell;"
        "if($ws.AppActivate('Spotify')){exit 0}else{exit 1}"
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


def _spotify_is_running() -> bool:
    """Spotify.exe görev listesinde var mı?"""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Spotify.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return "Spotify.exe" in (result.stdout or "")
    except Exception:
        return False


def _play_spotify(query: str, autoplay: bool = True) -> str:
    """Windows'ta Spotify'ı aç ve istenirse aramayı oynat.

    Yeni akış (autoplay=True) — klavye simülasyonundan tamamen vazgeçer:

    1. Eğer Spotify çalışmıyorsa süreci başlat.
    2. Aramayı Spotify'ın kendi URI şeması ile aç:
       ``spotify:search:<urlencoded query>``. Bu doğrudan arama sonuçları
       sayfasını çağırır; ``pyautogui.write`` ile Türkçe karakter yazma
       hataları, ``Ctrl+L`` kısayolunun sürüme göre değişmesi ve klavye
       odak yarışları tamamen ortadan kalkar.
    3. Pencereyi öne al, sonuç ağdan yüklendiği için 2 sn bekle.
    4. Gemini vision ile en üstteki şarkı satırını / Top result kartını
       bul ve üzerine **çift tık** at — Spotify'da satıra çift tık her
       sürümde oynatmayı başlatır.
    5. Vision başarısız olursa (ör. Gemini anahtarı yok) en azından
       arama sonucu açık olur ve kullanıcıya net bir mesaj döner.

    Eski klavye akışı (`Ctrl+L` → write → Enter → Tab → Enter) Türkçe
    layout'ta ``ü/ö/ı/ş`` gibi karakterleri yanlış yazıyordu; bu yüzden
    Spotify istenen şarkıyı bulamıyordu. URI scheme bu sınıf hataları
    bertaraf ediyor.
    """
    try:
        clean_query = (query or "").strip()
        if not clean_query:
            return "Spotify için bir arama ifadesi belirtilmedi."

        already_running = _spotify_is_running()

        if not already_running:
            # Spotify Desktop'ı çağırdığımızda URI ilk açılışta protokol
            # handler'a takılabilir; o yüzden önce uygulamayı başlatıyoruz
            # ve hazır olmasını bekliyoruz.
            subprocess.Popen(
                ["start", "", "spotify"],
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
            for _ in range(28):  # ~7 sn'ye kadar bekle
                time.sleep(0.25)
                if _spotify_is_running() and _focus_spotify_window():
                    break

        if not autoplay:
            _focus_spotify_window()
            return f"Spotify açıldı. '{clean_query}' araması yapabilirsin."

        # URI ile aramayı doğrudan aç. Spotify Desktop bu URI'yi alıp
        # arama sonuçları sayfasına gider; klavye simülasyonu yok.
        encoded_query = urllib.parse.quote(clean_query)
        search_uri = f"spotify:search:{encoded_query}"
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", search_uri],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as exc:
            return (
                f"Spotify arama URI'si açılamadı: {exc}. "
                "Spotify Desktop kurulu olduğundan emin ol."
            )

        # Pencereyi öne al, sonuçların yüklenmesini bekle.
        for _ in range(8):
            if _focus_spotify_window():
                break
            time.sleep(0.2)
        time.sleep(1.6)
        _focus_spotify_window()
        time.sleep(0.2)

        # Gemini vision ile top result'a çift tık at.
        played_via_vision = _play_spotify_top_result_via_vision(clean_query)
        if played_via_vision:
            return f"Spotify'da oynatılıyor: {clean_query}"

        return (
            f"Spotify'da '{clean_query}' araması açıldı ama Gemini vision "
            "Top Result'ı tanıyamadığı için oynatma tetiklenemedi. "
            "Üstteki ilk şarkıya tıklarsan başlar."
        )

    except Exception as exc:
        return f"Spotify açılamadı: {exc}"


def _play_spotify_top_result_via_vision(query: str) -> bool:
    """Spotify arama sonuçlarındaki ilk şarkıyı Gemini vision ile bulup oynat.

    Spotify Desktop'ta bir şarkı satırına çift tık her zaman oynatmayı
    başlatır; tek tık ise yalnızca seçer. Bu yüzden bulduğumuz koordinata
    çift tık atıyoruz. Ayrıca bazı sürümlerde "Top result" kartının
    üzerinde yeşil oynat (▶) butonu görünür; vision tarafında her ikisi
    de kabul edilir.

    Returns:
        ``True`` tıklama başarıyla gönderildiyse, ``False`` aksi halde
        (Gemini anahtarı yok, vision objeyi bulamadı, koordinat ekran
        dışında, vs.).
    """
    try:
        # Lazy import to avoid circulars at module load.
        from skills.vision.tools import (
            _ask_gemini_for_click_location,
            _capture_active_window,
            _coerce_normalized_click_point,
            _execute_stable_click,
            _normalized_bbox_to_pixel,
            _normalized_point_to_pixel,
            _refine_click_location,
            _virtual_screen_bounds,
        )
    except Exception:
        return False

    api_key = str(get_app_config_value("gemini_api_key", "") or "").strip()
    if not api_key:
        return False

    ok, _raw, payload = _capture_active_window()
    if not ok or not payload:
        return False

    image_path_str = payload.get("image_path") or ""
    bounds = payload.get("bounds") or {}
    if not image_path_str:
        return False

    from pathlib import Path as _Path  # noqa: PLC0415
    image_path = _Path(image_path_str)
    try:
        if not image_path.exists() or image_path.stat().st_size <= 0:
            return False

        try:
            from PIL import Image as _Image  # noqa: PLC0415
            with _Image.open(image_path) as im:
                img_w, img_h = im.size
        except Exception:
            return False

        target_desc = (
            "Spotify arama sonuçlarındaki en üstteki 'Top result' şarkı "
            f"kartı. Üzerinde yeşil oynat (play) butonu varsa o butonun "
            f"merkezi tercih edilmelidir; aksi halde kartın merkezi. "
            f"Aranan şarkı: '{query}'."
        )

        try:
            response = _ask_gemini_for_click_location(target_desc, image_path)
        except Exception:
            return False

        if not response.get("found"):
            return False

        bbox = response.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return False

        try:
            cx, cy, _ = _normalized_bbox_to_pixel(bbox, img_w, img_h, bounds)
        except Exception:
            return False

        norm_click_point = _coerce_normalized_click_point(response)
        if norm_click_point is not None:
            try:
                cx, cy = _normalized_point_to_pixel(
                    norm_click_point, img_w, img_h, bounds
                )
            except Exception:
                pass

        # Refine pass — Spotify satır kartı geniş olduğu için merkez
        # ofseti büyük olabilir; crop ile daha hassas bir merkez al.
        try:
            refined = _refine_click_location(
                target_desc, image_path, bbox, img_w, img_h
            )
        except Exception:
            refined = None
        if refined is not None:
            refined_x_img, refined_y_img = refined
            offset_x = int((bounds or {}).get("left", 0) or 0)
            offset_y = int((bounds or {}).get("top", 0) or 0)
            refined_cx = int(round(refined_x_img + offset_x))
            refined_cy = int(round(refined_y_img + offset_y))
            if abs(refined_cx - cx) <= 240 and abs(refined_cy - cy) <= 240:
                cx, cy = refined_cx, refined_cy

        v_left, v_top, v_right, v_bottom = _virtual_screen_bounds()
        if not (v_left <= cx < v_right and v_top <= cy < v_bottom):
            return False

        try:
            # Çift tık — Spotify satırlarında oynatmayı garanti eder.
            _execute_stable_click(cx, cy, "left", 2)
        except Exception:
            return False
        return True
    finally:
        try:
            if image_path.exists():
                image_path.unlink()
        except Exception:
            pass


def play_media(query: str, provider: str = "auto", autoplay: bool = True) -> str:
    """Sesli komutla şarkı / video oynatma; provider="auto" ise Spotify→YouTube."""
    if not query or not query.strip():
        return "Çalınacak içerik belirtilmedi."

    normalized_provider = (provider or "auto").strip().lower()
    if normalized_provider in {"yt", "youtube music", "youtube"}:
        normalized_provider = "youtube"
    elif normalized_provider in {"apple music", "music", "apple_music"}:
        # Windows'ta Apple Music yok; YouTube'a yönlendir.
        normalized_provider = "youtube"

    if normalized_provider == "spotify":
        return _play_spotify(query, autoplay=autoplay)
    if normalized_provider == "youtube":
        return _play_youtube(query)

    # auto: önce Spotify, başarısız olursa YouTube.
    result = _play_spotify(query, autoplay=autoplay)
    if "açılamadı" not in result and "yapılamadı" not in result:
        return result
    return _play_youtube(query)


play_media.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "play_media",
        "description": (
            "YouTube veya Spotify'da şarkı, müzik veya video açar. "
            "Kullanıcı belirli bir platform söylerse onu kullan. "
            "Belirtmezse uygun olanı dene. "
            "Kullanıcı 'çal', 'oynat', 'aç' diyorsa autoplay=true kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": "Şarkı, sanatçı, albüm veya video arama ifadesi",
                },
                "provider": {
                    "type": "STRING",
                    "description": "auto | youtube | spotify",
                },
                "autoplay": {
                    "type": "BOOLEAN",
                    "description": "true ise mümkünse doğrudan oynatır",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# get_youtube_channel_report — public YouTube Data API
# ---------------------------------------------------------------------------


API_ROOT = "https://www.googleapis.com/youtube/v3"
DEFAULT_VIDEO_LIMIT = 6
TIMEOUT = 14
CHANNEL_ID_RE = re.compile(r"^UC[a-zA-Z0-9_-]{22}$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com"}
ISO_DURATION_RE = re.compile(r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?")


def _format_int(value: int) -> str:
    return f"{int(value):,}".replace(",", ".")


def _parse_duration_seconds(raw: str) -> int:
    match = ISO_DURATION_RE.match(raw or "")
    if not match:
        return 0
    hours = int(match.group("h") or 0)
    minutes = int(match.group("m") or 0)
    seconds = int(match.group("s") or 0)
    return hours * 3600 + minutes * 60 + seconds


def _format_duration(raw: str) -> str:
    total = _parse_duration_seconds(raw)
    if total <= 0:
        return ""
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}s {minutes}dk"
    if minutes:
        return f"{minutes}dk {seconds:02d}sn"
    return f"{seconds}sn"


def _parse_dt(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except Exception:
        return None


def _days_ago_text(published_at: str) -> str:
    published = _parse_dt(published_at)
    if not published:
        return ""
    now = dt.datetime.now(dt.timezone.utc)
    delta = now - published.astimezone(dt.timezone.utc)
    days = max(0, delta.days)
    if days == 0:
        return "bugün"
    if days == 1:
        return "dün"
    return f"{days} gün önce"


def _average(values: list[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _normalize_channel_ref(raw: str) -> tuple[str | None, str]:
    value = str(raw or get_app_config_value("youtube_channel_handle", "") or "").strip()
    if not value:
        return None, ""

    if value.startswith("@"):
        return "forHandle", value

    if CHANNEL_ID_RE.match(value):
        return "id", value

    if value.startswith("http://") or value.startswith("https://"):
        parsed = urlparse(value)
        host = parsed.netloc.lower()
        if host in YOUTUBE_HOSTS:
            path = parsed.path.strip("/")
            if path.startswith("@"):
                return "forHandle", path
            if path.startswith("channel/"):
                channel_id = path.split("/", 1)[1].strip()
                if channel_id:
                    return "id", channel_id

    return "forHandle", value if value.startswith("@") else f"@{value}"


def _api_get(endpoint: str, params: dict, api_key: str) -> dict:
    response = requests.get(
        f"{API_ROOT}/{endpoint}",
        params={**params, "key": api_key},
        timeout=TIMEOUT,
        headers={"User-Agent": "JARVIS Windows"},
    )
    if response.ok:
        return response.json()

    try:
        payload = response.json()
    except Exception:
        payload = {}

    error = payload.get("error") or {}
    reasons = error.get("errors") or []
    reason = ""
    if reasons and isinstance(reasons[0], dict):
        reason = str(reasons[0].get("reason", "") or "")
    message = str(error.get("message", "") or "")

    if reason == "keyInvalid":
        raise RuntimeError("YouTube API anahtari gecersiz gorunuyor.")
    if reason == "quotaExceeded":
        raise RuntimeError("YouTube API kotasi su anda dolu gorunuyor.")
    if reason in {"accessNotConfigured", "forbidden"}:
        raise RuntimeError("YouTube Data API bu anahtar icin aktif degil veya erisim engelli.")
    if response.status_code == 404:
        raise RuntimeError("YouTube verisi bulunamadi.")
    raise RuntimeError(message or f"YouTube API hatasi ({response.status_code}).")


def _fetch_channel_payload(channel_ref: str, api_key: str) -> tuple[dict, str]:
    filter_name, filter_value = _normalize_channel_ref(channel_ref)
    if not filter_name or not filter_value:
        raise RuntimeError("YouTube kanal handle'i ayarlanmamis. Ayarlardan @handle gir.")

    payload = _api_get(
        "channels",
        {
            "part": "snippet,statistics,contentDetails",
            filter_name: filter_value,
            "maxResults": 1,
        },
        api_key,
    )
    items = payload.get("items") or []
    if not items:
        raise RuntimeError(
            f"YouTube kanalini bulamadim. Ayarlardaki kanal handle alanina '{filter_value}' benzeri bir deger gir."
        )
    return items[0], filter_value


def _fetch_recent_videos(uploads_playlist_id: str, api_key: str, video_limit: int) -> list[dict]:
    playlist_payload = _api_get(
        "playlistItems",
        {
            "part": "snippet,contentDetails",
            "playlistId": uploads_playlist_id,
            "maxResults": max(1, min(10, video_limit)),
        },
        api_key,
    )
    items = playlist_payload.get("items") or []
    if not items:
        return []

    by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []
    for item in items:
        snippet = item.get("snippet") or {}
        details = item.get("contentDetails") or {}
        video_id = (
            details.get("videoId")
            or ((snippet.get("resourceId") or {}).get("videoId"))
        )
        if not video_id:
            continue
        ordered_ids.append(video_id)
        by_id[video_id] = {
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "published_at": snippet.get("publishedAt", ""),
        }

    if not ordered_ids:
        return []

    videos_payload = _api_get(
        "videos",
        {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(ordered_ids),
        },
        api_key,
    )

    for item in videos_payload.get("items") or []:
        video_id = item.get("id", "")
        if video_id not in by_id:
            continue
        snippet = item.get("snippet") or {}
        stats = item.get("statistics") or {}
        by_id[video_id].update(
            {
                "title": snippet.get("title") or by_id[video_id]["title"],
                "published_at": snippet.get("publishedAt") or by_id[video_id]["published_at"],
                "views": int(stats.get("viewCount") or 0),
                "likes": int(stats.get("likeCount") or 0),
                "comments": int(stats.get("commentCount") or 0),
                "duration": item.get("contentDetails", {}).get("duration", ""),
            }
        )

    return [by_id[video_id] for video_id in ordered_ids if video_id in by_id]


def _trend_sentence(videos: list[dict]) -> str:
    if len(videos) < 4:
        return ""
    split = max(2, len(videos) // 2)
    recent = videos[:split]
    older = videos[split:]
    recent_avg = _average([video.get("views", 0) for video in recent])
    older_avg = _average([video.get("views", 0) for video in older])
    if older_avg <= 0:
        return ""
    ratio = recent_avg / older_avg
    if ratio >= 1.18:
        return "Son videolar onceki gruba gore daha guclu performans gosteriyor."
    if ratio <= 0.82:
        return "Son videolar onceki gruba gore biraz daha yavas gidiyor."
    return "Son videolarin performansi genel olarak dengeli."


def get_youtube_channel_report(
    query: str = "overview",
    handle: str = "",
    video_limit: int = DEFAULT_VIDEO_LIMIT,
) -> str:
    """Public YouTube Data API üzerinden kanal raporu çek."""
    api_key = str(get_app_config_value("youtube_api_key", "") or "").strip()
    if not api_key:
        return (
            "YouTube istatistikleri icin once YouTube API Key gerekli. "
            "Ayarlar > API Settings icinden YouTube API anahtarini gir."
        )

    try:
        channel, channel_ref = _fetch_channel_payload(handle, api_key)
        snippet = channel.get("snippet") or {}
        statistics = channel.get("statistics") or {}
        uploads_id = (
            (channel.get("contentDetails") or {}).get("relatedPlaylists") or {}
        ).get("uploads", "")

        channel_title = snippet.get("title", "YouTube kanalin")
        custom_url = str(snippet.get("customUrl", "") or "").strip()
        display_handle = custom_url if custom_url.startswith("@") else channel_ref
        subscribers = int(statistics.get("subscriberCount") or 0)
        total_views = int(statistics.get("viewCount") or 0)
        video_count = int(statistics.get("videoCount") or 0)

        videos = _fetch_recent_videos(uploads_id, api_key, video_limit) if uploads_id else []
        valid_videos = [
            video
            for video in videos
            if video.get("title") and video.get("title") != "Private video"
        ]

        parts = [
            (
                f"Public YouTube verine gore {channel_title} kanalinda "
                f"{_format_int(subscribers)} abone, {_format_int(total_views)} toplam goruntulenme "
                f"ve {_format_int(video_count)} video var."
            )
        ]
        if display_handle:
            parts.append(f"Kanal referansi: {display_handle}.")

        if valid_videos:
            avg_views = round(_average([video.get("views", 0) for video in valid_videos]))
            avg_likes = round(_average([video.get("likes", 0) for video in valid_videos]))
            avg_comments = round(_average([video.get("comments", 0) for video in valid_videos]))
            parts.append(
                f"Son {len(valid_videos)} videonun ortalamasi {_format_int(avg_views)} izlenme, "
                f"{_format_int(avg_likes)} begeni ve {_format_int(avg_comments)} yorum."
            )

            best_video = max(valid_videos, key=lambda item: item.get("views", 0))
            best_age = _days_ago_text(best_video.get("published_at", ""))
            best_duration = _format_duration(best_video.get("duration", ""))
            best_tail: list[str] = []
            if best_age:
                best_tail.append(best_age)
            if best_duration:
                best_tail.append(best_duration)
            parts.append(
                f"En guclu son video '{best_video.get('title', 'Video')}' "
                f"- {_format_int(best_video.get('views', 0))} izlenme"
                + (f" ({', '.join(best_tail)})" if best_tail else "")
                + "."
            )

            publish_dates = [
                _parse_dt(video.get("published_at", "")) for video in valid_videos
            ]
            publish_dates = [value for value in publish_dates if value]
            if len(publish_dates) >= 2:
                gaps: list[float] = []
                for earlier, later in zip(publish_dates[1:], publish_dates[:-1]):
                    delta = later - earlier
                    gaps.append(max(0.0, delta.total_seconds() / 86400))
                if gaps:
                    avg_gap = sum(gaps) / len(gaps)
                    parts.append(
                        f"Yayin tempon son videolarda ortalama {avg_gap:.1f} gunde bir."
                    )

            trend = _trend_sentence(valid_videos)
            if trend:
                parts.append(trend)

            query_l = str(query or "").lower()
            if any(word in query_l for word in ("detay", "analiz", "rapor", "son video", "son videolar")):
                recent_lines: list[str] = []
                for index, video in enumerate(valid_videos[: min(3, len(valid_videos))], start=1):
                    tail = _days_ago_text(video.get("published_at", ""))
                    recent_lines.append(
                        f"{index}. {video.get('title', 'Video')} - "
                        f"{_format_int(video.get('views', 0))} izlenme, "
                        f"{_format_int(video.get('likes', 0))} begeni, "
                        f"{_format_int(video.get('comments', 0))} yorum"
                        + (f" ({tail})" if tail else "")
                    )
                if recent_lines:
                    parts.append("Son video detayi: " + " | ".join(recent_lines) + ".")

        parts.append(
            "Not: Studio erisimi olmadan izlenme suresi, CTR, gosterim, gelir ve trafik kaynagi verilerini goremem."
        )
        return " ".join(parts)
    except Exception as exc:
        return f"YouTube istatistikleri alinamadi: {exc}"


get_youtube_channel_report.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "get_youtube_channel_report",
        "description": (
            "YouTube kanalinin public istatistiklerini ve son videolarin performansini raporlar. "
            "Kullanici kanal istatistiklerini, abone sayisini, son videolarini, buyume hizini "
            "veya YouTube analizini sordugunda kullan. Bu arac Studio yerine public YouTube Data API verisini kullanir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Dogal dilde analiz istegi. Ornek: "
                        "'YouTube istatistiklerim nasil', 'son videolarimi analiz et', "
                        "'kanal buyumemi ozetle'"
                    ),
                },
                "handle": {
                    "type": "STRING",
                    "description": (
                        "Opsiyonel kanal handle'i, kanal linki veya kanal ID'si. "
                        "Bos birakilirsa ayarlardaki youtube_channel_handle kullanilir."
                    ),
                },
                "video_limit": {
                    "type": "NUMBER",
                    "description": "Analize dahil edilecek son video sayisi. Varsayilan 6.",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


__all__ = ["play_media", "get_youtube_channel_report"]
