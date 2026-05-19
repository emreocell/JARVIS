"""Reasoning_Skill saf yardımcıları.

Bu modül HTTP, dosya I/O ve global durum içermez. ``RoutinePlanParser``
LLM ham yanıtını ``Routine``/``RoutineStep`` veri yapılarına çevirir;
geçersiz tool adları veya bozuk JSON bölümleri ``dropped_steps`` listesine
sebep mesajıyla birlikte düşer.

Sözleşme (Property 16, Requirements 6.3, 6.4, 6.8)::

    1. parse(raw, tools).routine.steps[*].tool ∈ tools (her zaman)
    2. Düşürülen her adım ``dropped_steps`` içinde sebep mesajıyla yer alır
    3. Bozuk JSON → ``routine.steps == []`` ve ``raw_text`` ham metni
       ``dropped_steps`` listesine sebep olarak eklenir

Tasarım kararı: Routine/RoutineStep mevcut ``runtime/types.py``
tanımlarından yeniden kullanılır (Routine_Engine doğrudan tüketebilsin).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from runtime.types import Routine, RoutineStep


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

_DEFAULT_ROUTINE_NAME = "dynamic_plan"
_VALID_ON_ERROR = ("continue", "stop")

# ``json`` dilinde kapanış fence'i ile çevrelenmiş bloklar; LLM'lerin
# kullandığı yaygın varyantlar (```json, ```JSON, ```python, çıplak ```).
_FENCE_RE = re.compile(r"```[A-Za-z0-9_-]*\s*(.+?)```", re.DOTALL)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class ParsedPlan:
    """``RoutinePlanParser.parse`` çıktısı.

    Attributes
    ----------
    routine:
        Plugin_Host'ta kayıtlı tool'lardan oluşan, çalıştırılabilir
        ``Routine``. Bozuk girdide ``steps`` boş listedir.
    dropped_steps:
        ``(tool_adi, sebep)`` çiftlerinin listesi. Bozuk JSON durumunda
        en az bir giriş ham yanıt metnini sebep alanında taşır
        (Requirement 6.8).
    raw_response:
        Parser'a verilen orijinal metin (debug ve loglama amaçlı korunur).
    """

    routine: Routine
    dropped_steps: list[tuple[str, str]] = field(default_factory=list)
    raw_response: str = ""


class RoutinePlanParser:
    """Saf, yan etkisiz LLM plan ayrıştırıcı.

    Tüm metodlar ``staticmethod``'tur; örnek durumu yoktur. Aynı girdi
    her çağrıda aynı çıktıyı üretir.
    """

    @staticmethod
    def parse(raw_text: str, registered_tools: Iterable[str]) -> ParsedPlan:
        """LLM ham çıktısını ``ParsedPlan``'a çevir.

        Parameters
        ----------
        raw_text:
            LLM'in ürettiği ham metin. Saf JSON, ``markdown`` kod bloğu
            içine sarılmış JSON veya çevresinde anlatım bulunan JSON
            kabul edilir.
        registered_tools:
            Plugin_Host'ta kayıtlı tool adlarının kümesi. Bu kümede
            olmayan adımlar düşürülür ve ``dropped_steps`` listesine
            sebep mesajıyla eklenir (Requirement 6.4).

        Returns
        -------
        ParsedPlan
            ``routine.steps`` her zaman yalnızca kayıtlı tool'lar içerir;
            ``dropped_steps`` filtrelenen ya da bozuk parçaları taşır.
        """
        raw = raw_text if isinstance(raw_text, str) else ""
        tools_set = {t for t in registered_tools if isinstance(t, str)}
        dropped: list[tuple[str, str]] = []

        payload, parse_error = _extract_first_json(raw)
        if payload is None:
            # Bozuk JSON: ham metni sebep olarak ekle (Requirement 6.8,
            # Property 16 invariant 3).
            reason = parse_error or "no_json_found"
            dropped.append(("", f"invalid_json: {reason}"))
            dropped.append(("", raw))
            return ParsedPlan(
                routine=Routine(name=_DEFAULT_ROUTINE_NAME, triggers=[], steps=[]),
                dropped_steps=dropped,
                raw_response=raw,
            )

        routine_name, triggers, raw_steps = _extract_routine_fields(payload)

        valid_steps: list[RoutineStep] = []
        for index, item in enumerate(raw_steps):
            step, reason = _coerce_step(item, index)
            if step is None:
                tool_label = ""
                if isinstance(item, dict):
                    tool_label = _safe_str(item.get("tool"))
                dropped.append((tool_label, reason or f"invalid_step[{index}]"))
                continue
            if step.tool not in tools_set:
                dropped.append(
                    (
                        step.tool,
                        f"unregistered_tool: '{step.tool}' Plugin_Host'ta kayıtlı değil",
                    )
                )
                continue
            valid_steps.append(step)

        routine = Routine(
            name=routine_name or _DEFAULT_ROUTINE_NAME,
            triggers=triggers,
            steps=valid_steps,
        )
        return ParsedPlan(routine=routine, dropped_steps=dropped, raw_response=raw)


# ---------------------------------------------------------------------------
# Module-private saf yardımcılar
# ---------------------------------------------------------------------------


def _extract_first_json(text: str) -> tuple[Any | None, str | None]:
    """Metinden ilk geçerli JSON nesnesini ya da listesini çıkarmayı dener.

    Aday sırası:
      1. Tüm metni doğrudan ``json.loads``
      2. ``\u0060\u0060\u0060...\u0060\u0060\u0060`` kod bloklarının içerikleri
      3. İlk dengelenmiş ``{...}`` ve ``[...]`` parçaları

    Sadece ``dict`` veya ``list`` kökü kabul edilir; sayı/dize/bool
    kökleri "JSON şeması uygunsuz" olarak hata sayılır.

    Returns
    -------
    (payload, None) — başarı; (None, last_error_message) — başarısızlık.
    """
    if not text:
        return None, "empty_input"

    candidates: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    _add(text)

    try:
        for match in _FENCE_RE.finditer(text):
            _add(match.group(1))
    except re.error:  # pragma: no cover — sabit desen, savunma amaçlı
        pass

    for opener, closer in (("{", "}"), ("[", "]")):
        sliced = _slice_balanced(text, opener, closer)
        if sliced is not None:
            _add(sliced)

    last_error: str | None = None
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = str(exc)
            continue
        if isinstance(value, (dict, list)):
            return value, None
        last_error = f"unsupported_json_root: {type(value).__name__}"

    return None, last_error


def _slice_balanced(text: str, opener: str, closer: str) -> str | None:
    """``opener`` ile başlayan ilk dengelenmiş alt diziyi döner.

    Tek/çift tırnaklı dize bölgelerindeki süslü/köşeli parantezler
    derinlik sayımına dahil edilmez. Eşleşme bulunamazsa ``None``.
    """
    start = text.find(opener)
    if start == -1:
        return None

    depth = 0
    in_string = False
    quote_char = ""
    escape = False

    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == quote_char:
                in_string = False
            continue
        if ch == '"' or ch == "'":
            in_string = True
            quote_char = ch
            continue
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return text[start : idx + 1]

    return None


def _extract_routine_fields(payload: Any) -> tuple[str, list[str], list[Any]]:
    """JSON yükünden routine adı, trigger'lar ve step listesini çıkarır.

    Kabul edilen şekiller::

        # 1) Düz routine sözlüğü
        {"name": str?, "triggers": list[str]?, "steps": list[step]}

        # 2) ``routine`` anahtarı altında sarılmış sözlük
        {"routine": {"name": ..., "steps": [...]}}

        # 3) ``routine`` doğrudan step listesi
        {"name": ..., "routine": [step, step]}

        # 4) Doğrudan step listesi
        [step, step, ...]
    """
    if isinstance(payload, list):
        return "", [], list(payload)

    if not isinstance(payload, dict):
        return "", [], []

    # ``routine`` sarmalayıcısı varsa içeriğini öncelikli al.
    container: dict = payload
    routine_field = payload.get("routine")
    if isinstance(routine_field, dict):
        container = routine_field
    elif isinstance(routine_field, list):
        return (
            _safe_str(payload.get("name")),
            _safe_str_list(payload.get("triggers")),
            list(routine_field),
        )

    name = _safe_str(container.get("name"))
    triggers = _safe_str_list(container.get("triggers"))
    steps_raw = container.get("steps", [])
    steps_list = list(steps_raw) if isinstance(steps_raw, list) else []
    return name, triggers, steps_list


def _coerce_step(item: Any, index: int) -> tuple[RoutineStep | None, str | None]:
    """Tek bir step girişini ``RoutineStep``'e çevirmeye çalışır.

    Geçersiz girdi için ``(None, sebep)`` döner. Tool adının kayıtlı
    olup olmadığı kontrolü çağrı yerine bırakılır.
    """
    if not isinstance(item, dict):
        return None, f"invalid_step[{index}]: not_a_dict"

    tool_raw = item.get("tool")
    if not isinstance(tool_raw, str) or not tool_raw.strip():
        return None, f"invalid_step[{index}]: missing_tool"
    tool = tool_raw.strip()

    args_raw = item.get("args", {})
    args: dict = dict(args_raw) if isinstance(args_raw, dict) else {}

    on_error_raw = item.get("on_error", "continue")
    on_error = on_error_raw if on_error_raw in _VALID_ON_ERROR else "continue"

    name_raw = item.get("name", "")
    name = name_raw if isinstance(name_raw, str) else ""

    return RoutineStep(tool=tool, args=args, on_error=on_error, name=name), None


def _safe_str(value: Any) -> str:
    """Bir değeri string'e indirgemenin güvenli yolu.

    ``None`` ve string-olmayan tipler boş dize olarak döner; bu sayede
    parser hiçbir aşamada ``TypeError`` üretmez.
    """
    if isinstance(value, str):
        return value.strip()
    return ""


def _safe_str_list(value: Any) -> list[str]:
    """Bir değeri ``list[str]``'e indirgemenin güvenli yolu."""
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str)]


__all__ = ["ParsedPlan", "RoutinePlanParser"]
