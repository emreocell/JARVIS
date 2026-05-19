from __future__ import annotations

import json

import pytest

from runtime import browser_playwright
from runtime.browser_playwright import PlaywrightBrowserController, PlaywrightBrowserError


def test_run_browser_action_reports_missing_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Controller:
        def run(self, action: str, **kwargs):  # noqa: ANN001
            raise PlaywrightBrowserError("Playwright kurulu degil.")

    monkeypatch.setattr(browser_playwright, "get_default_controller", lambda: _Controller())

    result = json.loads(browser_playwright.run_browser_action("open_url", url="example.com"))
    assert result["ok"] is False
    assert result["source"] == "playwright"
    assert "Playwright" in result["error"]


def test_run_browser_action_records_timeline(monkeypatch: pytest.MonkeyPatch) -> None:
    records: list[dict] = []

    class _Controller:
        def run(self, action: str, **kwargs):  # noqa: ANN001
            return {
                "ok": True,
                "action": action,
                "url": "https://example.com",
                "title": "Example",
                "selector": "#ok",
            }

    monkeypatch.setattr(browser_playwright, "get_default_controller", lambda: _Controller())
    monkeypatch.setattr(
        browser_playwright,
        "record_browser_action",
        lambda **kwargs: records.append(kwargs),
    )

    result = json.loads(browser_playwright.run_browser_action("click_smart", target="OK"))

    assert result["ok"] is True
    assert records[0]["action"] == "click_smart"
    assert records[0]["ok"] is True
    assert records[0]["result"]["selector"] == "#ok"


def test_controller_search_builds_google_url(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = PlaywrightBrowserController()
    opened: list[str] = []

    def _fake_open_url(url: str, *, wait_until: str = "domcontentloaded"):
        opened.append(url)
        return {"ok": True, "url": url, "title": ""}

    monkeypatch.setattr(controller, "open_url", _fake_open_url)
    result = controller.search("jarvis test")

    assert result["ok"] is True
    assert "google.com/search" in opened[0]
    assert "jarvis%20test" in opened[0]


def test_controller_run_maps_click_text() -> None:
    controller = PlaywrightBrowserController()
    calls: list[dict] = []

    def _fake_click(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return {"ok": True, "action": "click"}

    controller.click = _fake_click  # type: ignore[method-assign]
    result = controller.run("click_text", text="Play", index=3)

    assert result["ok"] is True
    assert calls[0]["text"] == "Play"
    assert calls[0]["index"] == 3


def test_click_smart_tries_saved_selector_first() -> None:
    controller = PlaywrightBrowserController()
    calls: list[dict] = []

    def _fake_click(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        return {"ok": True, "action": "click", "url": "https://example.com", "title": "Example"}

    controller.click = _fake_click  # type: ignore[method-assign]
    result = controller.click_smart(query="Play", selector="#play", index=2)

    assert result["ok"] is True
    assert result["strategy"] == "selector"
    assert result["selector"] == "#play"
    assert calls[0]["selector"] == "#play"
    assert calls[0]["index"] == 2


def test_click_smart_uses_generated_selector_after_text_failure() -> None:
    controller = PlaywrightBrowserController()
    calls: list[dict] = []

    def _fake_click(**kwargs):  # noqa: ANN003
        calls.append(kwargs)
        if kwargs.get("text"):
            raise RuntimeError("text not found")
        return {"ok": True, "action": "click", "url": "https://example.com", "title": "Example"}

    controller.click = _fake_click  # type: ignore[method-assign]
    controller.find_elements = lambda **_kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "elements": [{"text": "Play video", "selector": "a.video:nth-of-type(3)"}],
    }

    result = controller.click_smart(query="Play video")

    assert result["ok"] is True
    assert result["strategy"] == "generated_selector"
    assert result["selector"] == "a.video:nth-of-type(3)"


def test_controller_verify_passes_expected_state(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = PlaywrightBrowserController()

    class _VisibleLocator:
        def first(self):  # noqa: ANN201
            return self

        def is_visible(self, timeout: int = 0) -> bool:
            return True

    monkeypatch.setattr(
        controller,
        "snapshot",
        lambda include_text=False: browser_playwright.BrowserSnapshot(
            url="https://example.com/watch",
            title="Example Video",
            text="Play video and subscribe" if include_text else "",
        ),
    )
    monkeypatch.setattr(controller, "_locator_for", lambda **_kwargs: _VisibleLocator())

    result = controller.verify(
        url_contains="example.com",
        title_contains="Video",
        text_contains="subscribe",
        selector="button.play",
        target="Play video",
    )

    assert result["ok"] is True
    assert result["verified"] is True
    assert {check["name"] for check in result["checks"]} == {
        "url_contains",
        "title_contains",
        "text_contains",
        "selector_visible",
        "target_visible",
    }


def test_controller_verify_reports_failed_check(monkeypatch: pytest.MonkeyPatch) -> None:
    controller = PlaywrightBrowserController()
    monkeypatch.setattr(
        controller,
        "snapshot",
        lambda include_text=False: browser_playwright.BrowserSnapshot(
            url="https://example.com",
            title="Example",
            text="",
        ),
    )

    result = controller.verify(url_contains="youtube.com")

    assert result["ok"] is False
    assert result["checks"][0]["name"] == "url_contains"
    assert result["checks"][0]["ok"] is False
