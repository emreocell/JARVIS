"""Embodied skill tool implementations.

İçerdiği handler'lar:

- :func:`gui_next_action` — Aktif pencerenin ekran görüntüsünü
  ``actions/screen_vision.py`` üzerinden alır ve ``nvidia/cosmos-reason2-8b``
  modeline kullanıcı hedefiyle birlikte gönderir. Model yanıtı tek
  paragraflık Türkçe yönergeye dönüştürülür. Koordinat/bbox varsa
  parantez içinde sonda yer alır; doğrudan tıklama eylemi **yapılmaz**.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — hedef argümanı normalize eder, ekran
   görüntüsünü alır, Privacy_Mode kontrolü yapar.
2. **Model_Router çağrısı** — ``nvidia/cosmos-reason2-8b`` modeline
   vision isteği gönderir.
3. **Türkçe yanıt formatlama** — model çıktısını tek paragraflık
   Türkçe yönergeye dönüştürür; koordinat/bbox varsa sona ekler.

Privacy_Mode aktifken ekran görüntüsü diske yazılmaz, yalnızca bellekte
tutulur (Req 12.7). Ekran görüntüsü alınamazsa modele istek gönderilmez
ve Türkçe hata paragrafı döner (Req 12.6).
"""

from __future__ import annotations

import base64
import io
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

EMBODIED_MODEL = "nvidia/cosmos-reason2-8b"
NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Koordinat/bbox kalıpları — model çıktısında bunları tespit edip sona taşırız.
_COORD_PATTERNS = [
    # (x, y) veya (x1, y1, x2, y2) gibi sayısal çiftler/dörtlüler
    re.compile(r"\(\s*\d+\s*,\s*\d+(?:\s*,\s*\d+\s*,\s*\d+)?\s*\)"),
    # bbox: [x, y, w, h] veya [x1, y1, x2, y2]
    re.compile(r"\[\s*\d+\s*,\s*\d+(?:\s*,\s*\d+\s*,\s*\d+)?\s*\]"),
]


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA API anahtarı
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: Privacy_Mode erişimi
# ---------------------------------------------------------------------------

def _privacy_is_active() -> bool:
    """Privacy_Mode aktif mi? main.py'de wire edilmemişse False döner."""
    try:
        from runtime.privacy_mode import PrivacyMode  # noqa: F401
        # Singleton erişimi: main.py _privacy nesnesini modül düzeyinde
        # paylaşmıyorsa, tool_runtime context'inden alınır. Burada
        # güvenli fallback olarak False döneriz; gerçek wiring
        # tool_runtime üzerinden yapılır.
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "is_active"):
            return bool(pm.is_active())
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Yardımcı: Ekran görüntüsü alma
# ---------------------------------------------------------------------------

def _capture_active_window_bytes(
    privacy_active: bool,
) -> tuple[bool, str, bytes | None, str]:
    """Aktif pencereyi yakala; görüntüyü PNG baytları olarak döndür.

    Returns:
        (ok, error_message, image_bytes, window_title)

    Privacy_Mode aktifken görüntü diske yazılmaz; yalnızca bellekte tutulur
    (Req 12.7). Privacy_Mode kapalıyken de geçici dosya kullanılır ve
    işlem sonunda silinir.
    """
    try:
        import win32gui
        import win32ui
        from ctypes import windll
        from PIL import Image

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return False, "Aktif pencere bulunamadı.", None, ""

        title = win32gui.GetWindowText(hwnd)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top

        if width <= 0 or height <= 0:
            return False, "Pencere boyutları geçersiz.", None, ""

        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = win32ui.CreateDCFromHandle(hwndDC)
        saveDC = mfcDC.CreateCompatibleDC()

        saveBitMap = win32ui.CreateBitmap()
        saveBitMap.CreateCompatibleBitmap(mfcDC, width, height)
        saveDC.SelectObject(saveBitMap)

        result = windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), 2)
        if result == 0:
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

        # PNG baytlarını bellekte üret (Privacy_Mode veya normal akış)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        if not privacy_active:
            # Privacy_Mode kapalı: geçici dosyaya da yazabiliriz (log için)
            # ama burada yalnızca bellekte tutuyoruz; disk yazımı gerekmez.
            pass

        return True, "", image_bytes, title

    except ImportError:
        # win32gui yok: mss ile tam ekran yakala
        return _capture_fullscreen_bytes(privacy_active)
    except Exception as exc:
        return False, f"Aktif pencere yakalanamadı: {exc}", None, ""


