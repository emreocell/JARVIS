"""Routine_Engine — kullanıcı tanımlı adım dizilerini çalıştırır.

Design.md § 11 ve Requirements § 23'e karşılık gelir.

Sorumluluklar
-------------
* ``routines.json`` dosyasını yükler; her rutin ``name``, ``triggers`` ve
  ``steps`` alanlarına sahiptir.
* ``match(utterance)`` — Türkçe normalize (lowercase + diacritic temizliği)
  sonrası trigger substring eşleşmesi yapar.
* ``async run(routine)`` — her adımı Tool_Runtime üzerinden dispatch eder;
  başarısız adım ``RoutineRunReport.failed``'a eklenir, döngü devam eder
  (Req 23.4).
* ``async run_dynamic(routine)`` — Reasoning_Skill'den gelen dinamik
  ``Routine`` objelerini ``routines.json``'u **değiştirmeden** in-memory
  koşturur ve ``RoutineRunReport`` döner (Req 6.6, 17.2).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING

from runtime.types import Routine, RoutineRunReport, RoutineStep

if TYPE_CHECKING:
    from runtime.tool_runtime import ToolRuntime

log = logging.getLogger(__name__)


def _normalize(text: str) -> str:
    """Türkçe dahil tüm karakterleri ASCII-safe lowercase'e indirir."""
    nfkd = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in nfkd if not unicodedata.combining(c))


class RoutineEngine:
    """Rutin yükleyici ve çalıştırıcı.

    Parameters
    ----------
    routines_path:
        ``routines.json`` dosyasının yolu. Varsayılan: proje kökündeki
        ``routines.json``.
    tool_runtime:
        Adımları dispatch etmek için kullanılan Tool_Runtime örneği.
        ``None`` ise ``run()`` çağrısı öncesinde ``set_runtime()`` ile
        atanmalıdır.
    """

    def __init__(
        self,
        routines_path: Path | str | None = None,
        tool_runtime: "ToolRuntime | None" = None,
    ) -> None:
        if routines_path is None:
            routines_path = Path(__file__).resolve().parent.parent / "routines.json"
        self._path = Path(routines_path)
        self._routines: list[Routine] = []
        self._runtime = tool_runtime
        self.load(self._path)

    # ------------------------------------------------------------------ public

    def set_runtime(self, runtime: "ToolRuntime") -> None:
        """Tool_Runtime referansını sonradan ata (bootstrap sırası için)."""
        self._runtime = runtime

    def load(self, path: Path | str | None = None) -> None:
        """``routines.json`` dosyasını (yeniden) yükle.

        Dosya yoksa veya bozuksa uyarı loglanır ve mevcut rutin listesi
        korunur (ilk yüklemede boş liste kalır).
        """
        target = Path(path) if path is not None else self._path
        if not target.exists():
            log.warning("RoutineEngine: routines.json bulunamadı: %s", target)
            return
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("RoutineEngine: routines.json okunamadı (%s): %s", target, exc)
            return

        routines: list[Routine] = []
        for item in raw:
            try:
                steps = [
                    RoutineStep(
                        tool=s["tool"],
                        args=s.get("args", {}),
                        on_error=s.get("on_error", "continue"),
                        name=s.get("name", s["tool"]),
                    )
                    for s in item.get("steps", [])
                ]
                routines.append(
                    Routine(
                        name=item["name"],
                        triggers=item.get("triggers", []),
                        steps=steps,
                    )
                )
            except Exception as exc:
                log.warning("RoutineEngine: rutin ayrıştırılamadı (%s): %s", item, exc)

        self._routines = routines
        log.debug("RoutineEngine: %d rutin yüklendi.", len(self._routines))

    def list_routines(self) -> list[Routine]:
        """Yüklü rutinlerin kopyasını döner."""
        return list(self._routines)

    def match(self, utterance: str) -> Routine | None:
        """Utterance'a uyan ilk rutini döner; eşleşme yoksa ``None``.

        Eşleşme: normalize edilmiş utterance içinde normalize edilmiş
        trigger substring olarak geçiyor mu? (Req 23.1)
        """
        norm = _normalize(utterance)
        for routine in self._routines:
            for trigger in routine.triggers:
                if _normalize(trigger) in norm:
                    return routine
        return None

    async def run(self, routine: Routine) -> RoutineRunReport:
        """Rutini adım adım çalıştır ve rapor döner.

        Her adım Tool_Runtime üzerinden dispatch edilir. Adım başarısız
        olursa ``on_error == "stop"`` ise döngü kesilir; ``"continue"``
        ise devam eder (Req 23.3, 23.4).
        """
        if self._runtime is None:
            raise RuntimeError("RoutineEngine: Tool_Runtime atanmamış; set_runtime() çağırın.")

        report = RoutineRunReport(routine=routine.name)
        t0 = time.monotonic()

        for step in routine.steps:
            step_label = step.name or step.tool
            try:
                result = await self._runtime.dispatch(step.tool, step.args, voice=None)
                report.completed.append(step_label)
                log.debug("RoutineEngine: adım '%s' tamamlandı: %s", step_label, result)
            except Exception as exc:
                err_msg = f"{type(exc).__name__}: {exc}"
                report.failed.append((step_label, err_msg))
                log.warning("RoutineEngine: adım '%s' başarısız: %s", step_label, err_msg)
                if step.on_error == "stop":
                    log.info("RoutineEngine: on_error=stop, rutin durduruldu.")
                    break

        report.duration_sec = time.monotonic() - t0
        return report

    async def run_dynamic(self, routine: Routine) -> RoutineRunReport:
        """Reasoning_Skill'den gelen dinamik planı in-memory koştur.

        ``routines.json`` dosyasını **değiştirmez** ve ``_routines`` listesine
        ekleme yapmaz. Yalnızca verilen ``Routine`` objesini adım adım
        çalıştırır ve ``RoutineRunReport`` döner (Req 6.6, 17.2).

        Bu metod ``run()`` ile aynı dispatch mantığını kullanır; fark yalnızca
        sözleşme düzeyindedir: çağıran taraf dinamik bir planı kalıcı hale
        getirmeden çalıştırmak istediğini açıkça belirtmiş olur.

        Parameters
        ----------
        routine:
            Reasoning_Skill'in ``RoutinePlanParser`` aracılığıyla ürettiği
            geçici ``Routine`` objesi. ``triggers`` listesi boş olabilir;
            bu metod trigger eşleşmesi yapmaz.

        Returns
        -------
        RoutineRunReport
            Tamamlanan ve başarısız adımların listesi ile toplam süreyi içerir.
        """
        log.debug(
            "RoutineEngine.run_dynamic: dinamik plan '%s' (%d adım) çalıştırılıyor.",
            routine.name,
            len(routine.steps),
        )
        return await self.run(routine)


__all__ = ["RoutineEngine"]
