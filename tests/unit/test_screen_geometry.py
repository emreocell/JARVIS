from __future__ import annotations

from runtime import screen_geometry


def test_normalize_point_with_fake_monitor(monkeypatch) -> None:
    monkeypatch.setattr(
        screen_geometry,
        "get_monitors",
        lambda: [
            {
                "index": 0,
                "primary": True,
                "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1080, "width": 1920, "height": 1080},
                "work_area": {"left": 0, "top": 0, "right": 1920, "bottom": 1040, "width": 1920, "height": 1040},
                "center": {"x": 960, "y": 540},
            }
        ],
    )

    payload = screen_geometry.normalize_point(960, 540)

    assert payload["global"] == {"x": 960, "y": 540}
    assert payload["monitor"]["index"] == 0
    assert payload["monitor_local"]["x_ratio"] == 0.5
    assert payload["monitor_local"]["y_ratio"] == 0.5


def test_normalize_point_with_bounds(monkeypatch) -> None:
    monkeypatch.setattr(screen_geometry, "get_monitors", lambda: [])
    payload = screen_geometry.normalize_point(150, 75, {"left": 100, "top": 50, "right": 300, "bottom": 150})

    assert payload["bounds_local"]["x"] == 50
    assert payload["bounds_local"]["y"] == 25
    assert payload["bounds_local"]["x_ratio"] == 0.25
    assert payload["bounds_local"]["y_ratio"] == 0.25
