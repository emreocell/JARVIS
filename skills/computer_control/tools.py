"""Computer control tools.

This module is the first practical desktop-control layer for JARVIS. It keeps
actions small and inspectable: mouse/keyboard primitives, screen OCR, element
detection, browser automation, and selector memory for self-healing UI targets.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
import re
import shutil
import subprocess
import time
import unicodedata
import urllib.parse
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app_config import get_app_config_value
from runtime.screen_geometry import dump_geometry, get_active_window_info, normalize_point

_STORE_PATH = Path(__file__).resolve().parents[2] / "memory" / "computer_selectors.json"

_BROWSER_ALIASES: dict[str, tuple[str, ...]] = {
    "chrome": ("chrome", "google chrome", "google-chrome"),
    "edge": ("msedge", "edge", "microsoft edge"),
    "opera": ("opera", "opera browser"),
    "opera_gx": ("opera gx", "operagx", "opera gx browser"),
    "firefox": ("firefox", "mozilla firefox"),
    "brave": ("brave", "brave browser"),
}

_BROWSER_COMMON_PATHS: dict[str, tuple[str, ...]] = {
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    ),
    "opera": (
        r"%LocalAppData%\Programs\Opera\opera.exe",
        r"%ProgramFiles%\Opera\opera.exe",
        r"%ProgramFiles(x86)%\Opera\opera.exe",
    ),
    "opera_gx": (
        r"%LocalAppData%\Programs\Opera GX\opera.exe",
        r"%ProgramFiles%\Opera GX\opera.exe",
        r"%ProgramFiles(x86)%\Opera GX\opera.exe",
    ),
    "firefox": (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
        r"%LocalAppData%\Mozilla Firefox\firefox.exe",
    ),
    "brave": (
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%LocalAppData%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
}


def _norm_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold().replace("ı", "i").replace("İ", "i")


@dataclass(frozen=True)
class _CaptureResult:
    path: Path
    bounds: dict[str, int]
    title: str = ""


def _load_pyautogui():
    import pyautogui  # type: ignore[reportMissingImports]

    pyautogui.FAILSAFE = True
    return pyautogui


def _capture(capture: str = "screen") -> _CaptureResult:
    """Capture screen or active window using the existing vision helpers."""
    from skills.vision import tools as vision_tools

    mode = (capture or "screen").strip().lower()
    if mode == "active_window":
        ok, err, payload = vision_tools._capture_active_window()  # noqa: SLF001
        if not ok or not payload:
            raise RuntimeError(err or "Aktif pencere yakalanamadi.")
        return _CaptureResult(
            path=Path(payload["image_path"]),
            bounds={k: int(v) for k, v in (payload.get("bounds") or {}).items()},
            title=str(payload.get("window_title", "") or ""),
        )

    ok, err, payload = vision_tools._capture_full_virtual_screen()  # noqa: SLF001
    if not ok or not payload:
        raise RuntimeError(err or "Ekran yakalanamadi.")
    return _CaptureResult(
        path=Path(payload["image_path"]),
        bounds={k: int(v) for k, v in (payload.get("bounds") or {}).items()},
        title=str(payload.get("window_title", "") or "Sanal Ekran"),
    )


def _delete_capture(result: _CaptureResult | None) -> None:
    if result is None:
        return
    try:
        if result.path.exists():
            result.path.unlink()
    except Exception:
        pass


def _normalize_browser_name(browser: str) -> str:
    raw = str(browser or "").strip().lower().replace("-", "_")
    if not raw:
        return ""
    for canonical, aliases in _BROWSER_ALIASES.items():
        if raw == canonical or raw in {alias.replace("-", "_") for alias in aliases}:
            return canonical
    return raw


def _expand_env_path(path: str) -> Path:
    return Path(os.path.expandvars(path))


def _browser_executable(browser: str) -> str:
    canonical = _normalize_browser_name(browser)
    if not canonical:
        return ""
    for alias in _BROWSER_ALIASES.get(canonical, (canonical,)):
        found = shutil.which(alias)
        if found:
            return found
    for path in _BROWSER_COMMON_PATHS.get(canonical, ()):
        expanded = _expand_env_path(path)
        if expanded.exists():
            return str(expanded)
    return ""


def _open_url_in_browser(url: str, browser: str) -> str:
    final = url if url.startswith(("http://", "https://")) else "https://" + url
    canonical = _normalize_browser_name(browser)
    exe = _browser_executable(canonical)
    if not exe:
        return (
            f"{browser} bulunamadi. URL varsayilan tarayicida acilsin istersen "
            "browser parametresini bos birak veya engine='auto' ile tekrar dene."
        )
    subprocess.Popen(  # noqa: S603
        [exe, final],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return f"{canonical or browser} ile acildi: {final}"


def _gemini_vision_text(image_path: Path, prompt: str, *, max_tokens: int = 1024) -> str:
    """Send a captured image to Gemini vision and return text."""
    from google import genai  # type: ignore[reportMissingImports]
    from google.genai import types  # type: ignore[reportMissingImports]
    from skills.vision import tools as vision_tools

    api_key = str(get_app_config_value("gemini_api_key", "") or "").strip()
    if not api_key:
        raise RuntimeError("Gemini API anahtari eksik; OCR/element detection calisamaz.")

    client = genai.Client(api_key=api_key)
    image_part = vision_tools._build_image_part(image_path)  # noqa: SLF001
    response = client.models.generate_content(
        model="models/gemini-2.5-flash",
        contents=[types.Part.from_text(text=prompt), image_part],
        config=types.GenerateContentConfig(
            temperature=0.0,
            max_output_tokens=max_tokens,
        ),
    )
    text = vision_tools._extract_response_text(response)  # noqa: SLF001
    return str(text or "").strip()


def _extract_json_object(text: str) -> Any:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    start_candidates = [idx for idx in (raw.find("{"), raw.find("[")) if idx >= 0]
    if not start_candidates:
        raise ValueError("JSON bulunamadi.")
    start = min(start_candidates)
    opener = raw[start]
    closer = "}" if opener == "{" else "]"
    end = raw.rfind(closer)
    if end < start:
        raise ValueError("JSON kapanis ayraci bulunamadi.")
    return json.loads(raw[start : end + 1])


def _load_store() -> dict[str, Any]:
    try:
        return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"selectors": {}}


def _save_store(data: dict[str, Any]) -> None:
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STORE_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _selector_key(name: str, app: str = "", url: str = "") -> str:
    base = str(name or "").strip().lower()
    app_s = str(app or "").strip().lower()
    url_s = str(url or "").strip().lower()
    return "|".join([base, app_s, url_s])


def _find_selector(name: str, app: str = "", url: str = "") -> dict[str, Any] | None:
    store = _load_store().get("selectors", {})
    keys = [
        _selector_key(name, app, url),
        _selector_key(name, app, ""),
        _selector_key(name, "", ""),
    ]
    for key in keys:
        item = store.get(key)
        if isinstance(item, dict):
            return item
    return None


def _human_mouse_path(
    start: tuple[int, int],
    end: tuple[int, int],
    *,
    steps: int = 18,
    arc: float = 0.18,
) -> list[tuple[int, int]]:
    """Build a smooth deterministic curved mouse path."""
    sx, sy = start
    ex, ey = end
    steps = max(2, min(int(steps or 18), 80))
    dx = ex - sx
    dy = ey - sy
    distance = math.hypot(dx, dy)
    if distance <= 1:
        return [(ex, ey)]

    # Perpendicular control offset gives a subtle hand-like arc without
    # introducing nondeterministic jitter.
    nx = -dy / distance
    ny = dx / distance
    bend = min(140.0, max(6.0, distance * max(0.0, min(arc, 0.45))))
    direction = 1 if ((sx + sy + ex + ey) % 2 == 0) else -1
    cx = (sx + ex) / 2 + nx * bend * direction
    cy = (sy + ey) / 2 + ny * bend * direction

    points: list[tuple[int, int]] = []
    for idx in range(1, steps + 1):
        t = idx / steps
        eased = t * t * (3 - 2 * t)
        inv = 1 - eased
        x = inv * inv * sx + 2 * inv * eased * cx + eased * eased * ex
        y = inv * inv * sy + 2 * inv * eased * cy + eased * eased * ey
        point = (int(round(x)), int(round(y)))
        if not points or points[-1] != point:
            points.append(point)
    if points[-1] != (ex, ey):
        points.append((ex, ey))
    return points


def _move_mouse(pyautogui: Any, x: int, y: int, *, duration: float, style: str = "natural") -> None:
    style = (style or "natural").strip().lower()
    if style in {"direct", "instant"} or duration <= 0.02:
        pyautogui.moveTo(x, y, duration=max(0.0, duration), tween=pyautogui.easeInOutQuad)
        return

    try:
        pos = pyautogui.position()
        start = (int(pos.x), int(pos.y))
    except Exception:
        pyautogui.moveTo(x, y, duration=duration, tween=pyautogui.easeInOutQuad)
        return

    distance = math.hypot(x - start[0], y - start[1])
    steps = max(8, min(34, int(distance / 42) + 8))
    points = _human_mouse_path(start, (x, y), steps=steps, arc=0.16 if style == "natural" else 0.08)
    delay = max(0.001, min(duration, 1.8) / max(1, len(points)))
    for px, py in points:
        pyautogui.moveTo(px, py, duration=0)
        time.sleep(delay)


def mouse_control(
    action: str,
    x: int | float | None = None,
    y: int | float | None = None,
    button: str = "left",
    clicks: int = 1,
    duration: float = 0.18,
    text: str = "",
    keys: str = "",
    amount: int = 0,
    movement_style: str = "natural",
) -> str:
    """Mouse and keyboard primitive control."""
    pyautogui = _load_pyautogui()
    act = (action or "").strip().lower()
    btn = (button or "left").strip().lower()
    if btn not in {"left", "right", "middle"}:
        btn = "left"
    dur = max(0.0, min(float(duration or 0), 2.0))

    if act in {"position", "pos", "where"}:
        pos = pyautogui.position()
        return f"Mouse konumu: ({int(pos.x)}, {int(pos.y)})."

    if act in {"move", "move_to"}:
        if x is None or y is None:
            return "Mouse hareketi icin x ve y gerekli."
        _move_mouse(pyautogui, int(x), int(y), duration=dur, style=movement_style)
        return f"Mouse ({int(x)}, {int(y)}) konumuna tasindi."

    if act in {"click", "double_click", "right_click", "click_window_ratio"}:
        if x is not None and y is not None:
            target_x = float(x)
            target_y = float(y)
            if act == "click_window_ratio":
                info = get_active_window_info()
                rect = info.get("rect") if isinstance(info, dict) else None
                if not isinstance(rect, dict):
                    return "Aktif pencere geometrisi alinamadi."
                left = float(rect.get("left", 0))
                top = float(rect.get("top", 0))
                width = max(1.0, float(rect.get("right", left + 1)) - left)
                height = max(1.0, float(rect.get("bottom", top + 1)) - top)
                target_x = left + width * max(0.0, min(1.0, target_x))
                target_y = top + height * max(0.0, min(1.0, target_y))
            _move_mouse(pyautogui, int(target_x), int(target_y), duration=dur, style=movement_style)
        click_count = 2 if act == "double_click" else max(1, min(int(clicks or 1), 3))
        click_button = "right" if act == "right_click" else btn
        pyautogui.click(button=click_button, clicks=click_count, interval=0.08)
        return f"Mouse {click_button} tiklama gonderildi."

    if act == "drag":
        if x is None or y is None:
            return "Surukleme icin hedef x ve y gerekli."
        pyautogui.dragTo(int(x), int(y), duration=max(0.2, dur), button=btn)
        return f"Mouse ({int(x)}, {int(y)}) konumuna suruklendi."

    if act == "scroll":
        delta = int(amount or 0)
        if delta == 0:
            return "Scroll icin amount gerekli; pozitif yukari, negatif asagi."
        pyautogui.scroll(delta)
        return f"Scroll gonderildi: {delta}."

    if act == "type":
        if not text:
            return "Yazmak icin text gerekli."
        pyautogui.write(text, interval=0.01)
        return "Metin yazildi."

    if act == "hotkey":
        parts = [p.strip().lower() for p in re.split(r"[+, ]+", keys or "") if p.strip()]
        if not parts:
            return "Hotkey icin keys gerekli. Ornek: ctrl+l"
        pyautogui.hotkey(*parts)
        return f"Hotkey gonderildi: {'+'.join(parts)}."

    if act == "press":
        key = (keys or text or "").strip().lower()
        if not key:
            return "Press icin keys veya text ile tus adi gerekli."
        pyautogui.press(key)
        return f"Tus basildi: {key}."

    return f"Bilinmeyen mouse_control eylemi: {act}"


mouse_control.__tool__ = {
    "declaration": {
        "name": "mouse_control",
        "description": (
            "Mouse hareketi, tiklama, surukleme, scroll, klavye yazma ve hotkey "
            "gonderme icin dusuk seviyeli bilgisayar kontrol araci."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "position | move | click | click_window_ratio | double_click | right_click | drag | scroll | type | hotkey | press"},
                "x": {"type": "NUMBER", "description": "Global ekran X koordinati; click_window_ratio icin aktif pencere genislik orani 0..1."},
                "y": {"type": "NUMBER", "description": "Global ekran Y koordinati; click_window_ratio icin aktif pencere yukseklik orani 0..1."},
                "button": {"type": "STRING", "description": "left | right | middle"},
                "clicks": {"type": "NUMBER", "description": "Tiklama sayisi."},
                "duration": {"type": "NUMBER", "description": "Hareket suresi saniye."},
                "text": {"type": "STRING", "description": "Yazilacak metin veya tus adi."},
                "keys": {"type": "STRING", "description": "Hotkey/tus. Ornek: ctrl+l"},
                "amount": {"type": "NUMBER", "description": "Scroll miktari."},
                "movement_style": {"type": "STRING", "description": "natural | precise | direct"},
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


def window_tracking(action: str = "snapshot", x: int | float | None = None, y: int | float | None = None) -> str:
    """Report monitor and active-window geometry for stable desktop control."""
    act = (action or "snapshot").strip().lower()
    if act in {"snapshot", "monitors", "screen", "geometry"}:
        return dump_geometry()
    if act in {"active_window", "window"}:
        return json.dumps(get_active_window_info(), ensure_ascii=False)
    if act in {"point", "normalize_point"}:
        if x is None or y is None:
            return json.dumps({"ok": False, "error": "point icin x ve y gerekli."}, ensure_ascii=False)
        payload = normalize_point(x, y)
        payload["ok"] = True
        return json.dumps(payload, ensure_ascii=False)
    return json.dumps({"ok": False, "error": f"Bilinmeyen window_tracking eylemi: {act}"}, ensure_ascii=False)


window_tracking.__tool__ = {
    "declaration": {
        "name": "window_tracking",
        "description": (
            "Aktif pencere, sanal ekran, monitorler ve nokta koordinatlarini raporlar. "
            "Coklu monitor ve farkli laptop/monitor olceklerinde tiklama sapmasini "
            "azaltmak icin bilgisayar kontrolunden once kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "snapshot | active_window | point"},
                "x": {"type": "NUMBER", "description": "Global X koordinati."},
                "y": {"type": "NUMBER", "description": "Global Y koordinati."},
            },
        },
    },
    "execution_mode": "inline",
}


def screen_ocr(
    capture: str = "screen",
    focus: str = "",
    provider: str = "auto",
    allow_cloud: bool = False,
) -> str:
    """Read visible text from the screen using local methods before Gemini."""
    mode = (provider or "auto").strip().lower()

    if mode in {"auto", "uia", "local"}:
        lines = _uia_text_lines(focus)
        if lines:
            return "\n".join(lines)
        if mode == "uia":
            return "UI Automation ile okunabilir metin bulunamadi."

    cap: _CaptureResult | None = None
    try:
        cap = _capture(capture)

        if mode in {"auto", "tesseract", "local"}:
            try:
                text = _tesseract_ocr(cap.path)
                if text:
                    return text
            except Exception as exc:
                if mode == "tesseract":
                    return f"Yerel Tesseract OCR basarisiz: {exc}"

        if mode != "gemini" and not allow_cloud:
            return (
                "Yerel OCR ile metin bulunamadi. Gemini kotasi harcamamak icin "
                "bulut OCR calistirilmadi; gerekirse allow_cloud=true ile tekrar dene."
            )

        prompt = (
            "Read all visible text in this Windows screenshot. "
            "Return Turkish-friendly plain text. Preserve line breaks where useful. "
            "If a focus is provided, prioritize that region/meaning.\n"
            f"Focus: {focus or 'none'}"
        )
        text = _gemini_vision_text(cap.path, prompt, max_tokens=1400)
        return text or "Ekranda okunabilir metin bulunamadi."
    except Exception as exc:
        return f"OCR basarisiz: {exc}"
    finally:
        _delete_capture(cap)


screen_ocr.__tool__ = {
    "declaration": {
        "name": "screen_ocr",
        "description": "Ekrandaki veya aktif penceredeki gorunur metinleri OCR ile okur.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "capture": {"type": "STRING", "description": "screen | active_window"},
                "focus": {"type": "STRING", "description": "Opsiyonel odak tarifi."},
                "provider": {"type": "STRING", "description": "auto | uia | tesseract | gemini"},
                "allow_cloud": {"type": "BOOLEAN", "description": "true ise yerel OCR basarisiz olunca Gemini Vision kullanilabilir."},
            },
        },
    },
    "execution_mode": "inline",
}


def detect_screen_elements(
    query: str = "",
    element_type: str = "any",
    capture: str = "screen",
    provider: str = "auto",
    allow_cloud: bool = False,
) -> str:
    """Detect visible UI elements and return compact JSON."""
    mode = (provider or "auto").strip().lower()
    if mode in {"auto", "uia", "local"}:
        try:
            items = _uia_collect(max_depth=7, limit=250)
            scored: list[tuple[int, int, dict[str, Any]]] = []
            for pos, item in enumerate(items):
                score = _score_uia_item(item, query, element_type)
                if query or (element_type and element_type != "any"):
                    if score <= 0:
                        continue
                scored.append((score, pos, item))
            if query or (element_type and element_type != "any"):
                scored.sort(key=lambda row: (-row[0], row[1]))
                matches = [item for _score, _pos, item in scored]
            else:
                matches = items
            if matches or mode == "uia":
                return json.dumps(
                    {
                        "elements": [_public_uia_item(item) for item in matches[:80]],
                        "count": len(matches),
                        "source": "windows_uia",
                    },
                    ensure_ascii=False,
                )
        except Exception as exc:
            if mode == "uia":
                return json.dumps({"elements": [], "error": str(exc), "source": "windows_uia"}, ensure_ascii=False)

    if mode != "gemini" and not allow_cloud:
        return json.dumps(
            {
                "elements": [],
                "source": "local",
                "message": "Yerel element detection sonuc vermedi; Gemini Vision allow_cloud=false oldugu icin kullanilmadi.",
            },
            ensure_ascii=False,
        )

    cap: _CaptureResult | None = None
    try:
        cap = _capture(capture)
        prompt = (
            "Detect clickable/readable UI elements in this Windows screenshot. "
            "Return only JSON with key elements: an array of objects. Each object "
            "must have label, type, x, y, confidence, text. x and y are global-ish "
            "pixel coordinates relative to the screenshot bounds if possible. "
            "Element types include button, input, link, menu, tab, checkbox, icon, text. "
            f"Desired element_type: {element_type}. Query: {query}."
        )
        raw = _gemini_vision_text(cap.path, prompt, max_tokens=1600)
        try:
            data = _extract_json_object(raw)
        except Exception:
            data = {"elements": [], "raw": raw}
        return json.dumps(data, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"elements": [], "error": str(exc)}, ensure_ascii=False)
    finally:
        _delete_capture(cap)


detect_screen_elements.__tool__ = {
    "declaration": {
        "name": "detect_screen_elements",
        "description": "Ekrandaki buton, input, link, menu, ikon ve metin ogelerini vision ile bulur.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "Aranan ogeler icin dogal dil filtre."},
                "element_type": {"type": "STRING", "description": "button | input | link | menu | tab | checkbox | icon | text | any"},
                "capture": {"type": "STRING", "description": "screen | active_window"},
                "provider": {"type": "STRING", "description": "auto | uia | gemini"},
                "allow_cloud": {"type": "BOOLEAN", "description": "true ise yerel detection basarisiz olunca Gemini Vision kullanilabilir."},
            },
        },
    },
    "execution_mode": "inline",
}


def _uia_control_type_name(control: Any) -> str:
    try:
        return str(getattr(control, "ControlTypeName", "") or "")
    except Exception:
        return ""


def _uia_rect(control: Any) -> dict[str, int]:
    try:
        rect = control.BoundingRectangle
        return {
            "left": int(rect.left),
            "top": int(rect.top),
            "right": int(rect.right),
            "bottom": int(rect.bottom),
        }
    except Exception:
        return {"left": 0, "top": 0, "right": 0, "bottom": 0}


def _uia_name(control: Any) -> str:
    for attr in ("Name", "AutomationId", "ClassName"):
        try:
            value = str(getattr(control, attr, "") or "").strip()
            if value:
                return value
        except Exception:
            continue
    return ""


def _uia_visible(rect: dict[str, int]) -> bool:
    return rect["right"] > rect["left"] and rect["bottom"] > rect["top"]


def _uia_collect(max_depth: int = 6, limit: int = 250) -> list[dict[str, Any]]:
    """Collect visible UIA controls from the active window.

    UI Automation is quota-free and returns actual screen coordinates, so it is
    more stable than vision for browser links/buttons when accessibility names
    are available.
    """
    import uiautomation as auto  # type: ignore[reportMissingImports]

    root = auto.GetForegroundControl()
    if root is None:
        return []

    items: list[dict[str, Any]] = []
    queue: list[tuple[Any, int]] = [(root, 0)]
    seen: set[int] = set()

    while queue and len(items) < limit:
        control, depth = queue.pop(0)
        marker = id(control)
        if marker in seen:
            continue
        seen.add(marker)

        rect = _uia_rect(control)
        name = _uia_name(control)
        control_type = _uia_control_type_name(control)
        if name and _uia_visible(rect):
            items.append(
                {
                    "name": name,
                    "type": control_type,
                    "automation_id": str(getattr(control, "AutomationId", "") or ""),
                    "class_name": str(getattr(control, "ClassName", "") or ""),
                    "rect": rect,
                    "center": {
                        "x": int((rect["left"] + rect["right"]) / 2),
                        "y": int((rect["top"] + rect["bottom"]) / 2),
                    },
                    "_control": control,
                }
            )

        if depth >= max_depth:
            continue
        try:
            children = control.GetChildren()
        except Exception:
            children = []
        for child in children:
            queue.append((child, depth + 1))

    return items


def _uia_text_lines(query: str = "", limit: int = 180) -> list[str]:
    """Return visible UI Automation text/name lines without using vision quota."""
    try:
        items = _uia_collect(max_depth=7, limit=max(limit, 50))
    except Exception:
        return []
    q = (query or "").lower().strip()
    lines: list[str] = []
    seen: set[str] = set()
    for item in items:
        name = str(item.get("name", "") or "").strip()
        if not name:
            continue
        if q and q not in name.lower():
            # Keep context broad for empty query, focused for non-empty query.
            continue
        if name in seen:
            continue
        seen.add(name)
        typ = str(item.get("type", "") or "").replace("Control", "")
        line = f"{typ}: {name}" if typ else name
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _tesseract_ocr(image_path: Path) -> str:
    """Run local Tesseract OCR when available."""
    try:
        import pytesseract  # type: ignore[reportMissingImports]

        try:
            from PIL import Image  # type: ignore[reportMissingImports]
        except Exception as exc:
            raise RuntimeError("Pillow kurulu degil.") from exc
        with Image.open(image_path) as image:
            return str(pytesseract.image_to_string(image, lang="tur+eng") or "").strip()
    except ImportError:
        pass

    exe = shutil.which("tesseract")
    if not exe:
        raise RuntimeError("Yerel OCR icin pytesseract veya tesseract.exe bulunamadi.")

    cmd = [exe, str(image_path), "stdout", "-l", "tur+eng"]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Tesseract OCR basarisiz.").strip()[:300])
    return (result.stdout or "").strip()


def _score_uia_item(item: dict[str, Any], query: str, control_type: str) -> int:
    q = _norm_text(query).strip()
    wanted_type = _norm_text(control_type).strip()
    hay = " ".join(
        str(item.get(k, "") or "") for k in ("name", "type", "automation_id", "class_name")
    )
    hay = _norm_text(hay)
    score = 0
    if q:
        for token in re.findall(r"\w+", q, flags=re.UNICODE):
            if token and token in hay:
                score += 10
        if q in hay:
            score += 30
    if wanted_type and wanted_type != "any" and wanted_type in hay:
        score += 20
    return score


def _public_uia_item(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "_control"}


def ui_automation(
    action: str,
    query: str = "",
    index: int = 1,
    control_type: str = "any",
    max_depth: int = 6,
    limit: int = 80,
) -> str:
    """Use Windows UI Automation to list or click visible controls.

    This is the preferred quota-free path for browser/app elements that expose
    accessibility names. Example: "ekrandaki 3. videoya tikla" can be mapped to
    action=click, query=video, index=3.
    """
    act = (action or "").strip().lower()
    try:
        idx = max(1, int(index or 1))
    except Exception:
        idx = 1
    try:
        depth = max(1, min(int(max_depth or 6), 10))
        max_items = max(1, min(int(limit or 80), 250))
    except Exception:
        depth = 6
        max_items = 80

    try:
        items = _uia_collect(max_depth=depth, limit=250)
    except Exception as exc:
        return json.dumps(
            {
                "ok": False,
                "error": f"UI Automation kullanilamadi: {exc}",
                "hint": "Bu yolda kota yoktur ama uygulamanin accessibility bilgisi acik olmalidir.",
            },
            ensure_ascii=False,
        )

    scored: list[tuple[int, int, dict[str, Any]]] = []
    for pos, item in enumerate(items):
        score = _score_uia_item(item, query, control_type)
        if query or (control_type and control_type != "any"):
            if score <= 0:
                continue
        scored.append((score, pos, item))

    if query or (control_type and control_type != "any"):
        scored.sort(key=lambda row: (-row[0], row[1]))
        matches = [item for _score, _pos, item in scored]
    else:
        matches = items

    if act in {"list", "find"}:
        payload = {
            "ok": True,
            "count": len(matches),
            "elements": [_public_uia_item(item) for item in matches[:max_items]],
            "source": "windows_uia",
        }
        return json.dumps(payload, ensure_ascii=False)

    if act == "click":
        if len(matches) < idx:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"{idx}. eslesme bulunamadi.",
                    "count": len(matches),
                    "elements": [_public_uia_item(item) for item in matches[:10]],
                    "source": "windows_uia",
                },
                ensure_ascii=False,
            )
        item = matches[idx - 1]
        rect = item["rect"]
        x = int((rect["left"] + rect["right"]) / 2)
        y = int((rect["top"] + rect["bottom"]) / 2)
        try:
            control = item.get("_control")
            if control is not None:
                try:
                    control.Click()
                except Exception:
                    pyautogui = _load_pyautogui()
                    _move_mouse(pyautogui, x, y, duration=0.16, style="precise")
                    pyautogui.click()
            return json.dumps(
                {
                    "ok": True,
                    "clicked": _public_uia_item(item),
                    "x": x,
                    "y": y,
                    "geometry": normalize_point(x, y, rect),
                    "source": "windows_uia",
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps(
                {
                    "ok": False,
                    "error": f"UIA tiklama basarisiz: {exc}",
                    "target": _public_uia_item(item),
                    "source": "windows_uia",
                },
                ensure_ascii=False,
            )

    return json.dumps({"ok": False, "error": f"Bilinmeyen ui_automation eylemi: {act}"}, ensure_ascii=False)


def _find_window_rect_by_title(title_part: str) -> dict[str, Any] | None:
    if not hasattr(ctypes, "windll"):
        return None
    _set_process_dpi_aware()
    needle = _norm_text(title_part)
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    found: list[dict[str, Any]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _enum_proc(hwnd, _lparam):  # type: ignore[no-untyped-def]
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = str(buf.value or "")
        if needle and needle not in _norm_text(title):
            return True
        rect_raw = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect_raw))
        width = int(rect_raw.right - rect_raw.left)
        height = int(rect_raw.bottom - rect_raw.top)
        if width < 400 or height < 300:
            return True
        found.append(
            {
                "hwnd": int(hwnd),
                "title": title,
                "rect": {
                    "left": int(rect_raw.left),
                    "top": int(rect_raw.top),
                    "right": int(rect_raw.right),
                    "bottom": int(rect_raw.bottom),
                },
            }
        )
        return False

    try:
        user32.EnumWindows(_enum_proc, 0)
    except Exception:
        return None
    return found[0] if found else None


def _set_process_dpi_aware() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _focus_window(hwnd: int, maximize: bool = False) -> None:
    if not hasattr(ctypes, "windll") or not hwnd:
        return
    _set_process_dpi_aware()
    user32 = ctypes.windll.user32
    try:
        user32.ShowWindow(hwnd, 3 if maximize else 9)  # SW_MAXIMIZE / SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        if maximize:
            try:
                # Steam uses a custom frame and can ignore SW_MAXIMIZE in some states.
                # Move it to the primary work area as a second, deterministic nudge.
                cx = int(user32.GetSystemMetrics(0))
                cy = int(user32.GetSystemMetrics(1))
                if cx > 0 and cy > 0:
                    user32.SetWindowPos(hwnd, 0, 0, 0, cx, cy, 0x0040)  # SWP_SHOWWINDOW
            except Exception:
                pass
        time.sleep(0.25)
    except Exception:
        pass


def _rect_center(rect: dict[str, Any]) -> tuple[int, int]:
    return (
        int((float(rect.get("left", 0)) + float(rect.get("right", 0))) / 2),
        int((float(rect.get("top", 0)) + float(rect.get("bottom", 0))) / 2),
    )


def _rect_ratio(rect: dict[str, Any], window_rect: dict[str, Any]) -> tuple[float, float, float, float]:
    left = float(window_rect.get("left", 0))
    top = float(window_rect.get("top", 0))
    width = max(1.0, float(window_rect.get("right", left + 1)) - left)
    height = max(1.0, float(window_rect.get("bottom", top + 1)) - top)
    cx, cy = _rect_center(rect)
    rw = max(1.0, float(rect.get("right", 0)) - float(rect.get("left", 0)))
    rh = max(1.0, float(rect.get("bottom", 0)) - float(rect.get("top", 0)))
    return ((cx - left) / width, (cy - top) / height, rw / width, rh / height)


def _rect_size(rect: dict[str, Any]) -> tuple[float, float]:
    left = float(rect.get("left", 0))
    top = float(rect.get("top", 0))
    return (
        max(1.0, float(rect.get("right", left + 1)) - left),
        max(1.0, float(rect.get("bottom", top + 1)) - top),
    )


def _rect_inside(inner: dict[str, Any], outer: dict[str, Any], margin: float = 8.0) -> bool:
    return (
        float(inner.get("left", 0)) >= float(outer.get("left", 0)) - margin
        and float(inner.get("top", 0)) >= float(outer.get("top", 0)) - margin
        and float(inner.get("right", 0)) <= float(outer.get("right", 0)) + margin
        and float(inner.get("bottom", 0)) <= float(outer.get("bottom", 0)) + margin
    )


def _steam_app_uri_for_game(game: str) -> str:
    text = _norm_text(game)
    if any(token in text for token in ("counter-strike 2", "counter strike 2", "cs2")):
        return "steam://nav/games/details/730"
    if any(token in text for token in ("battlefield 1", "bf1")):
        return "steam://nav/games/details/1238840"
    return ""


def _open_steam_game_page(game: str) -> str:
    uri = _steam_app_uri_for_game(game)
    if not uri:
        return ""
    try:
        if hasattr(os, "startfile"):
            os.startfile(uri)  # type: ignore[attr-defined]
        else:
            webbrowser.open(uri)
        time.sleep(1.2)
        return uri
    except Exception:
        return ""


ui_automation.__tool__ = {
    "declaration": {
        "name": "ui_automation",
        "description": (
            "Windows UI Automation ile aktif penceredeki erisilebilir UI ogelerini "
            "kotasiz listeler veya tiklar. Ekran goruntusu koordinati yerine gercek "
            "accessibility bounding box kullandigi icin '3. video/link/buton' gibi "
            "isteklerde click_on_screen'den daha stabil olabilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "list | find | click"},
                "query": {"type": "STRING", "description": "Aranacak metin. Ornek: video, oynat, gonder."},
                "index": {"type": "NUMBER", "description": "Kacinci eslesme tiklanacak. 1 tabanli."},
                "control_type": {"type": "STRING", "description": "any | Button | Hyperlink | Edit | Text | ListItem vb."},
                "max_depth": {"type": "NUMBER", "description": "UIA agac derinligi."},
                "limit": {"type": "NUMBER", "description": "Listelenecek maksimum oge."},
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


def _steam_click_update_button_legacy(game: str = "Counter-Strike 2") -> str:
    """Click Steam's visible update/resume button using UIA first, geometry fallback last."""
    attempts: list[dict[str, Any]] = []
    candidates = ("GÜNCELLE", "Guncelle", "Update", "Resume", "Sürdür", "Devam Et")
    for query in candidates:
        raw = ui_automation(
            action="click",
            query=query,
            control_type="any",
            index=1,
            max_depth=8,
            limit=120,
        )
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"ok": False, "raw": raw}
        attempts.append({"query": query, "result": payload})
        if isinstance(payload, dict) and payload.get("ok") is True:
            payload["game"] = game
            payload["strategy"] = "windows_uia"
            return json.dumps(payload, ensure_ascii=False)

    pyautogui = _load_pyautogui()
    info = get_active_window_info()
    rect = info.get("rect") if isinstance(info, dict) else None
    if not isinstance(rect, dict):
        return json.dumps(
            {"ok": False, "error": "Steam penceresi geometrisi alinamadi.", "attempts": attempts},
            ensure_ascii=False,
        )

    left = float(rect.get("left", 0))
    top = float(rect.get("top", 0))
    width = max(1.0, float(rect.get("right", left + 1)) - left)
    height = max(1.0, float(rect.get("bottom", top + 1)) - top)
    x = int(left + width * 0.203)
    y = int(top + height * 0.418)
    _move_mouse(pyautogui, x, y, duration=0.16, style="precise")
    pyautogui.click()
    return json.dumps(
        {
            "ok": True,
            "game": game,
            "strategy": "steam_window_ratio_fallback",
            "x": x,
            "y": y,
            "attempts": attempts,
            "hint": "UIA adaylari bulunamayinca Steam oyun sayfasindaki stabil guncelle butonu konumuna tiklandi.",
        },
        ensure_ascii=False,
    )


