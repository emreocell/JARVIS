# Alp Ünlü tarafından yapılmıştır — @alppunlu
"""Uygulama yapılandırması (api_keys.json) yükleme, kaydetme ve şema migration.

JARVIS v2, v1'in düz `{gemini_api_key, nvidia_api_key, voice, ...}` şemasını
`schema_version=2` ile genişletir; jarvis-nvidia-skill-pack ile gelen v3
şeması ise `gemini_secondary_api_key`, `model_router`, `safety`, `memory_rag`,
`translate` ve `image_search` bloklarını ekler.

Bu modül v1/v2 çağıranlarını bozmadan v3 şemasına geçişi sağlar:

* `DEFAULT_CONFIG_V2`        — v1'den miras + v2 ek alanlarını içeren default.
* `DEFAULT_CONFIG_V3`        — v2 alanlarının üzerine v3 ek bloklarını ekler.
* `migrate_v1_to_v2(raw)`    — saf fonksiyon, v1 anahtarlarını korur, eksik
                                v2 alanlarını default'larla doldurur.
* `migrate_v2_to_v3(cfg)`    — saf fonksiyon, v2 alanlarını korur, eksik v3
                                alanlarını default'larla doldurur. Yan etkisiz;
                                diske yazmaz.
* `load_or_migrate(path)`    — diskten okur; `schema_version` 1/2/3 dallanmasını
                                yapar. v1 → v3'e tam migration disk üzerine
                                yazılır (bak yedeği ile). v2 dosyası diske
                                **yeniden yazılmaz**; v3 alanları yalnız bellekte
                                doldurulur (lazy migration). v3 dosyası ise
                                eksik alanları default'lara düşürerek döner.
                                Bozuk JSON `.corrupt-{ts}` olarak karantinaya
                                alınır.
* `load_app_config()`        — geriye dönük yardımcı; her zaman bir dict döner.
* `save_app_config(updates)` — `schema_version=3` damgalayarak diske yazar;
                                None olmayan tüm `updates` anahtarlarını
                                uygular, mevcut diğer alanları korur.
* `mask_secret(s)`           — bir gizli değeri log'a yazılabilecek hâle
                                maskeler. Tam değer çıktıda alt-string olarak
                                bulunmaz.
* `get_last_load_error()`    — son yüklemede oluşan hata raporu (UI'ya
                                gösterim için).
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = BASE_DIR / "config"
CONFIG_PATH = CONFIG_DIR / "api_keys.json"


# ---------------------------------------------------------------------------
# v2 default şeması — tüm v1 alanları + yeni v2 alanları.
# Tasarım: jarvis-v2-upgrade design.md "AppConfig (yeni alanlar)" bölümü.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_V2: dict[str, Any] = {
    "schema_version": 2,
    # v1 alanları (geri uyumluluk):
    "gemini_api_key": "",
    "nvidia_api_key": "",
    "voice": "Charon",
    "youtube_api_key": "",
    "youtube_channel_handle": "",
    # v2 yeni alanları:
    "theme": "Teal Core",
    "wake_word_enabled": False,
    "privacy_mode_default": False,
    "disabled_skills": [],
    "hotkeys": {
        "command_palette": "ctrl+shift+space",
    },
    "tray_minimize_on_close": True,
    "start_with_windows": False,
    "last_window_geometry": "",
    "last_monitor_index": 0,
    "voice_control": {
        "barge_in_enabled": True,
        "mute_mic_while_speaking": False,
        "stop_only_barge_in": True,
        "post_speech_input_cooldown_ms": 1600,
        "wake_word_resumes_pause": True,
        "classify_interrupts_with_groq": True,
    },
    "system_language": "tr-TR",
    "transcription_language_codes": ["tr-TR", "en-US"],
    "user_rules": "",
    "preferred_browser": "",
}


# ---------------------------------------------------------------------------
# v3 default şeması — v2'nin üzerine Model_Router, Dual Gemini, Safety,
# Memory_RAG, Translate ve Image_Search blokları eklenir.
# Tasarım: jarvis-nvidia-skill-pack design.md "Config_Loader v3" bölümü.
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_V3: dict[str, Any] = {
    **copy.deepcopy(DEFAULT_CONFIG_V2),
    "schema_version": 3,
    # v3 yeni alanları:
    "gemini_secondary_api_key": "",
    "gemini_extra_api_keys": ["", "", ""],
    "groq_api_key": "",
    "openrouter_api_key": "",
    "google_vision_api_key": "",
    "model_router": {
        "default_routes": {
            "low_latency.intent": {
                "provider": "groq",
                "model": "llama-3.1-8b-instant",
            },
            "self_eval.quick": {
                "provider": "groq",
                "model": "llama-3.1-8b-instant",
            },
            "openrouter.fast": {
                "provider": "openrouter",
                "model": "openai/gpt-oss-20b:free",
            },
            "openrouter.balanced": {
                "provider": "openrouter",
                "model": "meta-llama/llama-3.3-70b-instruct:free",
            },
            "openrouter.coder": {
                "provider": "openrouter",
                "model": "qwen/qwen3-coder:free",
            },
            "memory_rag.query": {
                "provider": "nvidia",
                "model": "nvidia/llama3-chatqa-1.5-70b",
            },
            "memory_rag.embed": {
                "provider": "nvidia",
                "model": "nvidia/nv-embedqa-e5-v5",
            },
            "doc_intel.parse": {
                "provider": "nvidia",
                "model": "nvidia/nemotron-parse",
            },
            "doc_intel.chart": {
                "provider": "nvidia",
                "model": "google/deplot",
            },
            "reasoning.plan": {
                "provider": "openrouter",
                "model": "qwen/qwen3-coder:free",
            },
            "translate.text": {
                "provider": "nvidia",
                "model": "nvidia/riva-translate-4b-instruct-v1.1",
            },
            "creative.write": {
                "provider": "nvidia",
                "model": "writer/palmyra-creative-122b",
            },
            "image_search.embed": {
                "provider": "nvidia",
                "model": "nvidia/nvclip",
            },
            "embodied.next_action": {
                "provider": "nvidia",
                "model": "nvidia/cosmos-reason2-8b",
            },
            "voice_core.intent": {
                "provider": "gemini_primary",
                "model": "models/gemini-3.1-flash-lite",
            },
            "voice_core.heavy": {
                "provider": "gemini_primary",
                "model": "models/gemini-2.5-flash",
            },
        },
        "fallback_chain": {
            "gemini_primary": ["gemini_secondary"],
            "gemini_secondary": ["gemini_primary"],
            "gemini_extra_1": ["gemini_primary"],
            "gemini_extra_2": ["gemini_primary"],
            "gemini_extra_3": ["gemini_primary"],
            "groq": ["openrouter", "gemini_primary"],
            "openrouter": ["groq", "gemini_primary"],
            "nvidia": ["openrouter", "gemini_primary"],
        },
        "gemini_chat_model": "models/gemini-3.1-flash-lite",
        "gemini_task_models": [
            "models/gemini-2.5-flash",
            "models/gemini-3.1-flash-lite",
            "models/gemini-2.5-flash-lite",
        ],
        "gemini_pool_providers": [
            "gemini_primary",
            "gemini_secondary",
            "gemini_extra_1",
            "gemini_extra_2",
            "gemini_extra_3",
        ],
        "health_check_interval_sec": 60,
        "disable_cache": False,
    },
    "safety": {
        "enforce_content_safety": True,
        "fail_closed": False,
        "allowed_topics": [],
    },
    "memory_rag": {
        "top_k": 5,
        "chunk_chars": 800,
        "chunk_overlap": 100,
        "embed_batch": 16,
    },
    "translate": {
        "default_target": "en",
    },
    "image_search": {
        "embed_batch": 8,
    },
    "browser_automation": {
        "engine": "playwright",
        "preferred_channel": "chrome",
        "allow_chromium_fallback": True,
        "profile_dir": "runtime/browser_profile",
        "headless": False,
        "slow_mo_ms": 30,
    },
}


# Geriye dönük takma ad: eski v1/v2 modülleri `DEFAULT_CONFIG`'i import edebilir.
# v3 v2'nin tüm alanlarını barındırdığı için aliası en güncel şemaya bağlarız.
DEFAULT_CONFIG: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG_V3)


# Modül içi son hata raporu — UI bunu kullanıcıya gösterebilir.
_last_load_error: str | None = None


@dataclass
class LoadResult:
    """`load_or_migrate` çıktısı.

    * `config` — bellekte kullanılabilir yapılandırma dict'i. Bozuk JSON
      durumunda `None`'dur (default oluşturulmaz; çağıran karar verir).
    * `migrated` — v1 dosyası v3 şemasına migrate edilip diske yazıldıysa
      `True`. v2 dosyaları için lazy in-memory tamamlama yapılır ve disk'e
      yazılmaz; bu durumda `False` döner.
    * `error` — bozuk JSON / okuma hatası varsa kullanıcıya gösterilebilecek
      Türkçe hata raporu, yoksa `None`.
    * `corrupt_path` — bozuk dosya `{path}.corrupt-{ts}` olarak yeniden
      adlandırıldıysa hedef yol.
    * `backup_path` — migration öncesi oluşturulan `.bak` yolu (varsa).
    """

    config: dict[str, Any] | None
    migrated: bool = False
    error: str | None = None
    corrupt_path: Path | None = None
    backup_path: Path | None = None
    source_existed: bool = True


# ---------------------------------------------------------------------------
# Saf yardımcılar
# ---------------------------------------------------------------------------

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """`override`'ı `base` üzerine derinlemesine birleştir.

    Saf fonksiyon: girdileri mutate etmez, yeni bir dict döner. Aynı anahtar
    her iki tarafta da dict ise alt sözlükler birleşir; aksi halde `override`
    değeri kazanır. Hassas v3 nested blokları (model_router/safety/memory_rag/
    translate/image_search) için bu davranış, kullanıcı bir alt-anahtarı
    elle kaydetmiş olsa bile diğer default alt-anahtarların doldurulmasını
    sağlar.
    """
    result: dict[str, Any] = copy.deepcopy(base)
    for key, value in override.items():
        existing = result.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            result[key] = _deep_merge(existing, value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# ---------------------------------------------------------------------------
# Saf migration mantığı
# ---------------------------------------------------------------------------

def migrate_v1_to_v2(raw: dict[str, Any] | None) -> dict[str, Any]:
    """v1 sözlüğünü v2 şemasına yükselt.

    * Tüm v1 anahtarlarını (ve bilinmeyen kullanıcı anahtarlarını) korur.
    * Eksik v2 alanlarını `DEFAULT_CONFIG_V2`'den default değerlerle doldurur.
    * `schema_version` her zaman 2 olarak damgalanır.
    * Saf fonksiyon: girdi `raw`'u mutate etmez.
    """
    merged: dict[str, Any] = copy.deepcopy(DEFAULT_CONFIG_V2)
    if isinstance(raw, dict):
        for key, value in raw.items():
            # `schema_version` daima 2'ye normalleşir; v1'de yoksa veya
            # rastgele bir değer taşıyorsa override edilir.
            if key == "schema_version":
                continue
            merged[key] = copy.deepcopy(value)
    merged["schema_version"] = 2
    return merged


def migrate_v2_to_v3(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """v2 sözlüğünü v3 şemasına yükselt.

    * Tüm v2 alanlarını (anahtarlar, voice, theme, disabled_skills, hotkeys,
      vb.) **korur**.
    * Eksik v3 alanlarını (`gemini_secondary_api_key`, `model_router`,
      `safety`, `memory_rag`, `translate`, `image_search`) `DEFAULT_CONFIG_V3`
      default'larıyla doldurur. v3 nested blokları derinlemesine birleştirilir
      ki kullanıcı yalnız bir alt-anahtarı kaydetmiş olsa bile diğer default
      alt-anahtarlar bellekte hazır kalsın.
    * `schema_version` her zaman 3 olarak damgalanır.
    * **Saf fonksiyon**: girdi `cfg`'yi mutate etmez ve **diske yazmaz**;
      yan etkisi yoktur.
    """
    if not isinstance(cfg, dict):
        return copy.deepcopy(DEFAULT_CONFIG_V3)
    merged = _deep_merge(DEFAULT_CONFIG_V3, cfg)
    merged["schema_version"] = 3
    return merged


# ---------------------------------------------------------------------------
# Disk işlemleri
# ---------------------------------------------------------------------------

def _ts_suffix() -> str:
    """`{path}.corrupt-{ts}` için saniye duyarlığında zaman damgası."""
    return time.strftime("%Y%m%d-%H%M%S", time.localtime())


def _quarantine_corrupt(path: Path) -> Path:
    """Bozuk JSON dosyasını `{path}.corrupt-{ts}` olarak yeniden adlandır.

    Hedef ad çakışırsa benzersiz bir ek alır. Default oluşturulmaz; çağıran
    karar verir.
    """
    target = path.with_name(f"{path.name}.corrupt-{_ts_suffix()}")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{_ts_suffix()}-{counter}")
        counter += 1
    path.rename(target)
    return target


def _write_backup(path: Path) -> Path:
    """Migration öncesi `{path}.bak` yedeği oluştur. Mevcut .bak üzerine yazar."""
    backup = path.with_name(f"{path.name}.bak")
    backup.write_bytes(path.read_bytes())
    return backup


def load_or_migrate(path: str | Path) -> LoadResult:
    """Yapılandırmayı diskten oku, gerekirse v1→v3 migration uygula.

    Davranış (Req 3.1, 3.2, 3.5):

    * Dosya yoksa: `DEFAULT_CONFIG_V3` kopyasıyla `LoadResult(migrated=False)`
      döner; disk dokunulmaz (çağıran isterse `save_app_config` ile yazar).
    * Dosya v3 ise (`schema_version == 3`): default'larla derinlemesine
      birleştirilir (yeni alanlar default'a düşer) ve döner; migration
      bayrağı False, disk dokunulmaz.
    * Dosya v2 ise (`schema_version == 2`): v2 alanları korunarak v3 alanları
      bellekte default'larla doldurulur. **Disk yeniden yazılmaz** (lazy
      migration). Kullanıcı bir UI/CLI çağrısı ile değişiklik kaydedene
      kadar dosya v2 baytlarını korur (Req 3.2, Property 8).
    * Dosya v1 ise (`schema_version` yok veya 1): önce `{path}.bak` yedeği
      yazılır, sonra `migrate_v1_to_v2` + `migrate_v2_to_v3` zinciri
      uygulanır, yeni v3 içerik diske yazılır ve `migrated=True` ile döner.
    * Bozuk JSON: dosya `{path}.corrupt-{ts}` olarak yeniden adlandırılır;
      `config=None`, `error="..."` ile döner. Default oluşturulmaz.
    """
    p = Path(path)

    if not p.exists():
        return LoadResult(
            config=copy.deepcopy(DEFAULT_CONFIG_V3),
            migrated=False,
            source_existed=False,
        )

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        return LoadResult(
            config=None,
            error=(
                f"Yapılandırma dosyası okunamadı ({p}): {exc}. "
                "Dosya korunuyor; lütfen dosya izinlerini kontrol edin."
            ),
        )

    # Boş dosyayı v1 stub olarak değil, eksik dosya muamelesinden ayrı tutmak
    # için `{}` parse'ı bekliyoruz; gerçek boş dosya hatadır.
    stripped = text.strip()
    if not stripped:
        try:
            corrupt_target = _quarantine_corrupt(p)
        except OSError as exc:
            return LoadResult(
                config=None,
                error=(
                    f"Yapılandırma dosyası boş ve karantinaya alınamadı "
                    f"({p}): {exc}."
                ),
            )
        return LoadResult(
            config=None,
            error=(
                f"Yapılandırma dosyası boş veya bozuk; "
                f"{corrupt_target.name} olarak karantinaya alındı."
            ),
            corrupt_path=corrupt_target,
        )

    try:
        raw = json.loads(stripped)
    except (ValueError, json.JSONDecodeError) as exc:
        try:
            corrupt_target = _quarantine_corrupt(p)
        except OSError as os_exc:
            return LoadResult(
                config=None,
                error=(
                    f"Yapılandırma dosyası bozuk ({exc}) ve karantinaya "
                    f"alınamadı ({os_exc}). Dosyayı manuel kontrol edin."
                ),
            )
        return LoadResult(
            config=None,
            error=(
                f"Yapılandırma dosyası bozuk JSON; {corrupt_target.name} "
                f"olarak karantinaya alındı. Detay: {exc}"
            ),
            corrupt_path=corrupt_target,
        )

    if not isinstance(raw, dict):
        try:
            corrupt_target = _quarantine_corrupt(p)
        except OSError as exc:
            return LoadResult(
                config=None,
                error=(
                    f"Yapılandırma dosyası beklenen sözlük yapısında değil "
                    f"ve karantinaya alınamadı ({p}): {exc}."
                ),
            )
        return LoadResult(
            config=None,
            error=(
                f"Yapılandırma dosyası beklenen sözlük yapısında değil; "
                f"{corrupt_target.name} olarak karantinaya alındı."
            ),
            corrupt_path=corrupt_target,
        )

    # Şema sürümünü oku.
    schema_version = raw.get("schema_version")

    if schema_version == 3:
        # v3 dosyası: yeni alanlar eksikse default'a düşür (forward-compat).
        # Disk dokunulmaz.
        merged = _deep_merge(DEFAULT_CONFIG_V3, raw)
        merged["schema_version"] = 3
        return LoadResult(config=merged, migrated=False)

    if schema_version == 2:
        # v2 dosyası: v3 alanları **bellekte** default'larla doldurulur.
        # Disk yeniden yazılmaz (Req 3.2, Property 8: byte-identity).
        merged = migrate_v2_to_v3(raw)
        return LoadResult(config=merged, migrated=False)

    # schema_version yok ya da 1 (veya bilinmeyen değer) → v1 olarak ele al,
    # yedek yaz, v1→v2→v3 zinciriyle migrate et ve diske yaz.
    backup_path: Path | None = None
    try:
        backup_path = _write_backup(p)
    except OSError as exc:
        # Yedek alınamıyorsa migration'a girişmeyiz; eski içeriği bellekte
        # kullanmak yerine kullanıcıyı bilgilendir, eski dosyayı koru.
        return LoadResult(
            config=None,
            error=(
                f"v1 yapılandırma yedeklenemediği için migration iptal edildi "
                f"({p}): {exc}. Lütfen dosya izinlerini kontrol edin."
            ),
        )

    v2 = migrate_v1_to_v2(raw)
    migrated_v3 = migrate_v2_to_v3(v2)

    try:
        p.write_text(
            json.dumps(migrated_v3, indent=4, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        # Diske yazma başarısız olsa bile bellek içi konfig döner; çağıran
        # ileride save_app_config ile yeniden deneyebilir.
        return LoadResult(
            config=migrated_v3,
            migrated=True,
            backup_path=backup_path,
            error=(
                f"v3 yapılandırması diske yazılamadı ({p}): {exc}. "
                f"Yedek: {backup_path.name if backup_path else 'yok'}."
            ),
        )

    return LoadResult(
        config=migrated_v3,
        migrated=True,
        backup_path=backup_path,
    )


# ---------------------------------------------------------------------------
# Geriye dönük yardımcılar
# ---------------------------------------------------------------------------

def get_last_load_error() -> str | None:
    """Son `load_app_config` çağrısında oluşan hata mesajını döner."""
    return _last_load_error


def load_app_config() -> dict[str, Any]:
    """Yapılandırmayı belleğe yükle.

    v1 çağıranlarıyla uyumlu kalmak için her zaman bir `dict` döndürür:

    * Geçerli dosya → diskteki içerik default'larla birleştirilmiş hâli (v2
      dosyaları için v3 default'ları bellekte doldurulur, disk dokunulmaz).
    * Dosya yok → `DEFAULT_CONFIG_V3` kopyası.
    * Bozuk JSON → bellekte `DEFAULT_CONFIG_V3` kopyası döner; orijinal
      dosya `.corrupt-{ts}` olarak karantinaya alınır ve `get_last_load_error`
      üzerinden hata raporlanır. Diskte default oluşturulmaz.
    """
    global _last_load_error

    result = load_or_migrate(CONFIG_PATH)
    _last_load_error = result.error

    if result.config is not None:
        return result.config

    # Bozuk dosya ya da yedek hatası: bellek içi default ile devam et,
    # diske default yazma.
    return copy.deepcopy(DEFAULT_CONFIG_V3)


def save_app_config(updates: dict[str, Any]) -> dict[str, Any]:
    """Yapılandırmayı güncelle ve diske `schema_version=3` damgasıyla yaz.

    Davranış (Req 3.3, Property 9):

    * Mevcut config diskten okunur (v2 dosyası için v3 default'ları bellekte
      doldurulur — bu "lazy migration"ın diske yazıldığı andır).
    * `updates` içindeki `None` değerler atlanır; mevcut değer korunur.
    * `updates` içinde olmayan tüm önceki alanlar değişmeden korunur.
    * `schema_version` her durumda `3` olarak yazılır.
    """
    config = load_app_config()
    for key, value in (updates or {}).items():
        if value is None:
            continue
        config[key] = value
    config["schema_version"] = 3

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=4, ensure_ascii=False),
        encoding="utf-8",
    )
    return config


def get_app_config_value(key: str, default: Any = None) -> Any:
    return load_app_config().get(key, default)


def has_gemini_api_key() -> bool:
    value = str(get_app_config_value("gemini_api_key", "") or "").strip()
    return bool(value)


def has_nvidia_api_key() -> bool:
    value = str(get_app_config_value("nvidia_api_key", "") or "").strip()
    return bool(value)


def has_groq_api_key() -> bool:
    value = str(get_app_config_value("groq_api_key", "") or "").strip()
    return bool(value)


def has_openrouter_api_key() -> bool:
    value = str(get_app_config_value("openrouter_api_key", "") or "").strip()
    return bool(value)


def has_google_vision_api_key() -> bool:
    value = str(get_app_config_value("google_vision_api_key", "") or "").strip()
    return bool(value)


# ---------------------------------------------------------------------------
# Secret masking
# ---------------------------------------------------------------------------

# Maskenin sabit son eki. `mask_secret` çıktısının uzunluğu daima `s`'nin
# uzunluğundan küçük (veya eşit) kalır; bu yüzden 8'den kısa girdilerde
# baş 4 karakter sızdırmamak için tamamen bu sabit döner.
_SECRET_MASK = "***"


def mask_secret(s: str | None) -> str:
    """Bir gizli değeri log'a yazılabilecek hâle maskeler.

    Sözleşme (Req 3.6, Property 10):

    1. Girdi `s`'nin tam değeri (uzunluk ≥ 8 olduğunda) çıktının hiçbir
       konumunda alt-string olarak bulunmaz.
    2. Çıktının uzunluğu en fazla `s`'nin uzunluğu kadardır.
    3. `s` boş veya 8'den kısa olduğunda çıktı tamamen sabit `"***"`
       döner; baş 4 karakter sızdırılmaz.
    4. Aynı `s` için fonksiyon **deterministik**'tir.

    Notlar:
    - 8 eşiği şuradan geliyor: çıktı `s[:4] + "***"` olduğunda uzunluk 7
      olur; bu, en az uzunluk 8 olan girdiler için Property 2'yi (`len(out)
      <= len(s)`) garanti eder. 8'den kısa girdiler için baş 4 karakteri
      sızdırmamak adına tamamen sabit döneriz.
    - `s` `None` veya str-dışı bir değer ise sabit maske döner; çağıran
      tarafa ek bir koruma katmanı sağlanır.
    """
    if not isinstance(s, str):
        return _SECRET_MASK
    if len(s) < 8:
        return _SECRET_MASK
    return s[:4] + _SECRET_MASK
