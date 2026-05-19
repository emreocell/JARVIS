"""Vision skill tool implementations.

İçerdiği handler'lar:

- :func:`analyze_screen` — mss + win32 ile aktif pencereyi yakalar ve
  Gemini vision ile analiz eder. ``inline``; tipik süre 2-4 sn.
- :func:`video_object_detect` — OpenCV ile videodan kareler örnekler ve
  NVIDIA vision modeli ile her kare için JSON obje tespiti yapar.
  ``background``; toplam süre kare sayısı × NVIDIA latency.
- :func:`audio_to_table` — ``speech_recognition`` ile transkripsiyon,
  ardından NVIDIA modeli ile markdown tablo üretimi. ``background``.
- :func:`nvidia_text_task` — NVIDIA REST chat tamamlaması; özetleme,
  planlama, dönüşüm, analiz vb. genel amaçlı text görevleri.
  ``background``.
- :func:`nvidia_image_analyze` — Yerel görseli base64 data URL'e çevirip
  NVIDIA vision modeline soru ile birlikte gönderir. ``background``.

Her tool, Plugin_Host'un kayıt sırasında okuyacağı ``__tool__`` metadata
sözlüğünü dosyanın sonunda fonksiyona ekler. ``declaration`` alanı
Gemini function-calling şemasına bire bir uyar ve eski
``main.TOOL_DECLARATIONS`` listesinden taşınmıştır.

NVIDIA tool'ları ``execution_mode="background"`` ile işaretlidir
(Req 2.2): Tool_Runtime bunları Task_Manager'a delege eder ve Voice_Core
akışını engellemez; sonuçlar Result_Announcer üzerinden uygun
Turn_Boundary'de duyurulur.

Tool sonuçları her zaman Türkçe ve voice-friendly tek paragraflık
metindir.
"""

from __future__ import annotations

import base64
import io
import json
import math
import mimetypes
import re
import tempfile
import time
from pathlib import Path
from typing import Any

import mss
import numpy as np
import pyautogui
import requests
import speech_recognition as sr
from google import genai
from google.genai import errors, types
from PIL import Image

from app_config import get_app_config_value
from memory.memory_manager import record_observation


try:
    import cv2  # type: ignore[reportMissingImports]
except ImportError:
    cv2 = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Ortak sabitler
# ---------------------------------------------------------------------------


VISION_MODELS = (
    "models/gemini-2.0-flash",
    "models/gemini-2.5-flash-lite",
    "models/gemini-2.5-flash",
)
VISION_MAX_DIMENSION = 1800
VISION_MAX_INLINE_BYTES = 5_500_000


# Pasif gözlem -> ilgi alanı eşleşmesi.
# Her tuple: (kategori, görüntülenen etiket, regex pattern listesi).
# Pencere başlığı + Gemini analizi metni üzerinde case-insensitive aranır.
_INTEREST_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "coding",
        "Yazılım geliştirme",
        (
            r"\bvisual studio code\b",
            r"\bvscode\b",
            r"\bpycharm\b",
            r"\bintellij\b",
            r"\bandroid studio\b",
            r"\bxcode\b",
            r"\bsublime text\b",
            r"\batom\b",
            r"\bnotepad\+\+\b",
            r"\bjupyter\b",
            r"\bgithub\b",
            r"\bgitlab\b",
            r"\bbitbucket\b",
            r"\bstack ?overflow\b",
            r"\bcode ?editor\b",
            r"\bkod ed[iı]t[oö]r\b",
            r"\bterminal\b",
            r"\bpowershell\b",
            r"\b(python|javascript|typescript|rust|golang|kotlin|swift)\b",
            r"\.(py|js|ts|tsx|jsx|rs|go|java|cpp|c|cs|kt|swift|rb)\b",
        ),
    ),
    (
        "design",
        "Tasarım ve görsel üretim",
        (
            r"\bphotoshop\b",
            r"\billustrator\b",
            r"\bfigma\b",
            r"\bsketch\b",
            r"\baffinity\b",
            r"\bcanva\b",
            r"\bblender\b",
            r"\bcinema\s*4d\b",
            r"\bafter effects\b",
            r"\bpremiere\b",
            r"\bdavinci\s*resolve\b",
        ),
    ),
    (
        "gaming",
        "Oyun",
        (
            r"\bsteam\b",
            r"\bepic\s*games\b",
            r"\briot\s*client\b",
            r"\bvalorant\b",
            r"\bleague\s*of\s*legends\b",
            r"\bcs2\b",
            r"\bcs:\s*go\b",
            r"\bdota\b",
            r"\bminecraft\b",
            r"\bfortnite\b",
            r"\bxbox\b",
            r"\bplaystation\b",
        ),
    ),
    (
        "music",
        "Müzik dinleme",
        (
            r"\bspotify\b",
            r"\byoutube\s*music\b",
            r"\bapple\s*music\b",
            r"\btidal\b",
            r"\bsoundcloud\b",
            r"\bdeezer\b",
        ),
    ),
    (
        "video_streaming",
        "Video / yayın izleme",
        (
            r"\byoutube\b",
            r"\bnetflix\b",
            r"\bdisney\+?\b",
            r"\bprime\s*video\b",
            r"\btwitch\b",
            r"\bmubi\b",
            r"\bblutv\b",
            r"\bgain\b",
            r"\bexxen\b",
        ),
    ),
    (
        "communication",
        "Mesajlaşma ve iletişim",
        (
            r"\bwhatsapp\b",
            r"\btelegram\b",
            r"\bdiscord\b",
            r"\bslack\b",
            r"\bmicrosoft\s*teams\b",
            r"\bzoom\b",
            r"\bgoogle\s*meet\b",
            r"\bskype\b",
        ),
    ),
    (
        "office_docs",
        "Belge / ofis çalışması",
        (
            r"\bmicrosoft\s*word\b",
            r"\bmicrosoft\s*excel\b",
            r"\bmicrosoft\s*powerpoint\b",
            r"\bgoogle\s*docs\b",
            r"\bgoogle\s*sheets\b",
            r"\bgoogle\s*slides\b",
            r"\bnotion\b",
            r"\bobsidian\b",
            r"\bevernote\b",
        ),
    ),
    (
        "ai_tools",
        "Yapay zeka araçları",
        (
            r"\bchatgpt\b",
            r"\bclaude\b",
            r"\bgemini\b",
            r"\bcopilot\b",
            r"\bperplexity\b",
            r"\bmidjourney\b",
            r"\bstable\s*diffusion\b",
        ),
    ),
    (
        "social_media",
        "Sosyal medya",
        (
            r"\binstagram\b",
            r"\btwitter\b",
            r"\bx\.com\b",
            r"\bfacebook\b",
            r"\btiktok\b",
            r"\blinkedin\b",
            r"\breddit\b",
        ),
    ),
    (
        "shopping",
        "Online alışveriş",
        (
            r"\btrendyol\b",
            r"\bhepsiburada\b",
            r"\bn11\b",
            r"\bamazon\b",
            r"\baliexpress\b",
            r"\bgittigidiyor\b",
        ),
    ),
)

# Aynı oturumda art arda gelen tekrarlı analizleri yutmak için kısa bir
# bellek-içi tampon. Modül yüklendiğinde sıfırlanır; kalıcı sayım her
# zaman ``memory.observations`` içindedir.
_observation_cache: list[tuple[str, float]] = []
_OBSERVATION_DEDUPE_WINDOW_SEC = 90.0

NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
DEFAULT_VIDEO_MODEL = "meta/llama-3.2-90b-vision-instruct"
DEFAULT_TABLE_MODEL = "meta/llama-3.1-70b-instruct"
DEFAULT_TEXT_MODEL = "meta/llama-3.1-70b-instruct"
DEFAULT_IMAGE_MODEL = "meta/llama-3.2-90b-vision-instruct"


# ---------------------------------------------------------------------------
# analyze_screen — Gemini vision üzerinden aktif pencere analizi
# ---------------------------------------------------------------------------


def _screen_permission_message() -> str:
    return (
        "Ekran analizi için Windows'ta özel bir izin gerekmiyor. "
        "Ancak bazı uygulamalar (örneğin VPN, güvenlik yazılımları) ekran "
        "yakalamayı engelleyebilir. Böyle bir durumda uygulamayı yönetici "
        "olarak çalıştırmayı dene."
    )