def steam_click_update_button(game: str = "Counter-Strike 2") -> str:
    """Click Steam's visible update/resume button without accepting false text matches."""
    attempts: list[dict[str, Any]] = []
    pyautogui = _load_pyautogui()

    opened_uri = _open_steam_game_page(game)
    if opened_uri:
        attempts.append({"stage": "open_game_page", "uri": opened_uri})

    steam_window = _find_window_rect_by_title("Steam")
    if steam_window and steam_window.get("hwnd"):
        _focus_window(int(steam_window["hwnd"]), maximize=True)
        steam_window = _find_window_rect_by_title("Steam") or steam_window

    info = get_active_window_info()
    rect = steam_window.get("rect") if isinstance(steam_window, dict) else None
    if not isinstance(rect, dict):
        rect = info.get("rect") if isinstance(info, dict) else None
    if not isinstance(rect, dict):
        return json.dumps(
            {"ok": False, "error": "Steam penceresi geometrisi alinamadi.", "attempts": attempts},
            ensure_ascii=False,
        )

    candidates = {"guncelle", "update", "resume", "surdur", "devam et"}

    def _collect_uia_candidates(stage: str, window_rect: dict[str, Any]) -> list[tuple[int, dict[str, Any], tuple[float, float, float, float]]]:
        try:
            items = _uia_collect(max_depth=8, limit=250)
        except Exception as exc:
            attempts.append({"stage": f"{stage}_uia_collect", "ok": False, "error": str(exc)})
            return []

        window_width, window_height = _rect_size(window_rect)
        compact_window = window_width < 1100 or window_height < 680
        matches: list[tuple[int, dict[str, Any], tuple[float, float, float, float]]] = []
        for item in items:
            name = _norm_text(str(item.get("name", "") or ""))
            if not any(candidate in name for candidate in candidates):
                continue
            exact_action = name in candidates
            if not exact_action and any(word in name for word in ("kuy", "queue", "guncelleme", "update queued")):
                attempts.append(
                    {
                        "stage": f"{stage}_uia_candidate",
                        "name": item.get("name"),
                        "accepted": False,
                        "reason": "Oyun listesi/kuyruk metni; guncelleme butonu degil.",
                    }
                )
                continue
            item_rect = item.get("rect")
            if not isinstance(item_rect, dict) or not _rect_inside(item_rect, window_rect):
                continue
            rx, ry, rw, rh = _rect_ratio(item_rect, window_rect)
            in_action_band = 0.12 <= rx <= 0.34 and 0.34 <= ry <= 0.49
            button_sized = 0.045 <= rw <= 0.24 and 0.02 <= rh <= 0.11
            accepted = button_sized and (in_action_band or (compact_window and exact_action))
            attempts.append(
                {
                    "stage": f"{stage}_uia_candidate",
                    "name": item.get("name"),
                    "rx": round(rx, 3),
                    "ry": round(ry, 3),
                    "rw": round(rw, 3),
                    "rh": round(rh, 3),
                    "compact_window": compact_window,
                    "accepted": accepted,
                }
            )
            if not accepted:
                continue
            score = 100
            if exact_action:
                score += 30
            if "button" in _norm_text(str(item.get("type", ""))):
                score += 10
            if in_action_band:
                score += 8
            matches.append((score, item, (rx, ry, rw, rh)))
        return matches

    uia_candidates = _collect_uia_candidates("initial", rect)

    width, height = _rect_size(rect)
    if not uia_candidates and steam_window and steam_window.get("hwnd") and (width < 1100 or height < 680):
        attempts.append(
            {
                "stage": "maximize_retry",
                "reason": "Steam penceresi kucuk oldugu icin oran fallback guvenli degil; pencere buyutulup UIA yeniden deneniyor.",
                "width": round(width),
                "height": round(height),
            }
        )
        _focus_window(int(steam_window["hwnd"]), maximize=True)
        steam_window = _find_window_rect_by_title("Steam") or steam_window
        info = get_active_window_info()
        refreshed_rect = steam_window.get("rect") if isinstance(steam_window, dict) else None
        if not isinstance(refreshed_rect, dict):
            refreshed_rect = info.get("rect") if isinstance(info, dict) else None
        if isinstance(refreshed_rect, dict):
            rect = refreshed_rect
            uia_candidates = _collect_uia_candidates("maximized", rect)

    if uia_candidates:
        uia_candidates.sort(key=lambda row: (-row[0], row[2][0]))
        _, item, ratios = uia_candidates[0]
        x, y = _rect_center(item["rect"])
        _move_mouse(pyautogui, x, y, duration=0.16, style="precise")
        pyautogui.click()
        return json.dumps(
            {
                "ok": True,
                "game": game,
                "strategy": "windows_uia_filtered_physical_click",
                "clicked": _public_uia_item(item),
                "x": x,
                "y": y,
                "ratios": {"x": round(ratios[0], 3), "y": round(ratios[1], 3)},
                "attempts": attempts,
            },
            ensure_ascii=False,
        )

    left = float(rect.get("left", 0))
    top = float(rect.get("top", 0))
    width = max(1.0, float(rect.get("right", left + 1)) - left)
    height = max(1.0, float(rect.get("bottom", top + 1)) - top)
    x = int(left + width * 0.203)
    y = int(top + height * 0.418)
    _move_mouse(pyautogui, x, y, duration=0.16, style="precise")
    pyautogui.click()
    return json.dumps(
        {
            "ok": True,
            "game": game,
            "strategy": "steam_window_ratio_fallback",
            "x": x,
            "y": y,
            "window": steam_window or info,
            "attempts": attempts,
            "hint": "UIA adaylari gercek buton bandinda bulunamayinca Steam penceresindeki guncelle butonu konumuna tiklandi.",
        },
        ensure_ascii=False,
    )


