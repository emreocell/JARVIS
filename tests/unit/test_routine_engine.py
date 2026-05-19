"""Unit tests for RoutineEngine.run_dynamic (Req 6.6, 17.2).

Doğrulanan davranışlar:
- run_dynamic, verilen Routine'i adım adım çalıştırır ve RoutineRunReport döner.
- run_dynamic, _routines listesini değiştirmez (routines.json'a dokunmaz).
- run_dynamic, on_error="stop" adımında döngüyü keser.
- run_dynamic, on_error="continue" adımında döngüye devam eder.
- Mevcut match() / load() akışı bozulmaz.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from runtime.routine_engine import RoutineEngine
from runtime.types import Routine, RoutineRunReport, RoutineStep


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(routines_json: list | None = None) -> tuple[RoutineEngine, Path]:
    """Geçici bir routines.json ile RoutineEngine oluşturur."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(routines_json or [], f)
        path = Path(f.name)
    engine = RoutineEngine(routines_path=path)
    return engine, path


def _make_runtime(dispatch_results: dict[str, object] | None = None) -> MagicMock:
    """Basit bir mock Tool_Runtime üretir."""
    runtime = MagicMock()
    dispatch_results = dispatch_results or {}

    async def _dispatch(tool: str, args: dict, voice=None):
        if tool in dispatch_results:
            result = dispatch_results[tool]
            if isinstance(result, Exception):
                raise result
            return result
        return f"ok:{tool}"

    runtime.dispatch = AsyncMock(side_effect=_dispatch)
    return runtime


def _run(coro):
    """asyncio.run() wrapper — Python 3.14 uyumlu."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRunDynamic:
    """run_dynamic davranış testleri."""

    def test_run_dynamic_returns_report(self):
        """run_dynamic bir RoutineRunReport döner."""
        engine, _ = _make_engine()
        engine.set_runtime(_make_runtime())

        routine = Routine(
            name="test_plan",
            triggers=[],
            steps=[
                RoutineStep(tool="tool_a", args={}, name="adım_a"),
                RoutineStep(tool="tool_b", args={}, name="adım_b"),
            ],
        )

        report = _run(engine.run_dynamic(routine))

        assert isinstance(report, RoutineRunReport)
        assert report.routine == "test_plan"
        assert "adım_a" in report.completed
        assert "adım_b" in report.completed
        assert report.failed == []
        assert report.duration_sec >= 0.0

    def test_run_dynamic_does_not_modify_routines_list(self):
        """run_dynamic, _routines listesini değiştirmez (Req 6.6, 17.2)."""
        static_routines = [
            {
                "name": "sabah_rutini",
                "triggers": ["günaydın"],
                "steps": [{"tool": "weather_get", "args": {}}],
            }
        ]
        engine, _ = _make_engine(static_routines)
        engine.set_runtime(_make_runtime())

        before = [r.name for r in engine.list_routines()]

        dynamic = Routine(
            name="dynamic_plan",
            triggers=[],
            steps=[RoutineStep(tool="weather_get", args={})],
        )
        _run(engine.run_dynamic(dynamic))

        after = [r.name for r in engine.list_routines()]
        assert before == after, "run_dynamic _routines listesini değiştirmemeli"

    def test_run_dynamic_on_error_continue(self):
        """on_error='continue' adımında hata sonrası döngü devam eder."""
        engine, _ = _make_engine()
        engine.set_runtime(
            _make_runtime({"tool_fail": RuntimeError("simüle hata")})
        )

        routine = Routine(
            name="plan",
            triggers=[],
            steps=[
                RoutineStep(tool="tool_fail", args={}, on_error="continue", name="fail_step"),
                RoutineStep(tool="tool_ok", args={}, on_error="continue", name="ok_step"),
            ],
        )

        report = _run(engine.run_dynamic(routine))

        assert "ok_step" in report.completed
        assert any(s == "fail_step" for s, _ in report.failed)

    def test_run_dynamic_on_error_stop(self):
        """on_error='stop' adımında hata sonrası döngü kesilir."""
        engine, _ = _make_engine()
        engine.set_runtime(
            _make_runtime({"tool_fail": RuntimeError("simüle hata")})
        )

        routine = Routine(
            name="plan",
            triggers=[],
            steps=[
                RoutineStep(tool="tool_fail", args={}, on_error="stop", name="fail_step"),
                RoutineStep(tool="tool_ok", args={}, on_error="continue", name="ok_step"),
            ],
        )

        report = _run(engine.run_dynamic(routine))

        assert "ok_step" not in report.completed
        assert any(s == "fail_step" for s, _ in report.failed)

    def test_run_dynamic_empty_steps(self):
        """Adımsız rutin boş rapor döner."""
        engine, _ = _make_engine()
        engine.set_runtime(_make_runtime())

        routine = Routine(name="bos_plan", triggers=[], steps=[])
        report = _run(engine.run_dynamic(routine))

        assert report.completed == []
        assert report.failed == []

    def test_run_dynamic_no_runtime_raises(self):
        """Tool_Runtime atanmamışsa RuntimeError fırlatır."""
        engine, _ = _make_engine()
        # set_runtime çağrılmadı

        routine = Routine(
            name="plan",
            triggers=[],
            steps=[RoutineStep(tool="tool_a", args={})],
        )

        with pytest.raises(RuntimeError, match="Tool_Runtime atanmamış"):
            _run(engine.run_dynamic(routine))

    def test_existing_match_flow_unaffected(self):
        """Mevcut match() akışı run_dynamic sonrasında bozulmaz (Req 17.2)."""
        static_routines = [
            {
                "name": "sabah_rutini",
                "triggers": ["günaydın", "sabah"],
                "steps": [{"tool": "weather_get", "args": {}}],
            }
        ]
        engine, _ = _make_engine(static_routines)
        engine.set_runtime(_make_runtime())

        # Dinamik plan çalıştır
        dynamic = Routine(name="dynamic", triggers=[], steps=[])
        _run(engine.run_dynamic(dynamic))

        # match() hâlâ çalışıyor olmalı
        matched = engine.match("günaydın jarvis")
        assert matched is not None
        assert matched.name == "sabah_rutini"

        # Eşleşmeyen utterance None döner
        assert engine.match("tamamen alakasız bir şey") is None

    def test_run_dynamic_report_has_duration(self):
        """RoutineRunReport.duration_sec sıfırdan büyük veya eşit olmalı."""
        engine, _ = _make_engine()
        engine.set_runtime(_make_runtime())

        routine = Routine(
            name="plan",
            triggers=[],
            steps=[RoutineStep(tool="tool_a", args={})],
        )
        report = _run(engine.run_dynamic(routine))
        assert report.duration_sec >= 0.0

    def test_run_dynamic_step_label_uses_name_field(self):
        """Adım etiketi 'name' alanından alınır; yoksa 'tool' adı kullanılır."""
        engine, _ = _make_engine()
        engine.set_runtime(_make_runtime())

        routine = Routine(
            name="plan",
            triggers=[],
            steps=[
                RoutineStep(tool="tool_x", args={}, name="özel_ad"),
                RoutineStep(tool="tool_y", args={}, name=""),  # boş name → tool adı
            ],
        )
        report = _run(engine.run_dynamic(routine))

        assert "özel_ad" in report.completed
        assert "tool_y" in report.completed