def _capture_full_virtual_screen() -> tuple[bool, str, dict | None]:
    """Tüm monitörleri kapsayan sanal ekranı (virtual screen) PNG olarak kaydet.

    ``mss.monitors[0]`` Windows'ta tüm görüntüleme aygıtlarını kapsayan
    bounding box'ı verir; primary monitör solda değilse left/top negatif
    olabilir. Dönen ``bounds`` koordinatları global ekran sistemini
    yansıtır ve çağıran tarafın bbox'ları gerçek piksel konumlarına
    offset'lemesi için kullanılır.
    """
    try:
        with mss.mss() as sct:
            try:
                monitor = sct.monitors[0]
            except IndexError:
                # Çok nadir; ekran sayılamadı.
                return False, "Sanal ekran ölçülemedi.", None

            # Monitor boyutu sıfırsa primary monitora düş (boş kapsama).
            mon_w = int(monitor.get("width", 0) or 0)
            mon_h = int(monitor.get("height", 0) or 0)
            if mon_w <= 0 or mon_h <= 0:
                if len(sct.monitors) > 1:
                    monitor = sct.monitors[1]
                    mon_w = int(monitor.get("width", 0) or 0)
                    mon_h = int(monitor.get("height", 0) or 0)
                if mon_w <= 0 or mon_h <= 0:
                    return False, "Ekran boyutları geçersiz.", None

            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

            temp_file = tempfile.NamedTemporaryFile(
                prefix="jarvis-vscreen-",
                suffix=".png",
                delete=False,
            )
            temp_path = Path(temp_file.name)
            img.save(temp_path, format="PNG")

            left = int(monitor.get("left", 0) or 0)
            top = int(monitor.get("top", 0) or 0)
            return True, "", {
                "image_path": str(temp_path),
                "owner_name": "",
                "window_title": "Sanal Ekran",
                "bounds": {
                    "left": left,
                    "top": top,
                    "right": left + mon_w,
                    "bottom": top + mon_h,
                },
                "detail": "",
            }
    except Exception as exc:
        return False, f"Ekran görüntüsü alınamadı: {exc}", None


def _capture_screen() -> tuple[bool, str, Path | None]:
    """Geriye uyumlu alıcı: tüm sanal ekranı PNG olarak kaydeder.

    Önceki sürüm ``mss.monitors[1]`` ile yalnızca primary monitörü
    yakalıyordu; bu çoklu monitör kurulumlarında ikinci ekranı ve
    pencereler arası taşmaları kaybediyordu. Yeni sürüm tüm sanal ekranı
    yakalar ve global koordinat sistemiyle tutarlı kalır. Bu fonksiyonu
    çağıran taraf bbox offset'i kullanmıyorsa (ör. ``analyze_screen``)
    yine de doğru çıktı alır; tıklama gibi pozisyon-duyarlı işler için
    ``_capture_full_virtual_screen()`` tercih edilmelidir.
    """
    ok, raw, payload = _capture_full_virtual_screen()
    if ok and payload:
        try:
            return True, "", Path(payload["image_path"])
        except Exception:
            return False, "Ekran görüntüsü dosyası bulunamadı.", None
    return False, raw, None


def _capture_active_window() -> tuple[bool, str, dict | None]:
    """Aktif pencereyi PrintWindow / win32 API ile yakala."""
    try:
        import win32gui
        import win32ui
        from ctypes import windll

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False, "Aktif pencere bulunamadı.", None

        title = win32gui.GetWindowText(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top

        if width <= 0 or height <= 0:
            return False, "Pencere boyutları geçersiz.", None

        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()

        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        result = windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        if result == 0:
            # Alternatif yöntem: BitBlt. ``win32con`` yalnızca burada
            # gerekli olduğundan ve yokluğunda fallback'in yine de
            # ekran görüntüsüne düşmesi gerektiğinden lazy import.
            try:
                import win32con  # type: ignore[reportMissingImports]
                saveDC.BitBlt(
                    (0, 0), (width, height), mfcDC, (0, 0), win32con.SRCCOPY
                )
            except Exception:
                pass

        bmpinfo = saveBitMap.GetInfo()
        bmpstr = saveBitMap.GetBitmapBits(True)
        img = Image.frombuffer(
            "RGB",
            (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
            bmpstr,
            "raw",
            "BGRX",
            0,
            1,
        )

        win32gui.DeleteObject(saveBitMap.GetHandle())
        saveDC.DeleteDC()
        mfcDC.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwndDC)

        temp_file = tempfile.NamedTemporaryFile(
            prefix="jarvis-window-",
            suffix=".png",
            delete=False,
        )
        temp_path = Path(temp_file.name)
        img.save(temp_path, format="PNG")

        return True, "", {
            "image_path": str(temp_path),
            "owner_name": "",
            "window_title": title,
            "hwnd": int(hwnd),
            "bounds": {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
            },
            "detail": "",
        }
    except ImportError:
        # win32gui yok: sade ekran görüntüsüne düş.
        ok, err, path = _capture_screen()
        if ok and path:
            return True, "", {
                "image_path": str(path),
                "owner_name": "",
                "window_title": "Ekran Görüntüsü",
                "bounds": {},
                "detail": "",
            }
        return False, err, None
    except Exception as exc:
        return False, f"Aktif pencere yakalanamadı: {exc}", None


def _get_foreground_window_snapshot() -> dict[str, int] | None:
    """Öndeki pencerenin kimliğini ve güncel sınırlarını döndür."""
    try:
        import win32gui  # type: ignore[reportMissingImports]

        hwnd = int(win32gui.GetForegroundWindow() or 0)
        if hwnd <= 0:
            return None
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right <= left or bottom <= top:
            return None
        return {
            "hwnd": hwnd,
            "left": int(left),
            "top": int(top),
            "right": int(right),
            "bottom": int(bottom),
        }
    except Exception:
        return None


def _reproject_point_between_bounds(
    x: int,
    y: int,
    source_bounds: dict[str, int] | None,
    target_bounds: dict[str, int] | None,
) -> tuple[int, int] | None:
    """Bir pencereye göre hesaplanan noktayı yeni pencere rect'ine taşı."""
    if not source_bounds or not target_bounds:
        return None
    try:
        src_left = int(source_bounds.get("left", 0) or 0)
        src_top = int(source_bounds.get("top", 0) or 0)
        src_right = int(source_bounds.get("right", 0) or 0)
        src_bottom = int(source_bounds.get("bottom", 0) or 0)
        dst_left = int(target_bounds.get("left", 0) or 0)
        dst_top = int(target_bounds.get("top", 0) or 0)
        dst_right = int(target_bounds.get("right", 0) or 0)
        dst_bottom = int(target_bounds.get("bottom", 0) or 0)
    except Exception:
        return None

    src_w = src_right - src_left
    src_h = src_bottom - src_top
    dst_w = dst_right - dst_left
    dst_h = dst_bottom - dst_top
    if src_w <= 0 or src_h <= 0 or dst_w <= 0 or dst_h <= 0:
        return None

    rel_x = (int(x) - src_left) / float(src_w)
    rel_y = (int(y) - src_top) / float(src_h)
    rel_x = max(0.0, min(1.0, rel_x))
    rel_y = max(0.0, min(1.0, rel_y))
    nx = dst_left + int(round(rel_x * max(0, dst_w - 1)))
    ny = dst_top + int(round(rel_y * max(0, dst_h - 1)))
    return nx, ny


def _image_looks_blank(image_path: Path) -> bool:
    try:
        with Image.open(image_path) as img:
            sample = img.convert("RGB")
            arr = np.array(sample)
            mean = arr.mean()
            return mean < 10 or mean > 245
    except Exception:
        return False


def _build_image_part(image_path: Path) -> types.Part:
    mime_type, _ = mimetypes.guess_type(str(image_path))
    if not mime_type:
        mime_type = "image/png"

    try:
        with Image.open(image_path) as img:
            work = img.copy()
        if work.mode not in {"RGB", "L"}:
            work = work.convert("RGB")

        if max(work.size) > VISION_MAX_DIMENSION:
            work.thumbnail(
                (VISION_MAX_DIMENSION, VISION_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )

        png_buffer = io.BytesIO()
        work.save(png_buffer, format="PNG", optimize=True)
        png_bytes = png_buffer.getvalue()
        if len(png_bytes) <= VISION_MAX_INLINE_BYTES:
            return types.Part.from_bytes(data=png_bytes, mime_type="image/png")

        jpg_buffer = io.BytesIO()
        rgb = work.convert("RGB") if work.mode != "RGB" else work
        rgb.save(jpg_buffer, format="JPEG", quality=88, optimize=True)
        return types.Part.from_bytes(
            data=jpg_buffer.getvalue(), mime_type="image/jpeg"
        )
    except Exception:
        return types.Part.from_bytes(
            data=image_path.read_bytes(),
            mime_type=mime_type,
        )


def _vision_prompt(query: str, owner_name: str, window_title: str) -> str:
    label = window_title or owner_name or "ekran"
    user_query = (query or "Ekranda ne var?").strip()
    return (
        "Sen Windows üzerinde JARVIS için ekran analizi yapan bir görüntü "
        "yorumlayıcısısın.\n"
        "Aşağıdaki ekran görüntüsü aktif pencereye ait.\n"
        f"Pencere bağlamı: {label}\n\n"
        "Görevlerin:\n"
        "1. Pencerenin genel amacını 1-2 cümlede açıkla.\n"
        "2. Görünen önemli metinleri, hata mesajlarını, butonları, "
        "başlıkları ve durum etiketlerini oku.\n"
        "3. Kullanıcı sorusunu bu görüntüye göre doğrudan cevapla.\n"
        "4. Eğer bir hata, uyarı veya dikkat edilmesi gereken bir şey "
        "varsa bunu ayrı ve net belirt.\n"
        "5. Uydurma yapma. Emin olmadığın kısımlarda bunu söyle.\n\n"
        f"Kullanıcı sorusu: {user_query}\n\n"
        "Yanıtı Türkçe ver. Gereksiz uzun olma, ama okunabilir detay ver."
    )


def _extract_response_text(response) -> str:
    text = str(getattr(response, "text", "") or "").strip()
    if text:
        return text

    candidates = getattr(response, "candidates", None) or []
    chunks: list[str] = []
    for candidate in candidates:
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) or []
        for part in parts:
            part_text = str(getattr(part, "text", "") or "").strip()
            if part_text:
                chunks.append(part_text)
    return "\n".join(chunk for chunk in chunks if chunk).strip()


def _is_transient_vision_error(exc: Exception) -> bool:
    if isinstance(exc, (errors.ServerError, TimeoutError)):
        return True

    message = str(exc or "").lower()
    transient_markers = (
        "503", "429", "deadline", "timed out", "timeout",
        "unavailable", "temporarily unavailable", "service unavailable",
        "internal error", "busy", "overloaded", "resource exhausted",
        "try again later", "backend error", "connection reset",
    )
    return any(marker in message for marker in transient_markers)


def _is_quota_vision_error(exc: Exception) -> bool:
    message = str(exc or "").lower()
    quota_markers = (
        "quota", "rate limit", "resource exhausted",
        "too many requests", "quota exceeded", "limit exceeded", "billing",
    )
    return any(marker in message for marker in quota_markers)


def _friendly_vision_error(exc: Exception) -> str:
    if _is_quota_vision_error(exc):
        return (
            "Gemini vision isteği kota veya hız limitine takıldı. Biraz "
            "bekleyip tekrar dene ya da API planını kontrol et."
        )
    if _is_transient_vision_error(exc):
        return (
            "Gemini vision servisi şu anda yoğun veya geçici olarak "
            "ulaşılamıyor. Biraz sonra tekrar dene."
        )
    return f"Gemini vision isteği başarısız oldu: {exc}"


def _analyze_with_gemini(
    query: str,
    image_path: Path,
    owner_name: str,
    window_title: str,
) -> str:
    api_key = str(get_app_config_value("gemini_api_key", "") or "").strip()
    if not api_key:
        return "Gemini API anahtarı eksik olduğu için ekran analizi yapılamadı."

    prompt = _vision_prompt(query, owner_name, window_title)
    client = genai.Client(api_key=api_key)
    image_part = _build_image_part(image_path)
    retry_delays = (0.9, 1.8, 3.0)
    last_error: Exception | None = None

    for model_name in VISION_MODELS:
        for attempt, delay in enumerate(retry_delays, start=1):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_text(text=prompt),
                        image_part,
                    ],
                    config=types.GenerateContentConfig(temperature=0.2),
                )
                merged = _extract_response_text(response)
                if merged:
                    return merged
                raise RuntimeError(
                    "Gemini geçerli bir ekran analizi metni döndürmedi."
                )
            except Exception as exc:
                last_error = exc
                if attempt < len(retry_delays) and _is_transient_vision_error(exc):
                    time.sleep(delay)
                    continue
                if _is_transient_vision_error(exc):
                    break
                raise RuntimeError(_friendly_vision_error(exc)) from exc

    assert last_error is not None
    raise RuntimeError(_friendly_vision_error(last_error))


