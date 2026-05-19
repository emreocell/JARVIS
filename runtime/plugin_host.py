"""Plugin_Host — JARVIS v2 skill manifesto loader.

Plugin_Host yeni mimaride ``main.py``'deki sabit ``TOOL_DECLARATIONS``
listesinin yerini alır. Her ``skills/<name>/`` paketi kendi manifestosunu
(``skill.yaml`` veya ``__skill__.py``) ve handler fonksiyonlarını içerir;
host bunları çalışma zamanında bulur, doğrular ve Tool_Runtime'a
beslenecek :class:`runtime.types.ToolDescriptor` listeleri üretir.

Tasarım referansı: ``design.md`` § "Plugin_Host" + Requirement 16, 17, 18.

Sözleşme özeti
--------------
* :meth:`PluginHost.discover` — verilen klasörlerin doğrudan alt
  klasörlerini gezer; her birinde ``skill.yaml`` veya ``__skill__.py``
  arar. Eksik / geçersiz manifestolar atlanır ve ``log`` üzerinden
  debug seviyesinde raporlanır (Req 16.4). Dönen liste, manifesto
  düzeyinde geçerli kabul edilen tüm skill'lerdir; ``enabled=false``
  olanlar da liste içinde yer alır ki çağıran taraf görsel listesinde
  gösterebilsin. Yükleme filtrelemesi :meth:`load` içinde yapılır
  (Req 16.3, 18.3).
* :meth:`PluginHost.load` — manifesto'nun ``entry_module``'unu import
  eder, ``manifest.tools`` içindeki her ad için fonksiyonun
  ``__tool__`` metadata'sını okur, Gemini şema doğrulamasını çalıştırır
  (``type ∈ {STRING,NUMBER,INTEGER,BOOLEAN,OBJECT,ARRAY}``, Req 17.1)
  ve geçerli olanları :class:`ToolDescriptor` olarak döner. Bir handler
  doğrulamadan geçemezse atlanır ve sebebi log'a yazılır (Req 17.2);
  diğer handler'lar etkilenmez.
* :meth:`PluginHost.disabled_skills` — host'un anlık devre dışı
  listesini sığ kopya olarak verir; çağıranlar ayarlar UI'sında ya da
  testlerde bu durumu inceleyebilir.
* :meth:`PluginHost.reload` — daha önce keşfedilmiş bir skill için
  ``entry_module``'u ``importlib.reload`` ile tazeler ve yeniden
  yükler. Geliştirme zamanında "skill düzelt + uygulamayı kapatma"
  döngüsünü kısaltmak için tutuldu.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from runtime.types import ExecutionMode, Route, RouteProfile, SkillManifest, ToolDescriptor

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# NVIDIA anahtar gating sabitleri (Req 17.1, 17.4)
# ---------------------------------------------------------------------------

#: NVIDIA API anahtarı olmadan yüklenemeyen skill adları.
#: Bu skill'ler ``nvidia_api_key`` boş olduğunda otomatik olarak atlanır.
_NVIDIA_DEPENDENT_SKILLS: frozenset[str] = frozenset(
    {
        "memory_rag",
        "doc_intel",
        "reasoning",
        "translate",
        "safety",
        "creative",
        "image_search",
        "embodied",
        "audio_structured",
    }
)


# ---------------------------------------------------------------------------
# Şema sabitleri
# ---------------------------------------------------------------------------


#: Gemini function-calling şemasında izin verilen tip etiketleri.
#:
#: Gemini Live JSON-Schema Draft-7 değil, kendi büyük harfli tip dilini
#: kullanır. Plugin_Host, harici ``jsonschema`` paketine bağımlı olmamak
#: için bu küçük etiket kümesini elle doğrular (design.md § Plugin_Host).
_VALID_GEMINI_TYPES: frozenset[str] = frozenset(
    {"STRING", "NUMBER", "INTEGER", "BOOLEAN", "OBJECT", "ARRAY"}
)

#: ``__tool__["route"]`` alanında izin verilen sağlayıcı kimlikleri
#: (Req 13.1). Bu küme ``runtime/types.py``'daki ``ProviderId`` ile
#: senkronize tutulmalıdır.
_VALID_PROVIDERS: frozenset[str] = frozenset(
    {
        "gemini_primary",
        "gemini_secondary",
        "gemini_extra_1",
        "gemini_extra_2",
        "gemini_extra_3",
        "nvidia",
        "groq",
        "openrouter",
    }
)

#: Tool_Runtime'ın anladığı yürütme modları. ``__tool__`` metadata'sında
#: belirtilmemişse default olarak ``"inline"`` kabul edilir.
_VALID_EXECUTION_MODES: frozenset[str] = frozenset({"inline", "background"})

#: Manifesto'nun zorunlu alanları (Req 16.2). Hem YAML hem ``__skill__.py``
#: girdileri bu alanların eksiksiz ve doğru tipte olduğunu garantilemek
#: zorundadır; aksi halde manifesto reddedilir (Req 16.4).
_REQUIRED_MANIFEST_FIELDS: tuple[str, ...] = (
    "name",
    "version",
    "tools",
    "entry_module",
)

#: Diskte aranan dosya adları. YAML, ``__skill__.py``'den önce kontrol
#: edilir; bir skill her ikisini birden tutarsa YAML kazanır (basitlik
#: ve sürüm kontrolünde diff'i daha kolay olduğu için).
_MANIFEST_YAML_NAME: str = "skill.yaml"
_MANIFEST_PY_NAME: str = "__skill__.py"


# ---------------------------------------------------------------------------
# İç doğrulama yardımcıları
# ---------------------------------------------------------------------------


def _is_string_list(value: Any) -> bool:
    """``value`` boş olabilir ama tüm öğeleri string ise True."""
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _validate_manifest_dict(raw: Any) -> str | None:
    """Ham manifesto sözlüğünü Req 16.2 alanlarına karşı doğrular.

    Geçerli ise ``None``, aksi halde insanın okuyabileceği kısa bir
    Türkçe/İngilizce karışık hata mesajı döner. Mesaj log'a yazılmak
    üzere üretilir; kullanıcıya gösterilmez.
    """
    if not isinstance(raw, dict):
        return "manifest must be a dict"

    for field in _REQUIRED_MANIFEST_FIELDS:
        if field not in raw:
            return f"required field {field!r} missing"

    if not isinstance(raw["name"], str) or not raw["name"]:
        return "name must be a non-empty string"
    if not isinstance(raw["version"], str) or not raw["version"]:
        return "version must be a non-empty string"
    if not _is_string_list(raw["tools"]):
        return "tools must be a list of strings"
    if not isinstance(raw["entry_module"], str):
        return "entry_module must be a string"

    # Opsiyonel alanlar: tip yanlışsa manifesto'yu reddederiz, ama
    # eksiklik kabul edilir (default değerler dataclass'ta).
    if "enabled" in raw and not isinstance(raw["enabled"], bool):
        return "enabled must be a bool"
    if "description" in raw and not isinstance(raw["description"], str):
        return "description must be a string"
    if "requires" in raw and not _is_string_list(raw["requires"]):
        return "requires must be a list of strings"

    return None


def _validate_declaration(declaration: Any) -> str | None:
    """Bir Gemini tool declaration'ını şema kurallarına karşı doğrula.

    Doğrulama (Req 17.1):

    * ``declaration`` bir dict olmalı.
    * ``name`` zorunlu, boş olmayan string.
    * ``description`` opsiyonel ama varsa string.
    * ``parameters`` varsa: ``type == "OBJECT"``, ``properties``
      bir dict; her property kendi ``type``'ını
      :data:`_VALID_GEMINI_TYPES` içinden almalı.
    * ``required`` listesindeki her ad ``properties`` içinde tanımlı
      olmalı.

    Args-suz tool'lar (örn. ``list_background_tasks``) için
    ``parameters`` alanı atlanabilir; bu durumda doğrulama erken döner.
    """
    if not isinstance(declaration, dict):
        return "declaration must be a dict"

    name = declaration.get("name")
    if not isinstance(name, str) or not name:
        return "declaration.name must be a non-empty string"

    if "description" in declaration and not isinstance(declaration["description"], str):
        return "declaration.description must be a string"

    parameters = declaration.get("parameters")
    if parameters is None:
        # Argümansız tool — geçerli.
        return None
    if not isinstance(parameters, dict):
        return "declaration.parameters must be a dict"

    ptype = parameters.get("type")
    if ptype != "OBJECT":
        return f"declaration.parameters.type must be 'OBJECT' (got {ptype!r})"

    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        return "declaration.parameters.properties must be a dict"

    for prop_name, prop_spec in properties.items():
        if not isinstance(prop_name, str):
            return f"property name {prop_name!r} must be a string"
        if not isinstance(prop_spec, dict):
            return f"property {prop_name!r} spec must be a dict"
        prop_type = prop_spec.get("type")
        if prop_type not in _VALID_GEMINI_TYPES:
            return (
                f"property {prop_name!r} has invalid type {prop_type!r}; "
                f"expected one of {sorted(_VALID_GEMINI_TYPES)}"
            )

    required = parameters.get("required", [])
    if not isinstance(required, list):
        return "declaration.parameters.required must be a list"
    for entry in required:
        if not isinstance(entry, str):
            return f"required entry {entry!r} must be a string"
        if entry not in properties:
            return f"required entry {entry!r} not declared in properties"

    return None


def _validate_route(value: Any) -> str | None:
    """``__tool__["route"]`` alanını Req 13.1 şemasına karşı doğrula.

    Geçerli ise ``None``, aksi halde Türkçe kısa hata mesajı döner.
    ``value is None`` durumu geçerli kabul edilir (opsiyonel alan).

    Şema::

        {
            "provider": "gemini_primary" | "gemini_secondary" | "nvidia",
            "model": "<boş olmayan string>",
            "fallback": [  # opsiyonel
                {"provider": ..., "model": ...},
                ...
            ],
        }

    Fallback öğeleri aynı şemaya özyinelemeli olarak tabi tutulur;
    ancak fallback öğelerinin kendi ``fallback`` alanı **yok sayılır**
    (tek seviye derinlik yeterlidir).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return "route alanı bir sözlük (dict) olmalıdır"
    provider = value.get("provider")
    if provider not in _VALID_PROVIDERS:
        return (
            f"route.provider geçersiz: {provider!r}; "
            f"beklenen değerler: {sorted(_VALID_PROVIDERS)}"
        )
    model = value.get("model")
    if not isinstance(model, str) or not model:
        return "route.model boş olmayan bir string olmalıdır"
    fallback = value.get("fallback", [])
    if not isinstance(fallback, list):
        return "route.fallback bir liste olmalıdır"
    for idx, item in enumerate(fallback):
        if not isinstance(item, dict):
            return f"route.fallback[{idx}] bir sözlük (dict) olmalıdır"
        fb_provider = item.get("provider")
        if fb_provider not in _VALID_PROVIDERS:
            return (
                f"route.fallback[{idx}].provider geçersiz: {fb_provider!r}; "
                f"beklenen değerler: {sorted(_VALID_PROVIDERS)}"
            )
        fb_model = item.get("model")
        if not isinstance(fb_model, str) or not fb_model:
            return f"route.fallback[{idx}].model boş olmayan bir string olmalıdır"
    return None


