from __future__ import annotations

from runtime.transcript import clean_transcript_text, join_transcript_fragments, language_codes_for


def test_clean_transcript_strips_control_tokens() -> None:
    text, had_noise = clean_transcript_text("Merhaba <ctrl12> orada mısın?")
    assert text == "Merhaba orada mısın?"
    assert had_noise is True


def test_join_transcript_fragments_repairs_split_turkish_words() -> None:
    assert (
        join_transcript_fragments(["Ne", "hak", "kında", "konuş", "tuğ", "umuzu"])
        == "Ne hakkında konuştuğumuzu"
    )


def test_join_transcript_fragments_keeps_normal_word_boundaries() -> None:
    assert join_transcript_fragments(["merhaba", "orada", "mısın"]) == "merhaba orada mısın"


def test_language_codes_prefers_system_language_with_english_fallback() -> None:
    assert language_codes_for("tr-TR") == ["tr-TR", "en-US"]
    assert language_codes_for("en-US") == ["en-US"]
