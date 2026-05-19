"""Unit tests for ``skills.document.readers`` (Task 11.1).

Covers Requirement 20.1: Document_QA reader supports ``.pdf``, ``.docx``,
``.txt`` and ``.md`` and returns whitespace-normalized text.

Property-based round-trip checks (Property 19) live in a separate file
gated by Task 11.4; the tests here exercise specific examples and edge
cases.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from skills.document import readers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_pdf(path: Path, text: str) -> None:
    """Minimal tek sayfalık PDF üret ve diske yaz.

    ``pypdf`` doğrudan PDF yazma API'si sağlamaz; bunun yerine bir boş
    sayfa oluşturup üzerine ``Annotation`` ile metin koyarız. Round-trip
    testleri için yeterli — extract_text basit ASCII metni geri okur.
    """

    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        TextStringObject,
    )

    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

    # Content stream içine metin yaz: BT / ET blokuyla basit bir metin
    # objesi kurarız. pypdf bu stream'den ``extract_text`` çağrısında metni
    # geri okuyabilir.
    safe_text = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 50 750 Td ({safe_text}) Tj ET".encode("latin-1")

    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = stream

    # Font kaynağı /F1 — Helvetica, standart 14 base font.
    font = DictionaryObject()
    font[NameObject("/Type")] = NameObject("/Font")
    font[NameObject("/Subtype")] = NameObject("/Type1")
    font[NameObject("/BaseFont")] = NameObject("/Helvetica")
    resources = DictionaryObject()
    resources[NameObject("/Font")] = DictionaryObject(
        {NameObject("/F1"): font}
    )
    page[NameObject("/Resources")] = resources
    page[NameObject("/MediaBox")] = ArrayObject(
        [FloatObject(0), FloatObject(0), FloatObject(612), FloatObject(792)]
    )

    with path.open("wb") as fp:
        writer.write(fp)


def _write_docx(path: Path, paragraphs: list[str]) -> None:
    """Verilen paragrafları içeren DOCX üret."""

    from docx import Document

    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(str(path))


# ---------------------------------------------------------------------------
# Plain text readers
# ---------------------------------------------------------------------------


def test_read_txt_normalizes_whitespace(tmp_path: Path) -> None:
    """``.txt`` okumada ardışık whitespace tek boşluğa iner."""

    src = tmp_path / "note.txt"
    src.write_text("merhaba\n\n  dünya\t!\n", encoding="utf-8")

    assert readers.read(src) == "merhaba dünya !"


def test_read_md_normalizes_whitespace(tmp_path: Path) -> None:
    """``.md`` da text reader üzerinden gider."""

    src = tmp_path / "note.md"
    src.write_text("# Başlık\n\nParagraf  metni.\n", encoding="utf-8")

    assert readers.read(src) == "# Başlık Paragraf metni."


def test_read_text_handles_cp1254_fallback(tmp_path: Path) -> None:
    """UTF-8 olmayan Türkçe Windows kodlaması (cp1254) okunabilmeli."""

    src = tmp_path / "windows.txt"
    src.write_bytes("şçğüöı".encode("cp1254"))

    assert readers.read(src) == "şçğüöı"


def test_read_strips_leading_and_trailing_whitespace(tmp_path: Path) -> None:
    """Çıktının başı ve sonu kırpılmış olmalı."""

    src = tmp_path / "spaced.txt"
    src.write_text("   \n\t  selam dünya  \n  ", encoding="utf-8")

    assert readers.read(src) == "selam dünya"


def test_extension_lookup_is_case_insensitive(tmp_path: Path) -> None:
    """``.TXT`` ve ``.MD`` gibi büyük harfli uzantılar kabul edilmeli."""

    src = tmp_path / "loud.TXT"
    src.write_text("hi", encoding="utf-8")

    assert readers.read(src) == "hi"


def test_accepts_pathlib_path(tmp_path: Path) -> None:
    src = tmp_path / "p.txt"
    src.write_text("ok", encoding="utf-8")

    assert readers.read(Path(src)) == "ok"


def test_accepts_string_path(tmp_path: Path) -> None:
    src = tmp_path / "s.txt"
    src.write_text("ok", encoding="utf-8")

    assert readers.read(str(src)) == "ok"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def test_read_docx_joins_paragraphs(tmp_path: Path) -> None:
    src = tmp_path / "doc.docx"
    _write_docx(src, ["Birinci paragraf.", "İkinci paragraf."])

    result = readers.read(src)

    assert "Birinci paragraf." in result
    assert "İkinci paragraf." in result
    # Whitespace normalize edildiği için iki paragraf tek boşlukla ayrılır.
    assert "  " not in result


def test_read_docx_skips_empty_paragraphs(tmp_path: Path) -> None:
    src = tmp_path / "doc.docx"
    _write_docx(src, ["alpha", "", "beta"])

    assert readers.read(src) == "alpha beta"


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_read_pdf_extracts_text(tmp_path: Path) -> None:
    src = tmp_path / "doc.pdf"
    _write_pdf(src, "Merhaba PDF dunyasi")

    result = readers.read(src)

    assert "Merhaba" in result
    assert "PDF" in result
    # Whitespace tek boşluk olmalı; baş/son kırpılı.
    assert result == result.strip()
    assert "  " not in result


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_unknown_extension_raises_value_error(tmp_path: Path) -> None:
    src = tmp_path / "data.csv"
    src.write_text("a,b,c", encoding="utf-8")

    with pytest.raises(ValueError, match="Desteklenmeyen"):
        readers.read(src)


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    missing = tmp_path / "yok.txt"

    with pytest.raises(FileNotFoundError, match="Dosya bulunamadı"):
        readers.read(missing)


def test_directory_path_raises_file_not_found(tmp_path: Path) -> None:
    """Dizin verilirse ``FileNotFoundError`` ile reddedilir."""

    with pytest.raises(FileNotFoundError):
        readers.read(tmp_path)


# ---------------------------------------------------------------------------
# Surface contract
# ---------------------------------------------------------------------------


def test_supported_extensions_lists_all_four_formats() -> None:
    """Req 20.1: ``.pdf``, ``.docx``, ``.txt`` ve ``.md`` desteklenir."""

    assert readers.supported_extensions() == (".docx", ".md", ".pdf", ".txt")