steam_click_update_button.__tool__ = {
    "declaration": {
        "name": "steam_click_update_button",
        "description": (
            "Steam oyun sayfasindaki GUNCELLE/Update/Resume butonuna tiklar. "
            "Once Windows UI Automation ile metin adaylarini dener; Steam UIA metni "
            "gizlerse aktif Steam penceresindeki stabil buton konumuna fallback tiklar."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "game": {"type": "STRING", "description": "Oyun adi. Ornek: Counter-Strike 2"},
            },
        },
    },
    "execution_mode": "inline",
}


def browser_automation(
    action: str,
    url: str = "",
    query: str = "",
    text: str = "",
    target: str = "",
    selector_name: str = "",
    engine: str = "auto",
    selector: str = "",
    role: str = "",
    index: int = 1,
    key: str = "",
    value: str = "",
    script: str = "",
    limit: int = 40,
    browser: str = "",
    url_contains: str = "",
    title_contains: str = "",
    text_contains: str = "",
    timeout_ms: int = 3000,
) -> str:
    """Browser automation using Playwright when requested plus desktop fallback."""
    act = (action or "").strip().lower()
    mode = (engine or "auto").strip().lower()

    playwright_actions = {
        "playwright_open_url",
        "playwright_search",
        "playwright_click",
        "playwright_click_text",
        "playwright_click_selector",
        "playwright_fill",
        "playwright_submit",
        "playwright_press",
        "playwright_list_links",
        "playwright_find_elements",
        "playwright_click_smart",
        "playwright_extract_text",
        "playwright_read_page",
        "playwright_snapshot",
        "playwright_verify",
        "playwright_verify_state",
        "playwright_evaluate",
        "playwright_close",
        "playwright_timeline",
        "playwright_clear_timeline",
    }
    if act in playwright_actions:
        act = act.removeprefix("playwright_")
        mode = "playwright"

    if mode == "playwright":
        if act in {"timeline", "history"}:
            from runtime.browser_timeline import read_browser_timeline

            return json.dumps(read_browser_timeline(limit=limit), ensure_ascii=False)
        if act in {"clear_timeline", "clear_history"}:
            from runtime.browser_timeline import clear_browser_timeline

            return json.dumps(clear_browser_timeline(), ensure_ascii=False)

        from runtime.browser_playwright import run_browser_action

        saved_selector = ""
        saved_target = ""
        if selector_name:
            saved = _find_selector(selector_name)
            if saved:
                saved_selector = str(saved.get("selector") or "")
                saved_target = str(saved.get("target") or "")

        result_raw = run_browser_action(
            act,
            url=url,
            query=query,
            text=text,
            target=target or saved_target,
            selector=selector or saved_selector,
            role=role,
            index=index,
            key=key,
            value=value,
            script=script,
            limit=limit,
            limit_chars=limit,
            url_contains=url_contains,
            title_contains=title_contains,
            text_contains=text_contains,
            timeout_ms=timeout_ms,
        )
        if selector_name and act in {"click_smart", "smart_click", "click", "click_text", "click_selector"}:
            try:
                data = json.loads(result_raw)
                found_selector = str(data.get("selector") or selector or saved_selector or "")
                found_target = str(data.get("target") or target or text or query or saved_target or selector_name)
                if data.get("ok") and found_selector:
                    selector_memory(
                        "save",
                        name=selector_name,
                        selector=found_selector,
                        target=found_target,
                        app="browser",
                        url=str(data.get("url") or url or ""),
                        strategy="playwright_smart",
                    )
            except Exception:
                pass
        return result_raw

    pyautogui = _load_pyautogui()

    if act == "open_url":
        if not url:
            return "URL gerekli."
        final = url if url.startswith(("http://", "https://")) else "https://" + url
        browser = browser or str(get_app_config_value("preferred_browser", "") or "")
        if browser:
            return _open_url_in_browser(final, browser)
        webbrowser.open_new_tab(final)
        return f"Tarayicida acildi: {final}"

    if act == "search":
        q = query or text
        if not q:
            return "Arama icin query gerekli."
        search_url = "https://www.google.com/search?q=" + urllib.parse.quote(q)
        browser = browser or str(get_app_config_value("preferred_browser", "") or "")
        if browser:
            return _open_url_in_browser(search_url, browser)
        webbrowser.open_new_tab(search_url)
        return f"Arama acildi: {q}"

    if act == "address_bar":
        pyautogui.hotkey("ctrl", "l")
        return "Adres cubuguna gecildi."

    if act == "type":
        if not text:
            return "Yazmak icin text gerekli."
        pyautogui.write(text, interval=0.01)
        return "Tarayiciya metin yazildi."

    if act == "submit":
        pyautogui.press("enter")
        return "Enter gonderildi."

    if act in {"back", "forward", "refresh", "new_tab", "close_tab"}:
        mapping = {
            "back": ("alt", "left"),
            "forward": ("alt", "right"),
            "refresh": ("ctrl", "r"),
            "new_tab": ("ctrl", "t"),
            "close_tab": ("ctrl", "w"),
        }
        pyautogui.hotkey(*mapping[act])
        return f"Tarayici komutu gonderildi: {act}."

    if act in {"click_text", "click_target"}:
        click_target = target or selector_name
        if selector_name:
            saved = _find_selector(selector_name)
            if saved and saved.get("target"):
                click_target = str(saved["target"])
        if not click_target:
            return "Tiklamak icin target veya selector_name gerekli."
        from skills.vision.tools import click_on_screen

        result = click_on_screen(click_target, capture="active_window", confirm=True)
        if selector_name and ("üzerine" in result or "uzerine" in result):
            selector_memory("save", selector_name, target=click_target, strategy="vision_text")
        return result

    if act in {"click_nth", "click_nth_element", "click_nth_video"}:
        q = target or query or ("video" if act == "click_nth_video" else "")
        try:
            idx = int(index or 0) or (int(text or "0") if text else 0)
        except Exception:
            idx = 0
        if idx <= 0:
            idx = 3 if act == "click_nth_video" else 1
        return ui_automation(
            action="click",
            query=q,
            index=idx,
            control_type="Hyperlink" if act == "click_nth_video" else "any",
        )

    if act in {"list_links", "find_elements", "click_smart", "extract_text", "read_page", "snapshot", "fill", "click_selector", "verify", "verify_state", "timeline", "clear_timeline"}:
        return (
            "Bu eylem icin engine='playwright' kullanin. "
            "Playwright DOM uzerinden daha stabil tarayici kontrolu saglar."
        )

    return f"Bilinmeyen browser_automation eylemi: {act}"