def _record_passive_interests(
    window_title: str,
    owner_name: str,
    analysis_text: str,
) -> None:
    """Pencere başlığı + Gemini analizi metninden ilgi alanı çıkarımı.

    Sessizce çalışır: hiçbir şey eşleşmezse no-op'tur. Eşleşen kategoriler
    için ``memory.observations`` sayacı arttırılır ve eşik aşıldığında
    ``interests`` bucket'ına otomatik kayıt düşer (bkz.
    ``memory_manager.record_observation``).

    Aynı kategorinin son 90 saniye içinde tekrar tetiklenmesi yutulur;
    böylece kullanıcı tek bir uzun oturumda 50 kez analyze_screen çağırsa
    bile sayaç gerçekçi bir hızda artar.
    """
    haystack_parts = [
        str(window_title or ""),
        str(owner_name or ""),
        str(analysis_text or ""),
    ]
    haystack = " ".join(part for part in haystack_parts if part).lower()
    if not haystack:
        return

    now = time.monotonic()
    # Eski cache girdilerini temizle.
    global _observation_cache
    _observation_cache = [
        (cat, ts)
        for cat, ts in _observation_cache
        if now - ts < _OBSERVATION_DEDUPE_WINDOW_SEC
    ]
    recently_seen = {cat for cat, _ in _observation_cache}

    matched: list[tuple[str, str]] = []
    for category, label, patterns in _INTEREST_PATTERNS:
        if category in recently_seen:
            continue
        for pattern in patterns:
            try:
                if re.search(pattern, haystack):
                    matched.append((category, label))
                    break
            except re.error:
                # Bozuk pattern'i sessizce atla; kullanıcı deneyimini bozma.
                continue

    if not matched:
        return

    for category, label in matched:
        try:
            record_observation(category, label)
        except Exception:
            # Hafıza yazımı başarısız olsa bile ekran analizi sonucunu
            # kullanıcıya ulaştırmamız önemli.
            continue
        _observation_cache.append((category, now))


def analyze_screen(query: str, target: str = "active_window") -> str:
    """Aktif pencerenin ekran görüntüsünü Gemini vision ile analiz et."""
    target = (target or "active_window").strip().lower()

    if target == "active_window":
        ok, raw, payload = _capture_active_window()
    else:
        ok, raw, path = _capture_screen()
        if ok and path:
            payload = {
                "image_path": str(path),
                "owner_name": "",
                "window_title": "Ekran Görüntüsü",
                "bounds": {},
                "detail": "",
            }
        else:
            payload = None

    if not ok:
        return f"Ekran görüntüsü alınamadı: {raw}"

    if not payload:
        return "Ekran görüntüsü alınamadı."

    image_path = Path(payload["image_path"])
    owner_name = str(payload.get("owner_name", "") or "").strip()
    window_title = str(payload.get("window_title", "") or "").strip()

    try:
        if not image_path.exists():
            return "Ekran görüntüsü dosyası bulunamadı. Tekrar dene."
        if image_path.stat().st_size <= 0:
            return "Ekran görüntüsü boş geldi. Tekrar dene."
        if _image_looks_blank(image_path):
            return (
                "Ekran görüntüsü siyah veya boş görünüyor. "
                + _screen_permission_message()
            )

        try:
            analysis = _analyze_with_gemini(
                query, image_path, owner_name, window_title
            )
        except Exception as exc:
            prefix = f"{owner_name} / {window_title}".strip(" /")
            if prefix:
                return (
                    f"Ekran görüntüsü alındı ({prefix}) ama analiz "
                    f"tamamlanamadı: {exc}"
                )
            return f"Ekran görüntüsü alındı ama analiz tamamlanamadı: {exc}"

        # Pasif ilgi alanı çıkarımı: pencere başlığı + Gemini analizi
        # metninden tanıdık uygulama izlerini sayar; eşik aşıldığında
        # otomatik olarak interests altına kaydeder.
        try:
            _record_passive_interests(window_title, owner_name, analysis)
        except Exception:
            pass

        if owner_name or window_title:
            title = " / ".join(
                part for part in (owner_name, window_title) if part
            ).strip()
            if title:
                return f"[Aktif pencere: {title}]\n{analysis}"
        return analysis
    finally:
        try:
            if image_path.exists():
                image_path.unlink()
        except Exception:
            pass


analyze_screen.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "analyze_screen",
        "description": (
            "Aktif pencerenin ekran goruntusunu alip Gemini vision ile "
            "analiz eder. Kullanici ekranda ne oldugunu, bir hatayi, "
            "gorunen metni, butonlari veya pencere icerigini sordugunda "
            "kullan. Bu surum yalnizca aktif pencereyi destekler."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Kullanicinin ekranla ilgili sorusu. Ornek: "
                        "'Bu hatayi oku', 'Ekranda ne var?'"
                    ),
                },
                "target": {
                    "type": "STRING",
                    "description": "Su an sadece active_window desteklenir.",
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "inline",
}


# ---------------------------------------------------------------------------
# NVIDIA tabanli ortak yardımcılar
# ---------------------------------------------------------------------------


def _nvidia_api_key() -> str:
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


