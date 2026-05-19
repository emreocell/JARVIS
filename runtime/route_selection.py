"""Saf rota seçim fonksiyonu — Model_Router'dan bağımsız, yan etkisiz.

Bu modül yalnızca `select_route()` fonksiyonunu ve yardımcı `derive_tool_category()`
fonksiyonunu içerir. Her ikisi de saf (pure) fonksiyondur: dış durum okumaz,
diske yazmaz, HTTP çağrısı yapmaz. Bu özellik sayesinde Hypothesis ile doğrudan
property-based test edilebilirler.

Karar zinciri (design.md § select_route):
  1. `prefer` verilmişse onu kullan.
  2. `tool_route` (tool descriptor'ından gelen RouteProfile) varsa onu kullan.
  3. `default_routes[tool_category]` haritasından kategori eşleşmesi varsa onu kullan.
  4. Hiçbiri eşleşmezse `gemini_primary` + Gemini varsayılan modeli kullan.

Her adımda `health` ve `blacklist` filtresi uygulanır: sağlıksız veya kara
listedeki sağlayıcılar zincirden çıkarılır. Tüm rotalar elendiyse son çare
olarak varsayılan rota döner (filtre uygulanmadan).

Requirements: 1.1, 1.2, 1.6, 2.1, 2.2, 13.2
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from runtime.types import HealthState, Route, RouteProfile


# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

# Gemini varsayılan modeli — hiçbir rota eşleşmediğinde son çare.
_GEMINI_DEFAULT_MODEL = "models/gemini-3.1-flash-lite"

# Son çare rota: gemini_primary + Gemini varsayılan modeli.
_FALLBACK_ROUTE = Route(provider="gemini_primary", model=_GEMINI_DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# Tool kategori türetimi
# ---------------------------------------------------------------------------

# Prefix/pattern → kategori eşleme tablosu.
# Sıra önemlidir: daha spesifik pattern'ler önce gelir.
_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    # memory_*
    (re.compile(r"^memory_"), "memory_rag.query"),
    # doc_*, chart_*, screenshot_*
    (re.compile(r"^(doc_|chart_|screenshot_)"), "doc_intel.parse"),
    # plan_*
    (re.compile(r"^plan_"), "reasoning.plan"),
    # translate_*
    (re.compile(r"^translate_"), "translate.text"),
    # creative_*, financial_*, medical_*
    (re.compile(r"^(creative_|financial_|medical_)"), "creative.write"),
    # image_*
    (re.compile(r"^image_"), "image_search.embed"),
    # gui_*
    (re.compile(r"^gui_"), "embodied.next_action"),
    # pii_*, *_safety_check, topic_control_check, deepfake_*
    (re.compile(r"^pii_|_safety_check$|^topic_control_check$|^deepfake_"), "safety.*"),
]


def derive_tool_category(tool_name: str) -> str:
    """Tool adından prefix tabanlı kategori türet.

    Saf fonksiyon: girdi `tool_name` string'inden deterministik olarak
    bir kategori string'i döner. Hiçbir kural eşleşmezse `voice_core.intent`
    döner.

    Args:
        tool_name: Tool'un kayıtlı adı (örn. ``"memory_rag_query"``).

    Returns:
        Kategori string'i (örn. ``"memory_rag.query"``).
    """
    for pattern, category in _CATEGORY_RULES:
        if pattern.search(tool_name):
            return category
    return "voice_core.intent"


# ---------------------------------------------------------------------------
# Sağlık / kara liste filtresi
# ---------------------------------------------------------------------------


def _is_provider_available(
    provider: str,
    health: dict[str, HealthState],
    blacklist: set[str],
) -> bool:
    """Bir sağlayıcının kullanılabilir olup olmadığını döner.

    Sağlayıcı kara listede ise veya health kaydı ``healthy=False`` ise
    ``False`` döner. Health kaydı yoksa sağlıklı kabul edilir.

    Args:
        provider: Sağlayıcı adı (``"gemini_primary"``, ``"nvidia"`` vb.).
        health: Sağlayıcı adından ``HealthState``'e eşleme.
        blacklist: Oturum boyunca devre dışı bırakılmış sağlayıcı adları.

    Returns:
        ``True`` ise sağlayıcı kullanılabilir.
    """
    if provider in blacklist:
        return False
    state = health.get(provider)
    if state is not None and not state.healthy:
        return False
    return True


def _filter_chain(
    chain: tuple[Route, ...],
    health: dict[str, HealthState],
    blacklist: set[str],
) -> tuple[Route, ...]:
    """Zincirden sağlıksız/kara listedeki rotaları çıkar.

    Sırayı ve tekil rotaları korur; aynı rota iki kez geçse bile yalnızca
    ilk geçerli örneği tutar.

    Args:
        chain: Sıralı rota zinciri.
        health: Sağlayıcı sağlık durumu haritası.
        blacklist: Devre dışı sağlayıcı adları.

    Returns:
        Filtrelenmiş rota zinciri (boş olabilir).
    """
    seen: set[tuple[str, str]] = set()
    result: list[Route] = []
    for route in chain:
        key = (route.provider, route.model)
        if key in seen:
            continue
        seen.add(key)
        if _is_provider_available(route.provider, health, blacklist):
            result.append(route)
    return tuple(result)


# ---------------------------------------------------------------------------
# RouteProfile → (primary, fallback) dönüşümü
# ---------------------------------------------------------------------------


def _profile_to_chain(profile: RouteProfile) -> tuple[Route, ...]:
    """RouteProfile'ı düz rota zinciri olarak döner."""
    return profile.chain()


def _dict_to_route(d: dict) -> Route:
    """``{"provider": ..., "model": ...}`` sözlüğünden ``Route`` üret."""
    return Route(provider=d["provider"], model=d["model"])


