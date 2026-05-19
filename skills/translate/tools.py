"""Translate skill tool implementations.

İçerdiği handler'lar:

- :func:`translate_text` — Verilen metni ``nvidia/riva-translate-4b-instruct-v1.1``
  modeli ile hedef dile çevirir. ``inline`` modda çalışır.
- :func:`translate_screen` — Aktif pencerenin ekran görüntüsünü alır, OCR
  ile metin çıkarır ve ``riva-translate-4b-instruct-v1.1`` ile çevirir.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, Privacy_Mode
   kontrolü yapar, OCR sonucunu doğrular.
2. **Model_Router çağrısı** — ``nvidia/riva-translate-4b-instruct-v1.1``
   modeline chat isteği gönderir.
3. **Türkçe yanıt formatlama** — ``_internal.format_translation_response``
   ile orijinal + çeviri tek paragraflık Türkçe yanıta dönüştürülür.

Privacy_Mode aktifken clipboard kaynaklı çeviriler durdurulur; kullanıcı
doğrudan diktiklerin çevirisi devam eder (Req 7.8).
OCR ekrandan boş metin döndürürse modele istek gönderilmez (Req 7.6).
"""

from __future__ import annotations

import base64
import io
import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

TRANSLATE_MODEL = "nvidia/riva-translate-4b-instruct-v1.1"
NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# OCR için kullanılan Gemini vision modelleri (öncelik sırasıyla)
_VISION_MODELS = (
    "models/gemini-2.0-flash",
    "models/gemini-2.5-flash-lite",
    "models/gemini-2.5-flash",
)


# ---------------------------------------------------------------------------
# Yardımcı: Config erişimi
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


def _gemini_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("gemini_api_key", "") or "").strip()


def _translate_default_target() -> str:
    """config/api_keys.json içindeki translate.default_target değerini oku."""
    from app_config import get_app_config_value
    val = get_app_config_value("translate", {})
    if isinstance(val, dict):
        target = str(val.get("default_target", "") or "").strip()
        if target:
            return target
    return "en"


# ---------------------------------------------------------------------------
# Yardımcı: Privacy_Mode erişimi
# ---------------------------------------------------------------------------

def _privacy_is_active() -> bool:
    """Privacy_Mode aktif mi? main.py'de wire edilmemişse False döner."""
    try:
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "is_active"):
            return bool(pm.is_active())
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA çeviri çağrısı
# ---------------------------------------------------------------------------

