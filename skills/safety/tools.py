"""Safety skill tool implementations.

İçerdiği handler'lar:

- :func:`pii_mask` — Metindeki PII alanlarını ``nvidia/gliner-pii`` modeli
  ile tespit eder ve ``[PII:tip]`` formatında maskeler. ``inline`` modda
  çalışır; Conversation_Logger ve Clipboard_Manager tarafından da
  ``skills.safety.pii.mask`` sarmalayıcısı üzerinden senkron çağrılır.

- :func:`content_safety_check` — LLM yanıtını ``meta/llama-guard-4-12b``
  veya ``nvidia/llama-3.1-nemoguard-8b-content-safety`` modeli ile
  denetler. ``safety.enforce_content_safety=false`` ise denetim atlanır,
  yalnızca "warn" log üretilir. ``inline`` modda çalışır.

- :func:`topic_control_check` — Kullanıcı sorgusunun ``safety.allowed_topics``
  listesine uygunluğunu ``nvidia/llama-3.1-nemoguard-8b-topic-control``
  modeli ile denetler. ``inline`` modda çalışır.

- :func:`deepfake_detect` — Video dosyasını ``nvidia/ai-synthetic-video-detector``
  modeli ile analiz eder; sentetik olma olasılığını yüzde olarak döner.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, config'i okur,
   Privacy_Mode / enforce_content_safety bayraklarını kontrol eder.
2. **Model_Router çağrısı** — NVIDIA NIM endpoint'ine istek gönderir.
3. **Türkçe yanıt formatlama** — model çıktısını kullanıcı dostu paragrafa
   çevirir.

``pii_mask`` ayrıca ``skills.safety.pii.set_provider`` üzerinden proje-içi
sarmalayıcıyı kayıt eder; böylece Safety_Skill yüklenmediğinde ``pii.mask``
no-op (identity) olarak kalır (Req 8.7).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_PII_MODEL = "nvidia/gliner-pii"
_CONTENT_SAFETY_MODEL = "meta/llama-guard-4-12b"
_CONTENT_SAFETY_FALLBACK_MODEL = "nvidia/llama-3.1-nemoguard-8b-content-safety"
_TOPIC_CONTROL_MODEL = "nvidia/llama-3.1-nemoguard-8b-topic-control"
_DEEPFAKE_MODEL = "nvidia/ai-synthetic-video-detector"

_NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"
_NVIDIA_NIM_BASE = "https://integrate.api.nvidia.com/v1"


# ---------------------------------------------------------------------------
# Yardımcılar: config ve API anahtarı
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    try:
        from app_config import get_app_config_value
        return str(get_app_config_value("nvidia_api_key", "") or "").strip()
    except Exception:
        return ""


def _get_safety_config() -> dict[str, Any]:
    """``config/api_keys.json`` içindeki ``safety`` bloğunu döner."""
    try:
        from app_config import get_app_config_value
        val = get_app_config_value("safety", {})
        return dict(val) if isinstance(val, dict) else {}
    except Exception:
        return {}


def _enforce_content_safety() -> bool:
    """``safety.enforce_content_safety`` config değerini döner (varsayılan True)."""
    cfg = _get_safety_config()
    return bool(cfg.get("enforce_content_safety", True))


def _fail_closed() -> bool:
    """``safety.fail_closed`` config değerini döner (varsayılan False)."""
    cfg = _get_safety_config()
    return bool(cfg.get("fail_closed", False))


def _allowed_topics() -> list[str]:
    """``safety.allowed_topics`` listesini döner (varsayılan boş liste)."""
    cfg = _get_safety_config()
    topics = cfg.get("allowed_topics", [])
    if isinstance(topics, list):
        return [str(t) for t in topics if t]
    return []


# ---------------------------------------------------------------------------
# Yardımcı: Privacy_Mode
# ---------------------------------------------------------------------------

def _privacy_is_active() -> bool:
    try:
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "is_active"):
            return bool(pm.is_active())
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA NIM çağrısı (chat/completions)
# ---------------------------------------------------------------------------

def _nim_chat(
    model: str,
    messages: list[dict],
    *,
    timeout: float = 60.0,
    max_tokens: int = 1024,
    temperature: float = 0.1,
) -> str:
    """NVIDIA NIM chat/completions endpoint'ine istek gönder; metin döner."""
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    resp = _requests.post(
        _NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if resp.status_code >= 400:
        detail = resp.text.strip()[:400]
        raise RuntimeError(f"NVIDIA API hatası ({resp.status_code}): {detail}")

    data = resp.json()
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
        return " ".join(p for p in parts if p).strip()
    return str(content or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: fail_closed kararı
# ---------------------------------------------------------------------------

def _handle_safety_error(exc: Exception, operation: str) -> str:
    """Safety endpoint hatası için fail_closed kararını uygula."""
    from skills.safety._internal import should_fail_closed

    fc = _fail_closed()
    if should_fail_closed(exc, fc):
        log.error("safety/%s: NIM hatası (fail_closed=True): %s", operation, exc)
        return (
            "Güvenlik denetimi tamamlanamadı ve güvenli mod etkin olduğu için "
            "bu istek işlenemiyor."
        )
    else:
        log.warning(
            "safety/%s: NIM hatası (fail_closed=False), geçişe izin veriliyor: %s",
            operation,
            exc,
        )
        return ""  # boş string → çağıran "geçiş" kararı verir


# ---------------------------------------------------------------------------
# Tool 1: pii_mask
# ---------------------------------------------------------------------------

def pii_mask(text: str) -> str:
    """Metindeki PII alanlarını ``nvidia/gliner-pii`` ile tespit edip maskele.

    Her PII alanı ``[PII:tip]`` formatında değiştirilir. Maskeleme
    idempotent ve deterministiktir (Req 8.2).

    ``inline`` modda çalışır; Conversation_Logger ve Clipboard_Manager
    tarafından ``skills.safety.pii.mask`` sarmalayıcısı üzerinden de
    çağrılabilir (Req 8.7).
    """
    from skills.safety._internal import mask_pii

    if not isinstance(text, str) or not text.strip():
        return text if isinstance(text, str) else ""

    api_key = _nvidia_api_key()
    if not api_key:
        log.warning("pii_mask: NVIDIA API anahtarı eksik, maskeleme atlandı.")
        return text

    # --- NIM çağrısı: gliner-pii span listesi al ---
    try:
        system_prompt = (
            "You are a PII detection model. "
            "Given the user text, return a JSON array of PII spans. "
            "Each span must be an object with keys: "
            "\"start\" (int, inclusive char offset), "
            "\"end\" (int, exclusive char offset), "
            "\"label\" (string, PII type such as NAME, EMAIL, PHONE, "
            "ID, ADDRESS, CREDIT_CARD, etc.). "
            "Return ONLY the JSON array, no explanation."
        )
        user_msg = f"Text:\n{text}"

        raw = _nim_chat(
            _PII_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=512,
            temperature=0.0,
        )

        # JSON dizisini ayrıştır
        raw_stripped = raw.strip()
        # Markdown kod bloğu varsa temizle
        if raw_stripped.startswith("```"):
            lines = raw_stripped.splitlines()
            raw_stripped = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()

        spans = json.loads(raw_stripped)
        if not isinstance(spans, list):
            spans = []

    except Exception as exc:
        err_msg = _handle_safety_error(exc, "pii_mask")
        if err_msg:
            return err_msg
        # fail_closed=False → orijinal metni döndür
        return text

    # --- Saf maskeleme ---
    masked = mask_pii(text, spans)
    return masked


pii_mask.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "pii_mask",
        "description": (
            "Metindeki kisisel bilgileri (PII) tespit edip [PII:tip] "
            "formatinda maskeler. Isim, e-posta, telefon, kimlik numarasi, "
            "adres, kredi karti gibi hassas bilgileri gizlemek icin kullan. "
            "Maskeleme idempotent ve deterministiktir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "PII maskelemesi uygulanacak metin.",
                },
            },
            "required": ["text"],
        },
    },
    "execution_mode": "inline",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/gliner-pii",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Tool 2: content_safety_check
