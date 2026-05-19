"""Reasoning skill tool implementations.

İçerdiği handler'lar:

- :func:`plan_generate` — Doğal dil hedefini ``nvidia/llama-3.3-nemotron-super-49b-v1.5``
  (varsayılan) veya ultra modele (``nvidia/llama-3.1-nemotron-ultra-253b-v1`` ya da
  ``qwen/qwen3-next-80b-a3b-thinking``) göndererek Routine_Engine uyumlu adım listesi
  üretir. ``background`` modda çalışır.

- :func:`plan_explain` — Mevcut bir planı doğal dilde Türkçe açıklar. ``inline`` modda
  çalışır.

- :func:`plan_save` — Dinamik planı ``routines.json`` dosyasına kalıcı olarak yazar.
  Mevcut rutin adıyla çakışırsa onay bekler. ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, ultra model tetikleyicisini
   kontrol eder, kayıtlı tool listesini alır.
2. **Model_Router çağrısı** — NVIDIA NIM'e chat isteği gönderir.
3. **Türkçe yanıt formatlama** — ``RoutinePlanParser`` ile ``Routine``/``RoutineStep``'e
   çevirir; düşürülen adımları Türkçe uyarıyla raporlar.

Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.7, 6.8
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from skills.reasoning._internal import RoutinePlanParser

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

# Varsayılan (orta seviye) reasoning modeli (Req 6.5)
_DEFAULT_MODEL = "nvidia/llama-3.3-nemotron-super-49b-v1.5"

# Ultra reasoning modelleri — "derin düşün" / "ultra reasoning" tetikleyicisiyle
_ULTRA_MODELS = [
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "qwen/qwen3-next-80b-a3b-thinking",
]
_ULTRA_MODEL = _ULTRA_MODELS[0]

# "derin düşün" / "ultra reasoning" tetikleyici kalıpları (Req 6.5)
_ULTRA_TRIGGERS = re.compile(
    r"derin\s+d[uü][sş][uü]n|ultra\s+reasoning|ultra\s+d[uü][sş][uü]n",
    re.IGNORECASE,
)

NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# routines.json varsayılan yolu (proje kökü)
_ROUTINES_PATH = Path(__file__).resolve().parent.parent.parent / "routines.json"

# Sistem prompt'u: LLM'den JSON çıktı şemasını dayatır (Req 6.3)
_SYSTEM_PROMPT = """\
Sen bir görev planlama asistanısın. Kullanıcının doğal dil hedefini,
JARVIS Routine_Engine ile çalıştırılabilecek adım listesine çevirirsin.

Yanıtını YALNIZCA aşağıdaki JSON şemasında ver; başka metin ekleme:

{
  "name": "<rutin_adi>",
  "triggers": ["<tetikleyici_ifade>"],
  "steps": [
    {
      "tool": "<kayitli_tool_adi>",
      "args": { "<parametre>": "<deger>" },
      "on_error": "continue",
      "name": "<adim_aciklamasi>"
    }
  ]
}

Kurallar:
- "tool" alanı yalnızca JARVIS'te kayıtlı tool adlarından biri olmalıdır.
- "on_error" değeri "continue" veya "stop" olabilir.
- "args" boş sözlük olabilir.
- Adım sayısı 1-20 arasında olmalıdır.
- Yanıt geçerli JSON olmalıdır; markdown kod bloğu kullanabilirsin.
"""


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA API anahtarı
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: Kayıtlı tool listesi
# ---------------------------------------------------------------------------

def _get_registered_tools() -> list[str]:
    """Plugin_Host'tan kayıtlı tool adlarını al; erişilemezse boş liste."""
    try:
        import main as _main  # type: ignore[import]
        ph = getattr(_main, "plugin_host", None)
        if ph is not None and hasattr(ph, "list_tools"):
            return [t.name for t in ph.list_tools()]
        # Alternatif: tool_runtime üzerinden
        tr = getattr(_main, "_tool_runtime", None)
        if tr is not None and hasattr(tr, "list_tools"):
            return list(tr.list_tools())
    except Exception:
        pass
    return []


# ---------------------------------------------------------------------------
# Yardımcı: Ultra model tetikleyici kontrolü
# ---------------------------------------------------------------------------

def _should_use_ultra(goal: str, force_ultra: bool) -> bool:
    """Hedef metinde ultra reasoning tetikleyicisi var mı?"""
    if force_ultra:
        return True
    return bool(_ULTRA_TRIGGERS.search(goal))


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA chat çağrısı
# ---------------------------------------------------------------------------