def _route_to_profile(value: dict[str, Any]) -> RouteProfile:
    """Doğrulanmış ``route`` sözlüğünü :class:`RouteProfile`'a dönüştür.

    ``value``'nun :func:`_validate_route` testinden geçmiş olduğu
    varsayılır; aksi halde ``KeyError`` / ``TypeError`` doğal yolla
    yükselebilir.
    """
    primary = Route(
        provider=value["provider"],  # type: ignore[arg-type]
        model=value["model"],
    )
    fallback_routes: tuple[Route, ...] = tuple(
        Route(provider=item["provider"], model=item["model"])  # type: ignore[arg-type]
        for item in value.get("fallback", [])
    )
    return RouteProfile(primary=primary, fallback=fallback_routes)


def _manifest_from_dict(raw: dict[str, Any]) -> SkillManifest:
    """Doğrulanmış sözlüğü :class:`SkillManifest`'e dönüştür.

    ``raw``'un :func:`_validate_manifest_dict` testinden geçmiş olduğu
    varsayılır; aksi halde ``KeyError`` / ``TypeError`` doğal yolla
    yükselebilir.
    """
    return SkillManifest(
        name=raw["name"],
        version=raw["version"],
        enabled=bool(raw.get("enabled", True)),
        entry_module=raw["entry_module"],
        tools=list(raw["tools"]),
        description=raw.get("description", ""),
        requires=list(raw.get("requires", [])),
    )


