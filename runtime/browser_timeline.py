"""Browser action timeline logging for JARVIS automation."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_PATH = Path(__file__).resolve().parents[1] / "logs" / "browser_timeline.jsonl"


def _safe_preview(value: Any, *, limit: int = 300) -> Any:
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_preview(item, limit=limit) for item in value[:8]]
    if isinstance(value, dict):
        return {str(k): _safe_preview(v, limit=limit) for k, v in list(value.items())[:20]}
    return str(value)[:limit]


@dataclass
class BrowserTimeline:
    path: Path = _DEFAULT_PATH
    max_read: int = 50

    def record(
        self,
        *,
        action: str,
        ok: bool,
        source: str = "playwright",
        args: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        item = {
            "ts": time.time(),
            "action": action,
            "ok": bool(ok),
            "source": source,
            "url": str((result or {}).get("url") or ""),
            "title": str((result or {}).get("title") or ""),
            "browser_channel": str((result or {}).get("browser_channel") or ""),
            "strategy": str((result or {}).get("strategy") or ""),
            "selector": str((result or {}).get("selector") or ""),
            "target": str((result or {}).get("target") or (args or {}).get("target") or ""),
            "error": error or str((result or {}).get("error") or ""),
            "args": _safe_preview(args or {}),
        }
        if result and result.get("attempts"):
            item["attempts"] = _safe_preview(result.get("attempts"))
        if result and result.get("warning"):
            item["warning"] = str(result.get("warning") or "")[:300]

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        return item

    def read(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        max_items = max(1, min(int(limit or self.max_read), 500))
        lines = self.path.read_text(encoding="utf-8").splitlines()
        items: list[dict[str, Any]] = []
        for line in lines[-max_items:]:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                items.append(parsed)
        return items

    def clear(self) -> int:
        count = len(self.read(limit=100000))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")
        return count

    def summary(self, *, limit: int = 20) -> dict[str, Any]:
        items = self.read(limit=limit)
        failures = [item for item in items if not item.get("ok")]
        return {
            "ok": True,
            "count": len(items),
            "failures": len(failures),
            "items": items,
        }


_DEFAULT_TIMELINE = BrowserTimeline()


def record_browser_action(
    *,
    action: str,
    ok: bool,
    source: str = "playwright",
    args: dict[str, Any] | None = None,
    result: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    return _DEFAULT_TIMELINE.record(
        action=action,
        ok=ok,
        source=source,
        args=args,
        result=result,
        error=error,
    )


def read_browser_timeline(*, limit: int = 20) -> dict[str, Any]:
    return _DEFAULT_TIMELINE.summary(limit=limit)


def clear_browser_timeline() -> dict[str, Any]:
    count = _DEFAULT_TIMELINE.clear()
    return {"ok": True, "cleared": count}


__all__ = [
    "BrowserTimeline",
    "record_browser_action",
    "read_browser_timeline",
    "clear_browser_timeline",
]
