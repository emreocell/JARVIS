"""Theme_Engine — HUD renk paleti yönetimi.

Bu modül design.md § 18 ve requirements.md § 30'a karşılık gelir.

Sorumluluklar
-------------

* En az üç yerleşik tema sunmak (Req 30.1):
  - "Teal Core"     → mevcut HUD paleti (varsayılan).
  - "Crimson Core"  → kırmızı/altın aksanlı koyu palet.
  - "Iris Core"     → mor/turkuaz aksanlı koyu palet.
* `apply(name)` çağrısıyla aktif temayı değiştirmek ve abone HUD
  widget'larına bildirmek (Req 30.2: 300 ms içinde geçiş — canvas
  widget'ları her tick'te `current()` okuduğundan tetikleyici sadece
  güncel referansı güncellemekle yükümlüdür).
* Kullanıcı seçimini `app_config.theme` üzerinden kalıcı kaydetmek
  (Req 30.3).
* Bilinmeyen / bozuk tema adında "Teal Core" varsayılanına geri düşmek
  (Req 30.4).

Tasarım kararları
-----------------

* Tema verisi `runtime.types.Theme` dataclass'ına yazılır; renkler
  `#RRGGBB` hex string formatındadır.
* `ThemeEngine` Tkinter'a doğrudan bağımlı değildir; HUD katmanı
  `subscribe(cb)` ile aboneliğe geçer ve `apply` sırasında `cb(theme)`
  çağrısı alır. Bu sayede başsız (headless) testler mümkün olur.
* Persistans `app_config` modülüne delege edilir; testler için
  `app_config_loader` / `app_config_saver` parametreleri override
  edilebilir (varsayılan: gerçek `app_config.load_app_config` /
  `app_config.save_app_config`).
* Thread-safety: birden fazla thread (HUD, settings paneli, hotkey)
  aynı anda `apply` çağırabileceğinden iç state `threading.RLock` ile
  korunur.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Iterable

from runtime.types import Theme


log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Yerleşik temalar (Req 30.1)
# ---------------------------------------------------------------------------

DEFAULT_THEME_NAME = "Teal Core"


_BUILTIN_THEMES: tuple[Theme, ...] = (
    # "Teal Core" — mevcut HUD paleti (ui.py'deki C_BG/C_PRI/... ile uyumlu).
    Theme(
        name="Teal Core",
        bg="#020c0c",
        primary="#00d4c0",
        accent="#7dfff6",
        danger="#ff3344",
        text="#7dfff6",
        gradient_start="#020c0c",
        gradient_end="#006a62",
        halo_alpha=0.60,
    ),
    # "Crimson Core" — kırmızı/altın aksanlı koyu palet.
    Theme(
        name="Crimson Core",
        bg="#0c0303",
        primary="#ff3344",
        accent="#ffcc00",
        danger="#ff6600",
        text="#ffd2c0",
        gradient_start="#0c0303",
        gradient_end="#5a0a0a",
        halo_alpha=0.65,
    ),
    # "Iris Core" — mor/turkuaz aksanlı koyu palet.
    Theme(
        name="Iris Core",
        bg="#0a0418",
        primary="#a070ff",
        accent="#00d4c0",
        danger="#ff3388",
        text="#dcc8ff",
        gradient_start="#0a0418",
        gradient_end="#3b1a6e",
        halo_alpha=0.62,
    ),
)


def _clone(theme: Theme) -> Theme:
    """`Theme` için güvenli sığ kopya — dış çağıranların paleti kazara
    mutate etmesini engeller (dataclass alanları immutable string/float
    olduğu için sığ kopya yeterli)."""
    return Theme(
        name=theme.name,
        bg=theme.bg,
        primary=theme.primary,
        accent=theme.accent,
        danger=theme.danger,
        text=theme.text,
        gradient_start=theme.gradient_start,
        gradient_end=theme.gradient_end,
        halo_alpha=theme.halo_alpha,
    )


def builtin_themes() -> list[Theme]:
    """Yerleşik tema listesinin sığ kopyalarını döner."""
    return [_clone(t) for t in _BUILTIN_THEMES]


# ---------------------------------------------------------------------------
# ThemeEngine
# ---------------------------------------------------------------------------


ThemeListener = Callable[[Theme], None]


class ThemeEngine:
    """HUD tema yöneticisi.

    Parameters
    ----------
    extra_themes:
        Yerleşik üç temaya ek olarak kaydedilecek özel temalar. Boş
        bırakılabilir.
    app_config_loader / app_config_saver:
        `app_config` modülüne karşı bağımlılığı test edilebilir kılan
        kanca. Varsayılan: gerçek `app_config.load_app_config` /
        `save_app_config`. `None` verilirse persistans devre dışı kalır
        (testlerde memory-only kullanım için).
    initial_name:
        Constructor'da `apply` ile uygulanacak tema adı. `None` ise
        `app_config_loader` aracılığıyla yüklenir; o da yoksa
        `DEFAULT_THEME_NAME` kullanılır. Bilinmeyen ad bozuk tema
        kabul edilir ve "Teal Core" varsayılanına düşülür (Req 30.4).
    """

    def __init__(
        self,
        *,
        extra_themes: Iterable[Theme] = (),
        app_config_loader: Callable[[], dict] | None = None,
        app_config_saver: Callable[[dict], dict] | None = None,
        initial_name: str | None = None,
    ) -> None:
        # Tema kayıt defteri: ad → Theme. İsim büyük/küçük harf duyarlı
        # kalır (kullanıcıya gösterilen ad olduğu için), arama
        # `_resolve` içinde case-insensitive normalize edilir.
        self._themes: dict[str, Theme] = {}
        for theme in _BUILTIN_THEMES:
            self._themes[theme.name] = _clone(theme)

        for theme in extra_themes:
            if not isinstance(theme, Theme):
                raise TypeError(f"extra_themes Theme örneği bekler, alındı: {type(theme)!r}")
            self._themes[theme.name] = _clone(theme)

        # Persistans kancaları — testlerde None geçilebilir.
        if app_config_loader is None and app_config_saver is None:
            try:
                from app_config import load_app_config, save_app_config
            except Exception as exc:  # pragma: no cover - dev ortamı
                log.debug("ThemeEngine: app_config yüklenemedi (%s); persistans pasif.", exc)
                app_config_loader = None
                app_config_saver = None
            else:
                app_config_loader = load_app_config
                app_config_saver = save_app_config

        self._loader = app_config_loader
        self._saver = app_config_saver

        self._lock = threading.RLock()
        self._listeners: list[ThemeListener] = []
        # Aktif temayı placeholder olarak belirle; aşağıda apply ile düzeltilecek.
        self._current: Theme = self._themes[DEFAULT_THEME_NAME]

        # Başlangıç teması: explicit > app_config > default.
        chosen = initial_name
        if chosen is None and self._loader is not None:
            try:
                cfg = self._loader() or {}
                chosen = cfg.get("theme")
            except Exception as exc:
                log.warning("ThemeEngine: app_config okunamadı (%s); default kullanılıyor.", exc)
                chosen = None

        if chosen is None:
            chosen = DEFAULT_THEME_NAME

        # `apply` persistans yapacak; başlangıçta zaten config'ten okuduysak
        # gereksiz disk yazımına girmemek için manuel set ediyoruz.
        # (Req 30.4: bilinmeyen ad → default'a düş.)
        resolved = self._resolve(chosen)
        if resolved is None:
            log.warning(
                "ThemeEngine: bilinmeyen tema %r; %r varsayılanına düşülüyor.",
                chosen,
                DEFAULT_THEME_NAME,
            )
            resolved = self._themes[DEFAULT_THEME_NAME]
        self._current = resolved

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list(self) -> list[Theme]:
        """Kayıtlı tüm temaları (yerleşik + özel) döner.

        Listenin sırası: yerleşik temalar tanım sırasında, sonra
        constructor'da verilen extra temalar. Dataclass kopyaları döner;
        çağıran güvenle modifiye edebilir.
        """
        with self._lock:
            return [_clone(t) for t in self._themes.values()]

    def names(self) -> list[str]:
        """Kayıtlı tema adlarının listesini döner."""
        with self._lock:
            return list(self._themes.keys())

    def get(self, name: str) -> Theme | None:
        """Verilen ada karşılık gelen temayı döner; bulunamazsa None.

        Arama case-insensitive yapılır; örn. "teal core" → "Teal Core".
        """
        with self._lock:
            theme = self._resolve(name)
            return _clone(theme) if theme is not None else None

    def current(self) -> Theme:
        """Aktif temanın kopyasını döner."""
        with self._lock:
            return _clone(self._current)

    def apply(self, name: str) -> Theme:
        """`name` temasını aktive et, persist et ve abonelere bildir.

        Bilinmeyen / bozuk ad: uyarı loglanır ve "Teal Core"
        varsayılanına düşülür (Req 30.4). Persistans `app_config_saver`
        üzerinden yapılır; saver `None` ise yalnızca bellekte uygulanır.

        Returns
        -------
        Theme
            Uygulanan temanın kopyası.
        """
        with self._lock:
            resolved = self._resolve(name)
            if resolved is None:
                log.warning(
                    "ThemeEngine.apply: bilinmeyen tema %r; %r varsayılanına düşülüyor.",
                    name,
                    DEFAULT_THEME_NAME,
                )
                resolved = self._themes[DEFAULT_THEME_NAME]

            self._current = resolved
            applied_copy = _clone(resolved)
            listeners = list(self._listeners)

        # Persistansı kilit dışında yap; disk I/O sırasında diğer çağrılar
        # bloklanmasın. Saver hatası HUD'u kilitlemez; sadece loglanır.
        if self._saver is not None:
            try:
                self._saver({"theme": applied_copy.name})
            except Exception as exc:  # pragma: no cover - disk hatası nadir
                log.warning(
                    "ThemeEngine.apply: tema diske kaydedilemedi (%s).", exc
                )

        # Aboneleri kilit dışında bilgilendir; bir abone hata fırlatırsa
        # diğerlerini etkilemesin.
        for cb in listeners:
            try:
                cb(_clone(applied_copy))
            except Exception:
                log.exception("ThemeEngine: abone callback'i hata fırlattı.")

        return applied_copy

    def register(self, theme: Theme) -> None:
        """Yeni bir tema kaydet veya mevcut adı override et.

        Yerleşik bir adı override etmek de mümkündür; bu, örn. kullanıcı
        özel renk paleti kullandığında devreye girer.
        """
        if not isinstance(theme, Theme):
            raise TypeError(f"register Theme örneği bekler, alındı: {type(theme)!r}")
        with self._lock:
            self._themes[theme.name] = _clone(theme)

    def subscribe(self, callback: ThemeListener) -> Callable[[], None]:
        """`callback(theme)` olarak çağrılacak abone ekler.

        Returns
        -------
        Callable[[], None]
            Aboneliği iptal etmek için çağrılabilir handle.
        """
        if not callable(callback):
            raise TypeError("subscribe çağrılabilir bekler")
        with self._lock:
            self._listeners.append(callback)

        def _unsubscribe() -> None:
            self.unsubscribe(callback)

        return _unsubscribe

    def unsubscribe(self, callback: ThemeListener) -> None:
        """Aboneliği iptal et. Callback abone değilse no-op."""
        with self._lock:
            try:
                self._listeners.remove(callback)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve(self, name: str | None) -> Theme | None:
        """Verilen adı kayıtlı bir Theme'a normalize eder.

        Eşleşme önce tam ad, sonra case-insensitive olarak aranır.
        Bilinmeyen ad veya boş giriş için `None` döner.
        """
        if not isinstance(name, str) or not name:
            return None

        # Tam eşleşme
        theme = self._themes.get(name)
        if theme is not None:
            return theme

        # Case-insensitive geri düşme
        lowered = name.casefold()
        for key, value in self._themes.items():
            if key.casefold() == lowered:
                return value
        return None


__all__ = [
    "DEFAULT_THEME_NAME",
    "ThemeEngine",
    "ThemeListener",
    "builtin_themes",
]