def _nvidia_chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.1,
    max_tokens: int = 1000,
) -> str:
    """NVIDIA chat completions REST çağrısı; tek metin döndürür."""
    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtari eksik.")

    response = requests.post(
        NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
        timeout=120,
    )

    if response.status_code >= 400:
        detail = response.text.strip()[:400]
        raise RuntimeError(
            f"NVIDIA API hatasi ({response.status_code}): {detail}"
        )

    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("NVIDIA API bos yanit dondurdu.")

    content = (choices[0] or {}).get("message", {}).get("content", "")
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
        text = "\n".join(p for p in text_parts if p).strip()
    else:
        text = str(content or "").strip()

    if not text:
        raise RuntimeError("NVIDIA modeli bos metin dondurdu.")
    return text


def _extract_json(text: str) -> dict[str, Any]:
    clean = text.strip()
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model gecerli JSON dondurmedi.")
    return json.loads(clean[start : end + 1])


def _file_to_data_url(file_path: Path, max_bytes: int = 7_500_000) -> str:
    if not file_path.exists() or not file_path.is_file():
        raise RuntimeError("Dosya bulunamadi.")
    size = file_path.stat().st_size
    if size <= 0:
        raise RuntimeError("Dosya bos.")
    if size > max_bytes:
        raise RuntimeError(
            f"Dosya cok buyuk ({size} bayt). Daha kucuk bir dosya kullan."
        )

    mime_type, _ = mimetypes.guess_type(str(file_path))
    if not mime_type:
        mime_type = "application/octet-stream"
    encoded = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


# ---------------------------------------------------------------------------
# video_object_detect — videodan kare örnekleyip NVIDIA vision ile obje tespiti
# ---------------------------------------------------------------------------


def _sample_video_frames(
    video_path: Path,
    frame_interval_sec: float,
    max_frames: int,
) -> list[dict[str, Any]]:
    if cv2 is None:
        raise RuntimeError(
            "Videodan obje algilama icin 'opencv-python' paketi gerekli."
        )

    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(
            "Video acilamadi. Dosya yolu veya codec gecersiz olabilir."
        )

    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0.0 or math.isnan(fps):
            fps = 24.0
        frame_step = max(1, int(round(fps * max(0.2, frame_interval_sec))))
        total_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)

        sampled: list[dict[str, Any]] = []
        frame_index = 0

        while len(sampled) < max_frames:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                break

            encoded_ok, encoded = cv2.imencode(
                ".jpg",
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), 86],
            )
            if not encoded_ok:
                raise RuntimeError("Video karesi kodlanamadi.")

            sampled.append(
                {
                    "frame_index": frame_index,
                    "timestamp": frame_index / fps,
                    "image_b64": base64.b64encode(encoded.tobytes()).decode("ascii"),
                }
            )

            frame_index += frame_step
            if total_frames and frame_index >= total_frames:
                break

        if not sampled:
            raise RuntimeError("Analiz icin videodan kare alinmadi.")
        return sampled
    finally:
        capture.release()


def _detect_objects_in_frame(
    image_b64: str,
    query: str,
    model: str,
) -> list[dict[str, Any]]:
    user_query = (query or "Videodaki objeleri tespit et.").strip()
    prompt = (
        "Bu goruntu bir videodan alinmis tek bir kare.\n"
        "Karedeki onemli objeleri tespit et.\n"
        "Sadece su formatta JSON dondur:\n"
        '{"objects":[{"name":"object","confidence":0.0}]}\n'
        "Ek aciklama yazma. confidence 0.0 ile 1.0 arasinda olsun.\n"
        f"Kullanici istegi: {user_query}"
    )

    raw = _nvidia_chat(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        },
                    },
                ],
            }
        ],
        temperature=0.0,
        max_tokens=500,
    )

    payload = _extract_json(raw)
    objects = payload.get("objects")
    if not isinstance(objects, list):
        raise RuntimeError("Model objeleri beklenen formatta dondurmedi.")

    cleaned: list[dict[str, Any]] = []
    for item in objects:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        try:
            confidence = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        cleaned.append(
            {"name": name, "confidence": max(0.0, min(confidence, 1.0))}
        )
    return cleaned


def video_object_detect(
    video_path: str,
    query: str = "",
    frame_interval_sec: float = 2.0,
    max_frames: int = 5,
    model: str = DEFAULT_VIDEO_MODEL,
) -> str:
    """Videodan kareler örnekleyip NVIDIA vision ile obje tespiti yap."""
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtari girilmedigi icin video obje analizi "
            "kullanilamiyor."
        )

    path = Path(str(video_path or "").strip()).expanduser()
    if not path.exists() or not path.is_file():
        return "Video dosyasi bulunamadi. Gecerli bir video yolu ver."

    try:
        interval = float(frame_interval_sec or 2.0)
    except (TypeError, ValueError):
        interval = 2.0

    try:
        frames = int(max_frames or 5)
    except (TypeError, ValueError):
        frames = 5

    interval = max(0.2, min(interval, 10.0))
    frames = max(1, min(frames, 12))

    try:
        sampled_frames = _sample_video_frames(path, interval, frames)
    except Exception as exc:
        return f"Video kareleri alinamadi: {exc}"

    summary: dict[str, dict[str, float]] = {}
    timeline: list[str] = []

    for frame in sampled_frames:
        try:
            detections = _detect_objects_in_frame(
                frame["image_b64"],
                query,
                str(model or DEFAULT_VIDEO_MODEL),
            )
        except Exception as exc:
            timeline.append(
                f"- {frame['timestamp']:.1f}s: analiz basarisiz ({exc})"
            )
            continue

        names_in_frame: list[str] = []
        for detected in detections:
            name = str(detected.get("name", "")).strip()
            if not name:
                continue
            confidence = float(detected.get("confidence", 0.0))
            bucket = summary.setdefault(
                name, {"count": 0.0, "confidence_sum": 0.0}
            )
            bucket["count"] += 1.0
            bucket["confidence_sum"] += confidence
            names_in_frame.append(f"{name} ({confidence:.2f})")

        if names_in_frame:
            timeline.append(
                f"- {frame['timestamp']:.1f}s: " + ", ".join(names_in_frame)
            )
        else:
            timeline.append(
                f"- {frame['timestamp']:.1f}s: belirgin obje bulunamadi"
            )

    if not summary:
        return (
            "Video analiz edildi ancak guvenilir obje tespiti yapilamadi.\n"
            + "\n".join(timeline[:10])
        )

    ranked = sorted(
        summary.items(),
        key=lambda item: (item[1]["count"], item[1]["confidence_sum"]),
        reverse=True,
    )
    top_lines = []
    for name, data in ranked[:12]:
        avg_conf = data["confidence_sum"] / max(1.0, data["count"])
        top_lines.append(
            f"- {name}: {int(data['count'])} kare, ort. guven {avg_conf:.2f}"
        )

    return (
        f"NVIDIA video obje analizi tamamlandi "
        f"({len(sampled_frames)} kare tarandi).\n"
        "Tespit ozeti:\n"
        + "\n".join(top_lines)
        + "\n\nKare bazli detay:\n"
        + "\n".join(timeline[:12])
    )


video_object_detect.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "video_object_detect",
        "description": (
            "Bir video dosyasindan belirli araliklarla kareler alip NVIDIA "
            "vision modeli ile obje tespiti yapar. Kullanici videodaki "
            "kisileri, araclari, nesneleri veya sahne icerigini analiz "
            "etmek istediginde kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "video_path": {
                    "type": "STRING",
                    "description": (
                        "Video dosya yolu. Ornek: "
                        "C:\\\\Users\\\\...\\\\video.mp4"
                    ),
                },
                "query": {
                    "type": "STRING",
                    "description": (
                        "Istege bagli odak sorusu. Ornek: 'Hangi objeler var?'"
                    ),
                },
                "frame_interval_sec": {
                    "type": "NUMBER",
                    "description": (
                        "Kare ornekleme araligi saniye cinsinden. "
                        "Varsayilan 2.0"
                    ),
                },
                "max_frames": {
                    "type": "NUMBER",
                    "description": (
                        "Analiz edilecek maksimum kare sayisi. Varsayilan 5"
                    ),
                },
                "model": {
                    "type": "STRING",
                    "description": (
                        "Opsiyonel NVIDIA model adi. Ornek: "
                        "meta/llama-3.2-90b-vision-instruct"
                    ),
                },
            },
            "required": ["video_path"],
        },
    },
    "execution_mode": "background",
}


# ---------------------------------------------------------------------------
# audio_to_table — sesi metne çevirip NVIDIA modeli ile markdown tablo üret
# ---------------------------------------------------------------------------


def _transcribe_audio_file(audio_path: Path, language: str) -> str:
    recognizer = sr.Recognizer()
    with sr.AudioFile(str(audio_path)) as source:
        audio = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio, language=language).strip()
    except sr.UnknownValueError as exc:
        raise RuntimeError(
            "Ses anlasilamadi. Daha temiz bir kayit deneyin."
        ) from exc
    except sr.RequestError as exc:
        raise RuntimeError(
            f"Ses tanima servisine ulasilamadi: {exc}"
        ) from exc