# ---------------------------------------------------------------------------

def content_safety_check(text: str) -> str:
    """LLM yanıtını içerik güvenliği açısından denetle.

    ``meta/llama-guard-4-12b`` (birincil) veya
    ``nvidia/llama-3.1-nemoguard-8b-content-safety`` (fallback) modeli
    kullanılır.

    ``safety.enforce_content_safety=false`` ise denetim atlanır ve yalnızca
    "warn" log üretilir (Req 8.9). Denetim "unsafe" döndürürse orijinal
    yanıt engellenir ve Türkçe hata mesajı döner (Req 8.4).

    ``inline`` modda çalışır.
    """
    if not isinstance(text, str) or not text.strip():
        return text if isinstance(text, str) else ""

    # Req 8.9: enforce_content_safety=false ise atla
    if not _enforce_content_safety():
        log.warning(
            "content_safety_check: enforce_content_safety=false, "
            "denetim atlandı."
        )
        return text

    api_key = _nvidia_api_key()
    if not api_key:
        log.warning(
            "content_safety_check: NVIDIA API anahtarı eksik, denetim atlandı."
        )
        return text

    # --- NIM çağrısı ---
    try:
        system_prompt = (
            "You are a content safety classifier. "
            "Analyze the following text and respond with exactly one word: "
            "\"safe\" if the content is appropriate, or \"unsafe\" if it "
            "contains harmful, dangerous, or policy-violating content. "
            "No explanation, just the single word."
        )
        raw = _nim_chat(
            _CONTENT_SAFETY_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=16,
            temperature=0.0,
        )
    except Exception as exc:
        # Birincil model başarısız → fallback dene
        log.warning(
            "content_safety_check: birincil model başarısız (%s), "
            "fallback deneniyor.",
            exc,
        )
        try:
            raw = _nim_chat(
                _CONTENT_SAFETY_FALLBACK_MODEL,
                [
                    {
                        "role": "system",
                        "content": (
                            "You are a content safety classifier. "
                            "Respond with 'safe' or 'unsafe' only."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                max_tokens=16,
                temperature=0.0,
            )
        except Exception as exc2:
            err_msg = _handle_safety_error(exc2, "content_safety_check")
            if err_msg:
                return err_msg
            return text

    verdict = raw.strip().lower()
    if "unsafe" in verdict:
        # Req 8.4: orijinal yanıtı engelle
        log.warning("content_safety_check: içerik 'unsafe' olarak işaretlendi.")
        return (
            "Bu istek güvenlik politikası dışı, gerçekleştiremiyorum."
        )

    return text


content_safety_check.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "content_safety_check",
        "description": (
            "Bir metni icerik guvenligi acisindan denetler. "
            "LLM yaniti kullaniciya iletilmeden once zararli, tehlikeli "
            "veya politika disi icerik icerip icermedigini kontrol etmek "
            "icin kullan. 'unsafe' ise orijinal yaniti engeller ve Turkce "
            "hata mesaji doner."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "Denetlenecek metin veya LLM yaniti.",
                },
            },
            "required": ["text"],
        },
    },
    "execution_mode": "inline",
    "route": {
        "provider": "nvidia",
        "model": "meta/llama-guard-4-12b",
        "fallback": [
            {
                "provider": "nvidia",
                "model": "nvidia/llama-3.1-nemoguard-8b-content-safety",
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# Tool 3: topic_control_check
# ---------------------------------------------------------------------------

def topic_control_check(text: str) -> str:
    """Kullanıcı sorgusunun izin verilen konular listesine uygunluğunu denetle.

    ``nvidia/llama-3.1-nemoguard-8b-topic-control`` modeli kullanılır.
    İzin verilen konular ``config/api_keys.json`` içindeki
    ``safety.allowed_topics`` listesinden okunur (Req 8.5).

    Liste boşsa denetim atlanır ve metin olduğu gibi döner.
    ``inline`` modda çalışır.
    """
    if not isinstance(text, str) or not text.strip():
        return text if isinstance(text, str) else ""

    topics = _allowed_topics()
    if not topics:
        # Konu kısıtı tanımlanmamış → denetim atla
        return text

    api_key = _nvidia_api_key()
    if not api_key:
        log.warning(
            "topic_control_check: NVIDIA API anahtarı eksik, denetim atlandı."
        )
        return text

    topics_str = ", ".join(f'"{t}"' for t in topics)

    try:
        system_prompt = (
            f"You are a topic control classifier. "
            f"The allowed topics are: [{topics_str}]. "
            "Analyze the user message and respond with exactly one word: "
            "\"allowed\" if the message is within the allowed topics, or "
            "\"rejected\" if it is outside the allowed topics. "
            "No explanation, just the single word."
        )
        raw = _nim_chat(
            _TOPIC_CONTROL_MODEL,
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": text},
            ],
            max_tokens=16,
            temperature=0.0,
        )
    except Exception as exc:
        err_msg = _handle_safety_error(exc, "topic_control_check")
        if err_msg:
            return err_msg
        return text

    verdict = raw.strip().lower()
    if "rejected" in verdict or "not allowed" in verdict or "outside" in verdict:
        log.warning(
            "topic_control_check: konu dışı sorgu tespit edildi: %r", text[:80]
        )
        allowed_display = ", ".join(topics[:5])
        if len(topics) > 5:
            allowed_display += f" ve {len(topics) - 5} konu daha"
        return (
            f"Bu konu izin verilen konular dışında. "
            f"İzin verilen konular: {allowed_display}."
        )

    return text


topic_control_check.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "topic_control_check",
        "description": (
            "Kullanici sorgusunun izin verilen konular listesine uygun olup "
            "olmadigini denetler. 'safety.allowed_topics' config alani "
            "bos ise denetim atlanir. Konu disi ise Turkce red mesaji doner."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "text": {
                    "type": "STRING",
                    "description": "Konu denetimi yapilacak kullanici sorgusu.",
                },
            },
            "required": ["text"],
        },
    },
    "execution_mode": "inline",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/llama-3.1-nemoguard-8b-topic-control",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Tool 4: deepfake_detect
