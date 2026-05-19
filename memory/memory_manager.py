"""
Kalıcı bellek — JSON dosyasına kaydedilir.
Alp Ünlü tarafından yapılmıştır — @alppunlu
"""

import datetime
import json
import re
import unicodedata
from pathlib import Path

BASE_DIR    = Path(__file__).resolve().parent.parent
MEMORY_FILE = BASE_DIR / "memory" / "memory.json"


def load_memory() -> dict:
    try:
        if MEMORY_FILE.exists():
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def update_memory(data: dict):
    mem = load_memory()
    _deep_merge(mem, data)
    _write_memory(mem)


def _write_memory(mem: dict):
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, ensure_ascii=False)


def _deep_merge(base: dict, update: dict):
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v


def _normalize_text(text: str) -> str:
    text = (text or "").strip().casefold()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("ı", "i")
    return " ".join(text.split())


def _entry_value_text(value) -> str:
    if isinstance(value, dict):
        base = value.get("value")
        if base is not None:
            return str(base)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _tokenize_text(text: str) -> list[str]:
    normalized = _normalize_text(text)
    return [token for token in re.split(r"[^a-z0-9]+", normalized) if token]


def _entry_matches(needle: str, category: str, item_key: str, item_value) -> bool:
    haystacks = [
        _normalize_text(category),
        _normalize_text(item_key),
        _normalize_text(_entry_value_text(item_value)),
    ]
    if any(needle in hay for hay in haystacks):
        return True

    tokens = [tok for tok in _tokenize_text(needle) if len(tok) >= 3]
    if not tokens:
        return False

    entry_tokens: list[str] = []
    for hay in haystacks:
        entry_tokens.extend(_tokenize_text(hay))

    matched = 0
    for token in tokens:
        if any(token in entry_token or entry_token in token for entry_token in entry_tokens):
            matched += 1

    if len(tokens) == 1:
        return matched == 1
    return matched >= min(2, len(tokens))


def delete_memory(category: str = "", key: str = "", match_text: str = "") -> str:
    mem = load_memory()
    if not mem:
        return "Hafizada silinecek bir kayit yok."

    category = (category or "").strip()
    key = (key or "").strip()
    match_text = (match_text or "").strip()

    if category and key:
        bucket = mem.get(category)
        if isinstance(bucket, dict) and key in bucket:
            del bucket[key]
            if not bucket:
                mem.pop(category, None)
            _write_memory(mem)
            return f"{category}/{key} hafizadan kaldirildi."
        return "Bu hafiza kaydini bulamadim."

    needle = _normalize_text(match_text or key)
    if not needle:
        return "Silmek icin category/key veya match_text gerekli."

    for cat, bucket in list(mem.items()):
        if not isinstance(bucket, dict):
            if _entry_matches(needle, cat, cat, bucket):
                del mem[cat]
                _write_memory(mem)
                return f"{cat} hafizadan kaldirildi."
            continue

        for item_key, item_value in list(bucket.items()):
            if _entry_matches(needle, cat, item_key, item_value):
                del bucket[item_key]
                if not bucket:
                    mem.pop(cat, None)
                _write_memory(mem)
                return f"{cat}/{item_key} hafizadan kaldirildi."

    return "Eslestigim bir hafiza kaydi bulamadim."


def format_memory_for_prompt(memory: dict) -> str:
    if not memory:
        return ""
    lines = ["[KULLANICI HAKKINDA BİLGİLER]"]
    for category, items in memory.items():
        # observations bucket'ı yalnızca dahili sayaç tutar; prompt'a dahil
        # edilmez. interests altında özetlenmiş hâli zaten Gemini'ye gider.
        if category == "observations":
            continue
        if isinstance(items, dict):
            for key, val in items.items():
                if category == "whatsapp_contacts" and isinstance(val, dict):
                    display_name = val.get("display_name", key)
                    value = val.get("value", "")
                    aliases = val.get("aliases", [])
                    alias_str = ""
                    if isinstance(aliases, list) and aliases:
                        alias_str = f" aliases={', '.join(str(a) for a in aliases)}"
                    lines.append(f"  {category}/{display_name}: {value}{alias_str}")
                else:
                    value = val.get("value", val) if isinstance(val, dict) else val
                    lines.append(f"  {category}/{key}: {value}")
        else:
            lines.append(f"  {category}: {items}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pasif gözlem -> ilgi alanı çıkarımı
# ---------------------------------------------------------------------------


_DEFAULT_OBSERVATION_THRESHOLD = 3
_MAX_OBSERVATION_CATEGORIES = 32


def record_observation(
    category: str,
    label: str,
    *,
    threshold: int = _DEFAULT_OBSERVATION_THRESHOLD,
) -> tuple[int, bool]:
    """Bir gözlem kategorisinin sayacını arttır; eşik aşıldığında
    ``interests`` altına otomatik kayıt düşür.

    Args:
        category: Kategori anahtarı (örn. ``"coding"``).
        label: Kullanıcıya gösterilecek Türkçe etiket
            (örn. ``"Yazılım geliştirme"``).
        threshold: Kaç gözlem sonrası ``interests`` kaydı oluşturulacak.

    Returns:
        ``(new_count, just_promoted)``: Güncellenen toplam sayım ve bu
        çağrı sırasında ilgi alanına ilk kez terfi ettiyse ``True``.

    ``observations`` bucket'ı yalnızca dahili sayaç olarak tutulur ve
    prompt'a yansımaz. ``interests`` ise ``format_memory_for_prompt``
    üzerinden Gemini'ye sunulur.
    """
    cat = (category or "").strip()
    if not cat:
        return (0, False)

    mem = load_memory()

    obs = mem.get("observations")
    if not isinstance(obs, dict):
        obs = {}
        mem["observations"] = obs

    bucket = obs.get(cat)
    if not isinstance(bucket, dict):
        bucket = {"count": 0}
        obs[cat] = bucket

    bucket["count"] = int(bucket.get("count", 0) or 0) + 1
    if label:
        bucket["label"] = label
    bucket["last_seen"] = datetime.datetime.now().isoformat(timespec="seconds")

    # Sayaç sözlüğünü makul bir boyutta tut.
    if len(obs) > _MAX_OBSERVATION_CATEGORIES:
        try:
            lowest_cat = min(
                obs.items(),
                key=lambda kv: int((kv[1] or {}).get("count", 0))
                if isinstance(kv[1], dict)
                else 0,
            )[0]
            if lowest_cat != cat:
                obs.pop(lowest_cat, None)
        except ValueError:
            pass

    promoted_now = False
    if bucket["count"] >= max(1, int(threshold)):
        interests = mem.get("interests")
        if not isinstance(interests, dict):
            interests = {}
            mem["interests"] = interests
        if cat not in interests:
            promoted_now = True
        interests[cat] = {
            "value": label or bucket.get("label") or cat,
            "source": "auto_screen_observation",
            "evidence_count": bucket["count"],
            "last_seen": bucket["last_seen"],
        }

    _write_memory(mem)
    return (bucket["count"], promoted_now)