def _extract_markdown_table(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    table_lines = [line for line in lines if "|" in line]
    if len(table_lines) < 2:
        raise RuntimeError("Model gecerli bir markdown tablo dondurmedi.")
    return "\n".join(table_lines)


def audio_to_table(
    audio_path: str,
    columns: str = "",
    language: str = "tr-TR",
    model: str = DEFAULT_TABLE_MODEL,
) -> str:
    """Ses kaydını metne çevir ve markdown tabloya dönüştür."""
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtari girilmedigi icin sesten tablo olusturma "
            "kullanilamiyor."
        )

    path = Path(str(audio_path or "").strip()).expanduser()
    if not path.exists() or not path.is_file():
        return "Ses dosyasi bulunamadi. Gecerli bir dosya yolu ver."

    suffix = path.suffix.lower()
    if suffix not in {".wav", ".aiff", ".aifc", ".flac"}:
        return "Desteklenen ses formatlari: .wav, .aiff, .aifc, .flac"

    try:
        transcript = _transcribe_audio_file(path, language or "tr-TR")
    except Exception as exc:
        return f"Ses metne cevrilemedi: {exc}"

    if not transcript:
        return "Ses kaydindan metin cikmadi."

    col_hint = str(columns or "").strip()
    if col_hint:
        col_line = f"Istenen sutunlar: {col_hint}"
    else:
        col_line = "Sutunlari metne gore anlamli sekilde sec."

    prompt = (
        "Asagidaki konusma transkriptini duzenli bir markdown tabloya cevir.\n"
        "Sadece markdown tablo dondur, baska aciklama yazma.\n"
        f"{col_line}\n\n"
        "TRANSKRIPT:\n"
        f"{transcript}"
    )

    try:
        raw_table = _nvidia_chat(
            model=str(model or DEFAULT_TABLE_MODEL),
            messages=[
                {
                    "role": "system",
                    "content": "Duzenli, kisa ve gecerli markdown tablo uret.",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1100,
        )
        table = _extract_markdown_table(raw_table)
    except Exception as exc:
        return f"Tablo olusturulamadi: {exc}"

    return (
        "Sesten tablo olusturma tamamlandi.\n\n"
        "### Transkript\n"
        f"{transcript}\n\n"
        "### Tablo\n"
        f"{table}"
    )


audio_to_table.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "audio_to_table",
        "description": (
            "Bir ses kaydini metne cevirip NVIDIA modeli ile markdown "
            "tabloya donusturur. Toplanti notlari, sesli listeler veya "
            "dikte icerigini tabloya cevirmek icin kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "audio_path": {
                    "type": "STRING",
                    "description": (
                        "Ses dosya yolu (.wav, .aiff, .aifc, .flac)."
                    ),
                },
                "columns": {
                    "type": "STRING",
                    "description": (
                        "Opsiyonel sutun listesi. Ornek: "
                        "gorev,sorumlu,tarih,durum"
                    ),
                },
                "language": {
                    "type": "STRING",
                    "description": "Konusma dili. Varsayilan tr-TR.",
                },
                "model": {
                    "type": "STRING",
                    "description": (
                        "Opsiyonel NVIDIA metin modeli. Ornek: "
                        "meta/llama-3.1-70b-instruct"
                    ),
                },
            },
            "required": ["audio_path"],
        },
    },
    "execution_mode": "background",
}


# ---------------------------------------------------------------------------
# nvidia_text_task — NVIDIA chat completions üzerinden genel text görevi
# ---------------------------------------------------------------------------


def nvidia_text_task(
    prompt: str,
    model: str = DEFAULT_TEXT_MODEL,
    system_prompt: str = "",
    temperature: float = 0.2,
    max_tokens: int = 1200,
) -> str:
    """NVIDIA text modelleriyle genel amaçlı bir prompt çalıştır."""
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtari girilmedigi icin NVIDIA metin gorevi "
            "kullanilamiyor."
        )

    clean_prompt = str(prompt or "").strip()
    if not clean_prompt:
        return "Metin gorevi icin prompt bos olamaz."

    selected_model = str(model or DEFAULT_TEXT_MODEL).strip()
    try:
        temp = float(temperature)
    except (TypeError, ValueError):
        temp = 0.2
    try:
        token_count = int(max_tokens)
    except (TypeError, ValueError):
        token_count = 1200

    temp = max(0.0, min(temp, 1.5))
    token_count = max(128, min(token_count, 4096))

    messages: list[dict[str, Any]] = []
    clean_system = str(system_prompt or "").strip()
    if clean_system:
        messages.append({"role": "system", "content": clean_system})
    messages.append({"role": "user", "content": clean_prompt})

    try:
        output = _nvidia_chat(
            model=selected_model,
            messages=messages,
            temperature=temp,
            max_tokens=token_count,
        )
    except Exception as exc:
        return f"NVIDIA metin gorevi basarisiz: {exc}"

    return (
        f"NVIDIA metin gorevi tamamlandi. Model: {selected_model}\n\n"
        f"{output}"
    )


nvidia_text_task.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "nvidia_text_task",
        "description": (
            "NVIDIA text modellerini genel amacli gorevlerde kullanir: "
            "ozetleme, planlama, donusum, analiz, fikir uretimi. "
            "Kullanici belirli bir NVIDIA modeline gore metin gorevi "
            "isterse kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {
                    "type": "STRING",
                    "description": "Modelin yapmasi istenen gorev aciklamasi.",
                },
                "model": {
                    "type": "STRING",
                    "description": (
                        "NVIDIA model adi. Ornek: "
                        "qwen/qwen3-coder-480b-a35b-instruct"
                    ),
                },
                "system_prompt": {
                    "type": "STRING",
                    "description": "Opsiyonel sistem talimati.",
                },
                "temperature": {
                    "type": "NUMBER",
                    "description": "Yaraticilik seviyesi. Varsayilan 0.2",
                },
                "max_tokens": {
                    "type": "NUMBER",
                    "description": (
                        "Maksimum cikti token limiti. Varsayilan 1200"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    "execution_mode": "background",
}


# ---------------------------------------------------------------------------
# nvidia_image_analyze — NVIDIA vision modeliyle yerel görsel analizi
# ---------------------------------------------------------------------------


def nvidia_image_analyze(
    image_path: str,
    query: str = "",
    model: str = DEFAULT_IMAGE_MODEL,
) -> str:
    """Yerel bir görseli NVIDIA vision modeliyle analiz et."""
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtari girilmedigi icin gorsel analiz "
            "kullanilamiyor."
        )

    path = Path(str(image_path or "").strip()).expanduser()
    if not path.exists() or not path.is_file():
        return "Gorsel dosyasi bulunamadi. Gecerli bir dosya yolu ver."

    selected_model = str(model or DEFAULT_IMAGE_MODEL).strip()
    user_query = str(query or "").strip() or "Bu gorseli detayli analiz et."

    try:
        image_data_url = _file_to_data_url(path)
    except Exception as exc:
        return f"Gorsel okunamadi: {exc}"

    prompt = (
        "Asagidaki gorseli analiz et. "
        "Nesneler, metinler, grafik/tablolar, kritik detaylar ve "
        "kullanicinin sorusuna net yanit ver. Gereksiz uzatma yapma.\n\n"
        f"Kullanici sorusu: {user_query}"
    )

    try:
        output = _nvidia_chat(
            model=selected_model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": image_data_url},
                        },
                    ],
                }
            ],
            temperature=0.1,
            max_tokens=1200,
        )
    except Exception as exc:
        return f"NVIDIA gorsel analizi basarisiz: {exc}"

    return (
        f"NVIDIA gorsel analizi tamamlandi. Model: {selected_model}\n\n"
        f"{output}"
    )


nvidia_image_analyze.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "nvidia_image_analyze",
        "description": (
            "Yerel bir gorseli NVIDIA vision modeliyle analiz eder. "
            "Kullanici ekran goruntusu, grafik, tablo, belge gorseli veya "
            "foto yorumlatmak istediginde kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "image_path": {
                    "type": "STRING",
                    "description": (
                        "Gorsel dosya yolu. Ornek: "
                        "C:\\\\Users\\\\...\\\\image.png"
                    ),
                },
                "query": {
                    "type": "STRING",
                    "description": (
                        "Gorsel hakkinda odak sorusu. Ornek: "
                        "'Bu grafikte ne anlatiliyor?'"
                    ),
                },
                "model": {
                    "type": "STRING",
                    "description": (
                        "NVIDIA vision modeli. Ornek: "
                        "meta/llama-3.2-90b-vision-instruct"
                    ),
                },
            },
            "required": ["image_path"],
        },
    },
    "execution_mode": "background",
}


