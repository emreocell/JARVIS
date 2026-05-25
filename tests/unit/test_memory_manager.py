from __future__ import annotations

import json
import logging
from pathlib import Path

from memory import memory_manager


def _set_memory_file(monkeypatch, tmp_path: Path) -> Path:
    memory_file = tmp_path / "memory.json"
    monkeypatch.setattr(memory_manager, "MEMORY_FILE", memory_file)
    return memory_file


def test_load_memory_returns_empty_dict_when_file_missing(monkeypatch, tmp_path: Path):
    _set_memory_file(monkeypatch, tmp_path)

    assert memory_manager.load_memory() == {}


def test_load_memory_quarantines_corrupt_json(monkeypatch, tmp_path: Path, caplog):
    memory_file = _set_memory_file(monkeypatch, tmp_path)
    memory_file.write_text("{not-valid-json", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = memory_manager.load_memory()

    quarantined = list(tmp_path.glob("memory.json.corrupt-*"))
    assert result == {}
    assert not memory_file.exists()
    assert len(quarantined) == 1
    assert "Bellek JSON dosyasi bozuk" in caplog.text
    assert quarantined[0].read_text(encoding="utf-8") == "{not-valid-json"


def test_load_memory_quarantines_non_dict_payload(monkeypatch, tmp_path: Path, caplog):
    memory_file = _set_memory_file(monkeypatch, tmp_path)
    memory_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        result = memory_manager.load_memory()

    quarantined = list(tmp_path.glob("memory.json.corrupt-*"))
    assert result == {}
    assert not memory_file.exists()
    assert len(quarantined) == 1
    assert "dict yerine list iceriyor" in caplog.text


def test_update_memory_merges_existing_content(monkeypatch, tmp_path: Path):
    memory_file = _set_memory_file(monkeypatch, tmp_path)
    memory_file.write_text(
        json.dumps({"preferences": {"theme": "teal"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    memory_manager.update_memory({"preferences": {"voice": "Charon"}})

    saved = json.loads(memory_file.read_text(encoding="utf-8"))
    assert saved == {
        "preferences": {
            "theme": "teal",
            "voice": "Charon",
        }
    }
