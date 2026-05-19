from __future__ import annotations

from runtime.interruption import (
    intent_requests_interrupt,
    looks_like_interrupt,
    parse_intent_json,
    strip_wake_word,
)


def test_looks_like_interrupt_for_short_stop_commands() -> None:
    assert looks_like_interrupt("dur")
    assert looks_like_interrupt("hayır")
    assert looks_like_interrupt("yanlış jarvis")
    assert looks_like_interrupt("sus lütfen")
    assert looks_like_interrupt("stop")
    assert not looks_like_interrupt("spotify ac")


def test_strip_wake_word_from_prefix() -> None:
    command, had_wake = strip_wake_word("Hey Jarvis Spotify ac")
    assert had_wake is True
    assert command == "spotify ac"


def test_intent_requests_interrupt_from_router_json() -> None:
    data = parse_intent_json('{"category":"interrupt","should_interrupt":false}')
    assert intent_requests_interrupt(data) is True


def test_parse_intent_json_handles_bad_text() -> None:
    assert parse_intent_json("not-json") == {}