# ---------------------------------------------------------------------------
# click_on_screen — Gemini vision ile koordinat bul + pyautogui ile tıkla
# ---------------------------------------------------------------------------


_CLICK_PROMPT_TEMPLATE = (
    "Bu bir Windows masaüstü ekran görüntüsü. Görüntü üzerinde bir kullanıcı "
    "şu objeyi tıklamak istiyor: {target}\n\n"
    "Görüntüyü dikkatlice incele ve tıklanması gereken obje için en olası "
    "tıklama noktasını ve sınırlayıcı kutuyu (bounding box) bul. "
    "Koordinatları görüntünün sol üst köşesi (0,0), sağ alt köşesi (1000,1000) "
    "olacak şekilde 0-1000 aralığına normalize et.\n\n"
    "Sadece şu JSON formatında yanıt ver, başka açıklama yazma:\n"
    '{{"found": true, "label": "kısa Türkçe açıklama", '
    '"bbox": [y_min, x_min, y_max, x_max], '
    '"click_point": [y, x], '
    '"confidence": 0.0}}\n\n'
    "Eğer obje görüntüde net olarak görünmüyorsa veya birden fazla muhtemel "
    "eşleşme varsa şunu döndür:\n"
    '{{"found": false, "reason": "Türkçe kısa açıklama", '
    '"candidates": [{{"label": "...", "bbox": [y_min, x_min, y_max, x_max], '
    '"click_point": [y, x]}}]}}'
)


def _parse_click_response(text: str) -> dict[str, Any]:
    """Modelin JSON yanıtını çıkar; markdown bloklarını temizle."""
    clean = (text or "").strip()
    if clean.startswith("```"):
        # ```json ... ``` veya ``` ... ```
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model geçerli JSON döndürmedi.")
    return json.loads(clean[start : end + 1])


def _virtual_screen_bounds() -> tuple[int, int, int, int]:
    """Tüm monitörleri kapsayan virtual screen'in (left, top, right, bottom)'unu döndürür.

    Windows'ta ``GetSystemMetrics`` ile ``SM_XVIRTUALSCREEN`` (76),
    ``SM_YVIRTUALSCREEN`` (77), ``SM_CXVIRTUALSCREEN`` (78),
    ``SM_CYVIRTUALSCREEN`` (79) sabitleri kullanılır. Win32 API'lere
    ulaşılamazsa ``pyautogui.size()`` fallback'ine düşer ve (0, 0,
    primary_w, primary_h) döner.
    """
    try:
        import ctypes  # noqa: PLC0415

        user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        if user32 is not None:
            gsm = getattr(user32, "GetSystemMetrics", None)
            if gsm is not None:
                left = int(gsm(76))
                top = int(gsm(77))
                width = int(gsm(78))
                height = int(gsm(79))
                if width > 0 and height > 0:
                    return left, top, left + width, top + height
    except Exception:
        pass
    try:
        screen_w, screen_h = pyautogui.size()
        return 0, 0, int(screen_w), int(screen_h)
    except Exception:
        return 0, 0, 1920, 1080


def _normalized_bbox_to_pixel(
    bbox: list[float] | tuple[float, ...],
    image_width: int,
    image_height: int,
    bounds: dict[str, int] | None,
) -> tuple[int, int, tuple[int, int, int, int]]:
    """Normalize edilmiş 0-1000 bbox'ı ekran piksel koordinatına çevir.

    Args:
        bbox: ``[y_min, x_min, y_max, x_max]`` (0-1000 aralığında).
        image_width: Yakalanan görüntünün genişliği (px).
        image_height: Yakalanan görüntünün yüksekliği (px).
        bounds: Aktif pencere bounds'u ``{"left", "top", ...}`` veya
            None (tam ekran yakalandığında).

    Returns:
        ``(center_x, center_y, (x1, y1, x2, y2))`` — hepsi global ekran
        piksel koordinatında.
    """
    if image_width <= 0 or image_height <= 0:
        raise ValueError("Görüntü boyutları pozitif olmalı.")

    y_min_n, x_min_n, y_max_n, x_max_n = (float(v) for v in bbox)
    # Bazı modeller koordinatları [x_min, y_min, x_max, y_max] formatında
    # döndürebilir; ikinci ve üçüncü değerden hangisinin x olduğunu
    # tahmin etmek yerine sözleşmeli format kullanıyoruz. Yine de değerler
    # ters ise normalize et.
    y_min_n, y_max_n = sorted((y_min_n, y_max_n))
    x_min_n, x_max_n = sorted((x_min_n, x_max_n))

    # 0-1000 aralığına sıkıştır.
    def _clip(v: float) -> float:
        return max(0.0, min(1000.0, v))

    y_min_n = _clip(y_min_n)
    y_max_n = _clip(y_max_n)
    x_min_n = _clip(x_min_n)
    x_max_n = _clip(x_max_n)

    max_x = max(0, image_width - 1)
    max_y = max(0, image_height - 1)
    rel_x1 = int(round(x_min_n / 1000.0 * max_x))
    rel_y1 = int(round(y_min_n / 1000.0 * max_y))
    rel_x2 = int(round(x_max_n / 1000.0 * max_x))
    rel_y2 = int(round(y_max_n / 1000.0 * max_y))

    cx = (rel_x1 + rel_x2) // 2
    cy = (rel_y1 + rel_y2) // 2

    # Eğer pencere bazlı yakalama yaptıysak global ekrana taşıyalım.
    if bounds:
        offset_x = int(bounds.get("left", 0) or 0)
        offset_y = int(bounds.get("top", 0) or 0)
        cx += offset_x
        cy += offset_y
        rel_x1 += offset_x
        rel_x2 += offset_x
        rel_y1 += offset_y
        rel_y2 += offset_y

    return cx, cy, (rel_x1, rel_y1, rel_x2, rel_y2)


def _normalized_point_to_pixel(
    point: list[float] | tuple[float, ...],
    image_width: int,
    image_height: int,
    bounds: dict[str, int] | None,
) -> tuple[int, int]:
    """Normalize edilmiş 0-1000 noktasını global ekran pikseline çevir."""
    if len(point) != 2:
        raise ValueError("click_point iki elemanlı olmalı: [y, x].")
    if image_width <= 0 or image_height <= 0:
        raise ValueError("Görüntü boyutları pozitif olmalı.")

    y_n = float(point[0])
    x_n = float(point[1])
    y_n = max(0.0, min(1000.0, y_n))
    x_n = max(0.0, min(1000.0, x_n))

    max_x = max(0, image_width - 1)
    max_y = max(0, image_height - 1)
    px = int(round(x_n / 1000.0 * max_x))
    py = int(round(y_n / 1000.0 * max_y))

    if bounds:
        px += int(bounds.get("left", 0) or 0)
        py += int(bounds.get("top", 0) or 0)
    return px, py


def _coerce_normalized_click_point(payload: dict[str, Any]) -> list[float] | None:
    """Model çıktısından normalize tıklama noktası çıkarmaya çalış."""
    raw_point = payload.get("click_point")
    if isinstance(raw_point, (list, tuple)) and len(raw_point) == 2:
        try:
            return [float(raw_point[0]), float(raw_point[1])]
        except (TypeError, ValueError):
            pass

    if "x" in payload and "y" in payload:
        try:
            return [float(payload["y"]), float(payload["x"])]
        except (TypeError, ValueError):
            return None
    return None


def _set_cursor_pos_windows(x: int, y: int) -> bool:
    """Win32 SetCursorPos ile imleci taşı."""
    try:
        import ctypes  # noqa: PLC0415

        user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        if user32 is None:
            return False
        return bool(user32.SetCursorPos(int(x), int(y)))
    except Exception:
        return False


def _send_windows_native_click(x: int, y: int, button: str, click_count: int) -> bool:
    """PyAutoGUI başarısız olursa Win32 mouse_event ile tıklamayı dene."""
    try:
        import ctypes  # noqa: PLC0415

        user32 = getattr(getattr(ctypes, "windll", None), "user32", None)
        if user32 is None:
            return False

        # winuser.h mouse_event bayrakları
        flags = {
            "left": (0x0002, 0x0004),
            "right": (0x0008, 0x0010),
            "middle": (0x0020, 0x0040),
        }
        down_flag, up_flag = flags.get(button, flags["left"])
        if not _set_cursor_pos_windows(x, y):
            return False
        for idx in range(max(1, int(click_count))):
            user32.mouse_event(down_flag, 0, 0, 0, 0)
            time.sleep(0.016)
            user32.mouse_event(up_flag, 0, 0, 0, 0)
            if idx + 1 < click_count:
                time.sleep(0.07)
        return True
    except Exception:
        return False