def _call_nvidia_translate(
    text: str,
    target_lang: str,
    source_lang_hint: str | None,
) -> str:
    """NVIDIA riva-translate modeline çeviri isteği gönder.

    Returns:
        Çevrilmiş metin (ham model çıktısı).

    Raises:
        RuntimeError: API hatası veya boş yanıt durumunda.
    """
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    # Sistem prompt'u: modele kaynak ve hedef dili bildir
    if source_lang_hint:
        system_content = (
            f"You are a translation assistant. "
            f"Translate the following text from {source_lang_hint} to {target_lang}. "
            f"Output only the translated text, nothing else."
        )
    else:
        system_content = (
            f"You are a translation assistant. "
            f"Detect the source language and translate the following text to {target_lang}. "
            f"Output only the translated text, nothing else."
        )

    payload: dict[str, Any] = {
        "model": TRANSLATE_MODEL,
        "messages": [
            {"role": "system", "content": system_content},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "max_tokens": 2048,
    }

    response = _requests.post(
        NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60,
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
        translated = " ".join(p for p in parts if p).strip()
    else:
        translated = str(content or "").strip()

    if not translated:
        raise RuntimeError("NVIDIA modeli boş çeviri döndürdü.")

    return translated


# ---------------------------------------------------------------------------
# Yardımcı: Ekran görüntüsü alma (Privacy_Mode uyumlu)
# ---------------------------------------------------------------------------

def _capture_screen_bytes(
    privacy_active: bool,
) -> tuple[bool, str, bytes | None, str]:
    """Aktif pencereyi yakala; görüntüyü PNG baytları olarak döndür.

    Privacy_Mode aktifken görüntü diske yazılmaz; yalnızca bellekte tutulur.

    Returns:
        (ok, error_message, image_bytes, window_title)
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

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return True, "", buf.getvalue(), title

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
# Yardımcı: Gemini vision ile OCR (ekrandan metin çıkarma)
# ---------------------------------------------------------------------------

def _ocr_with_gemini(image_bytes: bytes) -> str:
    """Gemini vision modeli ile ekran görüntüsünden metin çıkar.

    Returns:
        Ekrandan çıkarılan metin (boş string = metin bulunamadı).

    Raises:
        RuntimeError: Gemini API hatası durumunda.
    """
    from google import genai
    from google.genai import types

    api_key = _gemini_api_key()
    if not api_key:
        raise RuntimeError("Gemini API anahtarı eksik; OCR yapılamıyor.")

    ocr_prompt = (
        "Bu ekran görüntüsündeki tüm görünür metni çıkar. "
        "Yalnızca metni döndür; başka açıklama ekleme. "
        "Metin yoksa veya okunamıyorsa boş string döndür."
    )

    client = genai.Client(api_key=api_key)
    image_part = types.Part.from_bytes(data=image_bytes, mime_type="image/png")

    last_exc: Exception | None = None
    for model_name in _VISION_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_text(text=ocr_prompt),
                    image_part,
                ],
                config=types.GenerateContentConfig(temperature=0.0),
            )
            text = str(getattr(response, "text", "") or "").strip()
            return text
        except Exception as exc:
            last_exc = exc
            continue

    raise RuntimeError(
        f"Gemini OCR başarısız oldu: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Ana handler: translate_text
# ---------------------------------------------------------------------------

def translate_text(
    text: str,
    target_lang: str = "",
    source_lang: str = "",
    from_clipboard: bool = False,
) -> str:
    """Verilen metni hedef dile çevir (Req 7.1, 7.2, 7.3, 7.4, 7.5, 7.8).

    Args:
        text: Çevrilecek metin.
        target_lang: Hedef dil kodu (ör. "en", "de", "fr"). Boşsa config
            veya varsayılan "en" kullanılır.
        source_lang: Kaynak dil kodu (opsiyonel). Boşsa otomatik tespit.
        from_clipboard: True ise clipboard kaynaklı çağrı; Privacy_Mode
            aktifken bu çağrılar durdurulur (Req 7.8).

    Returns:
        Türkçe tek paragraflık yanıt (orijinal + çeviri).
    """
    from skills.translate._internal import (
        detect_source_lang_hint,
        format_translation_response,
        resolve_target_lang,
    )

    # --- Privacy_Mode kontrolü (Req 7.8) ---
    if from_clipboard and _privacy_is_active():
        return (
            "Gizlilik modu aktif olduğu için pano kaynaklı çeviri "
            "şu an durdurulmuştur. Metni doğrudan dikte ederek çevirebilirsin."
        )

    # --- Girdi doğrulama ---
    clean_text = str(text or "").strip()
    if not clean_text:
        return "Çevrilecek metin boş. Lütfen bir metin gir."

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için çeviri özelliği "
            "kullanılamıyor."
        )

    # --- Dil çözümleme (Req 7.4, 7.5) ---
    config_default = _translate_default_target()
    resolved_target = resolve_target_lang(target_lang, config_default)

    # Kaynak dil: kullanıcı belirtmişse onu kullan, yoksa heuristik
    if source_lang and source_lang.strip():
        src_hint = source_lang.strip().lower()
    else:
        src_hint = detect_source_lang_hint(clean_text)

    # --- NVIDIA çeviri çağrısı (Req 7.2) ---
    try:
        translated = _call_nvidia_translate(
            text=clean_text,
            target_lang=resolved_target,
            source_lang_hint=src_hint,
        )
    except Exception as exc:
        log.error("translate_text: NVIDIA çağrısı başarısız: %s", exc)
        return (
            f"Çeviri isteği tamamlanamadı: {exc} "
            "Lütfen NVIDIA API anahtarını ve bağlantını kontrol et."
        )

    # --- Türkçe yanıt formatlama (Req 7.2, 7.4) ---
    return format_translation_response(
        orig=clean_text,
        translation=translated,
        src_lang=src_hint,
        tgt_lang=resolved_target,
    )


translate_text.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "translate_text",
        "description": (
            "Verilen metni belirtilen dile cevir. Kullanici 'sunu Ingilizceye "
            "cevir', 'bu cumleyi Almancaya cevir', 'translate this to French' "
            "gibi isteklerde kullan. Kaynak dil belirtilmezse otomatik tespit "
            "edilir. Hedef dil belirtilmezse varsayilan dil kullanilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "Cevrilecek metin.",
                },
                "target_lang": {
                    "type": "STRING",
                    "description": (
                        "Hedef dil kodu. Ornek: 'en' (Ingilizce), "
                        "'de' (Almanca), 'fr' (Fransizca), 'tr' (Turkce). "
                        "Belirtilmezse varsayilan dil kullanilir."
                    ),
                },
                "source_lang": {
                    "type": "STRING",
                    "description": (
                        "Kaynak dil kodu (opsiyonel). Belirtilmezse "
                        "otomatik tespit edilir."
                    ),
                },
                "from_clipboard": {
                    "type": "BOOLEAN",
                    "description": (
                        "True ise pano kaynakli cagri; Gizlilik Modu "
                        "aktifken durdurulur."
                    ),
                },
            },
            "required": ["text"],
        },
    },
    "execution_mode": "inline",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/riva-translate-4b-instruct-v1.1",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Ana handler: translate_screen
# ---------------------------------------------------------------------------

def translate_screen(
    target_lang: str = "",
    source_lang: str = "",
) -> str:
    """Aktif pencerenin ekran görüntüsünü al, OCR uygula ve çevir.

    Ekran görüntüsü ``actions/screen_vision.py`` üzerinden alınır.
    OCR için Gemini vision modeli kullanılır. Çeviri için
    ``nvidia/riva-translate-4b-instruct-v1.1`` modeli kullanılır.

    OCR boş metin döndürürse modele istek gönderilmez (Req 7.6).
    Privacy_Mode aktifken ekran görüntüsü diske yazılmaz (Req 7.8).

    Args:
        target_lang: Hedef dil kodu. Boşsa config veya varsayılan "en".
        source_lang: Kaynak dil kodu (opsiyonel). Boşsa otomatik tespit.

    Returns:
        Türkçe tek paragraflık yanıt (orijinal + çeviri) veya hata mesajı.
    """
    from skills.translate._internal import (
        detect_source_lang_hint,
        format_translation_response,
        resolve_target_lang,
    )

    privacy_active = _privacy_is_active()
    api_key = _nvidia_api_key()

    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için ekran çevirisi özelliği "
            "kullanılamıyor."
        )

    # --- Ekran görüntüsü al (Req 7.3) ---
    ok, error_msg, image_bytes, window_title = _capture_screen_bytes(
        privacy_active=privacy_active
    )

    if not ok or image_bytes is None:
        log.warning("translate_screen: ekran görüntüsü alınamadı: %s", error_msg)
        return f"Ekran görüntüsü alınamadı: {error_msg}"

    # --- OCR: Gemini vision ile metin çıkar (Req 7.3) ---
    try:
        ocr_text = _ocr_with_gemini(image_bytes)
    except Exception as exc:
        log.error("translate_screen: OCR başarısız: %s", exc)
        return f"Ekran görüntüsü alındı ancak metin çıkarılamadı: {exc}"

    # --- OCR boş metin kontrolü (Req 7.6) ---
    clean_ocr = ocr_text.strip()
    if not clean_ocr:
        return "Ekranda çevrilebilir metin bulunamadı."

    # --- Dil çözümleme (Req 7.4, 7.5) ---
    config_default = _translate_default_target()
    resolved_target = resolve_target_lang(target_lang, config_default)

    if source_lang and source_lang.strip():
        src_hint = source_lang.strip().lower()
    else:
        src_hint = detect_source_lang_hint(clean_ocr)

    # --- NVIDIA çeviri çağrısı (Req 7.2, 7.3) ---
    try:
        translated = _call_nvidia_translate(
            text=clean_ocr,
            target_lang=resolved_target,
            source_lang_hint=src_hint,
        )
    except Exception as exc:
        log.error("translate_screen: NVIDIA çağrısı başarısız: %s", exc)
        return (
            f"Ekrandan metin çıkarıldı ancak çeviri tamamlanamadı: {exc}"
        )

    # --- Türkçe yanıt formatlama (Req 7.2, 7.4) ---
    result = format_translation_response(
        orig=clean_ocr,
        translation=translated,
        src_lang=src_hint,
        tgt_lang=resolved_target,
    )

    if window_title:
        return f"[Aktif pencere: {window_title}] {result}"
    return result


translate_screen.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "translate_screen",
        "description": (
            "Aktif pencerenin ekran goruntusunu alip OCR ile metin cikarir "
            "ve NVIDIA Riva ile ceviri yapar. Kullanici 'ekrandaki yazıyı "
            "cevir', 'ekranda ne yaziyor cevir', 'translate what is on screen' "
            "gibi isteklerde kullan. OCR bos metin dondurmesi durumunda "
            "kullaniciya bilgi verilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target_lang": {
                    "type": "STRING",
                    "description": (
                        "Hedef dil kodu. Ornek: 'en' (Ingilizce), "
                        "'de' (Almanca), 'fr' (Fransizca), 'tr' (Turkce). "
                        "Belirtilmezse varsayilan dil kullanilir."
                    ),
                },
                "source_lang": {
                    "type": "STRING",
                    "description": (
                        "Kaynak dil kodu (opsiyonel). Belirtilmezse "
                        "otomatik tespit edilir."
                    ),
                },
            },
            "required": [],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/riva-translate-4b-instruct-v1.1",
        "fallback": [],
    },
}


__all__ = ["translate_text", "translate_screen"]
