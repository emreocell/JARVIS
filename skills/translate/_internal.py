"""Translate skill — saf yardımcılar.

Bu modülün sözleşmesi tasarım belgesindeki "üç katmanlı" skill kalıbının
ilk katmanına denk düşer: ``translate_text`` ve ``translate_screen``
handler'ları HTTP çağrısından önce buradaki yardımcılarla girdi normalize
eder, çağrıdan sonra Türkçe yanıtı biçimler.

Tüm fonksiyonlar **saftır**:

* yan etkisiz (I/O, log, clipboard, dosya yok),
* deterministik (aynı girdi → aynı çıktı),
* idempotent (yeniden uygulamak çıktıyı değiştirmez),
* property-based test (Hypothesis) ile doğrulanabilir.

Yardımcılar:

- :func:`resolve_target_lang` — Req 7.5: kullanıcı argümanı yoksa
  ``config/api_keys.json`` içindeki ``translate.default_target`` değeri
  veya son çare olarak ``"en"`` döner.
- :func:`detect_source_lang_hint` — Req 7.4'ün hafif istemci-tarafı
  ipucu üreteni; otomatik dil tespiti için NVIDIA modeline yardımcı
  olur. Türkçe karakterler veya yalnızca ASCII harfler gibi belirgin
  sinyallerde dil kodu döner, aksi halde ``None``.
- :func:`format_translation_response` — Req 7.2 ve 7.4: orijinal metin
  ve çeviriyi tek paragraflık Türkçe yanıt olarak biçimler ve tespit
  edilen kaynak dil ile hedef dili açıkça raporlar.
"""

from __future__ import annotations

from typing import Final


# ---------------------------------------------------------------------------
# Dil kodu sözlükleri
# ---------------------------------------------------------------------------
#
# Türkçe yanıtta kullanılacak yerel dil isimleri. Sözlük yalnızca biçimleme
# için kullanılır; bilinmeyen kodlar olduğu gibi (lower-case) raporlanır.

_LANG_NAMES_TR: Final[dict[str, str]] = {
    "tr": "Türkçe",
    "en": "İngilizce",
    "de": "Almanca",
    "fr": "Fransızca",
    "es": "İspanyolca",
    "it": "İtalyanca",
    "pt": "Portekizce",
    "ru": "Rusça",
    "ar": "Arapça",
    "ja": "Japonca",
    "ko": "Korece",
    "zh": "Çince",
    "nl": "Hollandaca",
    "pl": "Lehçe",
    "uk": "Ukraynaca",
    "el": "Yunanca",
    "fa": "Farsça",
    "az": "Azerice",
}

# ISO 639-1 kodu olarak kabul edilen ASCII harfler.
_LANG_CODE_LEN: Final[int] = 2

# Türkçe'ye özel karakter kümesi (ASCII dışı). Heuristik bu karakterlerden
# herhangi biri görüldüğünde "tr" döner.
_TURKISH_MARKERS: Final[frozenset[str]] = frozenset("ıİğĞşŞçÇöÖüÜ")


def _normalize_lang_code(value: object) -> str:
    """Kullanıcıdan veya config'den gelen dil kodunu normalize et.

    Sözleşme:
    * ``None`` veya string olmayan → boş string.
    * Kenar boşlukları silinir, ASCII'ye dökülür, küçük harfe çevrilir.
    * Geçersiz format (boş, harf-dışı karakter veya 2-3 harf değil) → boş
      string. Bu sayede çağıran tarafa "kullanılamaz girdi" tek tip
      sinyalle iletilir.

    Saf, idempotent: ``f(f(x)) == f(x)`` her ``x`` için.
    """
    if not isinstance(value, str):
        return ""
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if not (2 <= len(candidate) <= 3):
        return ""
    if not candidate.isalpha() or not candidate.isascii():
        return ""
    return candidate


def resolve_target_lang(arg: object, config_default: object) -> str:
    """Hedef dil kodunu öncelik sırasıyla belirle (Req 7.5).

    Karar zinciri:
        1. ``arg`` geçerli bir 2-3 harfli ASCII dil koduysa onu döndür.
        2. ``config_default`` geçerli bir koda sahipse onu döndür.
        3. Hiçbiri geçerli değilse ``"en"`` döndür.

    Saf fonksiyon: yan etkisiz, deterministik, idempotent
    (``resolve_target_lang(resolve_target_lang(a, b), b)`` aynı sonucu
    verir).
    """
    arg_code = _normalize_lang_code(arg)
    if arg_code:
        return arg_code
    cfg_code = _normalize_lang_code(config_default)
    if cfg_code:
        return cfg_code
    return "en"


