from __future__ import annotations

import json
from pathlib import Path

import pytest

from skills.computer_control import tools
from skills.computer_control.__skill__ import MANIFEST
from runtime.plugin_host import PluginHost


def test_selector_memory_save_get_list_forget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "selectors.json"
    monkeypatch.setattr(tools, "_STORE_PATH", store_path)

    assert "kaydedildi" in tools.selector_memory(
        "save",
        name="login_button",
        target="Giris yap",
        app="chrome",
        strategy="vision_text",
    )

    raw = tools.selector_memory("get", name="login_button", app="chrome")
    data = json.loads(raw)
    assert data["name"] == "login_button"
    assert data["target"] == "Giris yap"

    listed = tools.selector_memory("list")
    assert "login_button" in listed

    forgotten = tools.selector_memory("forget", name="login_button")
    assert "1 selector" in forgotten
    assert "bulunamadi" in tools.selector_memory("get", name="login_button")


def test_extract_json_object_from_fenced_text() -> None:
    parsed = tools._extract_json_object('```json\n{"elements":[{"label":"OK"}]}\n```')
    assert parsed["elements"][0]["label"] == "OK"


def test_detect_screen_elements_wraps_model_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fake")

    monkeypatch.setattr(
        tools,
        "_capture",
        lambda capture="screen": tools._CaptureResult(
            path=image_path,
            bounds={"left": 0, "top": 0, "right": 100, "bottom": 100},
        ),
    )
    monkeypatch.setattr(
        tools,
        "_gemini_vision_text",
        lambda *_args, **_kwargs: '{"elements":[{"label":"Search","type":"input","x":10,"y":20}]}',
    )
    monkeypatch.setattr(tools, "_delete_capture", lambda _cap: None)

    result = json.loads(tools.detect_screen_elements(query="search", provider="gemini", allow_cloud=True))
    assert result["elements"][0]["label"] == "Search"


def test_screen_ocr_returns_model_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"fake")
    monkeypatch.setattr(
        tools,
        "_capture",
        lambda capture="screen": tools._CaptureResult(path=image_path, bounds={}),
    )
    monkeypatch.setattr(tools, "_gemini_vision_text", lambda *_args, **_kwargs: "Merhaba")
    monkeypatch.setattr(tools, "_delete_capture", lambda _cap: None)
    assert tools.screen_ocr(provider="gemini", allow_cloud=True) == "Merhaba"


def test_screen_ocr_prefers_uia_without_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tools, "_uia_text_lines", lambda _focus="": ["Button: Tamam"])

    def _should_not_capture(*_args, **_kwargs):
        raise AssertionError("Gemini/capture path should not run")

    monkeypatch.setattr(tools, "_capture", _should_not_capture)
    assert tools.screen_ocr() == "Button: Tamam"


def test_detect_screen_elements_prefers_uia_without_cloud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        tools,
        "_uia_collect",
        lambda max_depth=7, limit=250: [
            {
                "name": "Tamam",
                "type": "ButtonControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 1, "top": 2, "right": 30, "bottom": 40},
                "center": {"x": 15, "y": 21},
                "_control": object(),
            }
        ],
    )
    result = json.loads(tools.detect_screen_elements(query="tamam"))
    assert result["source"] == "windows_uia"
    assert result["elements"][0]["name"] == "Tamam"


def test_computer_control_manifest_loads() -> None:
    descriptors = PluginHost().load(MANIFEST)
    names = {item.name for item in descriptors}
    assert {
        "mouse_control",
        "screen_ocr",
        "detect_screen_elements",
        "window_tracking",
        "ui_automation",
        "browser_automation",
        "self_healing_click",
        "selector_memory",
    }.issubset(names)