def _dict_to_profile(d: dict) -> RouteProfile:
    """``default_routes`` haritasındaki sözlükten ``RouteProfile`` üret.

    ``fallback`` anahtarı opsiyoneldir; yoksa boş tuple kullanılır.
    """
    primary = _dict_to_route(d)
    fallback_list = d.get("fallback", [])
    fallback = tuple(_dict_to_route(fb) for fb in fallback_list)
    return RouteProfile(primary=primary, fallback=fallback)


# ---------------------------------------------------------------------------
# Ana saf fonksiyon
# ---------------------------------------------------------------------------


def select_route(
    *,
    tool_name: str,
    tool_route: RouteProfile | None,
    prefer: Route | RouteProfile | None,
    default_routes: dict[str, dict | RouteProfile],
    health: dict[str, HealthState],
    blacklist: set[str],
) -> tuple[Route, tuple[Route, ...]]:
    """Karar zincirini uygulayarak birincil rota ve fallback zincirini döner.

    Karar önceliği:
      1. ``prefer`` — çağıran tarafından açıkça belirtilen tercih.
      2. ``tool_route`` — tool descriptor'ındaki ``RouteProfile``.
      3. ``default_routes[tool_category]`` — config'den gelen kategori varsayılanı.
      4. Son çare: ``gemini_primary`` + Gemini varsayılan modeli.

    Her adımda ``health`` ve ``blacklist`` filtresi uygulanır. Filtreleme
    sonrası zincirde hiç rota kalmazsa bir sonraki karar adımına geçilir.
    Tüm adımlar tükendikten sonra da sağlıklı rota bulunamazsa son çare
    rota filtresiz döner (en az bir rota her zaman döner).

    Args:
        tool_name: Çağrılan tool'un adı; kategori türetimi için kullanılır.
        tool_route: Tool descriptor'ından gelen ``RouteProfile`` (opsiyonel).
        prefer: Çağıranın açık tercihi; ``Route`` veya ``RouteProfile``
            olabilir (opsiyonel).
        default_routes: Config'den gelen ``{kategori: {provider, model, ...}}``
            haritası. Değerler ``RouteProfile`` veya sözlük olabilir.
        health: Sağlayıcı adından ``HealthState``'e eşleme.
        blacklist: Oturum boyunca devre dışı bırakılmış sağlayıcı adları.

    Returns:
        ``(primary_route, fallback_tuple)`` çifti. ``primary_route`` seçilen
        birincil rotadır; ``fallback_tuple`` sıralı fallback rotalarıdır
        (birincil hariç). Her ikisi de ``Route`` nesnesidir.

    Notes:
        - Saf fonksiyon: dış durum okumaz, yan etkisi yoktur.
        - Aynı girdilerle her çağrı aynı sonucu üretir (deterministik).
        - En az bir rota her zaman döner (son çare garantisi).
    """
    # Adım 1: prefer
    if prefer is not None:
        chain = _resolve_prefer_chain(prefer)
        filtered = _filter_chain(chain, health, blacklist)
        if filtered:
            return filtered[0], filtered[1:]

    # Adım 2: tool_route
    if tool_route is not None:
        chain = _profile_to_chain(tool_route)
        filtered = _filter_chain(chain, health, blacklist)
        if filtered:
            return filtered[0], filtered[1:]

    # Adım 3: default_routes[category]
    category = derive_tool_category(tool_name)
    route_entry = default_routes.get(category)
    if route_entry is None:
        # Bazı kategoriler alt-kategori wildcard içerebilir (örn. "safety.*").
        # Haritada tam eşleşme yoksa prefix eşleşmesi dene.
        route_entry = _find_category_entry(category, default_routes)

    if route_entry is not None:
        profile = (
            route_entry
            if isinstance(route_entry, RouteProfile)
            else _dict_to_profile(route_entry)
        )
        chain = _profile_to_chain(profile)
        filtered = _filter_chain(chain, health, blacklist)
        if filtered:
            return filtered[0], filtered[1:]

    # Adım 4: Son çare — gemini_primary + Gemini varsayılan modeli.
    # Filtresiz döner; en az bir rota garantisi.
    return _FALLBACK_ROUTE, ()


# ---------------------------------------------------------------------------
# Yardımcı iç fonksiyonlar
# ---------------------------------------------------------------------------


def _resolve_prefer_chain(
    prefer: Route | RouteProfile,
) -> tuple[Route, ...]:
    """``prefer`` argümanını düz rota zincirine çevir."""
    if isinstance(prefer, Route):
        return (prefer,)
    # RouteProfile
    return prefer.chain()


def _find_category_entry(
    category: str,
    default_routes: dict[str, dict | RouteProfile],
) -> dict | RouteProfile | None:
    """Kategori string'i için haritada prefix/wildcard eşleşmesi ara.

    ``"safety.*"`` gibi wildcard kategoriler için haritada ``"safety.*"``
    anahtarı aranır. Bulunamazsa ``None`` döner.

    Args:
        category: Türetilmiş kategori string'i.
        default_routes: Config'den gelen rota haritası.

    Returns:
        Eşleşen harita girdisi veya ``None``.
    """
    # Tam eşleşme zaten dışarıda denendi; burada wildcard/prefix dene.
    # "safety.*" → haritada "safety.*" anahtarı var mı?
    if category in default_routes:
        return default_routes[category]

    # Nokta ile ayrılmış prefix eşleşmesi: "safety.pii" → "safety.*"
    parts = category.split(".")
    if len(parts) >= 2:
        wildcard_key = parts[0] + ".*"
        if wildcard_key in default_routes:
            return default_routes[wildcard_key]

    return None


__all__ = [
    "select_route",
    "derive_tool_category",
]