def _capture_fullscreen_bytes(
    privacy_active: bool,
) -> tuple[bool, str, bytes | None, str]:
    """Tam ekranı mss ile yakala; PNG baytları döndür."""
    try:
        import mss
        from PIL import Image

        with mss.mss() as sct:
            monitor = sct.monitors[1]
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.rgb)

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return True, "", buf.getvalue(), "Ekran Görüntüsü"

    except Exception as exc:
        return False, f"Ekran görüntüsü alınamadı: {exc}", None, ""


# ---------------------------------------------------------------------------
# Yardımcı: Koordinat/bbox çıkarma
# ---------------------------------------------------------------------------

def _extract_coords(text: str) -> tuple[str, list[str]]:
    """Metinden koordinat/bbox ifadelerini çıkar.

    Returns:
        (temizlenmiş_metin, koordinat_listesi)
    """
    coords: list[str] = []
    cleaned = text

    for pattern in _COORD_PATTERNS:
        matches = pattern.findall(cleaned)
        coords.extend(matches)
        cleaned = pattern.sub("", cleaned)

    # Çift boşlukları temizle
    cleaned = re.sub(r"  +", " ", cleaned).strip()
    return cleaned, coords


# ---------------------------------------------------------------------------
# Yardımcı: Model yanıtını Türkçe yönergeye dönüştür
# ---------------------------------------------------------------------------

def _format_guidance(raw_response: str, window_title: str) -> str:
    """Model yanıtını tek paragraflık Türkçe yönergeye dönüştür.

    Koordinat/bbox varsa parantez içinde sonda yer alır (Req 12.5).
    Doğrudan tıklama eylemi yapılmaz.
    """
    text = raw_response.strip()
    if not text:
        return "Model bir yönerge üretemedi. Lütfen tekrar deneyin."

    # Koordinatları çıkar
    cleaned_text, coords = _extract_coords(text)

    # Metni tek paragrafa indir (satır sonlarını boşluğa çevir)
    paragraph = " ".join(
        line.strip() for line in cleaned_text.splitlines() if line.strip()
    )

    if not paragraph:
        paragraph = "Model bir yönerge üretemedi. Lütfen tekrar deneyin."

    # Koordinatları sona ekle; varsa sondaki noktalama işaretini koru
    if coords:
        coord_str = ", ".join(coords)
        # Paragrafın sonundaki noktalama işaretini geçici olarak kaldır,
        # koordinatları ekle, sonra geri koy.
        trailing_punct = ""
        if paragraph and paragraph[-1] in ".!?":
            trailing_punct = paragraph[-1]
            paragraph = paragraph[:-1].rstrip()
        paragraph = f"{paragraph} ({coord_str}){trailing_punct}"

    return paragraph


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA vision çağrısı
# ---------------------------------------------------------------------------

