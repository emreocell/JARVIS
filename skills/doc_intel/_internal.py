"""Saf yardımcılar — Doc_Intel_Skill için HTTP'siz dönüşüm fonksiyonları.

Bu modül `tools.py` tarafından kullanılan üç saf fonksiyon ve bir
yarı-saf PDF yardımcısı yayımlar. NVIDIA NIM çağrısı burada yapılmaz;
HTTP istemcisi `Model_Router` üzerinden `tools.py` katmanında dispatch
edilir. Buradaki katmanın amacı:

* girdi normalize / doğrulama,
* yanıt formatlama (Türkçe özet),
* görsel ölçeklendirme kararı,
* PDF açma — bağımlılık yoksa anlamlı Türkçe hata.

Sözleşme — design.md "Property 15: Görsel ölçeklendirmede oran ve
uzun-kenar sınırı korunur":

    `resize_to_max_long_edge(w, h, max_edge)` çıktısı `(w', h')`:
        1. **Sınır**:    max(w', h') <= max_edge
        2. **Oran**:     |w/h - w'/h'| <= rounding tolerance
        3. **No-op**:    max(w, h) <= max_edge ⇒ çıktı `(w, h)`
        4. **Pozitiflik**: w', h' >= 1

`parse_doc_response`, `truncate_paragraphs` ve `pdf_to_image_first_page`
örnek bazlı testlerle (Req 5.2, 5.4, 5.6) kapsanır; saf oldukları için
mock'a ihtiyaç duymazlar (PyMuPDF hariç).
"""

from __future__ import annotations

import json
import os
from typing import Any, Mapping, Tuple


# Türkçe yasal/UI string'leri — tek noktadan değiştirilebilsin diye
# modül seviyesinde sabit tutuluyor. Hata mesajları **tek paragraf**
# formatında (Req 5.6, 5.7).
_PYMUPDF_MISSING_MESSAGE = (
    "PDF işlemek için PyMuPDF (fitz) kütüphanesi gerekli, "
    "ancak ortamda yüklü değil. Lütfen `pip install pymupdf` komutunu "
    "çalıştırın ve tekrar deneyin."
)
_PDF_NOT_FOUND_MESSAGE_TMPL = (
    "PDF dosyası bulunamadı veya okunamadı: {path}"
)
_PDF_EMPTY_MESSAGE = (
    "PDF dosyası boş görünüyor; çözümlenecek sayfa bulunamadı."
)


# ---------------------------------------------------------------------------
# resize_to_max_long_edge
# ---------------------------------------------------------------------------

def _validate_positive_int(value: Any, name: str) -> int:
    """Argümanın pozitif tamsayı olduğunu doğrular.

    `bool` değerler `int`'in alt sınıfı olduğu için ayrıca reddedilir
    (True/False'un sessizce 1/0 olarak yorumlanmasını engellemek için).
    """

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} pozitif bir tamsayı olmalı, "
            f"alındı: {type(value).__name__}"
        )
    if value <= 0:
        raise ValueError(f"{name} pozitif olmalı, alındı: {value}")
    return value


def resize_to_max_long_edge(
    w: int, h: int, max_edge: int = 4096
) -> Tuple[int, int]:
    """Bir görselin (w, h) boyutlarını uzun kenarı `max_edge`'i geçmeyecek
    şekilde ölçeklendirir; oranı korur, yan etki yapmaz.

    Args:
        w: Görselin genişliği (piksel). Pozitif tamsayı olmalı.
        h: Görselin yüksekliği (piksel). Pozitif tamsayı olmalı.
        max_edge: Uzun kenarın azami piksel uzunluğu. Varsayılan 4096
            (Req 5.7). Pozitif tamsayı olmalı.

    Returns:
        Yeni `(w', h')` tamsayı çifti. Aşağıdaki değişmezler garanti:

        * `max(w', h') <= max_edge` (sınır)
        * `max(w, h) <= max_edge` ise `(w', h') == (w, h)` (no-op)
        * `w', h' >= 1` (pozitiflik)
        * `|w/h - w'/h'|` yuvarlama hatası mertebesinde (oran)

    Raises:
        ValueError: Argümanlar pozitif tamsayı değilse.

    Property:
        Property 15 — Görsel ölçeklendirmede oran ve uzun-kenar sınırı
        korunur (Validates: Requirements 5.7).
    """

    _validate_positive_int(w, "w")
    _validate_positive_int(h, "h")
    _validate_positive_int(max_edge, "max_edge")

    long_edge = w if w >= h else h

    # No-op: zaten sınır içindeyiz; girdiyi bozmadan döndür.
    if long_edge <= max_edge:
        return (w, h)

    # Ölçek faktörü uzun kenara göre hesaplanır; böylece oran korunur.
    scale = max_edge / long_edge

    # `round` ile en yakın tamsayıya yuvarla; ardından pozitiflik için
    # alt sınır 1, max_edge için üst sınır clamp.  Floating-point hata
    # nedeniyle uzun kenar nadiren `max_edge + 1` çıkabilir; clamp bunu
    # garanti altına alır.
    new_w = max(1, min(max_edge, int(round(w * scale))))
    new_h = max(1, min(max_edge, int(round(h * scale))))

    return (new_w, new_h)


