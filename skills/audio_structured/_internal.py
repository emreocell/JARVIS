"""Audio_Structured_Skill — saf yardımcılar.

Bu modül :mod:`skills.audio_structured.tools`'un I/O ve HTTP'siz çekirdek
mantığını taşır. Tüm fonksiyonlar yan etkisiz, deterministik ve idempotenttir;
böylece :func:`chunk_audio` ve payload normalize ediciler doğrudan
Hypothesis ile property-based test edilebilir (bkz. tasks.md görev 19.1).

Tasarım kararları:

* :func:`chunk_audio` *yalnızca* matematiksel parçalamadır; gerçek ses
  okuma / kesme :mod:`skills.audio_structured.tools` katmanına aittir.
  Çıktı, ``[0, duration_sec]`` aralığını ardışık ve örtüşmesiz biçimde
  kaplar; son parça ``chunk_minutes``'tan daha kısa olabilir
  (Req 11.5).
* :func:`build_meeting_payload` ve :func:`build_crm_payload`,
  Reasoning modeli'nin ürettiği ham yanıtı (JSON metin veya parse
  edilmiş ``dict``) sözleşmedeki şemaya çeker. Eksik alanlar varsayılan
  değerlerle doldurulur, bilinmeyen alanlar düşürülür, tip uyumsuzlukları
  güvenli biçimde normalize edilir. Bu sayede skill'in dış dünyaya
  döndürdüğü JSON sözleşmesi (Req 11.2, 11.3) NIM modelinin sözleşme
  dışı çıktısından bağımsız olarak garantilidir.

Tüm fonksiyonlar Python standart kütüphanesi dışında bağımlılığa sahip
değildir; bu sayede test ortamı ek paket gerektirmeden saf-fonksiyon
property'lerini koşturabilir.
"""

from __future__ import annotations

import json
from typing import Any


__all__ = [
    "chunk_audio",
    "build_meeting_payload",
    "build_crm_payload",
]


# --------------------------------------------------------------------------- #
# Audio chunking                                                              #
# --------------------------------------------------------------------------- #


def chunk_audio(
    duration_sec: float,
    chunk_minutes: int = 10,
) -> list[tuple[float, float]]:
    """``duration_sec`` saniyelik bir kaydı eşit dilimlere böler.

    Saf, deterministik bir parçalayıcıdır. Gerçek ses dosyasını ne okur
    ne de keser; sadece ``[0, duration_sec]`` aralığını
    ``chunk_minutes`` dakikalık ardışık ve örtüşmesiz dilimlere ayırır.
    Son dilim ``chunk_minutes``'tan kısa olabilir (Req 11.5).

    Parameters
    ----------
    duration_sec:
        Kaydın toplam uzunluğu, saniye cinsinden. Negatif olamaz.
        ``0`` gönderilirse boş liste döner.
    chunk_minutes:
        Bir parçanın hedef uzunluğu, dakika cinsinden. Pozitif tam
        sayı olmalıdır.

    Returns
    -------
    list[tuple[float, float]]
        Her tuple ``(start_sec, end_sec)`` ardışık dilimin sınırlarıdır.
        Liste şu değişmezleri sağlar:

        * ``len(result) == 0`` ⇔ ``duration_sec == 0``.
        * Tüm ``start_sec`` küçük-eşit ``end_sec``'tir.
        * Ardışık dilimler temas eder: ``result[i].end_sec ==
          result[i+1].start_sec``.
        * ``result[0].start_sec == 0`` ve ``result[-1].end_sec ==
          duration_sec``.
        * Son hariç tüm dilimlerin uzunluğu tam olarak
          ``chunk_minutes * 60`` saniyedir.

    Raises
    ------
    ValueError
        ``duration_sec`` negatifse ya da ``chunk_minutes`` pozitif tam
        sayı değilse.
    TypeError
        Argümanların tipi sayısal değilse veya ``chunk_minutes``
        ``bool`` ise (Python ``bool``'u ``int``'in alt sınıfıdır;
        kazara ``True``/``False`` geçilmesine izin vermek istemiyoruz).
    """
    # bool, int'in alt sınıfı; ancak duration veya chunk_minutes için
    # anlamsız bir değerdir. ``bool`` girişlerini açıkça reddet.
    if isinstance(duration_sec, bool) or not isinstance(
        duration_sec, (int, float)
    ):
        raise TypeError(
            f"duration_sec must be a number, got {type(duration_sec).__name__}"
        )
    if isinstance(chunk_minutes, bool) or not isinstance(chunk_minutes, int):
        raise TypeError(
            "chunk_minutes must be an int, got "
            f"{type(chunk_minutes).__name__}"
        )

    if duration_sec < 0:
        raise ValueError(f"duration_sec must be >= 0, got {duration_sec!r}")
    if chunk_minutes <= 0:
        raise ValueError(
            f"chunk_minutes must be > 0, got {chunk_minutes!r}"
        )

    duration = float(duration_sec)
    if duration == 0.0:
        return []

    chunk_sec = float(chunk_minutes) * 60.0
    chunks: list[tuple[float, float]] = []
    start = 0.0
    while start < duration:
        end = start + chunk_sec
        if end > duration:
            end = duration
        chunks.append((start, end))
        start = end

    return chunks


# --------------------------------------------------------------------------- #
# Payload normalize ediciler                                                  #
# --------------------------------------------------------------------------- #