# ---------------------------------------------------------------------------

def deepfake_detect(video_path: str) -> str:
    """Video dosyasını deepfake/sentetik içerik açısından analiz et.

    ``nvidia/ai-synthetic-video-detector`` modeli kullanılır. Sentetik
    olma olasılığı yüzde olarak döner (Req 8.6).

    ``background`` modda çalışır.
    """
    if not isinstance(video_path, str) or not video_path.strip():
        return "Geçerli bir video dosyası yolu belirtilmedi."

    path = Path(video_path.strip())
    if not path.exists():
        return f"Video dosyası bulunamadı: {video_path}"
    if not path.is_file():
        return f"Belirtilen yol bir dosya değil: {video_path}"

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için deepfake tespiti "
            "kullanılamıyor."
        )

    # --- NIM çağrısı ---
    # ai-synthetic-video-detector modeli video dosyasını base64 olarak alır.
    try:
        import base64
        import requests as _requests

        with open(path, "rb") as f:
            video_bytes = f.read()

        video_b64 = base64.b64encode(video_bytes).decode("ascii")
        ext = path.suffix.lower().lstrip(".")
        mime = f"video/{ext}" if ext else "video/mp4"
        data_url = f"data:{mime};base64,{video_b64}"

        system_prompt = (
            "You are a synthetic video detection model. "
            "Analyze the provided video and return a JSON object with a single "
            "key 'synthetic_probability' whose value is a float between 0.0 "
            "(definitely real) and 1.0 (definitely synthetic/deepfake). "
            "Return ONLY the JSON object."
        )
        user_msg = (
            f"Analyze this video for synthetic/deepfake content.\n"
            f"Video: {data_url[:200]}..."  # URL'yi kısalt; model zaten data'yı alır
        )

        # Gerçek video verisi için ayrı bir payload yapısı kullan
        payload: dict[str, Any] = {
            "model": _DEEPFAKE_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Analyze this video for synthetic/deepfake content."},
                        {
                            "type": "video_url",
                            "video_url": {"url": data_url},
                        },
                    ],
                },
            ],
            "temperature": 0.0,
            "max_tokens": 128,
        }

        resp = _requests.post(
            _NVIDIA_CHAT_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )

        if resp.status_code >= 400:
            detail = resp.text.strip()[:400]
            raise RuntimeError(
                f"NVIDIA API hatası ({resp.status_code}): {detail}"
            )

        data = resp.json()
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
            raw = " ".join(p for p in parts if p).strip()
        else:
            raw = str(content or "").strip()

    except Exception as exc:
        err_msg = _handle_safety_error(exc, "deepfake_detect")
        if err_msg:
            return err_msg
        return (
            f"Deepfake tespiti sırasında bir hata oluştu: {exc}"
        )

    # --- Yanıtı ayrıştır ---
    try:
        raw_stripped = raw.strip()
        if raw_stripped.startswith("```"):
            lines = raw_stripped.splitlines()
            raw_stripped = "\n".join(
                l for l in lines if not l.startswith("```")
            ).strip()

        result = json.loads(raw_stripped)
        prob = float(result.get("synthetic_probability", 0.0))
        prob = max(0.0, min(1.0, prob))
        percent = round(prob * 100, 1)

        if percent >= 70:
            verdict = "Yüksek ihtimalle sentetik/deepfake"
        elif percent >= 40:
            verdict = "Orta ihtimalle sentetik/deepfake"
        else:
            verdict = "Büyük olasılıkla gerçek"

        return (
            f"Deepfake analizi tamamlandı. "
            f"Sentetik olma olasılığı: %{percent}. "
            f"Değerlendirme: {verdict}. "
            f"Dosya: {path.name}"
        )

    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Model ham metin döndürdüyse olduğu gibi sun
        return (
            f"Deepfake analizi tamamlandı. "
            f"Model yanıtı: {raw[:300]}. "
            f"Dosya: {path.name}"
        )


