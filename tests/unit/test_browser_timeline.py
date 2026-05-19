from __future__ import annotations

from runtime.browser_timeline import BrowserTimeline


def test_browser_timeline_records_and_reads(tmp_path) -> None:  # noqa: ANN001
    timeline = BrowserTimeline(path=tmp_path / "timeline.jsonl")

    item = timeline.record(
        action="click_smart",
        ok=True,
        args={"target": "Play"},
        result={
            "url": "https://example.com",
            "title": "Example",
            "browser_channel": "chrome",
            "strategy": "text",
            "selector": "button.play",
            "target": "Play",
            "attempts": [{"strategy": "text", "ok": True}],
        },
    )

    assert item["ok"] is True
    assert item["selector"] == "button.play"
    loaded = timeline.read(limit=5)
    assert loaded[0]["action"] == "click_smart"
    assert loaded[0]["strategy"] == "text"


def test_browser_timeline_clear(tmp_path) -> None:  # noqa: ANN001
    timeline = BrowserTimeline(path=tmp_path / "timeline.jsonl")
    timeline.record(action="open_url", ok=True, result={"url": "https://example.com"})

    assert timeline.clear() == 1
    assert timeline.read() == []