def _execute_stable_click(x: int, y: int, button: str, click_count: int) -> None:
    """Tıklamayı mümkün olduğunca stabil şekilde gönder."""
    pyautogui.moveTo(x, y, duration=0.12)
    time.sleep(0.03)

    # Çoklu monitörde moveTo bazen birkaç piksel sapabiliyor.
    try:
        pos = pyautogui.position()
        px = int(getattr(pos, "x", x))
        py = int(getattr(pos, "y", y))
        if abs(px - x) > 4 or abs(py - y) > 4:
            _set_cursor_pos_windows(x, y)
    except Exception:
        pass

    try:
        pyautogui.click(x=x, y=y, button=button, clicks=click_count, interval=0.08)
    except Exception as exc:
        # FailSafe (köşe koruması) davranışını bypass etmeyelim.
        if exc.__class__.__name__ == "FailSafeException":
            raise
        if not _send_windows_native_click(x, y, button, click_count):
            raise


def _ask_gemini_for_click_location(
    target: str,
    image_path: Path,
) -> dict[str, Any]:
    """Gemini'ye objeyi bul ve normalleştirilmiş koordinat döndür."""
    api_key = str(get_app_config_value("gemini_api_key", "") or "").strip()
    if not api_key:
        raise RuntimeError(
            "Gemini API anahtarı eksik olduğu için ekranda obje konumu bulunamadı."
        )

    client = genai.Client(api_key=api_key)
    image_part = _build_image_part(image_path)
    prompt = _CLICK_PROMPT_TEMPLATE.format(target=target)
    retry_delays = (0.6, 1.4)
    last_error: Exception | None = None

    for model_name in VISION_MODELS:
        for attempt, delay in enumerate((0.0,) + retry_delays):
            try:
                if attempt > 0:
                    time.sleep(delay)
                response = client.models.generate_content(
                    model=model_name,
                    contents=[
                        types.Part.from_text(text=prompt),
                        image_part,
                    ],
                    config=types.GenerateContentConfig(temperature=0.0),
                )
                text = _extract_response_text(response)
                if not text:
                    raise RuntimeError("Gemini boş yanıt döndürdü.")
                return _parse_click_response(text)
            except json.JSONDecodeError as exc:
                last_error = exc
                # JSON hatasını sadece bir model için tekrar deneme; yeni
                # modele geç.
                break
            except Exception as exc:
                last_error = exc
                if attempt < len(retry_delays) and _is_transient_vision_error(exc):
                    continue
                if _is_transient_vision_error(exc):
                    break
                raise RuntimeError(_friendly_vision_error(exc)) from exc

    assert last_error is not None
    raise RuntimeError(_friendly_vision_error(last_error))


def _refine_click_location(
    target_text: str,
    image_path: Path,
    initial_bbox_norm: list[float] | tuple[float, ...],
    img_w: int,
    img_h: int,
) -> tuple[float, float] | None:
    """Refine pass: crop around initial bbox, ask Gemini for precise center.

    İlk geçişte tüm ekran (veya pencere) görseli üzerinde tahmin
    yaptığımız için bbox kabaca doğru ama merkez 30-80 piksel sapabilir.
    Bu fonksiyon ilk bbox'ın etrafında 1.7× genişlik bir alanı kırpıp
    yalnızca o crop'u yeniden Gemini'ye gönderir ve crop içindeki
    normalleştirilmiş ``[y, x]`` merkez noktasını ister. Sonra bu noktayı
    orijinal görsel koordinat sistemine geri map'ler.

    Returns:
        ``(refined_x_in_image, refined_y_in_image)`` (orijinal görsel
        piksel koordinatında, crop offset'i uygulanmış) veya ``None``
        — refine başarısız olursa çağıran taraf ilk tahmini kullanmalı.
    """
    try:
        api_key = str(get_app_config_value("gemini_api_key", "") or "").strip()
        if not api_key:
            return None

        y_min_n, x_min_n, y_max_n, x_max_n = (float(v) for v in initial_bbox_norm)
        y_min_n, y_max_n = sorted((y_min_n, y_max_n))
        x_min_n, x_max_n = sorted((x_min_n, x_max_n))

        # Crop alanı: kaba bbox + her yöne genişletilmiş margin.
        # Margin oranı bbox'ın kendi boyutuna göredir; çok küçük objeler
        # (örn. 16×16 ikon) için crop bağlamı koruyacak kadar büyük olur.
        bbox_w_n = max(1.0, x_max_n - x_min_n)
        bbox_h_n = max(1.0, y_max_n - y_min_n)
        margin_x = max(60.0, bbox_w_n * 0.7)
        margin_y = max(60.0, bbox_h_n * 0.7)

        crop_x1_n = max(0.0, x_min_n - margin_x)
        crop_y1_n = max(0.0, y_min_n - margin_y)
        crop_x2_n = min(1000.0, x_max_n + margin_x)
        crop_y2_n = min(1000.0, y_max_n + margin_y)

        crop_x1 = int(round(crop_x1_n / 1000.0 * img_w))
        crop_y1 = int(round(crop_y1_n / 1000.0 * img_h))
        crop_x2 = int(round(crop_x2_n / 1000.0 * img_w))
        crop_y2 = int(round(crop_y2_n / 1000.0 * img_h))

        # Crop boyutunu makul tut (en az 200×200 piksel olsun).
        if crop_x2 - crop_x1 < 200:
            pad = (200 - (crop_x2 - crop_x1)) // 2
            crop_x1 = max(0, crop_x1 - pad)
            crop_x2 = min(img_w, crop_x2 + pad)
        if crop_y2 - crop_y1 < 200:
            pad = (200 - (crop_y2 - crop_y1)) // 2
            crop_y1 = max(0, crop_y1 - pad)
            crop_y2 = min(img_h, crop_y2 + pad)

        crop_w = crop_x2 - crop_x1
        crop_h = crop_y2 - crop_y1
        if crop_w <= 0 or crop_h <= 0:
            return None

        # Crop görselini geçici dosyaya yaz.
        with Image.open(image_path) as im:
            cropped = im.crop((crop_x1, crop_y1, crop_x2, crop_y2)).convert("RGB")

        crop_temp = tempfile.NamedTemporaryFile(
            prefix="jarvis-click-refine-",
            suffix=".png",
            delete=False,
        )
        crop_path = Path(crop_temp.name)
        crop_temp.close()
        cropped.save(crop_path, format="PNG")

        try:
            client = genai.Client(api_key=api_key)
            image_part = _build_image_part(crop_path)
            prompt = (
                "Bu görsel daha büyük bir ekran görüntüsünden kırpılmış bir "
                "bölgedir ve içinde tek bir tıklanması gereken obje vardır.\n"
                f"Tıklanacak obje: {target_text}\n\n"
                "Görselin sol üst köşesi (0,0), sağ alt köşesi (1000,1000) "
                "olacak şekilde tıklamanın YAPILMASI GEREKEN tam merkez "
                "noktasının koordinatını döndür.\n\n"
                "Sadece şu JSON formatında yanıt ver, başka açıklama yazma:\n"
                '{"x": <0-1000>, "y": <0-1000>, "confidence": <0.0-1.0>}\n'
                "Eğer obje görselde net değilse veya emin değilsen "
                'şunu döndür: {"x": -1, "y": -1}.'
            )

            for model_name in VISION_MODELS:
                try:
                    response = client.models.generate_content(
                        model=model_name,
                        contents=[
                            types.Part.from_text(text=prompt),
                            image_part,
                        ],
                        config=types.GenerateContentConfig(temperature=0.0),
                    )
                    text = _extract_response_text(response)
                    if not text:
                        continue
                    data = _parse_click_response(text)
                    rx = float(data.get("x", -1))
                    ry = float(data.get("y", -1))
                    if rx < 0 or ry < 0 or rx > 1000 or ry > 1000:
                        continue
                    # Crop koordinatından orijinal görsel koordinatına
                    # geri map'le.
                    refined_x = crop_x1 + (rx / 1000.0) * crop_w
                    refined_y = crop_y1 + (ry / 1000.0) * crop_h
                    return (refined_x, refined_y)
                except Exception:
                    continue
            return None
        finally:
            try:
                if crop_path.exists():
                    crop_path.unlink()
            except Exception:
                pass
    except Exception:
        return None