# ---------------------------------------------------------------------------
# parse_doc_response
# ---------------------------------------------------------------------------

_DOC_FIELDS: Tuple[str, ...] = (
    "vendor",
    "total",
    "currency",
    "date",
    "line_items",
)


def _coerce_total(value: Any) -> float | None:
    """`total` alanını float'a dönüştür; başarısızsa `None`."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().replace(",", ".")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_str(value: Any) -> str | None:
    """Verilen değeri non-empty string'e dönüştür; aksi halde `None`."""

    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    # Sayı ve diğer skaler tipler — string'e dök.
    return str(value).strip() or None


def parse_doc_response(raw: Any) -> dict:
    """`doc_parse` modelinden gelen ham yanıtı yapılandırılmış sözlüğe
    çevirir ve Türkçe tek paragraflık özet üretir.

    Beklenen alanlar (Req 5.2): ``vendor``, ``total``, ``currency``,
    ``date``, ``line_items``. Alanlar eksik/farklı tipte ise güvenli
    şekilde normalize edilir; eksik alanlar `None` döner ve özet metni
    "Belge alanları kısmen çıkarıldı" notuyla raporlar.

    Args:
        raw: Modelin döndürdüğü ham çıktı. JSON string ya da dict
            kabul edilir; başka tipler ``ValueError`` üretir.

    Returns:
        ``{"vendor", "total", "currency", "date", "line_items",
        "summary_tr"}`` anahtarlı sözlük. ``summary_tr`` her zaman
        non-empty Türkçe paragrafdır.

    Raises:
        ValueError: ``raw`` ne string ne dict ise veya string geçerli
            JSON içermiyorsa.

    Validates:
        Requirements 5.2 — `doc_parse` çıktısının yapılandırılmış JSON
        olarak normalize edilmesi.
    """

    if isinstance(raw, str):
        try:
            data: Any = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Belge çıktısı JSON olarak çözülemedi: " f"{exc.msg}"
            ) from exc
    elif isinstance(raw, Mapping):
        data = raw
    else:
        raise ValueError(
            "Belge çıktısı string ya da dict olmalı, "
            f"alındı: {type(raw).__name__}"
        )

    if not isinstance(data, Mapping):
        raise ValueError(
            "Belge çıktısının kök seviyesi nesne (dict) olmalı, "
            f"alındı: {type(data).__name__}"
        )

    vendor = _coerce_str(data.get("vendor"))
    total = _coerce_total(data.get("total"))
    currency = _coerce_str(data.get("currency"))
    date = _coerce_str(data.get("date"))

    raw_items = data.get("line_items", [])
    if isinstance(raw_items, list):
        # Sadece dict elemanları kabul edilir; aksi halde elenir.
        line_items = [item for item in raw_items if isinstance(item, Mapping)]
        # `Mapping` instance'larını saf dict'e normalize et.
        line_items = [dict(item) for item in line_items]
    else:
        line_items = []

    # Türkçe özet — sadece dolu alanlar gösterilir, hepsi boşsa kullanıcı
    # bilgilendirilir (Req 5.2).
    parts: list[str] = []
    if vendor:
        parts.append(f"Satıcı: {vendor}")
    if date:
        parts.append(f"Tarih: {date}")
    if total is not None:
        if currency:
            parts.append(f"Toplam: {total:.2f} {currency}")
        else:
            parts.append(f"Toplam: {total:.2f}")
    if line_items:
        parts.append(f"Kalem sayısı: {len(line_items)}")

    if parts:
        summary_tr = "Belge çözümlendi: " + ", ".join(parts) + "."
    else:
        summary_tr = (
            "Belge çözümlendi ancak yapılandırılmış alan çıkarılamadı; "
            "ham çıktıyı kontrol edin."
        )

    return {
        "vendor": vendor,
        "total": total,
        "currency": currency,
        "date": date,
        "line_items": line_items,
        "summary_tr": summary_tr,
    }