def _call_nvidia_chat(
    model: str,
    messages: list[dict],
    max_tokens: int = 2048,
    temperature: float = 0.2,
    timeout: float = 120.0,
) -> str:
    """NVIDIA NIM chat endpoint'ine istek gönder; ham metin döner."""
    import requests as _requests

    api_key = _nvidia_api_key()
    if not api_key:
        raise RuntimeError("NVIDIA API anahtarı eksik.")

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    response = _requests.post(
        NVIDIA_CHAT_ENDPOINT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=timeout,
    )

    if response.status_code >= 400:
        detail = response.text.strip()[:400]
        raise RuntimeError(
            f"NVIDIA API hatası ({response.status_code}): {detail}"
        )

    data = response.json()
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("NVIDIA API boş yanıt döndürdü.")

    content = (choices[0] or {}).get("message", {}).get("content", "")
    if isinstance(content, list):
        parts = [
            str(p.get("text", ""))
            for p in content
            if isinstance(p, dict) and p.get("type") == "text"
        ]
        text = " ".join(p for p in parts if p).strip()
    else:
        text = str(content or "").strip()

    if not text:
        raise RuntimeError("NVIDIA modeli boş metin döndürdü.")

    return text


# ---------------------------------------------------------------------------
# Yardımcı: Düşürülen adımlar için Türkçe uyarı
# ---------------------------------------------------------------------------

def _format_dropped_warning(dropped: list[tuple[str, str]]) -> str:
    """Düşürülen adımları Türkçe uyarı paragrafına çevir (Req 6.4)."""
    if not dropped:
        return ""
    lines = []
    for tool_name, reason in dropped:
        label = tool_name or "(bilinmeyen)"
        lines.append(f"  • {label}: {reason}")
    return (
        "⚠️ Aşağıdaki plan adımları geçersiz olduğu için atlandı:\n"
        + "\n".join(lines)
    )


# ---------------------------------------------------------------------------
# Yardımcı: routines.json okuma/yazma
# ---------------------------------------------------------------------------

def _load_routines(path: Path) -> list[dict]:
    """routines.json'ı yükle; yoksa veya bozuksa boş liste döner."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            return raw
    except Exception as exc:
        log.warning("plan_save: routines.json okunamadı: %s", exc)
    return []


def _save_routines(path: Path, routines: list[dict]) -> None:
    """routines.json'a yaz."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(routines, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _routine_to_dict(routine_obj: Any) -> dict:
    """Routine dataclass'ını JSON-serileştirilebilir dict'e çevir."""
    from runtime.types import Routine
    if isinstance(routine_obj, Routine):
        return {
            "name": routine_obj.name,
            "triggers": list(routine_obj.triggers),
            "steps": [
                {
                    "tool": s.tool,
                    "args": dict(s.args),
                    "on_error": s.on_error,
                    "name": s.name,
                }
                for s in routine_obj.steps
            ],
        }
    # Zaten dict ise olduğu gibi döndür
    return dict(routine_obj)


# ---------------------------------------------------------------------------
# Handler: plan_generate
# ---------------------------------------------------------------------------

