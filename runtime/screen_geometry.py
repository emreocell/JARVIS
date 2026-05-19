"""Screen, monitor, and active-window geometry helpers for desktop control."""

from __future__ import annotations

import ctypes
import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> dict[str, int]:
        return {"x": int((self.left + self.right) / 2), "y": int((self.top + self.bottom) / 2)}

    def to_dict(self) -> dict[str, int]:
        return {
            "left": self.left,
            "top": self.top,
            "right": self.right,
            "bottom": self.bottom,
            "width": self.width,
            "height": self.height,
        }

    def contains(self, x: int | float, y: int | float) -> bool:
        return self.left <= int(x) < self.right and self.top <= int(y) < self.bottom


def _set_dpi_aware() -> None:
    if not hasattr(ctypes, "windll"):
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _rect_from_win32(rect: Any) -> Rect:
    return Rect(int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))


def get_monitors() -> list[dict[str, Any]]:
    """Return monitor rectangles in virtual-screen coordinates."""
    _set_dpi_aware()
    if not hasattr(ctypes, "windll"):
        return []

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
        ]

    monitors: list[dict[str, Any]] = []

    def _callback(hmonitor, _hdc, _lprect, _lparam):  # noqa: ANN001
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            monitor = _rect_from_win32(info.rcMonitor)
            work = _rect_from_win32(info.rcWork)
            monitors.append(
                {
                    "index": len(monitors),
                    "primary": bool(info.dwFlags & 1),
                    "rect": monitor.to_dict(),
                    "work_area": work.to_dict(),
                    "center": monitor.center,
                }
            )
        return 1

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.POINTER(RECT),
        ctypes.c_double,
    )
    user32.EnumDisplayMonitors(0, 0, monitor_enum_proc(_callback), 0)
    return monitors


def get_virtual_screen() -> dict[str, int]:
    monitors = get_monitors()
    if monitors:
        left = min(int(m["rect"]["left"]) for m in monitors)
        top = min(int(m["rect"]["top"]) for m in monitors)
        right = max(int(m["rect"]["right"]) for m in monitors)
        bottom = max(int(m["rect"]["bottom"]) for m in monitors)
        return Rect(left, top, right, bottom).to_dict()

    try:
        import pyautogui  # type: ignore[reportMissingImports]

        size = pyautogui.size()
        return Rect(0, 0, int(size.width), int(size.height)).to_dict()
    except Exception:
        return Rect(0, 0, 0, 0).to_dict()


def monitor_for_point(x: int | float, y: int | float) -> dict[str, Any] | None:
    monitors = get_monitors()
    for monitor in monitors:
        rect = Rect(
            int(monitor["rect"]["left"]),
            int(monitor["rect"]["top"]),
            int(monitor["rect"]["right"]),
            int(monitor["rect"]["bottom"]),
        )
        if rect.contains(x, y):
            return monitor
    return monitors[0] if monitors else None


def normalize_point(x: int | float, y: int | float, bounds: dict[str, int] | None = None) -> dict[str, Any]:
    """Return global and local coordinates plus monitor context."""
    gx = int(round(float(x)))
    gy = int(round(float(y)))
    monitor = monitor_for_point(gx, gy)
    virtual = get_virtual_screen()
    payload: dict[str, Any] = {
        "global": {"x": gx, "y": gy},
        "virtual_screen": virtual,
        "monitor": monitor,
    }
    if monitor:
        rect = monitor["rect"]
        width = max(1, int(rect["width"]))
        height = max(1, int(rect["height"]))
        payload["monitor_local"] = {
            "x": gx - int(rect["left"]),
            "y": gy - int(rect["top"]),
            "x_ratio": round((gx - int(rect["left"])) / width, 6),
            "y_ratio": round((gy - int(rect["top"])) / height, 6),
        }
    if bounds:
        left = int(bounds.get("left", 0))
        top = int(bounds.get("top", 0))
        width = max(1, int(bounds.get("right", left) - left))
        height = max(1, int(bounds.get("bottom", top) - top))
        payload["bounds_local"] = {
            "x": gx - left,
            "y": gy - top,
            "x_ratio": round((gx - left) / width, 6),
            "y_ratio": round((gy - top) / height, 6),
        }
    return payload


def get_active_window_info() -> dict[str, Any]:
    """Return foreground window title, pid, and rect where available."""
    _set_dpi_aware()
    if not hasattr(ctypes, "windll"):
        return {"ok": False, "error": "Win32 API kullanilamiyor."}

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return {"ok": False, "error": "Aktif pencere bulunamadi."}

    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    rect_raw = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect_raw))
    rect = _rect_from_win32(rect_raw)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    center = rect.center
    return {
        "ok": True,
        "hwnd": int(hwnd),
        "pid": int(pid.value),
        "title": str(buf.value or ""),
        "rect": rect.to_dict(),
        "center": center,
        "monitor": monitor_for_point(center["x"], center["y"]),
    }


def dump_geometry() -> str:
    return json.dumps(
        {
            "ok": True,
            "virtual_screen": get_virtual_screen(),
            "monitors": get_monitors(),
            "active_window": get_active_window_info(),
        },
        ensure_ascii=False,
    )


__all__ = [
    "Rect",
    "dump_geometry",
    "get_active_window_info",
    "get_monitors",
    "get_virtual_screen",
    "monitor_for_point",
    "normalize_point",
]