# ---------------------------------------------------------------------------
# truncate_paragraphs
# ---------------------------------------------------------------------------

def truncate_paragraphs(text: str, max_paragraphs: int = 3) -> str:
    """Bir metni en fazla `max_paragraphs` paragraf olarak keser.

    `screenshot_summarize` çıktısının üç paragrafı aşmaması gerekiyor
    (Req 5.4). Paragraflar boş satırla (``\\n\\n``) ayrılır; tek satır
    içeriği tek paragraf sayılır. Boş paragraflar atılır.

    Args:
        text: Kesilecek tam metin.
        max_paragraphs: Tutulacak azami paragraf sayısı. Pozitif
            tamsayı olmalı.

    Returns:
        En fazla `max_paragraphs` paragraf içeren, paragraflar arası
        ``\\n\\n`` ile birleştirilmiş metin. Girdi zaten daha az ya da
        eşitse anlamlı paragraflar normalize edilerek döner.

    Raises:
        ValueError: ``text`` string değilse veya ``max_paragraphs``
            pozitif tamsayı değilse.

    Validates:
        Requirements 5.4 — `screenshot_summarize` çıktısı en fazla üç
        paragraf olmalı.
    """

    if not isinstance(text, str):
        raise ValueError(
            f"text string olmalı, alındı: {type(text).__name__}"
        )
    _validate_positive_int(max_paragraphs, "max_paragraphs")

    # Paragrafları boş satıra göre ayır; her paragrafın iç boşluklarını
    # koru, fakat çevre boşluklarını trimle. Tamamen boş olanları at.
    raw_paragraphs = text.split("\n\n")
    paragraphs = [p.strip() for p in raw_paragraphs if p.strip()]

    if not paragraphs:
        return ""

    return "\n\n".join(paragraphs[:max_paragraphs])


# ---------------------------------------------------------------------------
# pdf_to_image_first_page
# ---------------------------------------------------------------------------

def pdf_to_image_first_page(pdf_path: str, dpi: int = 150) -> bytes:
    """Bir PDF dosyasının ilk sayfasını PNG byte'larına dönüştürür.

    PyMuPDF opsiyonel bir bağımlılıktır; yüklü değilse Türkçe tek
    paragraflık ``RuntimeError`` üretir (Req 5.6 — kullanıcıya anlamlı
    geri bildirim). Dosya yoksa veya PDF boşsa yine `RuntimeError` ile
    hata zarflanır; çağıran tool dispatch öncesi modele istek
    göndermez.

    Args:
        pdf_path: Yerel PDF dosyasının tam yolu.
        dpi: Render çözünürlüğü. Pozitif tamsayı olmalı; varsayılan
            150 (ekran görüntüsü için yeterli).

    Returns:
        İlk sayfanın PNG kodlamalı byte içeriği.

    Raises:
        RuntimeError: PyMuPDF eksikse, dosya bulunamazsa veya PDF
            sayfa içermiyorsa. Mesaj her zaman tek Türkçe paragrafdır.
        ValueError: ``pdf_path`` boş string ya da ``dpi`` pozitif
            tamsayı değilse.
    """

    if not isinstance(pdf_path, str) or not pdf_path.strip():
        raise ValueError("pdf_path non-empty string olmalı.")
    _validate_positive_int(dpi, "dpi")

    try:
        import fitz  # type: ignore[import-not-found]  # PyMuPDF
    except ImportError as exc:  # pragma: no cover — bağımlılık yokluğu
        raise RuntimeError(_PYMUPDF_MISSING_MESSAGE) from exc

    if not os.path.exists(pdf_path):
        raise RuntimeError(_PDF_NOT_FOUND_MESSAGE_TMPL.format(path=pdf_path))

    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            raise RuntimeError(_PDF_EMPTY_MESSAGE)
        page = doc.load_page(0)
        # `dpi`'i 72'ye oranla zoom faktörüne çevir (PDF varsayılanı 72 dpi).
        zoom = dpi / 72.0
        matrix = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=matrix, alpha=False)
        return bytes(pix.tobytes("png"))
    finally:
        doc.close()


__all__ = [
    "resize_to_max_long_edge",
    "parse_doc_response",
    "truncate_paragraphs",
    "pdf_to_image_first_page",
]
