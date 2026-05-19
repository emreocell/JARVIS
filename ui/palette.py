"""Command_Palette — Ctrl+Shift+Space ile açılan komut paleti.

Design.md § 12 ve Requirements § 24'e karşılık gelir.

Sorumluluklar
-------------
* Ctrl+Shift+Space ile HUD merkezinde Tk Toplevel açılır (Req 24.1).
* Tool descriptor + routine name/description fuzzy arama
  (``difflib.SequenceMatcher``); ilk 8 sonuç (Req 24.2).
* Enter → Tool_Runtime.dispatch (Req 24.3).
* Esc → kapat ve eski focus'a geri dön (Req 24.4).
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import threading
import tkinter as tk
from typing import TYPE_CHECKING, Callable

from runtime.text_normalize import normalize_tr

if TYPE_CHECKING:
    from runtime.tool_runtime import ToolRuntime
    from runtime.routine_engine import RoutineEngine

log = logging.getLogger(__name__)

# Renk sabitleri (HUD ile uyumlu)
_C_BG = "#020c0c"
_C_PANEL = "#030f0f"
_C_PRI = "#00d4c0"
_C_TEXT = "#7dfff6"
_C_DIM = "#0a2a28"
_C_MID = "#006a62"
_C_GOLD = "#ffcc00"
_C_RED = "#ff3344"

_MAX_RESULTS = 8
_PALETTE_WIDTH = 560
_PALETTE_HEIGHT = 340


def _fuzzy_score(query: str, text: str) -> float:
    """difflib SequenceMatcher ile benzerlik skoru (0.0–1.0).

    Hem sorgu hem de hedef metin :func:`normalize_tr` üzerinden geçirilir;
    böylece Türkçe karakter farkları (``"şarkı"`` ↔ ``"sarki"``) eşleşmeyi
    bozmaz (Req 14.8).
    """
    q = normalize_tr(query)
    t = normalize_tr(text)
    if not q:
        return 0.0
    if q in t:
        return 1.0
    return difflib.SequenceMatcher(None, q, t).ratio()


class CommandPalette:
    """HUD üzerinde açılan komut paleti.

    Parameters
    ----------
    root:
        Ana Tk penceresi (JarvisUI.root).
    tool_runtime:
        Tool dispatch için.
    routine_engine:
        Rutin listesi için.
    loop:
        asyncio event loop (dispatch için). ``None`` ise senkron çağrı.
    """

    def __init__(
        self,
        root: tk.Tk,
        *,
        tool_runtime: "ToolRuntime | None" = None,
        routine_engine: "RoutineEngine | None" = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._root = root
        self._runtime = tool_runtime
        self._routines = routine_engine
        self._loop = loop
        self._window: tk.Toplevel | None = None
        self._entry_var = tk.StringVar()
        self._items: list[dict] = []  # {label, type, name, description}
        self._filtered: list[dict] = []
        self._selected_idx = 0

        # Ctrl+Shift+Space global bağlama
        root.bind_all("<Control-Shift-space>", lambda e: self.open())

    # ------------------------------------------------------------------ public

    def set_runtime(self, runtime: "ToolRuntime") -> None:
        self._runtime = runtime

    def set_routine_engine(self, engine: "RoutineEngine") -> None:
        self._routines = engine

    def open(self) -> None:
        """Paleti aç veya zaten açıksa öne getir."""
        if self._window is not None and self._window.winfo_exists():
            self._window.lift()
            self._window.focus_force()
            return

        self._build_items()
        self._create_window()

    def close(self) -> None:
        """Paleti kapat."""
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass
            self._window = None

    def search(self, query: str) -> list[dict]:
        """Fuzzy arama; ilk _MAX_RESULTS sonucu döner."""
        if not query.strip():
            return self._items[:_MAX_RESULTS]

        scored = []
        for item in self._items:
            text = f"{item['name']} {item['description']}"
            score = _fuzzy_score(query, text)
            if score > 0.1:
                scored.append((score, item))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:_MAX_RESULTS]]

    # ---------------------------------------------------------------- internal

    def _build_items(self) -> None:
        """Tool ve rutin listesini yenile."""
        items: list[dict] = []

        # Tool'lar
        if self._runtime is not None:
            try:
                for decl in self._runtime.declarations():
                    items.append(
                        {
                            "label": f"[Tool] {decl.get('name', '')}",
                            "type": "tool",
                            "name": decl.get("name", ""),
                            "description": decl.get("description", ""),
                        }
                    )
            except Exception as exc:
                log.debug("CommandPalette: tool listesi alınamadı: %s", exc)

        # Rutinler
        if self._routines is not None:
            try:
                for routine in self._routines.list_routines():
                    items.append(
                        {
                            "label": f"[Rutin] {routine.name}",
                            "type": "routine",
                            "name": routine.name,
                            "description": ", ".join(routine.triggers),
                        }
                    )
            except Exception as exc:
                log.debug("CommandPalette: rutin listesi alınamadı: %s", exc)

        self._items = items

    def _create_window(self) -> None:
        """Palette Toplevel penceresini oluştur."""
        win = tk.Toplevel(self._root)
        win.title("")
        win.configure(bg=_C_BG)
        win.overrideredirect(True)  # başlık çubuğu yok
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.96)

        # HUD merkezine konumlandır
        rw = self._root.winfo_width()
        rh = self._root.winfo_height()
        rx = self._root.winfo_rootx()
        ry = self._root.winfo_rooty()
        x = rx + (rw - _PALETTE_WIDTH) // 2
        y = ry + (rh - _PALETTE_HEIGHT) // 3
        win.geometry(f"{_PALETTE_WIDTH}x{_PALETTE_HEIGHT}+{x}+{y}")

        # Başlık
        tk.Label(
            win,
            text="Komut Paleti",
            bg=_C_BG,
            fg=_C_PRI,
            font=("Grift", 11, "bold"),
            anchor="w",
            padx=12,
            pady=6,
        ).pack(fill="x")

        # Ayırıcı
        tk.Frame(win, bg=_C_MID, height=1).pack(fill="x")

        # Arama kutusu
        self._entry_var.set("")
        entry = tk.Entry(
            win,
            textvariable=self._entry_var,
            bg=_C_PANEL,
            fg=_C_TEXT,
            insertbackground=_C_PRI,
            font=("Grift", 12),
            borderwidth=0,
            highlightthickness=1,
            highlightcolor=_C_PRI,
            highlightbackground=_C_MID,
        )
        entry.pack(fill="x", padx=10, pady=(8, 4), ipady=6)
        entry.focus_set()

        # Sonuç listesi
        list_frame = tk.Frame(win, bg=_C_BG)
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 8))

        scrollbar = tk.Scrollbar(list_frame, bg=_C_DIM, troughcolor=_C_BG, width=8)
        scrollbar.pack(side="right", fill="y")

        listbox = tk.Listbox(
            list_frame,
            bg=_C_PANEL,
            fg=_C_TEXT,
            selectbackground=_C_MID,
            selectforeground=_C_PRI,
            font=("Grift", 11),
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            yscrollcommand=scrollbar.set,
        )
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)

        self._listbox = listbox
        self._window = win

        # İlk listeyi doldur
        self._update_list("")

        # Bağlamalar
        self._entry_var.trace_add("write", lambda *_: self._on_search_change())
        entry.bind("<Return>", lambda e: self._execute_selected())
        entry.bind("<Escape>", lambda e: self.close())
        entry.bind("<Down>", lambda e: self._move_selection(1))
        entry.bind("<Up>", lambda e: self._move_selection(-1))
        listbox.bind("<Return>", lambda e: self._execute_selected())
        listbox.bind("<Double-Button-1>", lambda e: self._execute_selected())
        listbox.bind("<Escape>", lambda e: self.close())

        # Dışarı tıklanınca kapat
        win.bind("<FocusOut>", self._on_focus_out)

    def _on_focus_out(self, event: tk.Event) -> None:
        """Pencere focus kaybedince kapat (kısa gecikmeyle)."""
        if self._window:
            self._window.after(150, self._check_focus)

    def _check_focus(self) -> None:
        if self._window and not self._window.focus_get():
            self.close()

    def _on_search_change(self) -> None:
        query = self._entry_var.get()
        self._update_list(query)

    def _update_list(self, query: str) -> None:
        self._filtered = self.search(query)
        self._listbox.delete(0, tk.END)
        for item in self._filtered:
            desc = f"  {item['label']}"
            if item["description"]:
                desc += f"  —  {item['description'][:50]}"
            self._listbox.insert(tk.END, desc)
        if self._filtered:
            self._listbox.selection_set(0)
            self._selected_idx = 0

    def _move_selection(self, delta: int) -> None:
        if not self._filtered:
            return
        size = len(self._filtered)
        idx = (self._selected_idx + delta) % size
        self._selected_idx = idx
        self._listbox.selection_clear(0, tk.END)
        self._listbox.selection_set(idx)
        self._listbox.see(idx)

    def _execute_selected(self) -> None:
        """Seçili öğeyi çalıştır."""
        sel = self._listbox.curselection()
        idx = sel[0] if sel else self._selected_idx
        if idx >= len(self._filtered):
            return

        item = self._filtered[idx]
        self.close()

        if item["type"] == "tool":
            self._dispatch_tool(item["name"])
        elif item["type"] == "routine":
            self._dispatch_routine(item["name"])

    def _dispatch_tool(self, name: str) -> None:
        """Tool'u dispatch et."""
        if self._runtime is None:
            log.warning("CommandPalette: Tool_Runtime bağlı değil.")
            return

        async def _run():
            try:
                await self._runtime.dispatch(name, {}, voice=None)
            except Exception as exc:
                log.warning("CommandPalette: tool '%s' dispatch hatası: %s", name, exc)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_run(), self._loop)
        else:
            log.debug("CommandPalette: asyncio loop yok, tool '%s' atlandı.", name)

    def _dispatch_routine(self, name: str) -> None:
        """Rutini çalıştır."""
        if self._routines is None:
            log.warning("CommandPalette: RoutineEngine bağlı değil.")
            return

        routine = next(
            (r for r in self._routines.list_routines() if r.name == name), None
        )
        if routine is None:
            log.warning("CommandPalette: rutin '%s' bulunamadı.", name)
            return

        async def _run():
            try:
                await self._routines.run(routine)
            except Exception as exc:
                log.warning("CommandPalette: rutin '%s' hatası: %s", name, exc)

        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(_run(), self._loop)
        else:
            log.debug("CommandPalette: asyncio loop yok, rutin '%s' atlandı.", name)


__all__ = ["CommandPalette"]
