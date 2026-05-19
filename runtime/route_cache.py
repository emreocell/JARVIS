"""Model_Router için kısa süreli LRU + TTL sonuç önbelleği.

Sorumluluklar
-------------
* Aynı ``(tool_name, RouteRequest)`` çifti için son TTL (varsayılan 30 sn)
  içinde yapılan başarılı çağrının ``RouteResult``'ını yeniden döndürmek
  ve gereksiz NVIDIA / Gemini HTTP isteklerinin önüne geçmek
  (Requirements 18.5, 18.6).
* Kapasite (varsayılan 32) aşıldığında en eski (LRU) girişi tahliye etmek.
* ``model_router.disable_cache`` bayrağı veya ``RouteCache(disabled=True)``
  modunda her ``get``/``put`` çağrısının no-op davranmasını sağlamak.
* Saf, deterministik bir model sunmak: zaman bağımlılığı dışarıdan
  enjekte edilebilir bir ``time_provider`` ile yönetilir, böylece
  ``RouteCache`` Hypothesis tabanlı property testlerinde sahte saatlerle
  ilerletilebilir (bkz. Property 4 — ``test_route_cache_pbt.py``).

Tasarım notları
---------------
* Anahtar türü dışarıdan opak bir ``str`` olarak kabul edilir; çağıran
  taraf (Model_Router) ``make_key(tool_name, request)`` yardımcısıyla
  ``sha256(tool_name + canonical_json(request))`` üretir.
* Depolama ``collections.OrderedDict`` üzerine kuruludur: eklenen / okunan
  girişler sona taşınır, böylece ``popitem(last=False)`` en eski girişi
  tahliye eder.
* TTL süresi dolmuş bir giriş ``get`` sırasında tembel olarak silinir;
  aktif olarak temizleyen bir thread yoktur — bu sayede sınıfın saf modeli
  kolayca test edilebilir kalır.
* Cache iki sürüm bilgisini bir arada tutar: ``stored_at`` (ekleme anı) ve
  ``expires_at`` (``stored_at + ttl_sec``). ``get`` çağrısı yalnızca
  ``now < expires_at`` ise hit kabul eder.

Validates: Requirements 18.5, 18.6 (jarvis-nvidia-skill-pack)
"""

from __future__ import annotations

# Feature: jarvis-nvidia-skill-pack, Task 4.1 — runtime/route_cache.py

import dataclasses
import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

from runtime.types import RouteRequest


# ---------------------------------------------------------------------------
# Default sınırlar — design.md ve Req 18.5'te tanımlandığı gibi.
# ---------------------------------------------------------------------------

DEFAULT_CAPACITY: int = 32
"""LRU kapasitesi; Req 18.5'e göre üst sınır 32 girdidir."""

DEFAULT_TTL_SEC: float = 30.0
"""TTL üst sınırı; Req 18.5'e göre 30 saniyeyi aşamaz."""


T = TypeVar("T")


# ---------------------------------------------------------------------------
# Anahtar üretimi
# ---------------------------------------------------------------------------


def _canonical_payload(request: RouteRequest | dict[str, Any]) -> str:
    """``RouteRequest``'i (veya bir dict'i) deterministik JSON'a çevirir.

    Sözlük anahtarları sıralanır, ayraçlar sıkıştırılır ve None alanlar
    aynen korunur. Bu sayede aynı mantıksal istek her zaman aynı string
    karşılığını üretir; SHA-256 anahtarı stabildir.
    """
    if dataclasses.is_dataclass(request) and not isinstance(request, type):
        payload: dict[str, Any] = dataclasses.asdict(request)
    elif isinstance(request, dict):
        payload = dict(request)
    else:
        # Bilinmeyen tip için son çare: __dict__ varsa onu kullan, yoksa
        # repr — anahtar yine de deterministik olur.
        payload = getattr(request, "__dict__", {"_repr": repr(request)})

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,  # set / Path / vs. için güvenli düşüş.
    )