def click_on_screen(
    target: str,
    capture: str = "screen",
    button: str = "left",
    clicks: int = 1,
    confirm: bool = True,
    confidence_threshold: float = 0.45,
) -> str:
    """Bir ekran objesini doğal dille bul ve üzerine tıkla.

    Args:
        target: Tıklanacak objenin doğal dil tarifi
            (örn. ``"sağ üstteki kapat butonu"``).
        capture: ``"screen"`` (tüm ekran) veya ``"active_window"``.
            Aktif pencere modunda Gemini'ye yalnızca o pencerenin
            görüntüsü gönderilir; bu hassasiyeti arttırır.
        button: ``"left"`` | ``"right"`` | ``"middle"``.
        clicks: 1 (tek tık) veya 2 (çift tık).
        confirm: ``True`` ise Gemini güveni eşiğin altındaysa tıklamaz,
            sadece bulduğu konumu raporlar.
        confidence_threshold: 0-1 arası eşik. Varsayılan 0.45.

    Returns:
        Sesle okunan kısa Türkçe sonuç metni.
    """
    target_text = str(target or "").strip()
    if not target_text:
        return "Tıklanacak objeyi belirtmen gerek."

    capture_mode = (capture or "screen").strip().lower()
    if capture_mode not in {"screen", "active_window"}:
        capture_mode = "screen"

    btn = (button or "left").strip().lower()
    if btn not in {"left", "right", "middle"}:
        btn = "left"

    try:
        click_count = int(clicks or 1)
    except (TypeError, ValueError):
        click_count = 1
    click_count = max(1, min(click_count, 2))

    try:
        threshold = float(confidence_threshold)
    except (TypeError, ValueError):
        threshold = 0.45
    threshold = max(0.0, min(threshold, 1.0))

    # 1) Görüntüyü yakala
    anchor_hwnd = 0
    if capture_mode == "active_window":
        ok, raw, capture_payload = _capture_active_window()
        if not ok or not capture_payload:
            return f"Aktif pencere yakalanamadı: {raw or 'bilinmeyen hata'}"
        image_path = Path(capture_payload["image_path"])
        bounds = capture_payload.get("bounds") or {}
        try:
            anchor_hwnd = int(capture_payload.get("hwnd", 0) or 0)
        except Exception:
            anchor_hwnd = 0
    else:
        # Tüm sanal ekranı yakala (multi-monitor + DPI tutarlı).
        ok, raw, vscreen = _capture_full_virtual_screen()
        if not ok or not vscreen:
            return f"Ekran görüntüsü alınamadı: {raw or 'bilinmeyen hata'}"
        image_path = Path(vscreen["image_path"])
        # Bbox offset'i sanal ekranın left/top'undan gelir; primary
        # monitör solda değilse left negatif olabilir.
        bounds = vscreen.get("bounds") or {}

    try:
        if not image_path.exists() or image_path.stat().st_size <= 0:
            return "Ekran görüntüsü dosyası boş geldi, tıklama yapılamadı."

        try:
            with Image.open(image_path) as im:
                img_w, img_h = im.size
        except Exception as exc:
            return f"Ekran görüntüsü okunamadı: {exc}"

        # 2) Gemini'den koordinat al
        try:
            click_payload = _ask_gemini_for_click_location(target_text, image_path)
        except Exception as exc:
            return f"Hedef bulunamadı: {exc}"

        if not click_payload.get("found"):
            reason = str(
                click_payload.get("reason", "") or "Obje görüntüde net görünmüyor."
            )
            cands = click_payload.get("candidates") or []
            extra = ""
            if isinstance(cands, list) and cands:
                names = [
                    str((c or {}).get("label", "")).strip()
                    for c in cands[:3]
                    if isinstance(c, dict)
                ]
                names = [n for n in names if n]
                if names:
                    extra = " Olası adaylar: " + ", ".join(names) + "."
            return f"Hedefi bulamadım. {reason}{extra}"

        bbox = click_payload.get("bbox")
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            return "Model bbox bilgisini bekleneneden farklı döndürdü; tıklamadım."

        try:
            confidence = float(click_payload.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        label = str(click_payload.get("label", target_text) or target_text).strip()

        try:
            cx, cy, _ = _normalized_bbox_to_pixel(
                bbox, img_w, img_h, bounds
            )
        except Exception as exc:
            return f"Koordinat hesaplanamadı: {exc}"

        # Gemini'den doğrudan tıklama noktası geldiyse merkeze göre onu tercih et.
        norm_click_point = _coerce_normalized_click_point(click_payload)
        if norm_click_point is not None:
            try:
                cx, cy = _normalized_point_to_pixel(
                    norm_click_point, img_w, img_h, bounds
                )
            except Exception:
                pass

        # 2.5) Refine: ilk bbox kaba olabilir; etrafında crop alıp
        # Gemini'ye yeniden sor ve daha hassas bir merkez koordinatı al.
        # Refine başarısız olursa ilk tahmin kullanılır.
        refined = _refine_click_location(
            target_text, image_path, bbox, img_w, img_h
        )
        if refined is not None:
            refined_x_img, refined_y_img = refined
            # Crop sonucunu global koordinata offset'le.
            offset_x = int((bounds or {}).get("left", 0) or 0)
            offset_y = int((bounds or {}).get("top", 0) or 0)
            refined_cx = int(round(refined_x_img + offset_x))
            refined_cy = int(round(refined_y_img + offset_y))
            # İlk tahminden mantıklı bir mesafede ise refine sonucunu kullan.
            # Aşırı sıçrama (ör. 400 piksel) muhtemelen yanlış obje
            # bulundu demektir; ilk tahmini koruyalım.
            if abs(refined_cx - cx) <= 220 and abs(refined_cy - cy) <= 220:
                cx, cy = refined_cx, refined_cy

        # Güven kontrolü
        if confirm and confidence < threshold:
            return (
                f"Hedefi yaklaşık olarak '{label}' diye buldum ({cx},{cy} "
                f"konumunda) ama güvenim düşük (%{int(confidence * 100)}). "
                "Daha net bir tarif verirsen tıklayabilirim."
            )

        # 3) Tıkla
        try:
            if capture_mode == "active_window":
                live_snapshot = _get_foreground_window_snapshot()
                if live_snapshot:
                    if anchor_hwnd > 0:
                        live_hwnd = int(live_snapshot.get("hwnd", 0) or 0)
                        if live_hwnd > 0 and live_hwnd != anchor_hwnd:
                            return (
                                "Aktif pencere değiştiği için yanlış yere "
                                "tıklamamak adına işlemi durdurdum."
                            )
                    shifted = _reproject_point_between_bounds(
                        cx, cy, bounds, live_snapshot
                    )
                    if shifted is not None:
                        cx, cy = shifted

            v_left, v_top, v_right, v_bottom = _virtual_screen_bounds()
            if not (v_left <= cx < v_right and v_top <= cy < v_bottom):
                return (
                    f"Hesaplanan konum ({cx},{cy}) ekran sınırları "
                    f"({v_left},{v_top})-({v_right},{v_bottom}) dışında; "
                    "tıklamadım."
                )
            _execute_stable_click(cx, cy, btn, click_count)
        except Exception as exc:
            return f"Tıklama gönderilemedi: {exc}"

        action_word = "çift tıkladım" if click_count == 2 else "tıkladım"
        button_word = {"left": "sol", "right": "sağ", "middle": "orta"}.get(btn, btn)
        return (
            f"'{label}' üzerine {button_word} tuşla {action_word} "
            f"({cx},{cy} — güven %{int(confidence * 100)})."
        )
    finally:
        try:
            if image_path and image_path.exists():
                image_path.unlink()
        except Exception:
            pass


click_on_screen.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "click_on_screen",
        "description": (
            "Ekrandaki bir objeyi dogal dilde tarif edip uzerine tiklar. "
            "Gemini vision ile ekran goruntusunden objeyi bulur, koordinat "
            "hesaplar ve pyautogui ile tiklamayi gonderir. "
            "Kullanici 'su butona bas', 'X i tikla', 'kapat tusuna bas', "
            "'arama kutusuna gec' gibi GUI etkilesimi istediginde kullan. "
            "Eger sadece guvenli bir konum bulmak istiyorsan confirm=true "
            "birak; guven dusukse arac tiklamadan konumu raporlar."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {
                    "type": "STRING",
                    "description": (
                        "Tiklanacak objenin dogal dil tarifi. Ornek: "
                        "'sag ust kosedeki kapat butonu', 'Spotify deki "
                        "yesil oynat butonu', 'Sayfada Gonder yazan mavi tus'."
                    ),
                },
                "capture": {
                    "type": "STRING",
                    "description": (
                        "screen | active_window. active_window secilirse "
                        "yalnizca aktif pencere goruntulenir; bu hassasiyeti "
                        "arttirir."
                    ),
                },
                "button": {
                    "type": "STRING",
                    "description": "left | right | middle (varsayilan left).",
                },
                "clicks": {
                    "type": "NUMBER",
                    "description": "1 (tek tik) veya 2 (cift tik).",
                },
                "confirm": {
                    "type": "BOOLEAN",
                    "description": (
                        "true ise dusuk guven durumunda tiklamadan konum "
                        "raporlanir."
                    ),
                },
                "confidence_threshold": {
                    "type": "NUMBER",
                    "description": (
                        "0-1 arasi guven esigi. Varsayilan 0.45."
                    ),
                },
            },
            "required": ["target"],
        },
    },
    "execution_mode": "inline",
}


__all__ = [
    "analyze_screen",
    "video_object_detect",
    "audio_to_table",
    "nvidia_text_task",
    "nvidia_image_analyze",
    "click_on_screen",
]
