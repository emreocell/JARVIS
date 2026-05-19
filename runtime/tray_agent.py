"""Tray_Agent — Windows sistem tepsisi entegrasyonu.

Design.md § 14 ve Requirements § 26'ya karşılık gelir.

Sorumluluklar
-------------
* ``infi.systray.SysTrayIcon`` ile sol tık göster/gizle (Req 26.2).
* Menü: "Aç", "Mikrofonu Sustur/Aç", "Privacy Mode", "Çıkış" (Req 26.3).
* "Çıkış" → tüm Background_Task'leri cancel, root.destroy (Req 26.4).
* "Windows ile başlat" toggle: HKCU Run anahtarına yaz (Req 26.5).
* ``tray_minimize_on_close == True`` ise pencereyi gizle ve tepside tut
  (Req 26.1).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from runtime.task_manager import TaskManager
    from runtime.privacy_mode import PrivacyMode

log = logging.getLogger(__name__)

_ICON_PATH = Path(__file__).resolve().parent.parent / "Icon" / "youtube.png"
_APP_NAME = "JARVIS"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class TrayAgent:
    """Windows sistem tepsisi ajanı.

    Parameters
    ----------
    task_manager:
        Çıkışta tüm görevleri iptal etmek için kullanılır.
    privacy_mode:
        "Privacy Mode" menü öğesi için.
    on_show:
        HUD penceresini göstermek için çağrılacak callback.
    on_hide:
        HUD penceresini gizlemek için çağrılacak callback.
    on_mute_toggle:
        Mikrofonu sustur/aç için çağrılacak callback.
    on_quit:
        Uygulama çıkışı için çağrılacak callback (root.destroy vb.).
    minimize_on_close:
        ``True`` ise pencere kapatma butonu gizleme yapar (Req 26.1).
    """

    def __init__(
        self,
        *,
        task_manager: "TaskManager | None" = None,
        privacy_mode: "PrivacyMode | None" = None,
        on_show: Callable[[], None] | None = None,
        on_hide: Callable[[], None] | None = None,
        on_mute_toggle: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        minimize_on_close: bool = False,
    ) -> None:
        self._task_manager = task_manager
        self._privacy = privacy_mode
        self._on_show = on_show
        self._on_hide = on_hide
        self._on_mute_toggle = on_mute_toggle
        self._on_quit = on_quit
        self._minimize_on_close = minimize_on_close
        self._visible = True
        self._tray = None
        self._thread: threading.Thread | None = None
        self._available = False

        try:
            from infi.systray import SysTrayIcon  # noqa: F401
            self._available = True
        except ImportError:
            log.warning("TrayAgent: 'infi.systray' paketi yüklü değil; tepsi devre dışı.")

    # ------------------------------------------------------------------ public

    def start(self) -> None:
        """Tepsi ikonunu arka plan thread'inde başlat."""
        if not self._available:
            return
        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._run_tray, daemon=True, name="jarvis-tray")
        self._thread.start()
        log.debug("TrayAgent: başlatıldı.")

    def stop(self) -> None:
        """Tepsi ikonunu kaldır."""
        if self._tray is not None:
            try:
                self._tray.shutdown()
            except Exception as exc:
                log.debug("TrayAgent.stop: %s", exc)
            self._tray = None

    def set_minimize_on_close(self, value: bool) -> None:
        """Pencere kapatma davranışını güncelle."""
        self._minimize_on_close = value

    def handle_window_close(self) -> bool:
        """Pencere kapatma butonuna basıldığında çağrılır.

        Returns
        -------
        bool
            ``True`` → pencereyi gizle (minimize_on_close aktif).
            ``False`` → normal kapatma işlemine devam et.
        """
        if self._minimize_on_close:
            self._hide_window()
            return True
        return False

    @staticmethod
    def set_start_with_windows(enabled: bool) -> bool:
        """HKCU Run anahtarını yaz/sil (Req 26.5).

        Returns
        -------
        bool
            İşlem başarılıysa ``True``.
        """
        try:
            import winreg
            exe = sys.executable
            script = str(Path(__file__).resolve().parent.parent / "main.py")
            cmd = f'"{exe}" "{script}"'
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
            ) as key:
                if enabled:
                    winreg.SetValueEx(key, _APP_NAME, 0, winreg.REG_SZ, cmd)
                    log.debug("TrayAgent: Windows başlangıcına eklendi.")
                else:
                    try:
                        winreg.DeleteValue(key, _APP_NAME)
                        log.debug("TrayAgent: Windows başlangıcından kaldırıldı.")
                    except FileNotFoundError:
                        pass
            return True
        except Exception as exc:
            log.warning("TrayAgent: başlangıç kaydı güncellenemedi: %s", exc)
            return False

    @staticmethod
    def is_start_with_windows() -> bool:
        """HKCU Run anahtarında JARVIS kaydı var mı?"""
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
                winreg.QueryValueEx(key, _APP_NAME)
            return True
        except Exception:
            return False

    # ---------------------------------------------------------------- internal

    def _run_tray(self) -> None:
        """Tepsi ikonunu oluştur ve mesaj döngüsünü çalıştır."""
        try:
            from infi.systray import SysTrayIcon

            menu_options = (
                ("Aç", None, self._cb_show),
                ("Mikrofonu Sustur/Aç", None, self._cb_mute),
                ("Privacy Mode", None, self._cb_privacy),
                ("Windows ile başlat", None, self._cb_start_with_windows),
            )

            icon = str(_ICON_PATH) if _ICON_PATH.exists() else None
            self._tray = SysTrayIcon(
                icon=icon,
                hover_text=_APP_NAME,
                menu_options=menu_options,
                on_quit=self._cb_quit,
                default_menu_index=0,
            )
            self._tray.start()
        except Exception as exc:
            log.warning("TrayAgent: tepsi ikonu başlatılamadı: %s", exc)

    def _show_window(self) -> None:
        if self._on_show:
            try:
                self._on_show()
            except Exception:
                log.exception("TrayAgent: on_show callback hatası.")
        self._visible = True

    def _hide_window(self) -> None:
        if self._on_hide:
            try:
                self._on_hide()
            except Exception:
                log.exception("TrayAgent: on_hide callback hatası.")
        self._visible = False

    def _cb_show(self, _tray=None) -> None:
        if self._visible:
            self._hide_window()
        else:
            self._show_window()

    def _cb_mute(self, _tray=None) -> None:
        if self._on_mute_toggle:
            try:
                self._on_mute_toggle()
            except Exception:
                log.exception("TrayAgent: on_mute_toggle callback hatası.")

    def _cb_privacy(self, _tray=None) -> None:
        if self._privacy is None:
            return
        try:
            if self._privacy.is_active():
                self._privacy.disable()
                log.debug("TrayAgent: Privacy Mode devre dışı.")
            else:
                self._privacy.enable()
                log.debug("TrayAgent: Privacy Mode aktif.")
        except Exception:
            log.exception("TrayAgent: privacy toggle hatası.")

    def _cb_start_with_windows(self, _tray=None) -> None:
        current = self.is_start_with_windows()
        self.set_start_with_windows(not current)

    def _cb_quit(self, _tray=None) -> None:
        """Çıkış: tüm görevleri iptal et, sonra quit callback'i çağır (Req 26.4)."""
        log.info("TrayAgent: çıkış sinyali alındı.")
        if self._task_manager is not None:
            try:
                self._task_manager.shutdown(wait=False, cancel_pending=True)
            except Exception:
                log.exception("TrayAgent: TaskManager shutdown hatası.")
        if self._on_quit:
            try:
                self._on_quit()
            except Exception:
                log.exception("TrayAgent: on_quit callback hatası.")


__all__ = ["TrayAgent"]
