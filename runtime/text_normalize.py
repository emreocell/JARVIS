"""Türkçe metin normalizasyonu — Command_Palette araması ve benzeri eşleşmeler.

Sorumluluklar
-------------
* :func:`normalize_tr` — Türkçe karakterleri ASCII'ye eşler, ``casefold`` uygular ve
  yalnızca alfanümerik karakterler ile boşluk bırakır. Çoklu boşluklar tek boşluğa
  indirgenir, baş/son boşluk silinir.
* Çıktı yalnızca ``[a-z0-9 ]`` aralığındaki karakterlerden oluşur, böylece
  fonksiyon **idempotent**'tir: ``normalize_tr(normalize_tr(s)) == normalize_tr(s)``.

Kullanım
--------
Command_Palette araması, kullanıcı sorgusunu ve tool / rutin adlarını bu fonksiyon
üzerinden geçirerek Türkçe karakter farklarını (örn. ``"şarkı"`` ↔ ``"sarki"``)
tolere eder.

Validates: Requirements 14.8 (jarvis-nvidia-skill-pack)
"""

from __future__ import annotations

# Feature: jarvis-nvidia-skill-pack, Task 23.1 — runtime/text_normalize.py

# Türkçe → ASCII eşlemesi. Hem büyük hem küçük harfleri açıkça eşliyoruz; bu
# sayede ``str.casefold`` adımının yerel-bağımsız davranışıyla (örn. Python'un
# ``"İ".casefold()`` çıktısının ``"i̇"`` olması) çakışma yaşamayız.
_TR_MAP: dict[str, str] = {
    "ı": "i",
    "I": "i",  # Türkçe büyük noktasız I
    "İ": "i",
    "i": "i",
    "ç": "c",
    "Ç": "c",
    "ğ": "g",
    "Ğ": "g",
    "ö": "o",
    "Ö": "o",
    "ş": "s",
    "Ş": "s",
    "ü": "u",
    "Ü": "u",
}

_TRANSLATION = str.maketrans(_TR_MAP)


def normalize_tr(s: str) -> str:
    """Türkçe-duyarlı bir normalizasyon uygular.

    Adımlar:
        1. Türkçe karakterler ASCII karşılıklarına eşlenir (büyük/küçük harf
           ayrımı gözetilerek; ``İ → i``, ``ı → i``, ``ç → c`` …).
        2. ``str.casefold`` ile geri kalan harfler küçültülür.
        3. Yalnızca alfanümerik karakterler ve boşluk korunur; diğer her şey
           tek bir boşlukla değiştirilir.
        4. Çoklu boşluklar tek boşluğa indirgenir ve baş/son boşluklar silinir.

    Çıktı yalnızca ``[a-z0-9 ]`` karakterlerinden oluştuğu için fonksiyon
    idempotent'tir.

    Parameters
    ----------
    s:
        Normalleştirilecek metin. ``str`` olmayan değer bekleniyorsa çağıran
        tarafın ``str(...)`` çağrısı yapması beklenir.

    Returns
    -------
    str
        Normalleştirilmiş metin.

    Examples
    --------
    >>> normalize_tr("Şarkı Çal!")
    'sarki cal'
    >>> normalize_tr("Önemli Görev")
    'onemli gorev'
    >>> normalize_tr(normalize_tr("Şarkı Çal!")) == normalize_tr("Şarkı Çal!")
    True
    """
    if not s:
        return ""

    # 1. Türkçe → ASCII eşlemesi
    mapped = s.translate(_TRANSLATION)

    # 2. casefold (kalan harfleri küçük harfe çevirir; Türkçe karakterler artık
    #    ASCII olduğu için locale farkı yaratmaz).
    folded = mapped.casefold()

    # 3 + 4. alfanümerik / boşluk filtresi ve boşluk daraltma — tek geçişte yap.
    out_chars: list[str] = []
    prev_space = True  # baştaki boşlukları otomatik atmak için
    for ch in folded:
        if ch.isalnum():
            out_chars.append(ch)
            prev_space = False
        else:
            # Alfanümerik olmayan her şey (boşluk, noktalama, tire, vb.) tek
            # bir boşluk olarak temsil edilir.
            if not prev_space:
                out_chars.append(" ")
                prev_space = True

    # Sonda kalan boşluğu kaldır.
    if out_chars and out_chars[-1] == " ":
        out_chars.pop()

    return "".join(out_chars)


__all__ = ["normalize_tr"]
