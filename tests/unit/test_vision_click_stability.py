"""Stability-focused unit tests for skills/vision click helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

vision_tools = pytest.importorskip("skills.vision.tools")


def _make_png(path: Path, width: int = 100, height: int = 100) -> Path:
    Image.new("RGB", (width, height), color=(32, 32, 32)).save(path, format="PNG")
    return path


def test_normalized_bbox_to_pixel_clamps_edge_values() -> None:
    cx, cy, box = vision_tools._normalized_bbox_to_pixel(
        [0, 0, 1000, 1000], 100, 50, None
    )
    assert box == (0, 0, 99, 49)
    assert 0 <= cx <= 99
    assert 0 <= cy <= 49


def test_coerce_normalized_click_point_supports_fallback_xy() -> None:
    assert vision_tools._coerce_normalized_click_point({"x": 250, "y": 750}) == [750.0, 250.0]


def test_reproject_point_between_bounds_scales_and_moves() -> None:
    moved = vision_tools._reproject_point_between_bounds(
        300,
        300,
        {"left": 100, "top": 100, "right": 500, "bottom": 500},
        {"left": 200, "top": 100, "right": 1000, "bottom": 900},
    )
    assert moved == (600, 500)


def test_click_on_screen_uses_model_click_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = _make_png(tmp_path / "screen.png")

    monkeypatch.setattr(
        vision_tools,
        "_capture_full_virtual_screen",
        lambda: (
            True,
            "",
            {
                "image_path": str(image_path),
                "bounds": {"left": 0, "top": 0, "right": 100, "bottom": 100},
            },
        ),
    )
    monkeypatch.setattr(
        vision_tools,
        "_ask_gemini_for_click_location",
        lambda _target, _path: {
            "found": True,
            "label": "hedef buton",
            "bbox": [100, 100, 900, 900],
            "click_point": [100, 900],
            "confidence": 0.88,
        },
    )
    monkeypatch.setattr(vision_tools, "_refine_click_location", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(vision_tools, "_virtual_screen_bounds", lambda: (0, 0, 5000, 5000))

    clicked: list[tuple[int, int, str, int]] = []
    monkeypatch.setattr(
        vision_tools,
        "_execute_stable_click",
        lambda x, y, button, click_count: clicked.append((x, y, button, click_count)),
    )

    result = vision_tools.click_on_screen("hedef buton", capture="screen")

    assert clicked == [(89, 10, "left", 1)]
    assert "(89,10" in result


def test_click_on_screen_stops_when_active_window_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    image_path = _make_png(tmp_path / "window.png")

    monkeypatch.setattr(
        vision_tools,
        "_capture_active_window",
        lambda: (
            True,
            "",
            {
                "image_path": str(image_path),
                "hwnd": 111,
                "bounds": {"left": 100, "top": 100, "right": 500, "bottom": 500},
            },
        ),
    )
    monkeypatch.setattr(
        vision_tools,
        "_ask_gemini_for_click_location",
        lambda _target, _path: {
            "found": True,
            "label": "hedef",
            "bbox": [200, 200, 400, 400],
            "confidence": 0.9,
        },
    )
    monkeypatch.setattr(vision_tools, "_refine_click_location", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        vision_tools,
        "_get_foreground_window_snapshot",
        lambda: {"hwnd": 222, "left": 100, "top": 100, "right": 500, "bottom": 500},
    )

    called = {"value": False}

    def _fake_click(*_args, **_kwargs) -> None:
        called["value"] = True

    monkeypatch.setattr(vision_tools, "_execute_stable_click", _fake_click)
    monkeypatch.setattr(vision_tools, "_virtual_screen_bounds", lambda: (0, 0, 5000, 5000))

    result = vision_tools.click_on_screen("hedef", capture="active_window")

    assert "Aktif pencere değiştiği için" in result
    assert called["value"] is False