def plan_generate(
    goal: str,
    force_ultra: bool = False,
) -> str:
    """Doğal dil hedefini Routine_Engine uyumlu adım listesine çevir.

    ``nvidia/llama-3.3-nemotron-super-49b-v1.5`` varsayılan model olarak
    kullanılır. Hedef metinde "derin düşün" veya "ultra reasoning" ifadesi
    geçiyorsa ya da ``force_ultra=True`` ise ultra model devreye girer
    (Req 6.5).

    Üretilen plan ``RoutinePlanParser`` ile ``Routine``/``RoutineStep``'e
    çevrilir; Plugin_Host'ta kayıtlı olmayan tool adları düşürülür ve
    Türkçe uyarıyla raporlanır (Req 6.4).

    Reasoning modeli geçerli JSON üretemezse ham metin Türkçe paragraf
    olarak iletilir ve plan icrası yapılmaz (Req 6.8).
    """
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için plan üretme özelliği "
            "kullanılamıyor."
        )

    goal_text = (goal or "").strip()
    if not goal_text:
        return "Lütfen bir hedef belirtin. Örnek: 'Sabah rutinini planla'."

    # Model seçimi (Req 6.5)
    use_ultra = _should_use_ultra(goal_text, force_ultra)
    model = _ULTRA_MODEL if use_ultra else _DEFAULT_MODEL
    log.debug("plan_generate: model=%s, ultra=%s", model, use_ultra)

    # Kayıtlı tool listesi
    registered_tools = _get_registered_tools()

    # NVIDIA çağrısı
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Hedef: {goal_text}\n\n"
                "Kayıtlı tool'lar (yalnızca bunları kullan):\n"
                + (", ".join(registered_tools) if registered_tools else "(bilinmiyor)")
            ),
        },
    ]

    try:
        raw_response = _call_nvidia_chat(
            model=model,
            messages=messages,
            max_tokens=2048,
            temperature=0.2,
            timeout=120.0,
        )
    except Exception as exc:
        log.error("plan_generate: NVIDIA çağrısı başarısız: %s", exc)
        return f"Plan üretme sırasında bir hata oluştu: {exc}"

    # RoutinePlanParser ile ayrıştır (Req 6.3, 6.4, 6.8)
    parsed = RoutinePlanParser.parse(raw_response, registered_tools)

    # Bozuk JSON kontrolü (Req 6.8)
    if not parsed.routine.steps and parsed.dropped_steps:
        # Ham metin Türkçe paragraf olarak ilet
        log.warning("plan_generate: geçerli JSON üretilemedi, ham metin döndürülüyor.")
        return (
            "Reasoning modeli geçerli bir plan üretemedi. "
            "Ham yanıt:\n\n" + raw_response
        )

    # Başarılı plan özeti
    routine = parsed.routine
    step_count = len(routine.steps)
    plan_name = routine.name or "dinamik_plan"

    lines = [
        f"✅ Plan üretildi: **{plan_name}** ({step_count} adım)",
        "",
    ]
    for i, step in enumerate(routine.steps, 1):
        step_label = step.name or step.tool
        lines.append(f"  {i}. {step_label} (`{step.tool}`)")

    # Düşürülen adımlar uyarısı (Req 6.4)
    warning = _format_dropped_warning(parsed.dropped_steps)
    if warning:
        lines.append("")
        lines.append(warning)

    lines.append("")
    lines.append(
        "Planı çalıştırmak için 'planı çalıştır', "
        "kaydetmek için 'planı kaydet' diyebilirsiniz."
    )

    # Planı geçici olarak modül düzeyinde sakla (plan_save için)
    _store_last_plan(parsed)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handler: plan_explain
# ---------------------------------------------------------------------------