def make_key(tool_name: str, request: RouteRequest | dict[str, Any]) -> str:
    """``sha256(tool_name + canonical_json(request))`` anahtarını üretir.

    ``tool_name`` ile payload arasına ``\\x00`` ayracı koyulur; böylece
    ``("foo", {"bar": 1})`` ile ``("foobar", {"": 1})`` gibi çakışmalar
    oluşmaz.

    Parameters
    ----------
    tool_name:
        Çağrılan tool'un adı (ör. ``"memory_rag_query"``).
    request:
        ``RouteRequest`` dataclass'ı veya eşdeğer bir dict.

    Returns
    -------
    str
        Hex-encoded SHA-256 özeti (64 karakter).
    """
    payload = _canonical_payload(request)
    h = hashlib.sha256()
    h.update(tool_name.encode("utf-8"))
    h.update(b"\x00")
    h.update(payload.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Dahili giriş tipi
# ---------------------------------------------------------------------------


@dataclass
class _Entry(Generic[T]):
    """Cache içindeki tek bir giriş.

    ``stored_at`` ve ``expires_at`` birlikte tutulur, böylece TTL kontrolü
    saatten bağımsız olarak ``now`` parametresiyle yapılabilir.
    """

    value: T
    stored_at: float
    expires_at: float


# ---------------------------------------------------------------------------
# RouteCache
# ---------------------------------------------------------------------------


class RouteCache(Generic[T]):
    """Saf, zaman-enjekte edilebilir LRU + TTL cache.

    Parameters
    ----------
    capacity:
        En fazla tutulabilecek girdi sayısı. Pozitif olmalıdır.
    ttl_sec:
        Bir girdinin yaşam süresi saniye cinsinden. Pozitif olmalıdır.
    time_provider:
        Mevcut zamanı saniye olarak döndüren çağrılabilir.
        ``None`` verilirse ``time.monotonic`` kullanılır. Hypothesis
        testleri sahte bir saat ile property 4'ü ilerletir.
    disabled:
        ``True`` ise cache pasif moda alınır: ``get`` her zaman ``None``,
        ``put`` no-op olur, ``__len__`` 0 kalır. Saklı girdiler korunur
        ama erişilemez (``set_disabled(False)`` ile geri açılabilir,
        TTL'leri geçtiyse tembel olarak silinirler).

    Notes
    -----
    Sınıf **thread-safe değildir**. Model_Router tek bir asyncio
    event loop içinden çağırdığı için kilitlemeye gerek duyulmaz; eğer
    ileride çoklu thread erişimi gerekirse çağıran katman kilit ekler.
    """

    __slots__ = (
        "_capacity",
        "_ttl_sec",
        "_time_provider",
        "_disabled",
        "_entries",
    )

    def __init__(
        self,
        capacity: int = DEFAULT_CAPACITY,
        ttl_sec: float = DEFAULT_TTL_SEC,
        *,
        time_provider: Callable[[], float] | None = None,
        disabled: bool = False,
    ) -> None:
        if capacity <= 0:
            raise ValueError(f"capacity must be positive, got {capacity!r}")
        if ttl_sec <= 0:
            raise ValueError(f"ttl_sec must be positive, got {ttl_sec!r}")

        self._capacity: int = int(capacity)
        self._ttl_sec: float = float(ttl_sec)
        self._time_provider: Callable[[], float] = (
            time_provider if time_provider is not None else time.monotonic
        )
        self._disabled: bool = bool(disabled)
        # OrderedDict insertion-order'ı LRU sırası olarak kullanırız:
        # en yeni okunan / yazılan giriş sona taşınır.
        self._entries: OrderedDict[str, _Entry[T]] = OrderedDict()

    # ------------------------------------------------------------------
    # Kamuya açık API
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        """LRU kapasitesi (constructor'da sabitlenir)."""
        return self._capacity

    @property
    def ttl_sec(self) -> float:
        """TTL süresi (constructor'da sabitlenir)."""
        return self._ttl_sec

    @property
    def disabled(self) -> bool:
        """Cache şu an bypass modunda mı?

        ``True`` iken ``get`` ve ``put`` çağrıları no-op davranır.
        """
        return self._disabled

    def set_disabled(self, value: bool) -> None:
        """Bypass modunu çalışma zamanında değiştirir.

        Model_Router, ``model_router.disable_cache`` config alanı
        değişirse bu metodu çağırarak cache'i sıcak halde
        açıp/kapatabilir. Mevcut girdiler silinmez; sadece
        erişilemezleştirilir.
        """
        self._disabled = bool(value)

    def get(self, key: str, now: float | None = None) -> T | None:
        """Anahtara karşılık gelen değeri döndürür ya da ``None``.

        Cache devre dışıysa veya giriş yoksa / TTL aştıysa ``None`` döner
        ve TTL'i geçmiş giriş tembel olarak silinir. Hit durumunda giriş
        LRU sırasının sonuna taşınır (en taze).

        Parameters
        ----------
        key:
            ``make_key`` ile üretilmiş ya da harici olarak hesaplanmış
            opak anahtar.
        now:
            Mevcut zaman (saniye). ``None`` ise ``time_provider()``
            çağrılır.
        """
        if self._disabled:
            return None

        entry = self._entries.get(key)
        if entry is None:
            return None

        current = self._resolve_now(now)
        if current >= entry.expires_at:
            # TTL doldu — tembel temizlik.
            del self._entries[key]
            return None

        # Hit: en taze pozisyona taşı.
        self._entries.move_to_end(key, last=True)
        return entry.value

    def put(self, key: str, value: T, now: float | None = None) -> None:
        """Anahtara değeri yazar ve TTL saatini başlatır.

        Cache devre dışıysa hiçbir şey yapmaz. Aynı anahtar zaten varsa
        değer ve TTL güncellenir, giriş LRU sırasının sonuna taşınır.
        Kapasite dolu ise en eski (head) giriş tahliye edilir.

        Parameters
        ----------
        key:
            ``make_key`` ile üretilmiş opak anahtar.
        value:
            Önbelleklenecek değer (genellikle ``RouteResult``).
        now:
            Mevcut zaman (saniye). ``None`` ise ``time_provider()``.
        """
        if self._disabled:
            return

        current = self._resolve_now(now)
        entry: _Entry[T] = _Entry(
            value=value,
            stored_at=current,
            expires_at=current + self._ttl_sec,
        )

        if key in self._entries:
            # Aynı anahtara yeniden yazma — eskiyi bırak, yenisini sona koy.
            self._entries[key] = entry
            self._entries.move_to_end(key, last=True)
            return

        self._entries[key] = entry

        # Kapasite kontrolü — en eski (head) girişi at.
        while len(self._entries) > self._capacity:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        """Tüm girdileri temizler.

        ``disabled`` bayrağına dokunmaz; sadece içerik silinir. Test
        teardown'larında veya açık bir kullanıcı komutuyla cache'i
        sıfırlamak için kullanılır.
        """
        self._entries.clear()

    def __len__(self) -> int:
        """Önbellekte saklı girdi sayısı.

        Cache disabled iken 0 döner; saklı girdiler erişilemez sayılır.
        TTL süresi dolmuş girdiler ``get`` çağrısında tembel olarak
        temizlenir; ``__len__`` gerçek saat yerine ham depolama sayısını
        döndürür, böylece zaman enjeksiyonlu testlerde tutarlı davranır.
        """
        if self._disabled:
            return 0
        return len(self._entries)

    def __contains__(self, key: object) -> bool:
        """``key in cache`` testleri için tembel TTL kontrolü."""
        if self._disabled or not isinstance(key, str):
            return False
        entry = self._entries.get(key)
        if entry is None:
            return False
        if self._resolve_now(None) >= entry.expires_at:
            del self._entries[key]
            return False
        return True

    # ------------------------------------------------------------------
    # Yardımcılar
    # ------------------------------------------------------------------

    def _resolve_now(self, now: float | None) -> float:
        """``now`` verilmediyse ``time_provider``'ı çağırır."""
        if now is None:
            return float(self._time_provider())
        return float(now)

    def _purge_expired(self) -> None:
        """TTL'i geçmiş tüm girdileri temizler.

        Yalnızca ``__len__`` ve ``__contains__`` tarafından çağrılır;
        ``get``/``put`` zaten kendi tembel temizliğini yapar. Kapasiteyi
        bozmaz çünkü silme işlemi yalnızca süresi dolmuş girdileri
        kaldırır.
        """
        if not self._entries:
            return
        current = self._resolve_now(None)
        expired_keys = [
            k for k, entry in self._entries.items() if current >= entry.expires_at
        ]
        for k in expired_keys:
            del self._entries[k]


__all__ = [
    "DEFAULT_CAPACITY",
    "DEFAULT_TTL_SEC",
    "RouteCache",
    "make_key",
]
