"""Hotkey_Manager — global klavye kısayolu yönetimi.

Design.md § 17 ve Requirements § 29'a karşılık gelir.

Sorumluluklar
-------------
* ``keyboard`` paketi ile global hotkey ataması yapar.
* Çakışma kontrolü: Windows reserved set + halihazırda kayıtlı hotkey'ler;
  çakışma → atama reddedilir (Req 29.2, 29.3).
* Tetiklendiğinde Tool_Runtime.dispatch ilgili tool/routine için çağrılır
  (Req 29.4).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

# Windows'ta çakışmaması gereken sistem kısayolları (Req 29.2)
_WINDOWS_RESERVED: frozenset[str] = frozenset(
    {
        "ctrl+alt+del",
        "ctrl+alt+delete",
        "win+l",
        "win+d",
        "win+e",
        "win+r",
        "win+tab",
        "alt+f4",
        "ctrl+shift+esc",
        "ctrl+esc",
        "win+x",
        "win+i",
        "win+s",
        "win+a",
        "win+n",
        "win+k",
        "win+p",
        "win+g",
        "win+h",
        "win+v",
        "win+z",
        "win+f",
        "win+m",
        "win+b",
        "win+t",
        "win+u",
        "win+w",
        "win+c",
        "win+q",
        "win+j",
        "win+o",
        "win+y",
        "win+period",
        "win+semicolon",
        "win+space",
        "win+pause",
        "win+prtsc",
        "win+shift+s",
        "win+ctrl+d",
        "win+ctrl+f4",
        "win+ctrl+left",
        "win+ctrl+right",
    }
)


def _normalize_hotkey(hotkey: str) -> str:
    """Hotkey string'ini karşılaştırma için normalize eder."""
    return hotkey.strip().lower().replace(" ", "")


class HotkeyManager:
    """Global hotkey yöneticisi.

    Parameters
    ----------
    loop:
        Tool_Runtime dispatch çağrıları için asyncio event loop.
        ``None`` ise dispatch senkron çağrılır (test ortamı).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._loop = loop
        self._registered: dict[str, dict] = {}  # normalized_hotkey → {hotkey, action, handler}
        self._lock = threading.RLock()
        self._keyboard_available = False
        try:
            import keyboard as _kb  # noqa: F401
            self._keyboard_available = True
        except ImportError:
            log.warning("HotkeyManager: 'keyboard' paketi yüklü değil; hotkey'ler devre dışı.")

    # ------------------------------------------------------------------ public

    def register(
        self,
        hotkey: str,
        action: str,
        handler: Callable[[], None] | None = None,
        *,
        description: str = "",
    ) -> bool:
        """Hotkey'i kaydet.

        Parameters
        ----------
        hotkey:
            Örn. ``"ctrl+shift+space"``.
        action:
            Tool adı veya rutin adı (loglama ve çakışma raporlama için).
        handler:
            Tetiklendiğinde çağrılacak callable. ``None`` ise sadece
            kayıt yapılır (Tool_Runtime bağlantısı sonradan kurulur).
        description:
            İnsan tarafından okunabilir açıklama (Command_Palette için).

        Returns
        -------
        bool
            Kayıt başarılıysa ``True``, çakışma varsa ``False``.
        """
        norm = _normalize_hotkey(hotkey)

        # Windows reserved kontrolü (Req 29.2)
        if norm in _WINDOWS_RESERVED:
            log.warning(
                "HotkeyManager: '%s' Windows sistem kısayoluyla çakışıyor; reddedildi.",
                hotkey,
            )
            return False

        with self._lock:
            if norm in self._registered:
                existing = self._registered[norm]["action"]
                log.warning(
                    "HotkeyManager: '%s' zaten '%s' için kayıtlı; reddedildi.",
                    hotkey,
                    existing,
                )
                return False

            entry = {
                "hotkey": hotkey,
                "action": action,
                "description": description,
                "handler": handler,
            }
            self._registered[norm] = entry

        if self._keyboard_available and handler is not None:
            try:
                import keyboard
                keyboard.add_hotkey(hotkey, self._make_callback(norm), suppress=False)
                log.debug("HotkeyManager: '%s' → '%s' kaydedildi.", hotkey, action)
            except Exception as exc:
                log.warning("HotkeyManager: '%s' kaydedilemedi: %s", hotkey, exc)
                with self._lock:
                    self._registered.pop(norm, None)
                return False

        return True

    def unregister(self, hotkey: str) -> bool:
        """Hotkey kaydını kaldır."""
        norm = _normalize_hotkey(hotkey)
        with self._lock:
            if norm not in self._registered:
                return False
            self._registered.pop(norm)

        if self._keyboard_available:
            try:
                import keyboard
                keyboard.remove_hotkey(hotkey)
            except Exception as exc:
                log.debug("HotkeyManager: '%s' kaldırılırken hata: %s", hotkey, exc)

        return True

    def list_registered(self) -> list[dict]:
        """Kayıtlı hotkey'lerin listesini döner."""
        with self._lock:
            return [
                {
                    "hotkey": v["hotkey"],
                    "action": v["action"],
                    "description": v["description"],
                }
                for v in self._registered.values()
            ]

    def is_reserved(self, hotkey: str) -> bool:
        """Hotkey Windows sistem kısayoluyla çakışıyor mu?"""
        return _normalize_hotkey(hotkey) in _WINDOWS_RESERVED

    def is_registered(self, hotkey: str) -> bool:
        """Hotkey zaten kayıtlı mı?"""
        with self._lock:
            return _normalize_hotkey(hotkey) in self._registered

    def shutdown(self) -> None:
        """Tüm hotkey'leri kaldır."""
        if not self._keyboard_available:
            return
        try:
            import keyboard
            keyboard.unhook_all_hotkeys()
        except Exception as exc:
            log.debug("HotkeyManager.shutdown: %s", exc)
        with self._lock:
            self._registered.clear()

    # ---------------------------------------------------------------- internal

    def _make_callback(self, norm: str) -> Callable[[], None]:
        """Hotkey tetiklendiğinde çağrılacak closure üretir."""

        def _cb() -> None:
            with self._lock:
                entry = self._registered.get(norm)
            if entry is None:
                return
            handler = entry.get("handler")
            if handler is None:
                return
            try:
                handler()
            except Exception:
                log.exception("HotkeyManager: '%s' handler hatası.", entry["hotkey"])

        return _cb


__all__ = ["HotkeyManager"]
