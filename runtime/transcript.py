"""Helpers for cleaning streaming Live API transcripts."""

from __future__ import annotations

import re


CONTROL_TOKEN_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

_SHORT_FRAGMENT_MAX = 6
_TR_SUFFIXISH_PREFIXES = (
    "da",
    "de",
    "ta",
    "te",
    "ki",
    "kı",
    "ku",
    "kü",
    "la",
    "le",
    "li",
    "lı",
    "lu",
    "lü",
    "nın",
    "nin",
    "nun",
    "nün",
    "dan",
    "den",
    "tan",
    "ten",
    "dır",
    "dir",
    "dur",
    "dür",
    "tığ",
    "tiğ",
    "tuğ",
    "tüğ",
    "um",
    "üm",
    "ım",
    "im",
    "umuz",
    "ümüz",
    "ımız",
    "imiz",
    "umuzu",
    "ümüzü",
    "ımızı",
    "imizi",
    "dığı",
    "diği",
    "duğu",
    "düğü",
    "ında",
    "inde",
    "unda",
    "ünde",
)


def clean_transcript_text(text: str) -> tuple[str, bool]:
    """Return a printable one-line transcript fragment and noise flag."""
    raw = str(text or "")
    had_noise = False
    if CONTROL_TOKEN_RE.search(raw):
        had_noise = True
        raw = CONTROL_TOKEN_RE.sub(" ", raw)
    cleaned = []
    for ch in raw:
        if ch in "\n\r\t" or ord(ch) >= 32:
            cleaned.append(ch)
        else:
            had_noise = True
    normalized = " ".join("".join(cleaned).split())
    return normalized.strip(), had_noise


def _should_join_without_space(previous: str, current: str) -> bool:
    if not previous or not current:
        return False
    if " " in current:
        return False
    prev_tail = previous.rsplit(" ", 1)[-1]
    if len(prev_tail) < 3:
        return False
    if not (prev_tail[-1:].isalpha() and current[:1].islower()):
        return False
    return current.startswith(_TR_SUFFIXISH_PREFIXES)


def join_transcript_fragments(fragments: list[str]) -> str:
    """Join streaming transcript chunks without creating mid-word spaces.

    The Live API can emit partial word fragments such as ``hak`` + ``kında``.
    A plain ``" ".join(...)`` turns those into user-visible text like
    ``hak kında``. This keeps normal word boundaries while stitching short,
    lower-case continuation fragments back onto the previous token.
    """
    result = ""
    for fragment in fragments:
        text, _ = clean_transcript_text(fragment)
        if not text:
            continue
        if not result:
            result = text
            continue

        # Some providers send cumulative partials. Prefer the longer text.
        if text.startswith(result):
            result = text
            continue
        if result.endswith(text):
            continue

        if _should_join_without_space(result, text):
            result += text
        else:
            result += " " + text
    return " ".join(result.split()).strip()


def language_codes_for(system_language: str | None) -> list[str]:
    """Return preferred transcript language codes with English fallback."""
    primary = str(system_language or "tr-TR").strip() or "tr-TR"
    codes = [primary]
    if primary.lower() != "en-us":
        codes.append("en-US")
    return codes


def is_meaningful_transcript(text: str) -> bool:
    """Return True when transcript has at least one letter or digit."""
    return any(ch.isalnum() for ch in str(text or ""))


__all__ = [
    "clean_transcript_text",
    "is_meaningful_transcript",
    "join_transcript_fragments",
    "language_codes_for",
]
