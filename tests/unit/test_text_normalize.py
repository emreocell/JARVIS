"""Unit tests for :mod:`runtime.text_normalize`.

Bu modül ``normalize_tr`` saf fonksiyonunun davranışını birim testlerle
doğrular. Property tabanlı idempotans testi ayrı bir görevde
(``test_normalize_tr_pbt.py``) ele alınır.

Validates Requirements:
    14.8 — Command_Palette araması Türkçe karakter normalizasyonu yapar
"""

# Feature: jarvis-nvidia-skill-pack, Task 23.1 — runtime/text_normalize.py

from __future__ import annotations

import pytest

from runtime.text_normalize import normalize_tr


@pytest.mark.unit
class TestNormalizeTr:
    """:func:`normalize_tr` davranış spesifikasyonu."""

    def test_empty_string_returns_empty(self) -> None:
        assert normalize_tr("") == ""

    def test_pure_ascii_lowercase_passthrough(self) -> None:
        assert normalize_tr("hello world") == "hello world"

    def test_pure_ascii_uppercase_casefolds(self) -> None:
        assert normalize_tr("HELLO WORLD") == "hello world"

    def test_turkish_lowercase_to_ascii(self) -> None:
        assert normalize_tr("şarkı çal") == "sarki cal"
        assert normalize_tr("önemli görev") == "onemli gorev"
        assert normalize_tr("ığdır") == "igdir"

    def test_turkish_uppercase_to_ascii(self) -> None:
        # ``İ → i`` ve ``I → i`` (Türkçe noktasız büyük I) birlikte test edilir.
        assert normalize_tr("İSTANBUL") == "istanbul"
        assert normalize_tr("ÇALMA") == "calma"
        assert normalize_tr("GÖREV") == "gorev"
        assert normalize_tr("ÜYELER") == "uyeler"
        assert normalize_tr("ŞARKI") == "sarki"

    def test_alnum_only_kept_punctuation_to_space(self) -> None:
        # Tek bir satırda hem noktalama hem boşluk farklılıkları test edilir.
        assert normalize_tr("Şarkı, Çal!") == "sarki cal"
        assert normalize_tr("hello-world") == "hello world"
        assert normalize_tr("foo/bar?baz") == "foo bar baz"

    def test_consecutive_separators_collapse(self) -> None:
        assert normalize_tr("a   b") == "a b"
        assert normalize_tr("a---b...c") == "a b c"
        assert normalize_tr("   leading and trailing   ") == "leading and trailing"

    def test_digits_are_kept(self) -> None:
        assert normalize_tr("Top 10 Şarkı") == "top 10 sarki"
        assert normalize_tr("123") == "123"

    def test_idempotent(self) -> None:
        cases = [
            "",
            "Şarkı Çal!",
            "İstanbul'un Önemli Yerleri",
            "ığdır şehri",
            "  boşluklar  içinde  ",
            "ÇĞİÖŞÜ-çğıöşü",
            "Top 10 ŞARKI listesi",
            "alphanum 123 only",
        ]
        for s in cases:
            once = normalize_tr(s)
            twice = normalize_tr(once)
            assert once == twice, f"normalize_tr not idempotent for {s!r}: {once!r} != {twice!r}"

    def test_output_only_contains_lowercase_alnum_and_space(self) -> None:
        cases = [
            "Şarkı, Çal!",
            "İstanbul'un Önemli Yerleri",
            "Top 10 ŞARKI listesi",
            "ÇĞİÖŞÜ",
        ]
        for s in cases:
            out = normalize_tr(s)
            for ch in out:
                assert ch.isascii(), f"non-ASCII char {ch!r} in output {out!r}"
                assert ch == " " or ch.isalnum(), (
                    f"non-alnum/space char {ch!r} in output {out!r}"
                )
                if ch.isalpha():
                    assert ch == ch.lower(), f"non-lowercase {ch!r} in output {out!r}"

    def test_mapping_includes_all_required_pairs(self) -> None:
        # Tasarımın açıkça istediği eşlemeleri tek tek doğrula.
        assert normalize_tr("ı") == "i"
        assert normalize_tr("İ") == "i"
        assert normalize_tr("ç") == "c"
        assert normalize_tr("Ç") == "c"
        assert normalize_tr("ğ") == "g"
        assert normalize_tr("Ğ") == "g"
        assert normalize_tr("ö") == "o"
        assert normalize_tr("Ö") == "o"
        assert normalize_tr("ş") == "s"
        assert normalize_tr("Ş") == "s"
        assert normalize_tr("ü") == "u"
        assert normalize_tr("Ü") == "u"


@pytest.mark.unit
class TestCommandPaletteSearchTurkishMatch:
    """Command_Palette aramasının Türkçe karakter normalizasyonunu kullandığını doğrula.

    Tk pencere oluşturmadan, modül-seviyesindeki ``_fuzzy_score`` fonksiyonu
    üzerinden test edilir. Bu fonksiyon palette.search içinde kullanılır.
    """

    def test_fuzzy_score_matches_turkish_to_ascii_query(self) -> None:
        from ui.palette import _fuzzy_score

        # ASCII sorgu, Türkçe içerikli tool adında 1.0 (substring) skoruna ulaşmalı.
        assert _fuzzy_score("sarki", "Şarkı Çal") == 1.0
        assert _fuzzy_score("istanbul", "İstanbul Hava Durumu") == 1.0
        assert _fuzzy_score("gorev", "Önemli Görev") == 1.0

    def test_fuzzy_score_matches_turkish_query_to_ascii_text(self) -> None:
        from ui.palette import _fuzzy_score

        # Türkçe sorgu, ASCII normalize edilmiş tool adı üzerinde de eşleşir.
        assert _fuzzy_score("Şarkı", "sarki cal") == 1.0
        assert _fuzzy_score("İstanbul", "istanbul hava") == 1.0

    def test_fuzzy_score_empty_query_returns_zero(self) -> None:
        from ui.palette import _fuzzy_score

        assert _fuzzy_score("", "any text") == 0.0
        # Yalnızca noktalama içeren sorgu da normalize sonrası boş olur.
        assert _fuzzy_score("!!!", "any text") == 0.0
