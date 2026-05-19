"""Unit tests for ``skills.document.chunker`` (Task 11.2).

Covers Requirement 20.2: ``chunk(text, max_chunk_size) -> list[str]``
must produce parts whose ordered concatenation equals the original
text and whose lengths never exceed ``max_chunk_size``.

Property-based round-trip checks (Property 20) live in a separate
file gated by Task 11.5; the tests here exercise specific examples
and edge cases.
"""

from __future__ import annotations

import pytest

from skills.document import chunker


# ---------------------------------------------------------------------------
# Empty / boundary inputs
# ---------------------------------------------------------------------------


def test_empty_text_returns_empty_list() -> None:
    """Boş metin için sonuç boş liste."""

    assert chunker.chunk("", 10) == []


def test_text_shorter_than_size_returns_single_chunk() -> None:
    text = "kısa"
    assert chunker.chunk(text, 100) == [text]


def test_text_equal_to_size_returns_single_chunk() -> None:
    text = "0123456789"
    assert chunker.chunk(text, 10) == [text]


# ---------------------------------------------------------------------------
# Core invariants on hand-picked examples
# ---------------------------------------------------------------------------


def test_concatenation_equals_original_no_whitespace() -> None:
    """Whitespace içermeyen metinde sert kesim — concat == orijinal."""

    text = "abcdefghij"
    chunks = chunker.chunk(text, 3)

    assert "".join(chunks) == text
    assert all(len(c) <= 3 for c in chunks)


def test_concatenation_equals_original_with_whitespace() -> None:
    """Boşluklu metinde de bütünlük korunur."""

    text = "lorem ipsum dolor sit amet consectetur"
    chunks = chunker.chunk(text, 12)

    assert "".join(chunks) == text
    assert all(len(c) <= 12 for c in chunks)


def test_size_one_yields_per_character_chunks() -> None:
    """``max_chunk_size == 1`` her karakteri ayrı chunk'a koyar."""

    text = "abc d"
    chunks = chunker.chunk(text, 1)

    assert chunks == ["a", "b", "c", " ", "d"]
    assert "".join(chunks) == text


def test_prefers_word_boundary_when_available() -> None:
    """Whitespace pencerede varsa kesim ondan sonra yapılır."""

    text = "merhaba dünya selam"
    chunks = chunker.chunk(text, 10)

    # "merhaba " (8) tek başına bir chunk olmalı; sınır boşluktan sonra.
    assert chunks[0] == "merhaba "
    assert "".join(chunks) == text
    assert all(len(c) <= 10 for c in chunks)


def test_falls_back_to_hard_cut_when_no_whitespace_in_window() -> None:
    """Pencere yarısından sonra whitespace yoksa karakter sınırında keser."""

    text = "abcdefghijklmnop"  # whitespace yok
    chunks = chunker.chunk(text, 5)

    assert chunks == ["abcde", "fghij", "klmno", "p"]
    assert "".join(chunks) == text


def test_short_word_followed_by_long_token_uses_hard_cut() -> None:
    """Whitespace pencerenin ilk yarısındaysa kabul edilmez."""

    text = "a " + "b" * 20  # "a " sonra 20 'b'
    chunks = chunker.chunk(text, 8)

    # Pencere "a bbbbbb" — boşluk index 1, threshold pos+4'ten önce →
    # kabul edilmez, sert kesim. İlk chunk tam 8 karakter.
    assert len(chunks[0]) == 8
    assert "".join(chunks) == text
    assert all(len(c) <= 8 for c in chunks)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_is_deterministic() -> None:
    text = "Lorem ipsum dolor sit amet, consectetur adipiscing elit."
    a = chunker.chunk(text, 15)
    b = chunker.chunk(text, 15)

    assert a == b


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_size", [0, -1, -100])
def test_non_positive_size_raises(bad_size: int) -> None:
    with pytest.raises(ValueError, match="pozitif"):
        chunker.chunk("hello", bad_size)


@pytest.mark.parametrize("bad_size", [1.5, "10", None, True, False])
def test_non_int_size_raises(bad_size: object) -> None:
    with pytest.raises(ValueError, match="tamsayı"):
        chunker.chunk("hello", bad_size)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Larger fixture-style example
# ---------------------------------------------------------------------------


def test_large_text_preserves_all_characters() -> None:
    """3 KB'lık örnek metinde tek karakter kayıp/duplikasyon olmamalı."""

    paragraph = (
        "JARVIS v2 belge soru-cevap akışı, uzun belgeleri parçalara "
        "bölüp her parçayı ayrı bir LLM çağrısına göndererek özetler. "
    )
    text = paragraph * 30  # ~3 KB

    chunks = chunker.chunk(text, 200)

    assert "".join(chunks) == text
    assert all(len(c) <= 200 for c in chunks)
    # Boş chunk üretilmemeli.
    assert all(len(c) > 0 for c in chunks)