def _coerce_to_dict(transcript: Any) -> dict[str, Any]:
    """``transcript`` argümanını güvenli biçimde ``dict``'e indirger.

    Reasoning modelinden gelen yanıt birkaç farklı biçimde olabilir:

    * Zaten parse edilmiş ``dict``;
    * JSON metni (``str``);
    * Tipik çöp/serbest metin (parse edilemez ``str``).

    Sözleşme: bu fonksiyon **asla yükseltmez**. Parse edilemeyen ya da
    tip-uyumsuz girdilerde boş ``dict`` döner; üst katman varsayılan
    değerlerle dolduracaktır.
    """
    if isinstance(transcript, dict):
        return transcript
    if isinstance(transcript, str):
        text = transcript.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return {}
        if isinstance(parsed, dict):
            return parsed
        return {}
    return {}


def _normalize_str(value: Any) -> str:
    """Bir alanı güvenli ``str``'e zorlar; ``None`` ya da boşluk ⇒ ``''``."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        # bool, int alt sınıfıdır; sayısal döküm yerine sözel olarak
        # raporlamayalım; CRM/meeting alanlarında bool anlamsız.
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _normalize_optional_str(value: Any) -> str | None:
    """``owner`` / ``due`` gibi opsiyonel alanlar için ``None`` koruyan döküm."""
    if value is None:
        return None
    coerced = _normalize_str(value)
    return coerced if coerced else None


def _normalize_participants(value: Any) -> list[str]:
    """Katılımcı listesini boşluk-temizlenmiş benzersiz olmayan ``list[str]``'e çevirir.

    * Liste değilse boş liste döner.
    * String olmayan elemanlar düşürülür.
    * Boş ya da yalnızca boşluk olan adlar düşürülür.
    * Sıra korunur (LLM'in sıralaması anlamlı olabilir).
    """
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        if isinstance(item, str):
            stripped = item.strip()
            if stripped:
                cleaned.append(stripped)
    return cleaned


def _normalize_action_item(value: Any) -> dict[str, str | None] | None:
    """Tek bir aksiyon kalemini ``{owner, due}``'ya indirger.

    Geçersiz girdi (``dict`` değil) için ``None`` döner; çağıran tarafı
    bu None'ları filtreler. ``owner`` ve ``due`` Req 11.2 gereği
    opsiyoneldir; eksikse ``None`` saklanır.
    """
    if not isinstance(value, dict):
        return None
    return {
        "owner": _normalize_optional_str(value.get("owner")),
        "due": _normalize_optional_str(value.get("due")),
    }


def _normalize_action_items(value: Any) -> list[dict[str, str | None]]:
    """Aksiyon listesi için saf normalize edici."""
    if not isinstance(value, list):
        return []
    items: list[dict[str, str | None]] = []
    for raw in value:
        item = _normalize_action_item(raw)
        if item is not None:
            items.append(item)
    return items


def build_meeting_payload(transcript: Any) -> dict[str, Any]:
    """Toplantı çıktısını ``{participants, action_items}`` şemasına çeker.

    Reasoning modelinin ürettiği ham yanıtı (JSON metin veya parse
    edilmiş ``dict``) Req 11.2'deki sözleşmeye normalize eder:

    * ``participants`` her zaman ``list[str]``'tir; geçersiz/eksikse
      boş liste döner.
    * ``action_items`` her zaman ``list[dict]``'tir. Her kalem
      ``owner`` ve ``due`` anahtarlarını taşır; her ikisi de
      opsiyoneldir (``None`` olabilir).

    Saf, deterministik ve idempotenttir:
    ``build_meeting_payload(build_meeting_payload(x)) ==
    build_meeting_payload(x)``.

    Parameters
    ----------
    transcript:
        Reasoning modelinden gelen ham çıktı. ``str`` (JSON), ``dict``
        veya parse edilemez bir değer olabilir; tüm durumlarda
        sözleşmeye uygun bir ``dict`` döner. Asla yükseltmez.

    Returns
    -------
    dict[str, Any]
        ``{"participants": [...], "action_items": [...]}`` şemasında
        sözlük; her ``action_item`` ``{"owner": ..., "due": ...}``
        biçimindedir.
    """
    raw = _coerce_to_dict(transcript)
    return {
        "participants": _normalize_participants(raw.get("participants")),
        "action_items": _normalize_action_items(raw.get("action_items")),
    }


def build_crm_payload(transcript: Any) -> dict[str, str]:
    """Telefon görüşmesi çıktısını CRM şemasına çeker (Req 11.3).

    Şema sabittir: ``customer``, ``intent``, ``next_step``, ``summary``.
    Tüm alanlar zorunludur ve eksik/uygunsuz girdi için boş string'le
    doldurulur. Bu sayede CRM tarafına yapılan kayıt her zaman aynı
    anahtarları taşır ve bilinmeyen alanlar dışlanır.

    Saf, deterministik, idempotent.

    Parameters
    ----------
    transcript:
        Reasoning modelinden gelen ham çıktı. ``str`` (JSON), ``dict``
        veya parse edilemez bir değer olabilir.

    Returns
    -------
    dict[str, str]
        ``{"customer", "intent", "next_step", "summary"}`` anahtarlarına
        sahip sözlük. Tüm değerler ``str``'dir (en az boş string).
    """
    raw = _coerce_to_dict(transcript)
    return {
        "customer": _normalize_str(raw.get("customer")),
        "intent": _normalize_str(raw.get("intent")),
        "next_step": _normalize_str(raw.get("next_step")),
        "summary": _normalize_str(raw.get("summary")),
    }
