from __future__ import annotations

import json

from runtime.personality_engine import PersonalityEngine


def test_personality_engine_creates_default_prompt_block(tmp_path) -> None:
    engine = PersonalityEngine(tmp_path / "profile.json")
    block = engine.format_for_prompt()

    assert "[JARVIS PERSONALITY ENGINE]" in block
    assert "Tone:" in block
    assert "Emotional state:" in block


def test_personality_engine_updates_frustration_tone(tmp_path) -> None:
    path = tmp_path / "profile.json"
    engine = PersonalityEngine(path)

    profile = engine.observe_user_message("Bu biraz yetersiz, daha fazla kontrol istiyorum")

    assert profile["emotional_state"]["current"] == "frustrated"
    assert profile["tone"]["proactivity"] > 0.70
    assert path.exists()


def test_personality_engine_persists_preferences(tmp_path) -> None:
    path = tmp_path / "profile.json"
    engine = PersonalityEngine(path)
    engine.observe_user_message("HUD tasarimi daha modern olsun")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["preferences"]["likes_modern_ui"] is True


def test_personality_engine_detects_control_interest(tmp_path) -> None:
    engine = PersonalityEngine(tmp_path / "profile.json")
    profile = engine.observe_user_message("Mouse ve ekran kontrolunu gelistirelim")

    assert profile["preferences"]["likes_computer_control"] is True
    assert profile["tone"]["technical_depth"] > 0.66
