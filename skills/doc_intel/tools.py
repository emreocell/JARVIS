"""Doc_Intel skill tool implementations.

İçerdiği handler'lar:

- :func:`doc_parse` — PDF, fatura veya makbuz görselini ``nvidia/nemotron-parse``
  (fallback: ``nvidia/nemoretriever-parse``) modeli ile yapılandırılmış JSON'a
  çevirir. Başarılı çıktı Privacy_Mode kapalıysa ``logs/doc_intel/{timestamp}.json``
  dosyasına yazılır. ``background`` modda çalışır.

- :func:`chart_read` — Grafik/tablo görselini ``google/deplot`` modeli ile
  tabloya çevirir ve Türkçe açıklamayla döner. ``background`` modda çalışır.

- :func:`screenshot_summarize` — Uzun ekran görüntüsünü ``microsoft/kosmos-2``
  (fallback: ``adept/fuyu-8b``) modeli ile en fazla üç paragrafta Türkçe özetler.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — dosya yolunu kontrol eder, görseli okur,
   gerekirse 4096 px uzun kenara ölçeklendirir, PDF'i görüntüye çevirir.
2. **NVIDIA NIM çağrısı** — ``requests`` ile doğrudan REST çağrısı.
3. **Türkçe yanıt formatlama** — ``_internal`` yardımcıları ile sonucu
   kullanıcı dostu paragrafa çevirir.

Dosya yok/okunamaz → modele istek gönderilmez; Türkçe hata paragrafı döner
(Req 5.6). Görsel >4096 px uzun kenar ise gönderim öncesi resize yapılır
(Req 5.7). Privacy_Mode kapalıysa ``doc_parse`` çıktısı diske yazılır (Req 5.8).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from skills.doc_intel._internal import (
    parse_doc_response,
    pdf_to_image_first_page,
    resize_to_max_long_edge,
    truncate_paragraphs,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

DOC_PARSE_MODEL = "nvidia/nemotron-parse"
DOC_PARSE_FALLBACK_MODEL = "nvidia/nemoretriever-parse"

CHART_READ_MODEL = "google/deplot"

SCREENSHOT_MODEL = "microsoft/kosmos-2"
SCREENSHOT_FALLBACK_MODEL = "adept/fuyu-8b"

MAX_LONG_EDGE = 4096

# Desteklenen görsel uzantıları
_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff", ".tif"}
_PDF_EXTENSION = ".pdf"


# ---------------------------------------------------------------------------
# Yardımcılar
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    """Yapılandırmadan NVIDIA API anahtarını döndür."""
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


def _privacy_is_active() -> bool:
    """Privacy_Mode aktif mi? Wire edilmemişse False döner."""
    try:
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "is_active"):
            return bool(pm.is_active())
    except Exception:
        pass
    return False


def _load_image_bytes(file_path: str) -> tuple[bool, str, bytes | None]:
    """Dosyayı okuyup ham baytlarını döndür.

    PDF ise ilk sayfayı PNG'ye çevirir. Görsel ise doğrudan okur.

    Returns:
        (ok, error_message, image_bytes)
    """
    path = Path(file_path)

    if not path.exists():
        return (
            False,
            f"Dosya bulunamadı: {file_path}",
            None,
        )

    ext = path.suffix.lower()

    if ext == _PDF_EXTENSION:
        try:
            image_bytes = pdf_to_image_first_page(str(path))
            return True, "", image_bytes
        except RuntimeError as exc:
            return False, str(exc), None
        except Exception as exc:
            return False, f"PDF işlenirken hata oluştu: {exc}", None

    if ext in _IMAGE_EXTENSIONS:
        try:
            image_bytes = path.read_bytes()
            return True, "", image_bytes
        except OSError as exc:
            return False, f"Görsel okunamadı: {exc}", None

    # Bilinmeyen uzantı — yine de okumayı dene
    try:
        image_bytes = path.read_bytes()
        return True, "", image_bytes
    except OSError as exc:
        return False, f"Dosya okunamadı: {exc}", None


def _resize_image_if_needed(image_bytes: bytes) -> bytes:
    """Görsel uzun kenarı 4096 px'i aşıyorsa ölçeklendir.

    PIL/Pillow yüklü değilse orijinal baytları döndürür ve uyarı loglar.
    """
    try:
        from PIL import Image  # type: ignore[import]

        img = Image.open(io.BytesIO(image_bytes))
        w, h = img.size

        new_w, new_h = resize_to_max_long_edge(w, h, MAX_LONG_EDGE)

        if (new_w, new_h) == (w, h):
            return image_bytes

        img_resized = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        fmt = img.format or "PNG"
        img_resized.save(buf, format=fmt)
        log.debug(
            "Görsel ölçeklendirildi: %dx%d → %dx%d", w, h, new_w, new_h
        )
        return buf.getvalue()

    except ImportError:
        log.warning(
            "Pillow yüklü değil; görsel ölçeklendirme atlandı. "
            "`pip install pillow` ile yükleyebilirsiniz."
        )
        return image_bytes
    except Exception as exc:
        log.warning("Görsel ölçeklendirme başarısız: %s", exc)
        return image_bytes


def _image_to_data_url(image_bytes: bytes, mime: str = "image/png") -> str:
    """Görsel baytlarını base64 data URL'e çevir."""
    b64 = base64.b64encode(image_bytes).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _call_nvidia_vision(
    model: str,
    system_prompt: str,
    user_text: str,
    image_bytes: bytes,
    timeout: float = 90.0,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> str:
    """NVIDIA NIM vision endpoint'ine istek gönder; ham metin döndür.

    Raises:
        RuntimeError: API anahtarı eksikse, HTTP hatası veya boş yanıt.
    """
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    data_url = _image_to_data_url(image_bytes)

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_text},
                    {
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    },
                ],
            },
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    response = _requests.post(
        NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
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


def _call_nvidia_vision_with_fallback(
    primary_model: str,
    fallback_model: str | None,
    system_prompt: str,
    user_text: str,
    image_bytes: bytes,
    timeout: float = 90.0,
    max_tokens: int = 1024,
    temperature: float = 0.2,
) -> tuple[str, str]:
    """Birincil modeli dene; başarısız olursa fallback'e geç.

    Returns:
        (raw_text, used_model)

    Raises:
        RuntimeError: Her iki model de başarısız olursa.
    """
    try:
        text = _call_nvidia_vision(
            model=primary_model,
            system_prompt=system_prompt,
            user_text=user_text,
            image_bytes=image_bytes,
            timeout=timeout,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return text, primary_model
    except RuntimeError as primary_exc:
        if fallback_model is None:
            raise

        log.warning(
            "Birincil model başarısız (%s): %s. Fallback deneniyor: %s",
            primary_model,
            primary_exc,
            fallback_model,
        )
        try:
            text = _call_nvidia_vision(
                model=fallback_model,
                system_prompt=system_prompt,
                user_text=user_text,
                image_bytes=image_bytes,
                timeout=timeout,
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return text, fallback_model
        except RuntimeError as fallback_exc:
            raise RuntimeError(
                f"Her iki model de başarısız. "
                f"Birincil ({primary_model}): {primary_exc}. "
                f"Fallback ({fallback_model}): {fallback_exc}."
            ) from fallback_exc


def _save_doc_parse_log(data: dict, privacy_active: bool) -> None:
    """doc_parse çıktısını logs/doc_intel/{timestamp}.json dosyasına yaz.

    Privacy_Mode aktifken yazma yapılmaz (Req 5.8).
    """
    if privacy_active:
        return

    try:
        log_dir = Path("logs") / "doc_intel"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = log_dir / f"{timestamp}.json"
        log_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.debug("doc_parse çıktısı kaydedildi: %s", log_path)
    except OSError as exc:
        log.warning("doc_parse log dosyası yazılamadı: %s", exc)


# ---------------------------------------------------------------------------
# Handler: doc_parse
# ---------------------------------------------------------------------------

def doc_parse(file_path: str) -> str:
    """PDF, fatura veya makbuz görselini yapılandırılmış JSON'a çevir.

    ``nvidia/nemotron-parse`` (fallback: ``nvidia/nemoretriever-parse``)
    modeli kullanılır. Başarılı çıktı Privacy_Mode kapalıysa
    ``logs/doc_intel/{timestamp}.json`` dosyasına yazılır (Req 5.8).

    Dosya bulunamazsa veya okunamazsa modele istek gönderilmez (Req 5.6).
    Görsel >4096 px uzun kenar ise gönderim öncesi resize yapılır (Req 5.7).
    """
    privacy_active = _privacy_is_active()
    api_key = _nvidia_api_key()

    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için belge çözümleme özelliği "
            "kullanılamıyor."
        )

    # --- Dosyayı oku ---
    ok, error_msg, image_bytes = _load_image_bytes(file_path)
    if not ok or image_bytes is None:
        log.warning("doc_parse: dosya okunamadı: %s", error_msg)
        return error_msg

    # --- Gerekirse ölçeklendir ---
    image_bytes = _resize_image_if_needed(image_bytes)

    # --- NVIDIA çağrısı ---
    system_prompt = (
        "Sen bir belge analiz asistanısın. "
        "Verilen görüntüdeki belgeyi (fatura, makbuz, form vb.) analiz et. "
        "Aşağıdaki JSON formatında yanıt ver:\n"
        '{"vendor": "...", "total": ..., "currency": "...", '
        '"date": "...", "line_items": [{"description": "...", "amount": ...}]}\n'
        "Alanlar bulunamazsa null kullan. Yalnızca JSON döndür, açıklama ekleme."
    )
    user_text = "Bu belgeyi analiz et ve yapılandırılmış JSON formatında çıktı ver."

    try:
        raw_text, used_model = _call_nvidia_vision_with_fallback(
            primary_model=DOC_PARSE_MODEL,
            fallback_model=DOC_PARSE_FALLBACK_MODEL,
            system_prompt=system_prompt,
            user_text=user_text,
            image_bytes=image_bytes,
            max_tokens=1024,
            temperature=0.1,
        )
    except RuntimeError as exc:
        log.error("doc_parse: NVIDIA çağrısı başarısız: %s", exc)
        return f"Belge çözümlenirken hata oluştu: {exc}"

    # --- Yanıtı parse et ---
    # Model bazen JSON'u markdown kod bloğu içinde döndürebilir; temizle.
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # İlk ve son ``` satırlarını at
        inner = [
            l for l in lines[1:]
            if not l.strip().startswith("```")
        ]
        cleaned = "\n".join(inner).strip()

    try:
        parsed = parse_doc_response(cleaned)
    except ValueError as exc:
        log.warning(
            "doc_parse: yanıt parse edilemedi (%s). Ham metin döndürülüyor.",
            exc,
        )
        return (
            f"Belge analiz edildi ancak yapılandırılmış çıktı üretilemedi. "
            f"Ham yanıt: {raw_text[:500]}"
        )

    # --- Log dosyasına yaz ---
    _save_doc_parse_log(parsed, privacy_active)

    return parsed["summary_tr"]


doc_parse.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "doc_parse",
        "description": (
            "PDF, fatura veya makbuz gorselini yapilandirilmis JSON'a cevirir. "
            "Kullanici 'bu faturayı çözümle', 'makbuzu analiz et', "
            "'PDF'deki bilgileri çıkar' gibi isteklerde kullan. "
            "Satici, toplam tutar, para birimi, tarih ve kalem listesi cikarir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path": {
                    "type": "STRING",
                    "description": (
                        "Analiz edilecek PDF veya gorsel dosyasinin tam yolu. "
                        "Ornek: 'C:/Users/kullanici/Belgeler/fatura.pdf'"
                    ),
                },
            },
            "required": ["file_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/nemotron-parse",
        "fallback": [
            {"provider": "nvidia", "model": "nvidia/nemoretriever-parse"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Handler: chart_read
# ---------------------------------------------------------------------------

def chart_read(file_path: str) -> str:
    """Grafik veya tablo görselini tabloya çevirip Türkçe açıklamayla döndür.

    ``google/deplot`` modeli kullanılır (Req 5.3).

    Dosya bulunamazsa veya okunamazsa modele istek gönderilmez (Req 5.6).
    Görsel >4096 px uzun kenar ise gönderim öncesi resize yapılır (Req 5.7).
    """
    api_key = _nvidia_api_key()

    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için grafik okuma özelliği "
            "kullanılamıyor."
        )

    # --- Dosyayı oku ---
    ok, error_msg, image_bytes = _load_image_bytes(file_path)
    if not ok or image_bytes is None:
        log.warning("chart_read: dosya okunamadı: %s", error_msg)
        return error_msg

    # --- Gerekirse ölçeklendir ---
    image_bytes = _resize_image_if_needed(image_bytes)

    # --- NVIDIA çağrısı ---
    system_prompt = (
        "Sen bir grafik ve tablo analiz asistanısın. "
        "Verilen görseldeki grafik veya tabloyu analiz et. "
        "Önce veriyi tablo formatında (başlıklar ve satırlar) sun, "
        "ardından Türkçe olarak kısa bir açıklama yaz. "
        "Yanıtın iki bölümden oluşsun: 'TABLO:' ve 'AÇIKLAMA:' başlıklarıyla."
    )
    user_text = (
        "Bu grafik veya tabloyu analiz et. "
        "Önce veriyi tablo olarak göster, sonra Türkçe açıkla."
    )

    try:
        raw_text, used_model = _call_nvidia_vision_with_fallback(
            primary_model=CHART_READ_MODEL,
            fallback_model=None,
            system_prompt=system_prompt,
            user_text=user_text,
            image_bytes=image_bytes,
            max_tokens=1024,
            temperature=0.2,
        )
    except RuntimeError as exc:
        log.error("chart_read: NVIDIA çağrısı başarısız: %s", exc)
        return f"Grafik okunurken hata oluştu: {exc}"

    # --- Yanıtı formatla ---
    text = raw_text.strip()
    if not text:
        return "Grafik analiz edildi ancak içerik çıkarılamadı."

    # Yanıt zaten Türkçe açıklama içeriyorsa olduğu gibi döndür
    return text


chart_read.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "chart_read",
        "description": (
            "Grafik veya tablo gorselini analiz eder, veriyi tablo formatinda "
            "cikarir ve Turkce aciklama ekler. "
            "Kullanici 'bu grafigi oku', 'tablodaki verileri cıkar', "
            "'grafigi analiz et' gibi isteklerde kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path": {
                    "type": "STRING",
                    "description": (
                        "Analiz edilecek grafik veya tablo gorselinin tam yolu. "
                        "Ornek: 'C:/Users/kullanici/Belgeler/grafik.png'"
                    ),
                },
            },
            "required": ["file_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "google/deplot",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Handler: screenshot_summarize
# ---------------------------------------------------------------------------

def screenshot_summarize(file_path: str) -> str:
    """Uzun ekran görüntüsünü en fazla üç paragrafta Türkçe özetle.

    ``microsoft/kosmos-2`` (fallback: ``adept/fuyu-8b``) modeli kullanılır
    (Req 5.4). Çıktı ``truncate_paragraphs`` ile üç paragrafla sınırlandırılır.

    Dosya bulunamazsa veya okunamazsa modele istek gönderilmez (Req 5.6).
    Görsel >4096 px uzun kenar ise gönderim öncesi resize yapılır (Req 5.7).
    """
    api_key = _nvidia_api_key()

    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için ekran görüntüsü özetleme "
            "özelliği kullanılamıyor."
        )

    # --- Dosyayı oku ---
    ok, error_msg, image_bytes = _load_image_bytes(file_path)
    if not ok or image_bytes is None:
        log.warning("screenshot_summarize: dosya okunamadı: %s", error_msg)
        return error_msg

    # --- Gerekirse ölçeklendir ---
    image_bytes = _resize_image_if_needed(image_bytes)

    # --- NVIDIA çağrısı ---
    system_prompt = (
        "Sen bir ekran görüntüsü analiz asistanısın. "
        "Verilen ekran görüntüsünün içeriğini Türkçe olarak özetle. "
        "Yanıtın tam olarak üç paragraftan oluşsun:\n"
        "1. Paragraf: Ekranda ne görüldüğünün genel açıklaması.\n"
        "2. Paragraf: Önemli bilgiler, metinler veya arayüz öğeleri.\n"
        "3. Paragraf: Bağlam veya olası kullanım amacı.\n"
        "Her paragraf arasında boş satır bırak."
    )
    user_text = (
        "Bu ekran görüntüsünü Türkçe olarak üç paragrafta özetle."
    )

    try:
        raw_text, used_model = _call_nvidia_vision_with_fallback(
            primary_model=SCREENSHOT_MODEL,
            fallback_model=SCREENSHOT_FALLBACK_MODEL,
            system_prompt=system_prompt,
            user_text=user_text,
            image_bytes=image_bytes,
            max_tokens=768,
            temperature=0.3,
        )
    except RuntimeError as exc:
        log.error("screenshot_summarize: NVIDIA çağrısı başarısız: %s", exc)
        return f"Ekran görüntüsü özetlenirken hata oluştu: {exc}"

    # --- Üç paragrafla sınırla ---
    summary = truncate_paragraphs(raw_text, max_paragraphs=3)

    if not summary:
        return "Ekran görüntüsü analiz edildi ancak özet üretilemedi."

    return summary


screenshot_summarize.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "screenshot_summarize",
        "description": (
            "Uzun ekran goruntusunu en fazla uc paragrafta Turkce ozetler. "
            "Kullanici 'bu ekran goruntusunu ozetle', 'ekrandakileri anlat', "
            "'bu sayfada ne var' gibi isteklerde kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path": {
                    "type": "STRING",
                    "description": (
                        "Ozetlenecek ekran goruntusunun tam yolu. "
                        "Ornek: 'C:/Users/kullanici/Resimler/ekran.png'"
                    ),
                },
            },
            "required": ["file_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "microsoft/kosmos-2",
        "fallback": [
            {"provider": "nvidia", "model": "adept/fuyu-8b"},
        ],
    },
}


__all__ = ["doc_parse", "chart_read", "screenshot_summarize"]