def _call_nvidia_vision(
    image_bytes: bytes,
    goal: str,
    window_title: str,
) -> str:
    """NVIDIA cosmos-reason2-8b modeline vision isteği gönder."""
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    data_url = f"data:image/png;base64,{image_b64}"

    context_label = window_title or "aktif pencere"
    user_goal = (goal or "Şimdi ne yapmalıyım?").strip()

    system_prompt = (
        "Sen bir GUI agent reasoning asistanısın. "
        "Kullanıcıya ekrandaki arayüzde bir sonraki adımı Türkçe olarak "
        "açıklarsın. Yanıtın tek bir paragraf olmalı. "
        "Koordinat veya bounding box bilgisi varsa parantez içinde ver. "
        "Doğrudan tıklama eylemi yapma; yalnızca yönerge ver."
    )

    user_message = (
        f"Aktif pencere: {context_label}\n"
        f"Hedef: {user_goal}\n\n"
        "Ekran görüntüsüne bakarak bir sonraki adımı Türkçe tek paragrafta açıkla."
    )

    payload: dict[str, Any] = {
        "model": EMBODIED_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_message},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ],
        "temperature": 0.2,
        "max_tokens": 512,
    }

    response = _requests.post(
        NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=90,
    )

    if response.status_code >= 400:
        detail = response.text.strip()[:400]
        raise RuntimeError(
            f"NVIDIA API hatası ({response.status_code}): {detail}"
        )

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("NVIDIA API boş yanıt döndürdü.")

    content = (choices[0] or {}).get("message", {}).get("content", "")
    if isinstance(content, list):
        parts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = " ".join(p for p in parts if p).strip()
    else:
        text = str(content or "").strip()

    if not text:
        raise RuntimeError("NVIDIA modeli boş metin döndürdü.")

    return text


# ---------------------------------------------------------------------------
# Ana handler: gui_next_action
# ---------------------------------------------------------------------------

def gui_next_action(goal: str = "", target: str = "active_window") -> str:
    """Aktif pencerenin ekran görüntüsünü alıp GUI agent reasoning yap.

    Ekran görüntüsü ``actions/screen_vision.py`` üzerinden alınır (geriye
    uyumluluk shim'i). Model yanıtı tek paragraflık Türkçe yönergeye
    dönüştürülür. Koordinat/bbox varsa parantez içinde sonda yer alır;
    doğrudan tıklama eylemi yapılmaz (Req 12.3, 12.5).

    Privacy_Mode aktifken ekran görüntüsü diske yazılmaz (Req 12.7).
    Ekran görüntüsü alınamazsa modele istek gönderilmez (Req 12.6).
    """
    privacy_active = _privacy_is_active()
    api_key = _nvidia_api_key()

    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için GUI rehberlik özelliği "
            "kullanılamıyor."
        )

    # --- Ekran görüntüsü al ---
    ok, error_msg, image_bytes, window_title = _capture_active_window_bytes(
        privacy_active=privacy_active
    )

    if not ok or image_bytes is None:
        # Req 12.6: ekran görüntüsü alınamazsa modele istek gönderme
        log.warning("gui_next_action: ekran görüntüsü alınamadı: %s", error_msg)
        return "Ekran görüntüsü alınamadı."

    # --- NVIDIA vision çağrısı ---
    try:
        raw_response = _call_nvidia_vision(
            image_bytes=image_bytes,
            goal=goal,
            window_title=window_title,
        )
    except Exception as exc:
        log.error("gui_next_action: NVIDIA çağrısı başarısız: %s", exc)
        return (
            f"Ekran görüntüsü alındı ancak GUI analizi tamamlanamadı: {exc}"
        )

    # --- Yanıtı Türkçe yönergeye dönüştür ---
    guidance = _format_guidance(raw_response, window_title)

    if window_title:
        return f"[Aktif pencere: {window_title}] {guidance}"
    return guidance


gui_next_action.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "gui_next_action",
        "description": (
            "Aktif pencerenin ekran goruntusunu alip NVIDIA Cosmos modeli "
            "ile GUI agent reasoning yapar. Kullanici 'simdi ne tiklamaliyim', "
            "'bu arayuzde nasil ilerlerim', 'bir sonraki adim ne' gibi "
            "sorular sorduğunda kullan. Tek paragraflik Turkce yonerge doner; "
            "koordinat varsa parantez icinde verilir. Dogrudan tiklama yapilmaz."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": (
                        "Kullanicinin ulasmak istedigi hedef veya sorusu. "
                        "Ornek: 'Dosyayi kaydetmek istiyorum', "
                        "'Simdi ne yapmaliyim?'"
                    ),
                },
                "target": {
                    "type": "STRING",
                    "description": (
                        "Hedef pencere. Su an yalnizca 'active_window' "
                        "desteklenir."
                    ),
                },
            },
            "required": [],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/cosmos-reason2-8b",
        "fallback": [],
    },
}


__all__ = ["gui_next_action"]