def detect_source_lang_hint(text: object) -> str | None:
    """Kaynak dil için hafif heuristik bir ipucu döndür (Req 7.4).

    Bu fonksiyon **kesin** dil tespiti yapmaz; yalnızca açık ipuçları
    gördüğünde NVIDIA çeviri modeline yardımcı olacak bir hint üretir.
    Tespit gerçek anlamda NVIDIA tarafından yapılır ve sonuç biçimleme
    sırasında ayrıca raporlanır.

    Karar kuralları:
    * Boş veya string olmayan girdi → ``None``.
    * Türkçe'ye özgü karakterlerden (``ı, İ, ğ, Ğ, ş, Ş, ç, Ç, ö, Ö, ü,
      Ü``) en az biri varsa → ``"tr"``.
    * Aksi halde harf içeriyorsa ve **tüm** harfler ASCII ise → ``"en"``.
    * Diğer her durumda (ASCII-dışı harfler veya hiç harf yok) →
      ``None``.

    Saf, deterministik. Heuristik kasıtlı olarak konservatiftir; yanlış
    pozitif vermek yerine ``None`` döndürmeyi tercih eder.
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    if not stripped:
        return None

    has_turkish_marker = any(ch in _TURKISH_MARKERS for ch in stripped)
    if has_turkish_marker:
        return "tr"

    letters = [ch for ch in stripped if ch.isalpha()]
    if not letters:
        return None
    if all(ch.isascii() for ch in letters):
        return "en"
    return None


def _lang_name_tr(code: str) -> str:
    """Dil kodu için Türkçe görünüm adı döner; bilinmeyen kodlar olduğu
    gibi büyük harfle raporlanır."""
    normalized = _normalize_lang_code(code)
    if not normalized:
        return "bilinmeyen"
    return _LANG_NAMES_TR.get(normalized, normalized.upper())


def _collapse_whitespace(value: str) -> str:
    """Çok satırlı / fazla boşluklu metni tek paragraf hâline getir."""
    return " ".join(value.split())


def format_translation_response(
    orig: object,
    translation: object,
    src_lang: object,
    tgt_lang: object,
) -> str:
    """Çeviri sonucunu Türkçe tek paragraflık yanıt olarak biçimle.

    Req 7.2: Yanıt orijinal metni **ve** çeviriyi birlikte içermelidir.
    Req 7.4: Tespit edilen kaynak dil yanıtta açıkça raporlanır.

    Sözleşme:
    * ``orig`` ve ``translation`` string'e zorlanır; çok satırlı içerik
      tek satıra düşürülür (sesli yanıt için TTS dostu).
    * ``src_lang`` boş/None ise "otomatik" olarak raporlanır (NVIDIA
      tarafından tespit istenmiş demektir).
    * ``tgt_lang`` boş/None ise sözleşme gereği kullanıcıya görünür
      şekilde ``"en"`` olarak raporlanır (varsayılan hedef).
    * Çeviri boş geldiğinde paragraf açıkça "Çeviri üretilemedi" der;
      orijinal metin yine de raporlanır (Req 7.2'nin "orijinal +
      çeviri" sözleşmesi her iki alanı korur).

    Saf, deterministik ve TTS-dostu (newline yok).
    """
    orig_text = _collapse_whitespace(str(orig)) if orig is not None else ""
    translated_text = (
        _collapse_whitespace(str(translation)) if translation is not None else ""
    )

    src_code = _normalize_lang_code(src_lang)
    tgt_code = _normalize_lang_code(tgt_lang) or "en"

    src_label = "otomatik" if not src_code else _lang_name_tr(src_code)
    tgt_label = _lang_name_tr(tgt_code)

    if not translated_text:
        return (
            f"Kaynak dil {src_label} olarak alındı, hedef {tgt_label} fakat "
            f"çeviri üretilemedi. Orijinal metin: \"{orig_text}\"."
        )

    return (
        f"Kaynak dil {src_label}, hedef dil {tgt_label}. Orijinal metin: "
        f"\"{orig_text}\". Çeviri: \"{translated_text}\"."
    )


__all__ = [
    "resolve_target_lang",
    "detect_source_lang_hint",
    "format_translation_response",
]