def plan_explain(plan_json: str = "") -> str:
    """Mevcut planı doğal dilde Türkçe açıkla (inline).

    ``plan_json`` boşsa son üretilen plan kullanılır.
    """
    # Son planı al
    last = _get_last_plan()

    if plan_json:
        # Verilen JSON'u ayrıştır
        registered_tools = _get_registered_tools()
        parsed = RoutinePlanParser.parse(plan_json, registered_tools)
        routine = parsed.routine
    elif last is not None:
        routine = last.routine
    else:
        return "Açıklanacak bir plan bulunamadı. Önce 'plan üret' komutunu kullanın."

    if not routine.steps:
        return "Plan boş veya geçersiz adımlar içeriyor."

    plan_name = routine.name or "dinamik_plan"
    lines = [
        f"📋 **{plan_name}** planının açıklaması:",
        "",
        f"Bu plan {len(routine.steps)} adımdan oluşuyor:",
        "",
    ]

    for i, step in enumerate(routine.steps, 1):
        step_label = step.name or step.tool
        args_str = ""
        if step.args:
            args_str = " — parametreler: " + ", ".join(
                f"{k}={v}" for k, v in step.args.items()
            )
        on_error_str = (
            " (hata durumunda dur)" if step.on_error == "stop" else ""
        )
        lines.append(f"  {i}. **{step_label}** (`{step.tool}`){args_str}{on_error_str}")

    if routine.triggers:
        lines.append("")
        lines.append(
            "Tetikleyiciler: " + ", ".join(f'"{t}"' for t in routine.triggers)
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handler: plan_save
# ---------------------------------------------------------------------------

def plan_save(
    plan_json: str = "",
    confirm_overwrite: bool = False,
    routines_path: str = "",
) -> str:
    """Dinamik planı ``routines.json`` dosyasına kalıcı olarak yaz (Req 6.7).

    Mevcut rutin adıyla çakışırsa ``confirm_overwrite=True`` olmadan
    onay isteyen mesaj döner (Voice_Core "evet/hayır" turunu yönetir).
    """
    # Son planı al
    last = _get_last_plan()

    if plan_json:
        registered_tools = _get_registered_tools()
        parsed = RoutinePlanParser.parse(plan_json, registered_tools)
        routine = parsed.routine
    elif last is not None:
        routine = last.routine
    else:
        return "Kaydedilecek bir plan bulunamadı. Önce 'plan üret' komutunu kullanın."

    if not routine.steps:
        return "Plan boş veya geçersiz adımlar içeriyor; kaydedilmedi."

    # routines.json yolu
    target_path = Path(routines_path) if routines_path else _ROUTINES_PATH

    # Mevcut rutinleri yükle
    existing = _load_routines(target_path)
    existing_names = {r.get("name", "") for r in existing if isinstance(r, dict)}

    plan_name = routine.name or "dinamik_plan"

    # Çakışma kontrolü (Req 6.7)
    if plan_name in existing_names and not confirm_overwrite:
        return (
            f"⚠️ '{plan_name}' adında bir rutin zaten mevcut. "
            "Üzerine yazmak istiyor musunuz? "
            "Onaylamak için 'planı kaydet, üzerine yaz' diyebilirsiniz."
        )

    # Mevcut rutini güncelle veya yeni ekle
    new_entry = _routine_to_dict(routine)
    if plan_name in existing_names:
        updated = [
            new_entry if r.get("name") == plan_name else r
            for r in existing
        ]
    else:
        updated = existing + [new_entry]

    try:
        _save_routines(target_path, updated)
    except Exception as exc:
        log.error("plan_save: routines.json yazılamadı: %s", exc)
        return f"Plan kaydedilirken bir hata oluştu: {exc}"

    step_count = len(routine.steps)
    return (
        f"✅ '{plan_name}' planı ({step_count} adım) "
        f"routines.json dosyasına kaydedildi."
    )


# ---------------------------------------------------------------------------
# Geçici plan deposu (modül düzeyi, in-memory)
# ---------------------------------------------------------------------------

_last_parsed_plan: Any = None


def _store_last_plan(parsed: Any) -> None:
    global _last_parsed_plan
    _last_parsed_plan = parsed


def _get_last_plan() -> Any:
    return _last_parsed_plan


# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------

plan_generate.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "plan_generate",
        "description": (
            "Dogal dil hedefini Routine_Engine uyumlu adim listesine cevirir. "
            "Kullanici 'sabah rutinini planla', 'su gorevleri otomatiklestir', "
            "'benim icin bir plan olustur' gibi cok adimli gorev tanimlari "
            "yaptiginda kullan. "
            "'derin dusun' veya 'ultra reasoning' ifadesi geciyorsa ultra model "
            "devreye girer. Uretilen plan RoutinePlanParser ile dogrulanir; "
            "gecersiz adimlar Turkce uyariyla raporlanir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal": {
                    "type": "STRING",
                    "description": (
                        "Planlanacak dogal dil hedefi. "
                        "Ornek: 'Her sabah hava durumunu kontrol et, "
                        "takvimi ac ve musteri e-postalarini ozetle.'"
                    ),
                },
                "force_ultra": {
                    "type": "BOOLEAN",
                    "description": (
                        "True ise ultra reasoning modeli zorla kullan. "
                        "Varsayilan: False."
                    ),
                },
            },
            "required": ["goal"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "fallback": [
            {
                "provider": "nvidia",
                "model": "nvidia/llama-3.1-nemotron-ultra-253b-v1",
            },
        ],
    },
}

plan_explain.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "plan_explain",
        "description": (
            "Mevcut plani dogal dilde Turkce aciklar. "
            "Kullanici 'plani acikla', 'bu plan ne yapiyor', "
            "'adimlar neler' dediginde kullan. "
            "plan_json bos birakilirsa son uretilen plan kullanilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "plan_json": {
                    "type": "STRING",
                    "description": (
                        "Aciklanacak plan JSON metni (opsiyonel). "
                        "Bos birakilirsa son uretilen plan kullanilir."
                    ),
                },
            },
            "required": [],
        },
    },
    "execution_mode": "inline",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "fallback": [],
    },
}

plan_save.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "plan_save",
        "description": (
            "Dinamik plani routines.json dosyasina kalici olarak yazar. "
            "Kullanici 'plani kaydet', 'bu rutini kaydet' dediginde kullan. "
            "Mevcut rutin adiyla cakisirsa onay ister. "
            "confirm_overwrite=True ile uzerine yazilabilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "plan_json": {
                    "type": "STRING",
                    "description": (
                        "Kaydedilecek plan JSON metni (opsiyonel). "
                        "Bos birakilirsa son uretilen plan kullanilir."
                    ),
                },
                "confirm_overwrite": {
                    "type": "BOOLEAN",
                    "description": (
                        "True ise mevcut rutin adinin uzerine yaz. "
                        "Varsayilan: False."
                    ),
                },
                "routines_path": {
                    "type": "STRING",
                    "description": (
                        "routines.json dosyasinin tam yolu (opsiyonel). "
                        "Bos birakilirsa proje kokunde aranir."
                    ),
                },
            },
            "required": [],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/llama-3.3-nemotron-super-49b-v1.5",
        "fallback": [],
    },
}


__all__ = ["plan_generate", "plan_explain", "plan_save"]