deepfake_detect.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "deepfake_detect",
        "description": (
            "Bir video dosyasini deepfake veya sentetik icerik acisindan "
            "analiz eder. Sentetik olma olasiligi yuzde olarak doner. "
            "Kullanici 'bu video gercek mi', 'deepfake mi', 'yapay mi' "
            "gibi sorular sorduğunda kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "video_path": {
                    "type": "STRING",
                    "description": (
                        "Analiz edilecek video dosyasinin tam yolu. "
                        "Ornek: 'C:/Users/kullanici/Videos/ornek.mp4'"
                    ),
                },
            },
            "required": ["video_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/ai-synthetic-video-detector",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Proje-içi pii.mask sarmalayıcısını kayıt et
# ---------------------------------------------------------------------------

def _register_pii_provider() -> None:
    """``skills.safety.pii`` sarmalayıcısına bu skill'in provider'ını kayıt et.

    Plugin_Host skill'i yüklediğinde bu fonksiyon çağrılır; böylece
    ``runtime.conversation_logger`` ve ``runtime.clipboard`` modülleri
    ``safety.pii.mask(text)`` üzerinden PII maskelemesini kullanabilir.
    Safety_Skill yüklenmediğinde ``pii.mask`` no-op (identity) olarak kalır.
    """
    try:
        from skills.safety import pii as _pii

        def _provider(text: str) -> str:
            return pii_mask(text)

        _pii.set_provider(_provider)
        log.debug("safety: pii.mask provider kayıt edildi.")
    except Exception as exc:
        log.warning("safety: pii.mask provider kayıt edilemedi: %s", exc)


# Modül yüklendiğinde otomatik kayıt
_register_pii_provider()


__all__ = [
    "pii_mask",
    "content_safety_check",
    "topic_control_check",
    "deepfake_detect",
]