def test_ui_automation_clicks_nth_match(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked: list[str] = []

    class _Control:
        def __init__(self, name: str, top: int) -> None:
            self.Name = name
            self.ControlTypeName = "HyperlinkControl"
            self.AutomationId = ""
            self.ClassName = ""
            self.BoundingRectangle = type(
                "Rect",
                (),
                {"left": 10, "top": top, "right": 210, "bottom": top + 60},
            )()

        def Click(self) -> None:
            clicked.append(self.Name)

    monkeypatch.setattr(
        tools,
        "_uia_collect",
        lambda max_depth=6, limit=250: [
            {
                "name": "Video 1",
                "type": "HyperlinkControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 10, "top": 0, "right": 210, "bottom": 60},
                "center": {"x": 110, "y": 30},
                "_control": _Control("Video 1", 0),
            },
            {
                "name": "Video 2",
                "type": "HyperlinkControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 10, "top": 70, "right": 210, "bottom": 130},
                "center": {"x": 110, "y": 100},
                "_control": _Control("Video 2", 70),
            },
            {
                "name": "Video 3",
                "type": "HyperlinkControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 10, "top": 140, "right": 210, "bottom": 200},
                "center": {"x": 110, "y": 170},
                "_control": _Control("Video 3", 140),
            },
        ],
    )

    result = json.loads(tools.ui_automation("click", query="video", index=3, control_type="Hyperlink"))
    assert result["ok"] is True
    assert clicked == ["Video 3"]
    assert result["geometry"]["global"] == {"x": 110, "y": 170}


def test_human_mouse_path_starts_after_origin_and_ends_on_target() -> None:
    path = tools._human_mouse_path((0, 0), (100, 50), steps=10)

    assert path
    assert path[-1] == (100, 50)
    assert len(path) >= 2
    assert all(isinstance(x, int) and isinstance(y, int) for x, y in path)


def test_self_healing_click_saves_selector_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "selectors.json"
    monkeypatch.setattr(tools, "_STORE_PATH", store_path)

    def _fake_ui_automation(*_args, **_kwargs):
        return json.dumps(
            {
                "ok": True,
                "clicked": {"name": "Play"},
                "x": 10,
                "y": 20,
                "source": "windows_uia",
            }
        )

    monkeypatch.setattr(tools, "ui_automation", _fake_ui_automation)

    result = json.loads(tools.self_healing_click(target="Play", selector_name="play_button", app="chrome"))
    assert result["ok"] is True

    saved = json.loads(tools.selector_memory("get", name="play_button", app="chrome"))
    assert saved["target"] == "Play"
    assert saved["strategy"] == "uia_text"


def test_self_healing_click_retries_with_any_control_type(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _fake_ui_automation(action, query="", index=1, control_type="any", **_kwargs):
        calls.append({"action": action, "query": query, "index": index, "control_type": control_type})
        if len(calls) == 1:
            return json.dumps({"ok": False, "error": "not found"})
        return json.dumps({"ok": True, "clicked": {"name": query}})

    monkeypatch.setattr(tools, "ui_automation", _fake_ui_automation)

    result = json.loads(tools.self_healing_click(target="Send", control_type="Button", retries=2))
    assert result["ok"] is True
    assert calls[0]["control_type"] == "Button"
    assert calls[1]["control_type"] == "any"


def test_steam_click_update_filters_false_positive_text(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked: list[tuple[int, int]] = []

    monkeypatch.setattr(
        tools,
        "_find_window_rect_by_title",
        lambda _title: {
            "hwnd": 10,
            "title": "Steam",
            "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1032},
        },
    )
    monkeypatch.setattr(tools, "_focus_window", lambda _hwnd, maximize=False: None)
    monkeypatch.setattr(tools, "_open_steam_game_page", lambda _game: "steam://nav/games/details/730")
    monkeypatch.setattr(
        tools,
        "get_active_window_info",
        lambda: {"ok": True, "rect": {"left": 0, "top": 0, "right": 1920, "bottom": 1032}},
    )
    monkeypatch.setattr(
        tools,
        "_uia_collect",
        lambda max_depth=8, limit=250: [
            {
                "name": "Counter-Strike 2 - Guncelleme Kuyrugunda",
                "type": "TextControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 360, "top": 650, "right": 680, "bottom": 710},
            },
            {
                "name": "GÜNCELLE",
                "type": "TextControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 294, "top": 409, "right": 486, "bottom": 455},
            },
        ],
    )

    class _PyAutoGui:
        def click(self) -> None:
            clicked.append(("click", "left"))  # type: ignore[arg-type]

    monkeypatch.setattr(tools, "_load_pyautogui", lambda: _PyAutoGui())
    monkeypatch.setattr(tools, "_move_mouse", lambda _pg, x, y, **_kwargs: clicked.append((x, y)))

    result = json.loads(tools.steam_click_update_button("Counter-Strike 2"))

    assert result["ok"] is True
    assert result["strategy"] == "windows_uia_filtered_physical_click"
    assert result["clicked"]["name"] == "GÜNCELLE"
    assert (390, 432) in clicked
    false_attempt = [item for item in result["attempts"] if item.get("name") == "Counter-Strike 2 - Guncelleme Kuyrugunda"][0]
    assert false_attempt["accepted"] is False
    assert "butonu degil" in false_attempt["reason"]


def test_steam_click_update_opens_game_page_and_maximizes_before_click(monkeypatch: pytest.MonkeyPatch) -> None:
    clicked: list[tuple[int, int] | tuple[str, str]] = []
    focus_calls: list[tuple[int, bool]] = []
    collect_calls = {"count": 0}
    current_rect = {"left": 0, "top": 0, "right": 820, "bottom": 520}

    def _fake_find_window(_title: str):
        return {"hwnd": 10, "title": "Steam", "rect": dict(current_rect)}

    def _fake_focus(hwnd: int, maximize: bool = False) -> None:
        focus_calls.append((hwnd, maximize))
        if maximize:
            current_rect.update({"left": 0, "top": 0, "right": 1920, "bottom": 1032})

    def _fake_uia_collect(max_depth=8, limit=250):
        collect_calls["count"] += 1
        return [
            {
                "name": "GUNCELLE",
                "type": "ButtonControl",
                "automation_id": "",
                "class_name": "",
                "rect": {"left": 294, "top": 409, "right": 486, "bottom": 455},
            }
        ]

    monkeypatch.setattr(tools, "_find_window_rect_by_title", _fake_find_window)
    monkeypatch.setattr(tools, "_focus_window", _fake_focus)
    monkeypatch.setattr(tools, "_open_steam_game_page", lambda _game: "steam://nav/games/details/730")
    monkeypatch.setattr(tools, "get_active_window_info", lambda: {"ok": True, "rect": dict(current_rect)})
    monkeypatch.setattr(tools, "_uia_collect", _fake_uia_collect)

    class _PyAutoGui:
        def click(self) -> None:
            clicked.append(("click", "left"))

    monkeypatch.setattr(tools, "_load_pyautogui", lambda: _PyAutoGui())
    monkeypatch.setattr(tools, "_move_mouse", lambda _pg, x, y, **_kwargs: clicked.append((x, y)))

    result = json.loads(tools.steam_click_update_button("Counter-Strike 2"))

    assert result["ok"] is True
    assert result["strategy"] == "windows_uia_filtered_physical_click"
    assert result["clicked"]["name"] == "GUNCELLE"
    assert collect_calls["count"] == 1
    assert (10, True) in focus_calls
    assert (390, 432) in clicked
    assert result["attempts"][0]["stage"] == "open_game_page"


def test_browser_automation_routes_playwright_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _fake_run_browser_action(action: str, **kwargs):
        calls.append({"action": action, **kwargs})
        return json.dumps({"ok": True, "source": "playwright", "action": action})

    import runtime.browser_playwright as browser_playwright

    monkeypatch.setattr(browser_playwright, "run_browser_action", _fake_run_browser_action)

    result = json.loads(
        tools.browser_automation(
            "click",
            engine="playwright",
            text="Giris yap",
            role="button",
            index=2,
        )
    )

    assert result["ok"] is True
    assert calls[0]["action"] == "click"
    assert calls[0]["text"] == "Giris yap"
    assert calls[0]["role"] == "button"
    assert calls[0]["index"] == 2


def test_browser_automation_new_tab_uses_active_window(monkeypatch: pytest.MonkeyPatch) -> None:
    hotkeys: list[tuple[str, ...]] = []

    class _PyAutoGui:
        def hotkey(self, *keys: str) -> None:
            hotkeys.append(keys)

    monkeypatch.setattr(tools, "_load_pyautogui", lambda: _PyAutoGui())

    result = tools.browser_automation("new_tab")

    assert "new_tab" in result
    assert hotkeys == [("ctrl", "t")]


def test_browser_automation_opens_url_in_named_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[list[str]] = []

    monkeypatch.setattr(tools, "_load_pyautogui", lambda: object())
    monkeypatch.setattr(tools, "_browser_executable", lambda browser: "C:\\Browsers\\opera.exe")
    monkeypatch.setattr(
        tools.subprocess,
        "Popen",
        lambda args, **_kwargs: launched.append(args),
    )

    result = tools.browser_automation("open_url", url="youtube.com", browser="opera")

    assert "opera ile acildi" in result
    assert launched == [["C:\\Browsers\\opera.exe", "https://youtube.com"]]


def test_browser_automation_saves_playwright_smart_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store_path = tmp_path / "selectors.json"
    monkeypatch.setattr(tools, "_STORE_PATH", store_path)

    def _fake_run_browser_action(action: str, **kwargs):
        return json.dumps(
            {
                "ok": True,
                "source": "playwright",
                "action": action,
                "selector": "button.play",
                "target": kwargs.get("target") or kwargs.get("query"),
                "url": "https://example.com",
            }
        )

    import runtime.browser_playwright as browser_playwright

    monkeypatch.setattr(browser_playwright, "run_browser_action", _fake_run_browser_action)

    result = json.loads(
        tools.browser_automation(
            "click_smart",
            engine="playwright",
            target="Play",
            selector_name="play_button",
        )
    )

    assert result["ok"] is True
    saved = json.loads(tools.selector_memory("get", name="play_button", app="browser", url="https://example.com"))
    assert saved["selector"] == "button.play"
    assert saved["strategy"] == "playwright_smart"


def test_browser_automation_reads_playwright_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    import runtime.browser_timeline as browser_timeline

    monkeypatch.setattr(
        browser_timeline,
        "read_browser_timeline",
        lambda limit=20: {"ok": True, "count": 1, "items": [{"action": "open_url"}]},
    )

    result = json.loads(tools.browser_automation("timeline", engine="playwright", limit=5))

    assert result["ok"] is True
    assert result["count"] == 1
    assert result["items"][0]["action"] == "open_url"


def test_browser_automation_routes_playwright_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict] = []

    def _fake_run_browser_action(action: str, **kwargs):
        calls.append({"action": action, **kwargs})
        return json.dumps({"ok": True, "source": "playwright", "action": action, "verified": True})

    import runtime.browser_playwright as browser_playwright

    monkeypatch.setattr(browser_playwright, "run_browser_action", _fake_run_browser_action)

    result = json.loads(
        tools.browser_automation(
            "verify",
            engine="playwright",
            url_contains="youtube.com",
            title_contains="YouTube",
            text_contains="Ara",
            selector="input[name='search_query']",
            timeout_ms=1200,
        )
    )

    assert result["verified"] is True
    assert calls[0]["action"] == "verify"
    assert calls[0]["url_contains"] == "youtube.com"
    assert calls[0]["title_contains"] == "YouTube"
    assert calls[0]["text_contains"] == "Ara"
    assert calls[0]["selector"] == "input[name='search_query']"
    assert calls[0]["timeout_ms"] == 1200