browser_automation.__tool__ = {
    "declaration": {
        "name": "browser_automation",
        "description": (
            "Tarayicida URL acma, arama, adres cubugu, yazma, enter, sekme ve "
            "gorsel hedefe tiklama otomasyonu yapar. engine='playwright' ile "
            "DOM tabanli URL acma, metin/selector tiklama, form doldurma, "
            "link listeleme, element bulma, akilli/self-healing tiklama ve "
            "sayfa metni okuma yapabilir. Timeline ile son tarayici adimlarini "
            "raporlayabilir; verify ile URL/baslik/metin/selector dogrulamasi "
            "yapabilir. browser='chrome' "
            "veya browser='opera' gibi hedef tarayici secilebilir; bos ise "
            "Windows varsayilan/aktif tarayici akisi kullanilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "open_url | search | address_bar | type | submit | back | forward | refresh | new_tab | close_tab | click_text | click_nth | click_nth_video | list_links | find_elements | click_smart | extract_text | fill | press | verify | evaluate | close | timeline | clear_timeline"},
                "url": {"type": "STRING", "description": "Acilacak URL."},
                "query": {"type": "STRING", "description": "Arama sorgusu."},
                "text": {"type": "STRING", "description": "Yazilacak metin."},
                "target": {"type": "STRING", "description": "Tiklanacak gorsel/metinsel hedef."},
                "selector_name": {"type": "STRING", "description": "Kayitli selector adi."},
                "engine": {"type": "STRING", "description": "auto | desktop | playwright. DOM tabanli kontrol icin playwright kullan."},
                "selector": {"type": "STRING", "description": "Playwright CSS selector. Ornek: input[name='q']"},
                "role": {"type": "STRING", "description": "Playwright aria role. Ornek: button, link, textbox."},
                "index": {"type": "NUMBER", "description": "Kacinci eslesme kullanilacak. 1 tabanli."},
                "key": {"type": "STRING", "description": "Playwright press icin tus. Ornek: Enter, Control+L."},
                "value": {"type": "STRING", "description": "Playwright fill icin yazilacak deger."},
                "script": {"type": "STRING", "description": "Playwright evaluate icin JavaScript."},
                "limit": {"type": "NUMBER", "description": "Link/metin liste limitleri."},
                "browser": {"type": "STRING", "description": "Opsiyonel hedef tarayici: chrome | opera | opera_gx | edge | firefox | brave. Bos ise varsayilan/aktif tarayici."},
                "url_contains": {"type": "STRING", "description": "Playwright verify: URL icinde beklenen metin."},
                "title_contains": {"type": "STRING", "description": "Playwright verify: sayfa basliginda beklenen metin."},
                "text_contains": {"type": "STRING", "description": "Playwright verify: sayfa metninde beklenen metin."},
                "timeout_ms": {"type": "NUMBER", "description": "Playwright verify/click bekleme suresi ms."},
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


def self_healing_click(
    target: str = "",
    selector_name: str = "",
    app: str = "",
    url: str = "",
    index: int = 1,
    control_type: str = "any",
    retries: int = 2,
) -> str:
    """Click a UI target with selector memory and retry fallbacks."""
    click_target = (target or "").strip()
    selector = (selector_name or "").strip()
    if selector:
        saved = _find_selector(selector, app, url)
        if saved:
            click_target = str(saved.get("target") or click_target or selector)
            if not app:
                app = str(saved.get("app") or "")
            if not url:
                url = str(saved.get("url") or "")

    if not click_target:
        click_target = selector
    if not click_target:
        return json.dumps({"ok": False, "error": "Tiklamak icin target veya selector_name gerekli."}, ensure_ascii=False)

    attempts: list[dict[str, Any]] = []
    try:
        idx = max(1, int(index or 1))
    except Exception:
        idx = 1
    try:
        max_retries = max(1, min(int(retries or 2), 4))
    except Exception:
        max_retries = 2

    plans = [
        {"query": click_target, "control_type": control_type or "any", "index": idx},
        {"query": click_target, "control_type": "any", "index": idx},
    ]
    if idx != 1:
        plans.append({"query": click_target, "control_type": control_type or "any", "index": 1})

    for plan in plans[:max_retries + 1]:
        raw = ui_automation(
            "click",
            query=plan["query"],
            index=plan["index"],
            control_type=plan["control_type"],
            max_depth=8,
            limit=160,
        )
        try:
            data = json.loads(raw)
        except Exception:
            data = {"ok": False, "raw": raw}
        attempts.append({"strategy": "uia", "plan": plan, "result": data})
        if data.get("ok"):
            if selector:
                selector_memory(
                    "save",
                    name=selector,
                    target=click_target,
                    app=app,
                    url=url,
                    strategy="uia_text",
                )
            return json.dumps(
                {
                    "ok": True,
                    "target": click_target,
                    "selector_name": selector,
                    "attempts": attempts,
                    "source": "self_healing_click",
                },
                ensure_ascii=False,
            )

    return json.dumps(
        {
            "ok": False,
            "target": click_target,
            "selector_name": selector,
            "attempts": attempts,
            "hint": "UIA eslesmesi basarisiz. Gerekirse detect_screen_elements veya screen_ocr ile hedefi yeniden dogrula.",
            "source": "self_healing_click",
        },
        ensure_ascii=False,
    )


self_healing_click.__tool__ = {
    "declaration": {
        "name": "self_healing_click",
        "description": (
            "Kayıtlı selector hafızası, Windows UIA ve retry stratejileriyle hedefe "
            "tıklamaya çalışır. Tekrarlanan web/app hedeflerinde selector_name ver."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "target": {"type": "STRING", "description": "Tiklanacak metin/hedef tarifi."},
                "selector_name": {"type": "STRING", "description": "Kayitli veya kaydedilecek selector adi."},
                "app": {"type": "STRING", "description": "Opsiyonel uygulama/pencere kapsami."},
                "url": {"type": "STRING", "description": "Opsiyonel URL kapsami."},
                "index": {"type": "NUMBER", "description": "Kacinci eslesme tiklanacak."},
                "control_type": {"type": "STRING", "description": "any | Button | Hyperlink | Edit | Text | ListItem vb."},
                "retries": {"type": "NUMBER", "description": "Ek retry sayisi, 1-4."},
            },
        },
    },
    "execution_mode": "inline",
}


def selector_memory(
    action: str,
    name: str = "",
    selector: str = "",
    target: str = "",
    app: str = "",
    url: str = "",
    strategy: str = "vision_text",
) -> str:
    """Store and retrieve self-healing selectors."""
    act = (action or "").strip().lower()
    store = _load_store()
    selectors = store.setdefault("selectors", {})

    if act == "save":
        if not name:
            return "Selector kaydi icin name gerekli."
        key = _selector_key(name, app, url)
        selectors[key] = {
            "name": name,
            "selector": selector,
            "target": target or selector or name,
            "app": app,
            "url": url,
            "strategy": strategy or "vision_text",
            "updated_at": time.time(),
        }
        _save_store(store)
        return f"Selector kaydedildi: {name}."

    if act == "get":
        if not name:
            return "Selector okumak icin name gerekli."
        item = _find_selector(name, app, url)
        if not item:
            return f"Selector bulunamadi: {name}."
        return json.dumps(item, ensure_ascii=False)

    if act == "forget":
        if not name:
            return "Selector silmek icin name gerekli."
        keys = [k for k, v in selectors.items() if isinstance(v, dict) and v.get("name") == name]
        for key in keys:
            selectors.pop(key, None)
        _save_store(store)
        return f"{len(keys)} selector kaydi silindi."

    if act == "list":
        names = sorted({str(v.get("name")) for v in selectors.values() if isinstance(v, dict) and v.get("name")})
        return "Kayitli selectorlar: " + (", ".join(names) if names else "yok")

    return f"Bilinmeyen selector_memory eylemi: {act}"


selector_memory.__tool__ = {
    "declaration": {
        "name": "selector_memory",
        "description": (
            "Self-healing selector hafizasi. Bir UI hedefini isim, selector, "
            "metinsel hedef ve strateji ile kaydeder/okur/siler/listeler."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "save | get | forget | list"},
                "name": {"type": "STRING", "description": "Selector adi."},
                "selector": {"type": "STRING", "description": "CSS/XPath/UIA selector veya bos."},
                "target": {"type": "STRING", "description": "Gorsel/metinsel hedef tarifi."},
                "app": {"type": "STRING", "description": "Opsiyonel uygulama/pencere adi."},
                "url": {"type": "STRING", "description": "Opsiyonel URL kapsami."},
                "strategy": {"type": "STRING", "description": "css | xpath | uia | vision_text"},
            },
            "required": ["action"],
        },
    },
    "execution_mode": "inline",
}


__all__ = [
    "mouse_control",
    "screen_ocr",
    "detect_screen_elements",
    "window_tracking",
    "ui_automation",
    "steam_click_update_button",
    "browser_automation",
    "self_healing_click",
    "selector_memory",
]
