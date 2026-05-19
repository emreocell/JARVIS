"""Audio_Structured skill tool implementations.

İçerdiği handler'lar:

- :func:`meeting_to_actions` — Ses kaydını transkribe eder ve reasoning
  modeli ile ``{participants, action_items}`` JSON yapısına çevirir.
  ``background`` modda çalışır.
- :func:`call_to_crm` — Telefon görüşmesi kaydını transkribe eder ve
  ``{customer, intent, next_step, summary}`` CRM şemasına çevirir.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — dosya yolunu kontrol eder, ses süresini
   ölçer, 60 dk üzeri kayıtları 10 dk parçalara böler.
2. **Model çağrısı** — ``speech_recognition`` ile transkripsiyon (3x
   exponential backoff), ardından NVIDIA reasoning modeli ile JSON üretimi.
3. **Türkçe yanıt formatlama** — JSON çıktısını Türkçe tek paragraflık
   özete dönüştürür; Privacy_Mode kapalıysa ``logs/audio_structured/``
   dizinine kalıcı yazar.

Mevcut ``skills/vision/audio_to_table`` tool'u ve
``actions/nvidia_tools.py`` shim'i **bozulmaz** (Req 11.1).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Reasoning modeli — design.md § Audio_Structured_Skill
REASONING_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

# Transkripsiyon için exponential backoff gecikmeleri (saniye)
_BACKOFF_DELAYS = (1.0, 2.0, 4.0)

# 60 dakika eşiği (saniye)
_LONG_AUDIO_THRESHOLD_SEC = 60 * 60

# Parça uzunluğu (dakika)
_CHUNK_MINUTES = 10


# ---------------------------------------------------------------------------
# Yardımcı: API anahtarları
# ---------------------------------------------------------------------------


def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


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
# Yardımcı: Ses dosyası süresi
# ---------------------------------------------------------------------------


def _get_audio_duration_sec(audio_path: Path) -> float:
    """Ses dosyasının süresini saniye cinsinden döndür.

    ``wave`` modülü ile WAV dosyaları için kesin süre hesaplanır.
    Diğer formatlar için ``speech_recognition`` AudioFile kullanılır.
    Hata durumunda 0.0 döner.
    """
    try:
        import wave
        if audio_path.suffix.lower() == ".wav":
            with wave.open(str(audio_path), "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate()
                if rate > 0:
                    return frames / float(rate)
    except Exception:
        pass

    try:
        import speech_recognition as sr
        with sr.AudioFile(str(audio_path)) as source:
            return float(source.DURATION or 0.0)
    except Exception:
        pass

    return 0.0


# ---------------------------------------------------------------------------
# Yardımcı: Ses dosyasını parçalara böl ve transkribe et
# ---------------------------------------------------------------------------


def _transcribe_segment(
    audio_path: Path,
    start_sec: float,
    end_sec: float,
    language: str = "tr-TR",
) -> str:
    """Ses dosyasının belirli bir segmentini transkribe et.

    ``speech_recognition`` ile Google STT kullanılır. Segment için
    geçici bir WAV dosyası oluşturulur.
    """
    import speech_recognition as sr

    recognizer = sr.Recognizer()

    # Tüm dosyayı yükle ve segmenti kes
    with sr.AudioFile(str(audio_path)) as source:
        # offset ve duration ile sadece ilgili segmenti oku
        duration = end_sec - start_sec
        audio = recognizer.record(source, offset=start_sec, duration=duration)

    return recognizer.recognize_google(audio, language=language).strip()


def _transcribe_with_backoff(
    audio_path: Path,
    start_sec: float,
    end_sec: float,
    language: str = "tr-TR",
) -> str:
    """Transkripsiyon denemesi; başarısızlıkta 3x exponential backoff.

    Req 11.8: transkripsiyon başarısız → 3x exponential backoff.
    """
    import speech_recognition as sr

    last_exc: Exception | None = None

    for attempt, delay in enumerate(_BACKOFF_DELAYS, start=1):
        try:
            return _transcribe_segment(audio_path, start_sec, end_sec, language)
        except sr.UnknownValueError:
            # Ses anlaşılamadı — yeniden denemeye gerek yok
            raise RuntimeError(
                "Ses anlaşılamadı. Daha temiz bir kayıt kullanın."
            )
        except sr.RequestError as exc:
            last_exc = exc
            log.warning(
                "Transkripsiyon denemesi %d başarısız: %s. %.1f sn bekleniyor.",
                attempt,
                exc,
                delay,
            )
            if attempt < len(_BACKOFF_DELAYS):
                time.sleep(delay)
        except Exception as exc:
            last_exc = exc
            log.warning(
                "Transkripsiyon denemesi %d başarısız: %s. %.1f sn bekleniyor.",
                attempt,
                exc,
                delay,
            )
            if attempt < len(_BACKOFF_DELAYS):
                time.sleep(delay)

    raise RuntimeError(
        f"Transkripsiyon servisi {len(_BACKOFF_DELAYS)} denemede başarısız oldu: "
        f"{last_exc}"
    )


def _transcribe_full(audio_path: Path, duration_sec: float) -> str:
    """Ses dosyasını tamamen transkribe et.

    60 dk üzeri kayıtlar 10 dk parçalara bölünür, sıralı transkribe edilir
    ve sonuçlar birleştirilir (Req 11.5).
    """
    from skills.audio_structured._internal import chunk_audio

    if duration_sec > _LONG_AUDIO_THRESHOLD_SEC:
        log.info(
            "Ses dosyası %.1f dk — %d dk parçalara bölünüyor.",
            duration_sec / 60,
            _CHUNK_MINUTES,
        )
        chunks = chunk_audio(duration_sec, chunk_minutes=_CHUNK_MINUTES)
    else:
        chunks = [(0.0, duration_sec)]

    parts: list[str] = []
    for i, (start, end) in enumerate(chunks, start=1):
        log.debug("Parça %d/%d transkribe ediliyor (%.1f-%.1f sn).", i, len(chunks), start, end)
        segment_text = _transcribe_with_backoff(audio_path, start, end)
        if segment_text:
            parts.append(segment_text)

    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA reasoning çağrısı
# ---------------------------------------------------------------------------


def _nvidia_chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    temperature: float = 0.1,
    max_tokens: int = 2000,
) -> str:
    """NVIDIA chat completions REST çağrısı; tek metin döndürür."""
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    response = _requests.post(
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
            f"NVIDIA API hatası ({response.status_code}): {detail}"
        )

    payload = response.json()
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError("NVIDIA API boş yanıt döndürdü.")

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
        raise RuntimeError("NVIDIA modeli boş metin döndürdü.")
    return text


def _extract_json_from_text(text: str) -> dict[str, Any]:
    """Model çıktısından JSON nesnesini çıkar."""
    clean = text.strip()
    # Markdown kod bloğu varsa temizle
    if clean.startswith("```"):
        clean = clean.strip("`")
        if clean.lower().startswith("json"):
            clean = clean[4:].strip()
    start = clean.find("{")
    end = clean.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("Model geçerli JSON döndürmedi.")
    return json.loads(clean[start: end + 1])


# ---------------------------------------------------------------------------
# Yardımcı: Log dosyasına yaz
# ---------------------------------------------------------------------------


def _write_log(data: dict[str, Any], tool_name: str) -> None:
    """Yapılandırılmış çıktıyı logs/audio_structured/{timestamp}.json'a yaz.

    Privacy_Mode aktifken yazma yapılmaz (Req 11.7).
    """
    if _privacy_is_active():
        log.debug("Privacy_Mode aktif — %s çıktısı diske yazılmıyor.", tool_name)
        return

    try:
        log_dir = Path("logs") / "audio_structured"
        log_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        log_path = log_dir / f"{timestamp}.json"
        log_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("Audio_Structured çıktısı yazıldı: %s", log_path)
    except Exception as exc:
        log.warning("Log dosyası yazılamadı: %s", exc)


# ---------------------------------------------------------------------------
# meeting_to_actions
# ---------------------------------------------------------------------------


def meeting_to_actions(audio_path: str, language: str = "tr-TR") -> str:
    """Toplantı kaydını katılımcı + aksiyon listesine çevir.

    Ses dosyasını transkribe eder ve NVIDIA reasoning modeli ile
    ``{participants, action_items}`` JSON yapısına dönüştürür (Req 11.2).

    60 dk üzeri kayıtlar 10 dk parçalara bölünür (Req 11.5).
    Transkripsiyon başarısız → 3x exponential backoff (Req 11.8).
    Çıktı ``logs/audio_structured/{timestamp}.json``'a yazılır (Req 11.7).
    Türkçe tek paragraflık özet döner (Req 11.6).
    """
    from skills.audio_structured._internal import build_meeting_payload

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için toplantı analizi "
            "kullanılamıyor."
        )

    path = Path(str(audio_path or "").strip()).expanduser()
    if not path.exists() or not path.is_file():
        return "Ses dosyası bulunamadı. Geçerli bir dosya yolu girin."

    # --- Süre ölç ---
    duration_sec = _get_audio_duration_sec(path)
    if duration_sec <= 0:
        return (
            "Ses dosyasının süresi ölçülemedi. Desteklenen bir format "
            "kullandığınızdan emin olun (WAV önerilir)."
        )

    # --- Transkripsiyon ---
    try:
        transcript = _transcribe_full(path, duration_sec)
    except RuntimeError as exc:
        return (
            f"Transkripsiyon tamamlanamadı: {exc} "
            "Lütfen daha temiz bir kayıt veya farklı bir dosya deneyin."
        )

    if not transcript:
        return (
            "Ses dosyasından metin çıkarılamadı. "
            "Kayıt kalitesini kontrol edin."
        )

    # --- Reasoning modeli ile JSON üret ---
    system_prompt = (
        "Sen bir toplantı analiz asistanısın. "
        "Verilen transkriptten katılımcıları ve aksiyon kalemlerini çıkar. "
        "Yalnızca şu JSON formatında yanıt ver:\n"
        '{"participants": ["Ad Soyad", ...], '
        '"action_items": [{"owner": "Ad veya null", "due": "Tarih veya null"}, ...]}\n'
        "Ek açıklama yazma. Tüm metin Türkçe olsun."
    )

    user_message = f"Toplantı transkripti:\n\n{transcript}"

    try:
        raw_response = _nvidia_chat(
            model=REASONING_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
    except RuntimeError as exc:
        return f"Toplantı analizi tamamlanamadı: {exc}"

    # --- JSON normalize et ---
    try:
        raw_json = _extract_json_from_text(raw_response)
    except (RuntimeError, json.JSONDecodeError):
        raw_json = {}

    payload = build_meeting_payload(raw_json)

    # --- Log dosyasına yaz ---
    log_data: dict[str, Any] = {
        "tool": "meeting_to_actions",
        "audio_path": str(path),
        "duration_sec": duration_sec,
        "transcript": transcript,
        "result": payload,
        "timestamp": datetime.now().isoformat(),
    }
    _write_log(log_data, "meeting_to_actions")

    # --- Türkçe özet üret (Req 11.6) ---
    participants = payload.get("participants", [])
    action_items = payload.get("action_items", [])

    participant_str = (
        ", ".join(participants) if participants else "belirlenemedi"
    )
    action_count = len(action_items)

    summary = (
        f"Toplantı analizi tamamlandı. "
        f"Katılımcılar: {participant_str}. "
        f"Toplam {action_count} aksiyon kalemi tespit edildi."
    )

    if action_items:
        action_lines = []
        for item in action_items[:5]:  # İlk 5 aksiyonu özetle
            owner = item.get("owner") or "Belirsiz"
            due = item.get("due") or "Tarih yok"
            action_lines.append(f"{owner} ({due})")
        summary += " Aksiyonlar: " + "; ".join(action_lines) + "."

    return summary


meeting_to_actions.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "meeting_to_actions",
        "description": (
            "Toplanti ses kaydini transkribe edip katilimci listesi ve "
            "aksiyon kalemlerine donusturur. Kullanici toplanti kaydini "
            "analiz etmek, katilimcilari veya yapilacaklari ogrenmek "
            "istediginde kullan. 60 dk uzeri kayitlar otomatik parcalanir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "audio_path": {
                    "type": "STRING",
                    "description": (
                        "Ses dosyasinin tam yolu. Ornek: "
                        "C:\\\\Users\\\\...\\\\toplanti.wav"
                    ),
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Transkripsiyon dili kodu. Varsayilan: tr-TR. "
                        "Ornek: en-US, tr-TR"
                    ),
                },
            },
            "required": ["audio_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": REASONING_MODEL,
        "fallback": [
            {
                "provider": "nvidia",
                "model": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
            },
        ],
    },
}


# ---------------------------------------------------------------------------
# call_to_crm
# ---------------------------------------------------------------------------


def call_to_crm(audio_path: str, language: str = "tr-TR") -> str:
    """Telefon görüşmesi kaydını CRM girdisine çevir.

    Ses dosyasını transkribe eder ve NVIDIA reasoning modeli ile
    ``{customer, intent, next_step, summary}`` CRM şemasına dönüştürür
    (Req 11.3).

    60 dk üzeri kayıtlar 10 dk parçalara bölünür (Req 11.5).
    Transkripsiyon başarısız → 3x exponential backoff (Req 11.8).
    Çıktı ``logs/audio_structured/{timestamp}.json``'a yazılır (Req 11.7).
    Türkçe tek paragraflık özet döner (Req 11.6).
    """
    from skills.audio_structured._internal import build_crm_payload

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için görüşme analizi "
            "kullanılamıyor."
        )

    path = Path(str(audio_path or "").strip()).expanduser()
    if not path.exists() or not path.is_file():
        return "Ses dosyası bulunamadı. Geçerli bir dosya yolu girin."

    # --- Süre ölç ---
    duration_sec = _get_audio_duration_sec(path)
    if duration_sec <= 0:
        return (
            "Ses dosyasının süresi ölçülemedi. Desteklenen bir format "
            "kullandığınızdan emin olun (WAV önerilir)."
        )

    # --- Transkripsiyon ---
    try:
        transcript = _transcribe_full(path, duration_sec)
    except RuntimeError as exc:
        return (
            f"Transkripsiyon tamamlanamadı: {exc} "
            "Lütfen daha temiz bir kayıt veya farklı bir dosya deneyin."
        )

    if not transcript:
        return (
            "Ses dosyasından metin çıkarılamadı. "
            "Kayıt kalitesini kontrol edin."
        )

    # --- Reasoning modeli ile JSON üret ---
    system_prompt = (
        "Sen bir CRM veri çıkarma asistanısın. "
        "Verilen telefon görüşmesi transkriptinden CRM bilgilerini çıkar. "
        "Yalnızca şu JSON formatında yanıt ver:\n"
        '{"customer": "Müşteri adı", '
        '"intent": "Görüşme amacı", '
        '"next_step": "Sonraki adım", '
        '"summary": "Kısa özet"}\n'
        "Ek açıklama yazma. Tüm metin Türkçe olsun."
    )

    user_message = f"Telefon görüşmesi transkripti:\n\n{transcript}"

    try:
        raw_response = _nvidia_chat(
            model=REASONING_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        )
    except RuntimeError as exc:
        return f"Görüşme analizi tamamlanamadı: {exc}"

    # --- JSON normalize et ---
    try:
        raw_json = _extract_json_from_text(raw_response)
    except (RuntimeError, json.JSONDecodeError):
        raw_json = {}

    payload = build_crm_payload(raw_json)

    # --- Log dosyasına yaz ---
    log_data: dict[str, Any] = {
        "tool": "call_to_crm",
        "audio_path": str(path),
        "duration_sec": duration_sec,
        "transcript": transcript,
        "result": payload,
        "timestamp": datetime.now().isoformat(),
    }
    _write_log(log_data, "call_to_crm")

    # --- Türkçe özet üret (Req 11.6) ---
    customer = payload.get("customer") or "Belirsiz müşteri"
    intent = payload.get("intent") or "belirlenemedi"
    next_step = payload.get("next_step") or "belirtilmedi"
    summary_text = payload.get("summary") or ""

    summary = (
        f"Görüşme analizi tamamlandı. "
        f"Müşteri: {customer}. "
        f"Görüşme amacı: {intent}. "
        f"Sonraki adım: {next_step}."
    )

    if summary_text:
        summary += f" Özet: {summary_text}"

    return summary


call_to_crm.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "call_to_crm",
        "description": (
            "Telefon gorusmesi ses kaydini transkribe edip CRM girdisine "
            "donusturur. Musteri adi, gorusme amaci, sonraki adim ve ozet "
            "bilgilerini cikarir. Kullanici gorusme kaydini CRM'e aktarmak "
            "istediginde kullan. 60 dk uzeri kayitlar otomatik parcalanir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "audio_path": {
                    "type": "STRING",
                    "description": (
                        "Ses dosyasinin tam yolu. Ornek: "
                        "C:\\\\Users\\\\...\\\\gorusme.wav"
                    ),
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Transkripsiyon dili kodu. Varsayilan: tr-TR. "
                        "Ornek: en-US, tr-TR"
                    ),
                },
            },
            "required": ["audio_path"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": REASONING_MODEL,
        "fallback": [
            {
                "provider": "nvidia",
                "model": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
            },
        ],
    },
}


__all__ = ["meeting_to_actions", "call_to_crm"]