# ---------------------------------------------------------------------------
# PluginHost
# ---------------------------------------------------------------------------


class PluginHost:
    """Skill manifestolarını keşfeder, doğrular ve yükler.

    Parameters
    ----------
    disabled_skills:
        Yüklenmemesi gereken skill adları (Req 18.3). Genelde
        ``app_config.disabled_skills`` listesinden gelir; runtime
        değişikliği için :meth:`set_disabled_skills` kullanılır.

    State
    -----
    ``_manifests`` keşfedilen son manifesto'ları ad → manifest sözlüğünde
    tutar; :meth:`reload` bu cache'i kullanır. Cache başarılı her
    :meth:`discover` çağrısında güncellenir; kayıp veya bozuk
    manifestolar cache'e eklenmez.
    """

    def __init__(self, *, disabled_skills: Iterable[str] = ()) -> None:
        self._disabled: set[str] = set(disabled_skills)
        self._manifests: dict[str, SkillManifest] = {}
        # NVIDIA anahtar uyarısı yalnızca bir kez log'lanır (Req 17.4).
        self._nvidia_key_warning_logged: bool = False

    # --------------------------------------------------------------- config

    def disabled_skills(self) -> set[str]:
        """Anlık devre dışı skill listesinin sığ kopyası (Req 18.3)."""
        return set(self._disabled)

    def set_disabled_skills(self, names: Iterable[str]) -> None:
        """Devre dışı listesini değiştir (örn. ayarlar UI'sından gelirse).

        Daha önce yüklenmiş tool'lar geri alınmaz; çağıran ToolRuntime
        üzerinden ``unregister`` etmeli ve gerekirse :meth:`load`'u
        yeniden çağırmalıdır.
        """
        self._disabled = set(names)

    # --------------------------------------------------- requires checking

    @staticmethod
    def _find_missing_requires(manifest: SkillManifest) -> list[str]:
        """``manifest.requires`` listesindeki eksik Python paketlerini döner.

        Her paket adı için ``importlib.util.find_spec`` ile varlık
        kontrolü yapılır. Eksik paketlerin adları liste olarak döner;
        tümü mevcutsa boş liste döner (Req 13.4).

        Paket adları PyPI adı değil, import adıdır (örn. ``"PIL"`` değil
        ``"Pillow"`` değil, ``"PIL"``). Skill yazarı manifesto'da import
        adını kullanmalıdır.
        """
        missing: list[str] = []
        for pkg in manifest.requires:
            if pkg == "nvidia_api_key":
                # This is a config sentinel, not an importable Python package.
                # NVIDIA-dependent skills are gated explicitly in load().
                continue
            if importlib.util.find_spec(pkg) is None:
                missing.append(pkg)
        return missing

    # ------------------------------------------------------------- discover

    def discover(self, search_paths: Iterable[Path | str]) -> list[SkillManifest]:
        """Verilen kök klasörler altındaki tüm skill manifestolarını topla.

        Her ``search_path`` doğrudan alt klasörleri gezilir; bir alt
        klasörde önce ``skill.yaml``, yoksa ``__skill__.py`` aranır.
        Geçersiz manifestolar (eksik alan, yanlış tip, parse hatası)
        atlanır ve ``log`` debug seviyesinde sebebi yazılır
        (Req 16.4).

        Returns
        -------
        list[SkillManifest]
            Manifesto-düzeyinde geçerli kabul edilen skill'ler.
            ``enabled == False`` olanlar da listede yer alır; çağıran
            isterse devre dışı skill'leri kullanıcıya gösterebilir.
            Liste, kararlı (alfabetik) sırada döner.
        """
        manifests: list[SkillManifest] = []
        seen: set[str] = set()

        for raw_path in search_paths:
            base = Path(raw_path)
            if not base.is_dir():
                log.debug(
                    "PluginHost: search path %s is not a directory; skipping",
                    base,
                )
                continue

            for child in sorted(base.iterdir(), key=lambda p: p.name):
                if not child.is_dir():
                    continue
                # ``__pycache__`` ve gizli klasörleri atla.
                if child.name.startswith(("_", ".")):
                    continue

                manifest = self._discover_one(child)
                if manifest is None:
                    continue
                if manifest.name in seen:
                    log.warning(
                        "PluginHost: duplicate skill name %r at %s; "
                        "first occurrence wins",
                        manifest.name,
                        child,
                    )
                    continue
                manifests.append(manifest)
                seen.add(manifest.name)
                self._manifests[manifest.name] = manifest

        return manifests

    def _discover_one(self, skill_dir: Path) -> SkillManifest | None:
        yaml_path = skill_dir / _MANIFEST_YAML_NAME
        py_path = skill_dir / _MANIFEST_PY_NAME

        if yaml_path.is_file():
            return self._parse_yaml_manifest(yaml_path)
        if py_path.is_file():
            return self._parse_py_manifest(py_path)
        # Manifesto yok → bu klasör skill değil. Sessizce atla.
        return None

    # ------------------------------------------------------ manifest parsers

    def _parse_yaml_manifest(self, path: Path) -> SkillManifest | None:
        """``skill.yaml`` dosyasını oku ve doğrula."""
        try:
            import yaml  # noqa: WPS433 - pyyaml runtime opsiyonu olarak tutuluyor
        except ImportError as exc:
            log.error(
                "PluginHost: cannot parse %s without pyyaml installed (%s)",
                path,
                exc,
            )
            return None

        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("PluginHost: cannot read %s: %s", path, exc)
            return None

        try:
            raw = yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 - yaml her türlü hatayı parse'da fırlatabilir
            log.error("PluginHost: invalid YAML in %s: %s", path, exc)
            return None

        err = _validate_manifest_dict(raw)
        if err:
            log.error("PluginHost: manifest %s invalid: %s", path, err)
            return None

        return _manifest_from_dict(raw)

    def _parse_py_manifest(self, path: Path) -> SkillManifest | None:
        """``__skill__.py`` modülünü import edip ``MANIFEST`` global'ini oku."""
        # Aynı yolu birden fazla discover çağrısında tekrar import etmemek
        # için modül adına dizin adını gömüyoruz; bu sayede iki farklı
        # skill aynı dosya adıyla bile çakışmaz.
        unique_module_name = f"_jarvis_skill_manifest_{path.parent.name}"

        try:
            spec = importlib.util.spec_from_file_location(unique_module_name, path)
            if spec is None or spec.loader is None:
                log.error("PluginHost: cannot build module spec for %s", path)
                return None
            module = importlib.util.module_from_spec(spec)
            # ``sys.modules``'a yazmıyoruz; manifesto modülü diğer
            # yerlerden import edilmek üzere tasarlanmadı.
            spec.loader.exec_module(module)
        except Exception as exc:  # noqa: BLE001 - bozuk skill her şey fırlatabilir
            log.error("PluginHost: failed to import manifest %s: %s", path, exc)
            return None

        manifest = getattr(module, "MANIFEST", None)
        if manifest is None:
            log.error(
                "PluginHost: manifest module %s does not export MANIFEST",
                path,
            )
            return None
        if not isinstance(manifest, SkillManifest):
            log.error(
                "PluginHost: %s::MANIFEST is not a SkillManifest instance "
                "(got %s)",
                path,
                type(manifest).__name__,
            )
            return None

        # YAML ile aynı doğrulamayı SkillManifest'in dict view'ı üzerinde
        # de çalıştır; field tipleri dataclass tarafından zaten kontrol
        # ediliyor olabilir, ama gevşek (Any) annotation'lı user-koduna
        # karşı defansif bir güvenlik kemeri sağlar.
        as_dict: dict[str, Any] = {
            "name": manifest.name,
            "version": manifest.version,
            "tools": manifest.tools,
            "entry_module": manifest.entry_module,
            "enabled": manifest.enabled,
            "description": manifest.description,
            "requires": manifest.requires,
        }
        err = _validate_manifest_dict(as_dict)
        if err:
            log.error("PluginHost: manifest %s invalid: %s", path, err)
            return None

        return manifest

    # ------------------------------------------------------------------ load

    def load(self, manifest: SkillManifest) -> list[ToolDescriptor]:
        """Manifesto'yu yükle ve geçerli ToolDescriptor'ları döner.

        Davranış kuralları:

        * ``manifest.enabled is False`` → boş liste, info log (Req 16.3).
        * ``manifest.name in disabled_skills`` → boş liste, info log
          (Req 18.3).
        * ``entry_module`` import edilemezse → boş liste, error log;
          diğer skill'lere etki etmemesi için exception yutulur.
        * ``manifest.tools`` içinde modülde bulunmayan veya callable
          olmayan adlar → atlanır, error log (Req 17.2).
        * Her geçerli handler için ``__tool__`` metadata'sı okunur;
          declaration ve ``execution_mode`` doğrulanır. Doğrulamadan
          geçemeyen tool'lar atlanır (Req 17.2).
        """
        if not manifest.enabled:
            log.info(
                "PluginHost: skill %r marked enabled=False; skipping",
                manifest.name,
            )
            return []
        if manifest.name in self._disabled:
            log.info(
                "PluginHost: skill %r in disabled_skills; skipping",
                manifest.name,
            )
            return []

        # Req 17.1, 17.4: NVIDIA anahtarı yoksa NVIDIA bağımlı skill'leri yükleme.
        if manifest.name in _NVIDIA_DEPENDENT_SKILLS:
            from app_config import has_nvidia_api_key  # noqa: PLC0415 - geç import; döngüsel bağımlılığı önler
            if not has_nvidia_api_key():
                if not self._nvidia_key_warning_logged:
                    log.warning(
                        "NVIDIA anahtarı bulunamadığı için memory_rag, doc_intel, "
                        "reasoning, translate, safety, creative, image_search, "
                        "embodied skill'leri devre dışı"
                    )
                    self._nvidia_key_warning_logged = True
                return []

        if not manifest.entry_module:
            log.warning(
                "PluginHost: skill %r has empty entry_module; nothing to load",
                manifest.name,
            )
            return []

        # Req 13.4: requires listesindeki paketler eksikse skill'i devre dışı bırak.
        missing_packages = self._find_missing_requires(manifest)
        if missing_packages:
            log.warning(
                "PluginHost: '%s' skill'i devre dışı bırakıldı çünkü şu paketler "
                "yüklü değil: %s",
                manifest.name,
                ", ".join(missing_packages),
            )
            return []

        try:
            module = importlib.import_module(manifest.entry_module)
        except Exception as exc:  # noqa: BLE001 - skill import'u her şey fırlatabilir
            log.error(
                "PluginHost: failed to import entry_module %s for skill %r: %s",
                manifest.entry_module,
                manifest.name,
                exc,
            )
            return []

        descriptors: list[ToolDescriptor] = []
        for tool_name in manifest.tools:
            handler = getattr(module, tool_name, None)
            if handler is None or not callable(handler):
                log.error(
                    "PluginHost: skill %r tool %r missing or not callable in %s",
                    manifest.name,
                    tool_name,
                    manifest.entry_module,
                )
                continue
            descriptor = self._build_descriptor(manifest, tool_name, handler)
            if descriptor is not None:
                descriptors.append(descriptor)

        return descriptors

    def _build_descriptor(
        self,
        manifest: SkillManifest,
        tool_name: str,
        handler: Any,
    ) -> ToolDescriptor | None:
        """Tek bir handler fonksiyonundan ToolDescriptor üret veya ``None``."""
        meta = getattr(handler, "__tool__", None)
        if not isinstance(meta, dict):
            log.error(
                "PluginHost: skill %r tool %r missing __tool__ metadata dict",
                manifest.name,
                tool_name,
            )
            return None

        declaration = meta.get("declaration")
        err = _validate_declaration(declaration)
        if err is not None:
            log.error(
                "PluginHost: skill %r tool %r declaration invalid: %s",
                manifest.name,
                tool_name,
                err,
            )
            return None

        # Declaration adı, fonksiyon adından farklı olabilir; Gemini'nin
        # gördüğü ad declaration'dakidir, biz onu kanonik kabul ederiz.
        decl_name = declaration["name"]
        if decl_name != tool_name:
            log.debug(
                "PluginHost: skill %r tool %r declaration name=%r differs "
                "from function name; using declaration name",
                manifest.name,
                tool_name,
                decl_name,
            )

        execution_mode = meta.get("execution_mode", "inline")
        if execution_mode not in _VALID_EXECUTION_MODES:
            log.error(
                "PluginHost: skill %r tool %r has invalid execution_mode %r; "
                "expected 'inline' or 'background'",
                manifest.name,
                tool_name,
                execution_mode,
            )
            return None

        timeout_raw = meta.get("timeout_sec", 30.0)
        try:
            timeout_sec = float(timeout_raw)
        except (TypeError, ValueError):
            log.error(
                "PluginHost: skill %r tool %r has invalid timeout_sec %r",
                manifest.name,
                tool_name,
                timeout_raw,
            )
            return None
        if timeout_sec <= 0:
            log.error(
                "PluginHost: skill %r tool %r timeout_sec must be positive (got %r)",
                manifest.name,
                tool_name,
                timeout_sec,
            )
            return None

        # Req 13.1, 13.3: opsiyonel route alanını doğrula ve RouteProfile'a dönüştür.
        route_raw = meta.get("route")
        route_err = _validate_route(route_raw)
        if route_err is not None:
            log.warning(
                "PluginHost: '%s' skill'inin '%s' tool'u geçersiz route alanı "
                "nedeniyle atlandı — %s",
                manifest.name,
                tool_name,
                route_err,
            )
            return None

        route_profile: RouteProfile | None = None
        if route_raw is not None:
            route_profile = _route_to_profile(route_raw)

        return ToolDescriptor(
            name=decl_name,
            declaration=declaration,
            handler=handler,
            execution_mode=execution_mode,  # type: ignore[arg-type]
            skill_id=manifest.name,
            timeout_sec=timeout_sec,
            route_profile=route_profile,
        )

    # ---------------------------------------------------------------- reload

    def reload(self, name: str) -> list[ToolDescriptor]:
        """Daha önce keşfedilmiş bir skill'i yeniden yükle.

        ``entry_module`` ``sys.modules``'ta varsa
        :func:`importlib.reload` ile tazelenir; sonra :meth:`load`
        çağrılarak yeni ToolDescriptor listesi üretilir. Bilinmeyen
        skill için boş liste döner ve uyarı log'lanır.
        """
        manifest = self._manifests.get(name)
        if manifest is None:
            log.warning(
                "PluginHost: cannot reload unknown skill %r; "
                "did you forget to call discover()?",
                name,
            )
            return []

        if manifest.entry_module:
            existing = sys.modules.get(manifest.entry_module)
            if existing is not None:
                try:
                    importlib.reload(existing)
                except Exception as exc:  # noqa: BLE001 - reload'da her şey olabilir
                    log.error(
                        "PluginHost: failed to reload module %s for skill %r: %s",
                        manifest.entry_module,
                        name,
                        exc,
                    )
                    return []

        return self.load(manifest)


__all__ = [
    "PluginHost",
]
