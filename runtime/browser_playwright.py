"""Persistent Playwright browser automation helper for JARVIS."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_config import get_app_config_value
from runtime.browser_timeline import record_browser_action


class PlaywrightBrowserError(RuntimeError):
    """Raised when Playwright browser automation cannot complete."""


def _load_browser_settings() -> dict[str, Any]:
    raw = get_app_config_value("browser_automation", {}) or {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "preferred_channel": str(raw.get("preferred_channel", "chrome") or "chrome").strip(),
        "allow_chromium_fallback": bool(raw.get("allow_chromium_fallback", True)),
        "profile_dir": str(raw.get("profile_dir", "runtime/browser_profile") or "runtime/browser_profile"),
        "headless": bool(raw.get("headless", False)),
        "slow_mo_ms": int(raw.get("slow_mo_ms", 30) or 0),
    }


@dataclass
class BrowserSnapshot:
    url: str
    title: str
    text: str = ""

    def compact(self) -> dict[str, str]:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text[:2000],
        }


class PlaywrightBrowserController:
    """Small persistent browser controller using Playwright sync API."""

    def __init__(
        self,
        *,
        user_data_dir: str | Path | None = None,
        headless: bool | None = None,
        browser_channel: str | None = None,
        allow_chromium_fallback: bool | None = None,
        slow_mo_ms: int | None = None,
    ) -> None:
        settings = _load_browser_settings()
        self.user_data_dir = Path(user_data_dir or settings["profile_dir"])
        self.headless = bool(settings["headless"] if headless is None else headless)
        self.browser_channel = str(settings["preferred_channel"] if browser_channel is None else browser_channel).strip()
        self.allow_chromium_fallback = bool(
            settings["allow_chromium_fallback"]
            if allow_chromium_fallback is None
            else allow_chromium_fallback
        )
        self.slow_mo_ms = max(0, int(settings["slow_mo_ms"] if slow_mo_ms is None else slow_mo_ms))
        self._pw: Any = None
        self._context: Any = None
        self._page: Any = None
        self._channel_used = ""
        self._startup_warning = ""

    def _import_playwright(self) -> Any:
        local_browsers = Path(__file__).resolve().parents[1] / "runtime" / "ms-playwright"
        if local_browsers.exists() and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)
        try:
            from playwright.sync_api import Error, TimeoutError, sync_playwright
        except ImportError as exc:
            raise PlaywrightBrowserError(
                "Playwright kurulu degil. Kurulum: pip install playwright ve "
                "python -m playwright install chromium"
            ) from exc
        return sync_playwright, Error, TimeoutError

    def ensure_page(self) -> Any:
        sync_playwright, Error, _TimeoutError = self._import_playwright()
        try:
            if self._pw is None:
                self.user_data_dir.mkdir(parents=True, exist_ok=True)
                self._pw = sync_playwright().start()
            if self._context is None:
                self._context = self._launch_context(Error)
            if self._page is None or self._page.is_closed():
                pages = [page for page in self._context.pages if not page.is_closed()]
                self._page = pages[0] if pages else self._context.new_page()
            return self._page
        except Error as exc:
            raise PlaywrightBrowserError(
                "Playwright tarayicisi baslatilamadi. Chromium yuklu degilse "
                "python -m playwright install chromium calistirin."
            ) from exc

    def _launch_kwargs(self, channel: str = "") -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "headless": self.headless,
            "slow_mo": self.slow_mo_ms,
            "viewport": {"width": 1366, "height": 768},
        }
        if channel:
            kwargs["channel"] = channel
        return kwargs

    def _launch_context(self, playwright_error_type: Any) -> Any:
        channel = self.browser_channel
        try:
            context = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir),
                **self._launch_kwargs(channel),
            )
            self._channel_used = channel or "chromium"
            self._startup_warning = ""
            return context
        except playwright_error_type as exc:
            if not channel or not self.allow_chromium_fallback:
                raise
            context = self._pw.chromium.launch_persistent_context(
                str(self.user_data_dir),
                **self._launch_kwargs(""),
            )
            self._channel_used = "chromium"
            self._startup_warning = (
                f"Tercih edilen tarayici '{channel}' baslatilamadi; "
                "Playwright Chromium ile devam edildi. Chrome kullanmak genelde "
                "oturumlar ve gundelik web kullanimi icin daha iyi olur."
            )
            return context

    def _runtime_meta(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "browser_channel": self._channel_used or self.browser_channel or "chromium",
            "preferred_channel": self.browser_channel or "chromium",
        }
        if self._startup_warning:
            data["warning"] = self._startup_warning
        return data

    def close(self) -> dict[str, Any]:
        errors: list[str] = []
        for obj_name in ("_context", "_pw"):
            obj = getattr(self, obj_name)
            if obj is None:
                continue
            try:
                if obj_name == "_context":
                    obj.close()
                else:
                    obj.stop()
            except Exception as exc:  # noqa: BLE001
                errors.append(str(exc))
            setattr(self, obj_name, None)
        self._page = None
        return {"ok": not errors, "errors": errors, **self._runtime_meta()}

    def snapshot(self, *, include_text: bool = False) -> BrowserSnapshot:
        page = self.ensure_page()
        text = ""
        if include_text:
            try:
                text = page.locator("body").inner_text(timeout=2500)
            except Exception:
                text = ""
        return BrowserSnapshot(url=str(page.url or ""), title=str(page.title() or ""), text=text)

    def open_url(self, url: str, *, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        if not url:
            raise PlaywrightBrowserError("URL gerekli.")
        final = url if url.startswith(("http://", "https://")) else "https://" + url
        page = self.ensure_page()
        page.goto(final, wait_until=wait_until, timeout=45000)
        snap = self.snapshot(include_text=False)
        return {"ok": True, "action": "open_url", **snap.compact(), **self._runtime_meta()}

    def search(self, query: str) -> dict[str, Any]:
        if not query:
            raise PlaywrightBrowserError("Arama icin query gerekli.")
        url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
        return self.open_url(url)

    def _locator_for(self, selector: str = "", text: str = "", role: str = "") -> Any:
        page = self.ensure_page()
        if selector:
            return page.locator(selector)
        if role and text:
            return page.get_by_role(role, name=text)
        if text:
            return page.get_by_text(text, exact=False)
        raise PlaywrightBrowserError("selector veya text gerekli.")

    def click(
        self,
        *,
        selector: str = "",
        text: str = "",
        role: str = "",
        index: int = 1,
        timeout_ms: int = 8000,
    ) -> dict[str, Any]:
        locator = self._locator_for(selector=selector, text=text, role=role)
        nth = max(0, int(index or 1) - 1)
        target = locator.nth(nth)
        target.scroll_into_view_if_needed(timeout=timeout_ms)
        target.click(timeout=timeout_ms)
        snap = self.snapshot(include_text=False)
        return {"ok": True, "action": "click", "index": nth + 1, **snap.compact(), **self._runtime_meta()}

    def fill(
        self,
        *,
        selector: str = "",
        text: str = "",
        value: str = "",
        role: str = "",
        index: int = 1,
        timeout_ms: int = 8000,
    ) -> dict[str, Any]:
        if not value:
            raise PlaywrightBrowserError("Yazilacak value gerekli.")
        locator = self._locator_for(selector=selector, text=text, role=role)
        nth = max(0, int(index or 1) - 1)
        target = locator.nth(nth)
        target.fill(value, timeout=timeout_ms)
        return {"ok": True, "action": "fill", "index": nth + 1, **self._runtime_meta()}

    def press(self, key: str) -> dict[str, Any]:
        if not key:
            raise PlaywrightBrowserError("Basmak icin key gerekli.")
        page = self.ensure_page()
        page.keyboard.press(key)
        return {"ok": True, "action": "press", "key": key, **self._runtime_meta()}

    def list_links(self, *, limit: int = 40) -> dict[str, Any]:
        page = self.ensure_page()
        max_items = max(1, min(int(limit or 40), 120))
        links = page.locator("a").evaluate_all(
            """(nodes, limit) => nodes.slice(0, limit).map((a, index) => ({
                index: index + 1,
                text: (a.innerText || a.textContent || '').trim().slice(0, 180),
                href: a.href || '',
                aria: a.getAttribute('aria-label') || ''
            })).filter(item => item.text || item.aria || item.href)""",
            max_items,
        )
        snap = self.snapshot(include_text=False)
        return {"ok": True, "action": "list_links", "links": links, **snap.compact(), **self._runtime_meta()}

    @staticmethod
    def _score_candidate(candidate: dict[str, Any], query: str) -> int:
        q = (query or "").strip().lower()
        if not q:
            return 1
        hay = " ".join(
            str(candidate.get(key, "") or "")
            for key in ("text", "aria", "placeholder", "title", "role", "tag", "selector")
        ).lower()
        score = 0
        if q in hay:
            score += 50
        for token in re.findall(r"\w+", q, flags=re.UNICODE):
            if token and token in hay:
                score += 10
        if str(candidate.get("text", "") or "").strip().lower() == q:
            score += 30
        return score

    def find_elements(self, *, query: str = "", limit: int = 40) -> dict[str, Any]:
        page = self.ensure_page()
        max_items = max(1, min(int(limit or 40), 150))
        candidates = page.evaluate(
            """(limit) => {
                const selector = [
                    'a', 'button', 'input', 'textarea', 'select',
                    '[role]', '[aria-label]', '[placeholder]', '[title]',
                    '[data-testid]', '[data-test]', '[data-cy]'
                ].join(',');
                const cssPath = (el) => {
                    if (!el || !el.tagName) return '';
                    if (el.id) return `#${CSS.escape(el.id)}`;
                    const testId = el.getAttribute('data-testid') || el.getAttribute('data-test') || el.getAttribute('data-cy');
                    if (testId) return `[data-testid="${CSS.escape(testId)}"], [data-test="${CSS.escape(testId)}"], [data-cy="${CSS.escape(testId)}"]`;
                    const parts = [];
                    let node = el;
                    while (node && node.nodeType === 1 && parts.length < 4) {
                        let part = node.tagName.toLowerCase();
                        const cls = (node.className || '').toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2);
                        if (cls.length) part += cls.map(c => '.' + CSS.escape(c)).join('');
                        const parent = node.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children).filter(child => child.tagName === node.tagName);
                            if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
                        }
                        parts.unshift(part);
                        node = parent;
                    }
                    return parts.join(' > ');
                };
                return Array.from(document.querySelectorAll(selector)).slice(0, limit * 4).map((el, index) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        index: index + 1,
                        tag: el.tagName.toLowerCase(),
                        role: el.getAttribute('role') || '',
                        text: (el.innerText || el.textContent || el.value || '').trim().slice(0, 240),
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        title: el.getAttribute('title') || '',
                        href: el.href || '',
                        selector: cssPath(el),
                        visible: rect.width > 0 && rect.height > 0,
                        rect: {x: Math.round(rect.x), y: Math.round(rect.y), width: Math.round(rect.width), height: Math.round(rect.height)}
                    };
                }).filter(item => item.visible && (item.text || item.aria || item.placeholder || item.title || item.href || item.selector));
            }""",
            max_items,
        )
        scored = []
        for item in candidates:
            score = self._score_candidate(item, query)
            if query and score <= 0:
                continue
            item["score"] = score
            scored.append(item)
        scored.sort(key=lambda item: (-int(item.get("score") or 0), int(item.get("index") or 0)))
        snap = self.snapshot(include_text=False)
        return {
            "ok": True,
            "action": "find_elements",
            "query": query,
            "count": len(scored),
            "elements": scored[:max_items],
            **snap.compact(),
            **self._runtime_meta(),
        }

    def click_smart(
        self,
        *,
        query: str = "",
        selector: str = "",
        text: str = "",
        role: str = "",
        index: int = 1,
        timeout_ms: int = 8000,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        target_text = text or query
        idx = max(1, int(index or 1))

        strategies: list[tuple[str, dict[str, Any]]] = []
        if selector:
            strategies.append(("selector", {"selector": selector}))
        if role and target_text:
            strategies.append(("role", {"role": role, "text": target_text}))
        if target_text:
            strategies.append(("text", {"text": target_text}))

        for strategy, kwargs in strategies:
            try:
                result = self.click(index=idx, timeout_ms=timeout_ms, **kwargs)
                result["action"] = "click_smart"
                result["strategy"] = strategy
                result["attempts"] = attempts + [{"strategy": strategy, "ok": True}]
                result["selector"] = kwargs.get("selector", "")
                result["target"] = target_text
                return result
            except Exception as exc:  # noqa: BLE001
                attempts.append({"strategy": strategy, "ok": False, "error": str(exc)[:240]})

        found = self.find_elements(query=target_text, limit=max(20, idx + 5))
        elements = found.get("elements") or []
        if len(elements) >= idx:
            element = elements[idx - 1]
            generated_selector = str(element.get("selector") or "")
            if generated_selector:
                try:
                    result = self.click(selector=generated_selector, index=1, timeout_ms=timeout_ms)
                    result["action"] = "click_smart"
                    result["strategy"] = "generated_selector"
                    result["attempts"] = attempts + [{"strategy": "generated_selector", "ok": True}]
                    result["selector"] = generated_selector
                    result["target"] = target_text
                    result["matched"] = element
                    return result
                except Exception as exc:  # noqa: BLE001
                    attempts.append({"strategy": "generated_selector", "ok": False, "error": str(exc)[:240]})

        return {
            "ok": False,
            "action": "click_smart",
            "target": target_text,
            "attempts": attempts,
            "candidates": elements[:10],
            "error": "Hedef Playwright ile bulunamadi veya tiklanamadi.",
            **self._runtime_meta(),
        }

    def extract_text(self, *, limit_chars: int = 4000) -> dict[str, Any]:
        max_chars = max(200, min(int(limit_chars or 4000), 20000))
        snap = self.snapshot(include_text=True)
        payload = snap.compact()
        payload["text"] = snap.text[:max_chars]
        return {"ok": True, "action": "extract_text", **payload, **self._runtime_meta()}

    @staticmethod
    def _contains(haystack: str, needle: str) -> bool:
        return str(needle or "").strip().lower() in str(haystack or "").lower()

    @staticmethod
    def _first_locator(locator: Any) -> Any:
        first = getattr(locator, "first")
        return first() if callable(first) else first

    def verify(
        self,
        *,
        url_contains: str = "",
        title_contains: str = "",
        text_contains: str = "",
        selector: str = "",
        target: str = "",
        query: str = "",
        timeout_ms: int = 3000,
    ) -> dict[str, Any]:
        """Verify the current browser state after an action."""
        checks: list[dict[str, Any]] = []
        needs_text = bool(text_contains or target or query)
        snap = self.snapshot(include_text=needs_text)
        timeout = max(500, min(int(timeout_ms or 3000), 15000))

        if url_contains:
            checks.append(
                {
                    "name": "url_contains",
                    "expected": url_contains,
                    "actual": snap.url,
                    "ok": self._contains(snap.url, url_contains),
                }
            )
        if title_contains:
            checks.append(
                {
                    "name": "title_contains",
                    "expected": title_contains,
                    "actual": snap.title,
                    "ok": self._contains(snap.title, title_contains),
                }
            )
        if text_contains:
            checks.append(
                {
                    "name": "text_contains",
                    "expected": text_contains,
                    "actual": snap.text[:500],
                    "ok": self._contains(snap.text, text_contains),
                }
            )
        if selector:
            visible = False
            error = ""
            try:
                visible = bool(self._first_locator(self._locator_for(selector=selector)).is_visible(timeout=timeout))
            except Exception as exc:  # noqa: BLE001
                error = str(exc)[:240]
            item: dict[str, Any] = {
                "name": "selector_visible",
                "expected": selector,
                "actual": "visible" if visible else "not_visible",
                "ok": visible,
            }
            if error:
                item["error"] = error
            checks.append(item)

        target_text = target or query
        if target_text:
            visible = False
            error = ""
            try:
                visible = bool(self._first_locator(self._locator_for(text=target_text)).is_visible(timeout=timeout))
            except Exception as exc:  # noqa: BLE001
                error = str(exc)[:240]
            if not visible:
                try:
                    found = self.find_elements(query=target_text, limit=10)
                    visible = int(found.get("count") or 0) > 0
                except Exception as exc:  # noqa: BLE001
                    if not error:
                        error = str(exc)[:240]
            item = {
                "name": "target_visible",
                "expected": target_text,
                "actual": "visible" if visible else "not_visible",
                "ok": visible,
            }
            if error and not visible:
                item["error"] = error
            checks.append(item)

        if not checks:
            checks.append({"name": "page_loaded", "expected": "url or title", "actual": snap.url or snap.title, "ok": bool(snap.url or snap.title)})

        ok = all(bool(item.get("ok")) for item in checks)
        return {
            "ok": ok,
            "verified": ok,
            "action": "verify",
            "checks": checks,
            **snap.compact(),
            **self._runtime_meta(),
        }

    def evaluate(self, script: str) -> dict[str, Any]:
        if not script:
            raise PlaywrightBrowserError("Calistirilacak script gerekli.")
        page = self.ensure_page()
        result = page.evaluate(script)
        return {"ok": True, "action": "evaluate", "result": result, **self._runtime_meta()}

    def run(self, action: str, **kwargs: Any) -> dict[str, Any]:
        act = (action or "").strip().lower()
        if act == "open_url":
            return self.open_url(str(kwargs.get("url") or ""))
        if act == "search":
            return self.search(str(kwargs.get("query") or kwargs.get("text") or ""))
        if act in {"click", "click_text", "click_selector"}:
            return self.click(
                selector=str(kwargs.get("selector") or ""),
                text=str(kwargs.get("text") or kwargs.get("target") or ""),
                role=str(kwargs.get("role") or ""),
                index=int(kwargs.get("index") or 1),
            )
        if act == "fill":
            return self.fill(
                selector=str(kwargs.get("selector") or ""),
                text=str(kwargs.get("target") or ""),
                value=str(kwargs.get("text") or kwargs.get("value") or ""),
                role=str(kwargs.get("role") or ""),
                index=int(kwargs.get("index") or 1),
            )
        if act in {"submit", "enter"}:
            return self.press("Enter")
        if act == "press":
            return self.press(str(kwargs.get("key") or kwargs.get("text") or ""))
        if act in {"list_links", "links"}:
            return self.list_links(limit=int(kwargs.get("limit") or 40))
        if act in {"find_elements", "find"}:
            return self.find_elements(
                query=str(kwargs.get("query") or kwargs.get("target") or kwargs.get("text") or ""),
                limit=int(kwargs.get("limit") or 40),
            )
        if act in {"click_smart", "smart_click"}:
            return self.click_smart(
                query=str(kwargs.get("query") or kwargs.get("target") or ""),
                selector=str(kwargs.get("selector") or ""),
                text=str(kwargs.get("text") or ""),
                role=str(kwargs.get("role") or ""),
                index=int(kwargs.get("index") or 1),
            )
        if act in {"extract_text", "read_page"}:
            return self.extract_text(limit_chars=int(kwargs.get("limit_chars") or 4000))
        if act in {"verify", "verify_state", "assert"}:
            return self.verify(
                url_contains=str(kwargs.get("url_contains") or ""),
                title_contains=str(kwargs.get("title_contains") or ""),
                text_contains=str(kwargs.get("text_contains") or ""),
                selector=str(kwargs.get("selector") or ""),
                target=str(kwargs.get("target") or ""),
                query=str(kwargs.get("query") or ""),
                timeout_ms=int(kwargs.get("timeout_ms") or 3000),
            )
        if act == "evaluate":
            return self.evaluate(str(kwargs.get("script") or ""))
        if act == "snapshot":
            return {"ok": True, "action": "snapshot", **self.snapshot(include_text=True).compact(), **self._runtime_meta()}
        if act == "close":
            return self.close()
        raise PlaywrightBrowserError(f"Bilinmeyen Playwright eylemi: {act}")


_DEFAULT_CONTROLLER: PlaywrightBrowserController | None = None


def get_default_controller() -> PlaywrightBrowserController:
    global _DEFAULT_CONTROLLER
    if _DEFAULT_CONTROLLER is None:
        _DEFAULT_CONTROLLER = PlaywrightBrowserController()
    return _DEFAULT_CONTROLLER


def run_browser_action(action: str, **kwargs: Any) -> str:
    data: dict[str, Any]
    try:
        data = get_default_controller().run(action, **kwargs)
    except PlaywrightBrowserError as exc:
        data = {"ok": False, "error": str(exc), "source": "playwright"}
        record_browser_action(action=action, ok=False, args=kwargs, result=data, error=str(exc))
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        data = {"ok": False, "error": f"Playwright automation basarisiz: {exc}", "source": "playwright"}
        record_browser_action(action=action, ok=False, args=kwargs, result=data, error=str(exc))
        return json.dumps(data, ensure_ascii=False)
    data["source"] = "playwright"
    record_browser_action(action=str(data.get("action") or action), ok=bool(data.get("ok")), args=kwargs, result=data)
    return json.dumps(data, ensure_ascii=False)


__all__ = [
    "BrowserSnapshot",
    "PlaywrightBrowserController",
    "PlaywrightBrowserError",
    "get_default_controller",
    "run_browser_action",
]
