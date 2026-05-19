"""Creative skill tool implementations.

İçerdiği handler'lar:

- :func:`creative_write` — Blog, sosyal medya veya hikaye taslağı üretir.
  ``writer/palmyra-creative-122b`` modelini kullanır. ``background`` modda
  çalışır.
- :func:`financial_analyze` — Finansal analiz üretir.
  ``writer/palmyra-fin-70b-32k`` modelini kullanır. Çıktının başında
  "Bu yatırım tavsiyesi değildir" Türkçe uyarısı zorunludur (Req 9.3).
  ``background`` modda çalışır.
- :func:`medical_qa` — Sağlık sorusu yanıtlar.
  ``writer/palmyra-med-70b`` modelini kullanır. Çıktının başında
  "Bu profesyonel tıbbi tavsiye yerine geçmez, bir doktora danışın"
  Türkçe uyarısı zorunludur (Req 9.4). ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, boş girdi kontrolü.
2. **Model_Router çağrısı** — NVIDIA NIM endpoint'ine chat isteği gönderir.
3. **Türkçe yanıt formatlama** — ``_internal.format_with_disclaimer`` ile
   yasal uyarıyı başa zorlar.

30 sn timeout: Task_Manager üzerinden iptal sinyali + Türkçe zaman aşımı
paragrafı (Req 9.7). Finansal ve tıbbi uyarılar ``suppress_disclaimer=True``
olsa bile kaldırılmaz; kalıcı yasal gerekliliktir (Req 9.6).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from skills.creative._internal import format_with_disclaimer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

CREATIVE_MODEL = "writer/palmyra-creative-122b"
FINANCIAL_MODEL = "writer/palmyra-fin-70b-32k"
MEDICAL_MODEL = "writer/palmyra-med-70b"

NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# Req 9.7: 30 sn timeout
_TIMEOUT_SEC = 30


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA API anahtarı
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: Task_Manager iptal sinyali
# ---------------------------------------------------------------------------

def _get_cancel_event() -> threading.Event | None:
    """Mevcut Task_Manager görevinin iptal sinyalini döndür.

    Tool_Runtime, handler'ı Task_Manager üzerinden çalıştırırken
    ``cancel_event`` nesnesini thread-local veya context üzerinden
    enjekte edebilir. Burada güvenli fallback olarak ``None`` döneriz;
    gerçek wiring tool_runtime üzerinden yapılır.
    """
    try:
        import main as _main  # type: ignore[import]
        tm = getattr(_main, "task_manager", None)
        if tm is not None:
            # Aktif görevin cancel_event'ini al
            current = getattr(tm, "_current_task", None)
            if current is not None:
                return getattr(current, "cancel_event", None)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA chat çağrısı
# ---------------------------------------------------------------------------

def _call_nvidia_chat(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout_sec: float = _TIMEOUT_SEC,
) -> str:
    """NVIDIA NIM chat endpoint'ine istek gönder; ham metin döndür.

    Raises:
        TimeoutError: İstek ``timeout_sec`` içinde tamamlanmazsa.
        RuntimeError: API hatası veya boş yanıt durumunda.
    """
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0.7,
        "max_tokens": 2048,
    }

    try:
        response = _requests.post(
            NVIDIA_CHAT_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_sec,
        )
    except _requests.exceptions.Timeout:
        raise TimeoutError(
            f"NVIDIA modeli {timeout_sec:.0f} saniye içinde yanıt vermedi."
        )
    except _requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"NVIDIA bağlantı hatası: {exc}")

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
    text = str(content or "").strip()

    if not text:
        raise RuntimeError("NVIDIA modeli boş metin döndürdü.")

    return text


# ---------------------------------------------------------------------------
# Yardımcı: Timeout ile çağrı + iptal sinyali kontrolü
# ---------------------------------------------------------------------------

def _call_with_timeout(
    model: str,
    system_prompt: str,
    user_message: str,
    timeout_msg: str,
) -> str:
    """NVIDIA chat çağrısını timeout ve iptal sinyali ile yönet.

    Req 9.7: 30 sn içinde yanıt gelmezse Task_Manager üzerinden iptal
    sinyali yayar ve Türkçe zaman aşımı paragrafı döner.
    """
    cancel_event = _get_cancel_event()

    result_holder: list[str] = []
    error_holder: list[Exception] = []

    def _worker() -> None:
        try:
            text = _call_nvidia_chat(
                model=model,
                system_prompt=system_prompt,
                user_message=user_message,
                timeout_sec=_TIMEOUT_SEC,
            )
            result_holder.append(text)
        except Exception as exc:
            error_holder.append(exc)

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    thread.join(timeout=_TIMEOUT_SEC + 2)  # biraz tolerans

    if thread.is_alive():
        # Zaman aşımı: iptal sinyali yay
        if cancel_event is not None:
            cancel_event.set()
        log.warning("creative skill: %s zaman aşımı.", model)
        return timeout_msg

    if error_holder:
        exc = error_holder[0]
        if isinstance(exc, TimeoutError):
            if cancel_event is not None:
                cancel_event.set()
            return timeout_msg
        raise exc

    return result_holder[0] if result_holder else timeout_msg


# ---------------------------------------------------------------------------
# Handler: creative_write
# ---------------------------------------------------------------------------

def creative_write(
    prompt: str,
    format: str = "blog",
    language: str = "tr",
) -> str:
    """Blog, sosyal medya veya hikaye taslağı üret.

    ``writer/palmyra-creative-122b`` modelini kullanır. İstenen formata
    (blog, sosyal medya, hikaye) uygun Türkçe çıktı üretir (Req 9.2).
    ``background`` modda çalışır (Req 9.5).

    Args:
        prompt: İçerik konusu veya taslak isteği.
        format: Çıktı formatı — ``"blog"``, ``"sosyal_medya"`` veya
            ``"hikaye"``. Varsayılan ``"blog"``.
        language: Çıktı dili. Varsayılan ``"tr"`` (Türkçe).

    Returns:
        Üretilen içerik metni.
    """
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için yaratıcı yazım özelliği "
            "kullanılamıyor."
        )

    prompt_clean = (prompt or "").strip()
    if not prompt_clean:
        return "Lütfen bir konu veya içerik isteği belirtin."

    format_clean = (format or "blog").strip().lower()
    _FORMAT_LABELS = {
        "blog": "blog yazısı",
        "sosyal_medya": "sosyal medya gönderisi",
        "hikaye": "kısa hikaye",
    }
    format_label = _FORMAT_LABELS.get(format_clean, format_clean)

    lang_instruction = (
        "Yanıtını Türkçe yaz." if language.lower().startswith("tr")
        else f"Write your response in {language}."
    )

    system_prompt = (
        f"Sen yaratıcı bir içerik yazarısın. "
        f"Kullanıcının isteğine göre {format_label} formatında "
        f"özgün ve akıcı içerik üretirsin. {lang_instruction}"
    )

    timeout_msg = (
        "Yaratıcı içerik üretimi zaman aşımına uğradı. "
        "Lütfen daha kısa bir istek ile tekrar deneyin."
    )

    try:
        raw = _call_with_timeout(
            model=CREATIVE_MODEL,
            system_prompt=system_prompt,
            user_message=prompt_clean,
            timeout_msg=timeout_msg,
        )
    except Exception as exc:
        log.error("creative_write: NVIDIA çağrısı başarısız: %s", exc)
        return f"Yaratıcı içerik üretilemedi: {exc}"

    # Creative için yasal uyarı yok; format_with_disclaimer "creative" ile
    # metni olduğu gibi döndürür.
    return format_with_disclaimer("creative", raw)


creative_write.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "creative_write",
        "description": (
            "Blog, sosyal medya gonderisi veya kisa hikaye taslagi uretir. "
            "Kullanici 'blog yaz', 'sosyal medya icerigi olustur', "
            "'hikaye yaz' gibi isteklerde kullan. "
            "writer/palmyra-creative-122b modeli kullanilir. "
            "Arka planda calisir; sonuc duyurulur."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "prompt": {
                    "type": "STRING",
                    "description": (
                        "Icerik konusu veya taslak istegi. "
                        "Ornek: 'Yapay zeka hakkinda bir blog yazisi yaz', "
                        "'Instagram icin motivasyon gonderisi olustur'."
                    ),
                },
                "format": {
                    "type": "STRING",
                    "description": (
                        "Cikti formati: 'blog', 'sosyal_medya' veya 'hikaye'. "
                        "Varsayilan: 'blog'."
                    ),
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Cikti dili. Varsayilan: 'tr' (Turkce)."
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "writer/palmyra-creative-122b",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Handler: financial_analyze
# ---------------------------------------------------------------------------

def financial_analyze(
    query: str,
    context: str = "",
) -> str:
    """Finansal analiz üret.

    ``writer/palmyra-fin-70b-32k`` modelini kullanır. Çıktının başında
    "Bu yatırım tavsiyesi değildir" Türkçe uyarısı zorunludur (Req 9.3).
    ``suppress_disclaimer=True`` olsa bile uyarı kaldırılmaz (Req 9.6).
    ``background`` modda çalışır (Req 9.5).

    Args:
        query: Finansal analiz sorusu veya isteği.
        context: Ek bağlam (opsiyonel) — şirket adı, dönem, veri vb.

    Returns:
        Yasal uyarı başta olmak üzere finansal analiz metni.
    """
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için finansal analiz özelliği "
            "kullanılamıyor."
        )

    query_clean = (query or "").strip()
    if not query_clean:
        return "Lütfen bir finansal analiz sorusu veya isteği belirtin."

    system_prompt = (
        "Sen deneyimli bir finansal analist asistanısın. "
        "Kullanıcının finansal sorularını ve analiz isteklerini "
        "kapsamlı, nesnel ve Türkçe olarak yanıtlarsın. "
        "Yatırım tavsiyesi vermediğini her zaman belirtirsin."
    )

    user_message = query_clean
    if context.strip():
        user_message = f"Bağlam: {context.strip()}\n\nSoru: {query_clean}"

    timeout_msg = (
        "Finansal analiz zaman aşımına uğradı. "
        "Lütfen daha kısa bir istek ile tekrar deneyin."
    )

    try:
        raw = _call_with_timeout(
            model=FINANCIAL_MODEL,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout_msg=timeout_msg,
        )
    except Exception as exc:
        log.error("financial_analyze: NVIDIA çağrısı başarısız: %s", exc)
        return f"Finansal analiz tamamlanamadı: {exc}"

    # Req 9.3, 9.6: finansal uyarı başa zorlanır; suppress_disclaimer yoksayılır.
    return format_with_disclaimer("financial", raw)


financial_analyze.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "financial_analyze",
        "description": (
            "Finansal analiz uretir. Kullanici 'bu hisseyi analiz et', "
            "'portfoy onerileri', 'ekonomik durum degerlendirmesi' gibi "
            "finansal sorular sorduğunda kullan. "
            "writer/palmyra-fin-70b-32k modeli kullanilir. "
            "Cikti basta 'Bu yatirim tavsiyesi degildir' uyarisi icerir. "
            "Arka planda calisir; sonuc duyurulur."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Finansal analiz sorusu veya istegi. "
                        "Ornek: 'BIST 100 son durumu nedir?', "
                        "'Enflasyon portfoyumu nasil etkiler?'"
                    ),
                },
                "context": {
                    "type": "STRING",
                    "description": (
                        "Ek baglamsal bilgi (opsiyonel): sirket adi, "
                        "donem, veri veya ozel kosullar."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "writer/palmyra-fin-70b-32k",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Handler: medical_qa
# ---------------------------------------------------------------------------

def medical_qa(
    question: str,
    context: str = "",
) -> str:
    """Sağlık sorusu yanıtla.

    ``writer/palmyra-med-70b`` modelini kullanır. Çıktının başında
    "Bu profesyonel tıbbi tavsiye yerine geçmez, bir doktora danışın"
    Türkçe uyarısı zorunludur (Req 9.4). ``suppress_disclaimer=True``
    olsa bile uyarı kaldırılmaz (Req 9.6). ``background`` modda çalışır
    (Req 9.5).

    Args:
        question: Sağlık sorusu.
        context: Ek bağlam (opsiyonel) — semptomlar, yaş, ilaçlar vb.

    Returns:
        Yasal uyarı başta olmak üzere sağlık bilgisi yanıtı.
    """
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için sağlık bilgisi özelliği "
            "kullanılamıyor."
        )

    question_clean = (question or "").strip()
    if not question_clean:
        return "Lütfen bir sağlık sorusu belirtin."

    system_prompt = (
        "Sen tıbbi bilgi asistanısın. "
        "Kullanıcının sağlık sorularını genel tıbbi bilgi çerçevesinde "
        "Türkçe olarak yanıtlarsın. "
        "Profesyonel tıbbi tavsiye vermediğini ve bir doktora danışılması "
        "gerektiğini her zaman vurgularsın."
    )

    user_message = question_clean
    if context.strip():
        user_message = f"Bağlam: {context.strip()}\n\nSoru: {question_clean}"

    timeout_msg = (
        "Sağlık bilgisi sorgusu zaman aşımına uğradı. "
        "Lütfen daha kısa bir soru ile tekrar deneyin."
    )

    try:
        raw = _call_with_timeout(
            model=MEDICAL_MODEL,
            system_prompt=system_prompt,
            user_message=user_message,
            timeout_msg=timeout_msg,
        )
    except Exception as exc:
        log.error("medical_qa: NVIDIA çağrısı başarısız: %s", exc)
        return f"Sağlık bilgisi sorgusu tamamlanamadı: {exc}"

    # Req 9.4, 9.6: tıbbi uyarı başa zorlanır; suppress_disclaimer yoksayılır.
    return format_with_disclaimer("medical", raw)


medical_qa.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "medical_qa",
        "description": (
            "Saglik sorusu yanitlar. Kullanici 'bu belirti ne anlama gelir', "
            "'ilac etkilesimi', 'saglik bilgisi' gibi sorular sorduğunda kullan. "
            "writer/palmyra-med-70b modeli kullanilir. "
            "Cikti basta 'Bu profesyonel tibbi tavsiye yerine gecmez, "
            "bir doktora danisin' uyarisi icerir. "
            "Arka planda calisir; sonuc duyurulur."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": (
                        "Saglik sorusu. "
                        "Ornek: 'Bas agrisi icin ne yapabilirim?', "
                        "'D vitamini eksikligi belirtileri nelerdir?'"
                    ),
                },
                "context": {
                    "type": "STRING",
                    "description": (
                        "Ek baglamsal bilgi (opsiyonel): semptomlar, "
                        "yas, mevcut ilaclar veya ozel kosullar."
                    ),
                },
            },
            "required": ["question"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "writer/palmyra-med-70b",
        "fallback": [],
    },
}


__all__ = ["creative_write", "financial_analyze", "medical_qa"]
