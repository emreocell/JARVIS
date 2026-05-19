"""Pure helpers for :mod:`skills.memory_rag`.

Bu modül Memory_RAG_Skill'in property-tabanlı testle (Hypothesis)
doğrulanan saf yardımcılarını içerir. NVIDIA NIM HTTP istekleri,
``Vector_Store`` insert/select işlemleri, dosya I/O ve gerçek
``logging.Handler``'ları dışarıda tutulur; bu modül yalnızca veri
dönüşümü ve in-memory kuyruk yönetimi yapar.

Sözleşme
========

* :func:`chunk_text` — Bir metni paragraf/cümle sınırlarını tercih
  eden sabit-uzunluklu pencerelere böler. Property 14 invariantlarını
  garantiler:

  1. **Uzunluk**: her chunk uzunluğu ``chunk_chars`` değerini aşmaz.
  2. **Kapsama**: chunk'ları sırasıyla birleştirip her ardışık çiftin
     ``overlap`` uzunluklu örtüşme bölgesini düşürdüğümüzde orijinal
     metin tamı tamına yeniden oluşur (max_chunks aşılmadığı sürece).
  3. **Üst sınır**: chunk sayısı ``max_chunks`` (varsayılan 1000)
     değerini aşmaz; aşılırsa fonksiyon sessizce keser ve sonraki
     chunk'lar üretilmez (Req 4.10: 1000 chunk üst sınırı).
  4. **Boş metin**: ``text == ""`` veya yalnızca whitespace içeren
     girdi için çıktı boş listedir.

* :func:`format_rag_answer` — Yanıt metnini Türkçe tek paragrafa
  indirger ve kullanılan kaynak başlıklarını sonuna parantez içinde
  ekler (Req 4.6). Saf string dönüşümü; ne dosya yazar ne log düşer.

* :class:`PendingIndexQueue` — Privacy_Mode aktifken biriken
  indeksleme isteklerini tutan, kapasitesi 5000 olan ``deque``
  sarmalayıcısı. Doluyken yeni öğe gelirse en eskisi düşer ve uyarı
  log'u atılır (Req 4.7 + Req 16.8). Tek bir süreç içinde
  thread-safe olması beklenmez; çağıran taraf serileştirir.

* :func:`batch_for_embed` — Bir iterable'ı sabit boyutlu (varsayılan
  16) gruplara böler; embedding API'nin batch=16 sözleşmesi için
  kullanılır (Req 18.3 / design.md "Embedder").

Tüm saf fonksiyonlar deterministiktir: aynı girdi her zaman aynı
çıktıyı üretir.
"""

from __future__ import annotations

import logging
import re
from collections import deque
from collections.abc import Iterable, Iterator, Sequence
from typing import Any


log = logging.getLogger(__name__)


__all__ = [
    "chunk_text",
    "format_rag_answer",
    "PendingIndexQueue",
    "batch_for_embed",
    "DEFAULT_CHUNK_CHARS",
    "DEFAULT_CHUNK_OVERLAP",
    "DEFAULT_MAX_CHUNKS",
    "DEFAULT_EMBED_BATCH",
    "PENDING_QUEUE_CAPACITY",
]


DEFAULT_CHUNK_CHARS: int = 800
"""``chunk_text`` varsayılan parça boyutu (Req 4.3 / design.md ``memory_rag.chunk_chars``)."""

DEFAULT_CHUNK_OVERLAP: int = 100
"""``chunk_text`` varsayılan örtüşme uzunluğu."""

DEFAULT_MAX_CHUNKS: int = 1000
"""Tek seferde üretilen chunk sayısı için sert üst sınır (Req 4.10)."""

DEFAULT_EMBED_BATCH: int = 16
"""``batch_for_embed`` için NVIDIA embedding API batch boyutu (Req 18.3)."""

PENDING_QUEUE_CAPACITY: int = 5000
"""``PendingIndexQueue`` kapasitesi; doluysa en eski atılır."""


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_positive_int(name: str, value: Any, *, allow_zero: bool = False) -> None:
    """``value`` pozitif (veya allow_zero=True ise non-negative) tamsayı mı doğrula.

    ``bool`` ``int``'in alt sınıfı olduğu için ``True``/``False`` yanlışlıkla
    sayı olarak yorumlanmasın diye açıkça reddedilir.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} pozitif bir tamsayı olmalı, alındı: "
            f"{type(value).__name__}={value!r}"
        )
    if allow_zero:
        if value < 0:
            raise ValueError(f"{name} negatif olamaz, alındı: {value}")
    else:
        if value <= 0:
            raise ValueError(f"{name} pozitif olmalı, alındı: {value}")


# ---------------------------------------------------------------------------
# chunk_text — paragraf/cümle bazlı bölme
# ---------------------------------------------------------------------------

# Snap için tercih sırası: önce paragraf, sonra cümle, sonra yumuşak satır
# sonu, en son boşluk. Tuple elemanı: (regex pattern, boundary_offset).
# ``boundary_offset`` chunk'ın bu pattern'in ``end()`` konumuna kadar
# uzayacağını söyler (boundary karakterleri **dahil**).
_BOUNDARY_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Paragraf sonu — bir veya daha fazla blank line.
    re.compile(r"\n\s*\n"),
    # Cümle terminatörü + whitespace (Türkçe ve İngilizce).
    re.compile(r"[.!?…][\"')\]]*\s+"),
    # Yumuşak yeni satır.
    re.compile(r"\n"),
    # Whitespace (en zayıf snap noktası).
    re.compile(r"\s"),
)


def _find_snap_end(text: str, lo: int, hi: int) -> int | None:
    """``[lo, hi]`` aralığında en güçlü doğal sınırın bittiği konumu bul.

    Aralıkta paragraf sınırı varsa onu, yoksa cümle terminatörü, yoksa
    satır sonu, yoksa boşluk arar. Hiçbiri yoksa ``None`` döner.

    Args:
        text: Tüm metin.
        lo: Snap noktasının izin verilen en küçük konumu (dahil).
            Bu, chunk'ın en az ``lo - pos`` karakter uzunluğunda olmasını
            sağlar; çağıran taraf ``lo > pos + overlap`` seçerek
            ``next_pos > pos`` invariantını korur.
        hi: Snap noktasının izin verilen en büyük konumu (dahil).
            Genelde ``pos + chunk_chars``.

    Returns:
        Snap konumu (chunk'ın bu offset'te biteceği yer, exclusive) ya da
        ``None``. Dönen değer her zaman ``lo <= snap <= hi`` aralığındadır.

    Notes:
        Saf, deterministik. Boundary pattern'leri sırayla denenir; önce
        bulunan en sağdaki eşleşme tercih edilir (geniş chunk → daha az
        çağrı + daha iyi semantik bağlam).
    """
    if lo > hi:
        return None
    # Genişlemiş "yakalama" alanı: pattern lo'dan biraz önce başlayıp
    # hi'da bitebilir; ``re.finditer`` verilen slice üzerinde çalışır.
    # Boundary'nin ``end()`` konumu ``lo`` ile ``hi`` arasında olmalı.
    for pattern in _BOUNDARY_PATTERNS:
        best: int | None = None
        # ``hi`` exclusive olabilir; pattern hi'dan sonra başlasa bile
        # end() <= hi sağlanmalı, bu yüzden ``text[:hi]`` üzerinde
        # arıyoruz ve start >= max(0, lo - max_pattern_len) sınırlaması
        # için ``lo - 8`` kadar geriye gidiyoruz (pattern'lerin azami
        # uzunluğu birkaç karakterdir).
        search_start = max(0, lo - 8)
        for match in pattern.finditer(text, search_start, hi):
            end = match.end()
            if lo <= end <= hi:
                best = end  # son eşleşme (en sağ) seçilir
        if best is not None:
            return best
    return None


def chunk_text(
    text: str,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
    max_chunks: int = DEFAULT_MAX_CHUNKS,
) -> list[str]:
    """Bir metni paragraf/cümle sınırlarını tercih eden parçalara böl.

    Bu fonksiyon Memory_RAG_Skill'in ``memory_index_add`` boru hattının
    embedding üretmeden önceki ilk adımıdır (Req 4.3). Saf bir string
    dönüşümüdür — log, dosya I/O veya HTTP çağrısı yoktur.

    Args:
        text: Bölünecek metin. ``""`` veya yalnızca whitespace içeren
            girdi için boş liste döner (Property 14, invariant 4).
        chunk_chars: Her chunk'ın azami karakter uzunluğu. Pozitif
            tamsayı olmalı (varsayılan 800).
        overlap: Ardışık chunk'lar arasındaki örtüşen karakter sayısı.
            ``0 <= overlap < chunk_chars`` olmalı; aksi halde
            ``ValueError``. Varsayılan 100.
        max_chunks: Üretilecek chunk sayısının sert üst sınırı (Req 4.10:
            "tek seferlik 1000 chunk üst sınırı"). Non-negative tamsayı;
            varsayılan 1000. Sınırlar aşılırsa fonksiyon erken durur ve
            geri kalan metin atılır (cost guard).

    Returns:
        Sıralı string chunk'ların listesi.

    Raises:
        ValueError: Parametreler bu fonksiyonun sözleşmesini ihlal eder
            (negatif sayı, ``overlap >= chunk_chars`` veya ``bool`` tip).

    Property invariantları (Property 14, design.md):
        1. **Uzunluk**: ``all(len(c) <= chunk_chars for c in result)``.
        2. **Kapsama**: ``max_chunks`` aşılmadığı sürece, sıralı chunk'lar
           ardışık çiftlerden ``overlap`` karakter düşürülerek
           birleştirildiğinde orijinal metin tam olarak yeniden oluşur:
           ``result[0] + "".join(c[overlap:] for c in result[1:]) == text``.
        3. **Üst sınır**: ``len(result) <= max_chunks``.
        4. **Boş metin**: ``text.strip() == ""`` ⇒ ``result == []``.

    Notes:
        Saf ve deterministik. ``re`` modülü thread-safe'dir; aynı
        ``(text, chunk_chars, overlap, max_chunks)`` çağrısı her zaman
        aynı listeyi üretir.
    """
    _validate_positive_int("chunk_chars", chunk_chars)
    _validate_positive_int("overlap", overlap, allow_zero=True)
    _validate_positive_int("max_chunks", max_chunks, allow_zero=True)
    if overlap >= chunk_chars:
        raise ValueError(
            f"overlap ({overlap}) chunk_chars ({chunk_chars}) değerinden "
            "küçük olmalı; aksi halde chunker ilerleyemez."
        )

    # Boş veya yalnızca whitespace içeren metinler için boş liste.
    if text == "" or text.strip() == "":
        return []
    if max_chunks == 0:
        return []

    n = len(text)
    chunks: list[str] = []
    pos = 0

    # ``min_chunk_len`` = chunk'ın en az kaç karakter olması gerekir.
    # Bu, ``next_pos = end - overlap > pos`` invariantını korur, yani
    # her chunk overlap'ten en az 1 karakter daha uzundur. Aksi halde
    # next_pos >= pos olur ve sonsuz döngüye gireriz.
    min_chunk_len = overlap + 1

    while pos < n and len(chunks) < max_chunks:
        hard_end = min(pos + chunk_chars, n)
        end = hard_end

        # Snap: yalnızca metnin sonunda değilsek doğal sınır arıyoruz;
        # son chunk olası bir whitespace'e snap edilirse karakterler
        # kaybolur. ``end == n`` durumunda olduğu gibi bırak.
        if hard_end < n:
            snap_lo = pos + min_chunk_len
            if snap_lo <= hard_end:
                snap = _find_snap_end(text, snap_lo, hard_end)
                if snap is not None:
                    end = snap

        chunks.append(text[pos:end])

        if end >= n:
            break

        # Bir sonraki chunk overlap karakter geriden başlar; böylece
        # ``chunks[i+1][:overlap] == chunks[i][-overlap:]`` ve
        # ``"".join([chunks[0]] + [c[overlap:] for c in chunks[1:]]) == text``
        # invariantı sağlanır (snap ne yaparsa yapsın).
        pos = end - overlap

    return chunks


# ---------------------------------------------------------------------------
# format_rag_answer — Türkçe tek paragraf + kaynaklar parantez içinde
# ---------------------------------------------------------------------------


def format_rag_answer(
    answer: str,
    sources: Sequence[str] | Iterable[str],
) -> str:
    """RAG yanıtını Türkçe tek paragrafa indirir ve kaynakları ekler.

    Req 4.6: "yanıtın sonuna kullanılan kaynak başlıklarını parantez
    içinde listeler".

    Args:
        answer: LLM'in (``llama3-chatqa-1.5-70b``) ürettiği ham yanıt.
            Çok satırlı olabilir; bu fonksiyon iç whitespace dizilerini
            tek boşluğa indirir ve kenar boşluklarını trim eder.
        sources: Kullanılan kaynakların başlık veya kimliklerinin
            iterable'ı. Boş başlıklar yok sayılır; sıralı ve duplikat'lar
            korunur (kullanıcının niyetini koruyoruz).

    Returns:
        Tek bir paragraflık string. Yanıt boşsa "Yanıt üretilemedi."
        Türkçe paragrafına düşülür. Hiç kaynak yoksa parantezli son ek
        eklenmez; kaynak varsa son ek `` (Kaynaklar: A, B, C)`` formundadır.

    Notes:
        Saf, deterministik, yan etkisiz. ``str(...)`` çağrısı sayesinde
        ``answer`` veya kaynak elemanları ``str`` olmasa da güvenle
        formatlanır.
    """
    # ``answer.split()`` whitespace karakterlerinin tüm türlerini
    # (boşluk, tab, \n, \r) tek bir ayırıcı olarak ele alır ve baş/son
    # whitespace'i atar; bu da "tek paragraf" gereksinimini karşılar.
    body = " ".join(str(answer).split())
    if body == "":
        body = "Yanıt üretilemedi."

    titles: list[str] = []
    for src in sources or ():
        title = " ".join(str(src).split())
        if title:
            titles.append(title)

    if not titles:
        return body

    return f"{body} (Kaynaklar: {', '.join(titles)})"


# ---------------------------------------------------------------------------
# PendingIndexQueue — Privacy_Mode aktifken biriken indeksleme istekleri
# ---------------------------------------------------------------------------


class PendingIndexQueue:
    """Privacy_Mode aktifken biriken indeksleme isteklerini tutar.

    Req 4.7: "indeksleme kuyruğunu Privacy_Mode kapanana kadar tutar".
    Req 16.8: "Privacy_Mode kapanınca Memory_RAG kuyruğu drain olur".

    Kapasite ``PENDING_QUEUE_CAPACITY`` (5000); kuyruk doluyken yeni
    öğe gelirse en eski atılır ve uyarı log'u düşürülür. Bu davranış
    bellek kullanımını sınırlamak için tasarımdan gelir; bellek dolu
    bir oturumda kuyruğu kontrolsüz büyütmek istemiyoruz.

    Sözleşme:

    * Saf veri yapısı; ``deque`` üzerine ince bir sarmalayıcıdır.
    * Tek thread ortamında deterministik. Çağıran taraf eşzamanlılık
      gerekiyorsa harici kilit kullanmalıdır (mevcut kullanım: tek
      arka plan thread'i Privacy_Mode kapandığında drain eder).
    * ``__len__`` mevcuttur; ``bool(queue)`` dolu/boş için kullanılabilir.

    Yan etkiler:

    * Kapasite aşımında ``logging`` modülü üzerinden ``WARNING`` kaydı
      atılır. Test sırasında ``caplog`` ile gözlemlenebilir; bunun
      dışında sınıf saf kalır (dosya/HTTP yok).
    """

    __slots__ = ("_capacity", "_deque", "_dropped")

    def __init__(self, capacity: int = PENDING_QUEUE_CAPACITY) -> None:
        _validate_positive_int("capacity", capacity)
        self._capacity: int = capacity
        # ``deque(maxlen=...)`` tek başına yeterli olurdu, ama
        # ``maxlen`` aşımında düşen elemanı sessizce atar; biz uyarı
        # log'u istediğimiz için manuel pop ediyoruz.
        self._deque: deque[Any] = deque()
        self._dropped: int = 0

    @property
    def capacity(self) -> int:
        """Kuyruğun azami eleman sayısı."""
        return self._capacity

    @property
    def dropped_count(self) -> int:
        """Şimdiye kadar kapasite aşımı nedeniyle düşürülen eleman sayısı."""
        return self._dropped

    def __len__(self) -> int:
        return len(self._deque)

    def __bool__(self) -> bool:
        return bool(self._deque)

    def is_full(self) -> bool:
        """Kuyruk kapasitesine ulaştıysa ``True``."""
        return len(self._deque) >= self._capacity

    def enqueue(self, item: Any) -> bool:
        """Kuyruğun sonuna ``item`` ekle.

        Args:
            item: Kuyruğa konacak nesne (genellikle ``PendingIndexEntry``).
                Tip kontrolü yapılmaz; sınıf jenerik çalışır.

        Returns:
            Eski bir eleman düşürüldüyse ``True``, aksi halde ``False``.
            Çağıran taraf bu sinyali metrik için kullanabilir.
        """
        dropped = False
        if len(self._deque) >= self._capacity:
            old = self._deque.popleft()
            self._dropped += 1
            dropped = True
            old_id = getattr(old, "id", None)
            log.warning(
                "PendingIndexQueue kapasitesi (%d) doldu; en eski indeks "
                "isteği düşürüldü (id=%r, toplam_düşen=%d).",
                self._capacity,
                old_id,
                self._dropped,
            )
        self._deque.append(item)
        return dropped

    def drain(self) -> Iterator[Any]:
        """Kuyruğu FIFO sırayla boşaltan jeneratör.

        Yields:
            Kuyruğa eklenmiş öğeleri ekleme sırasında yield eder.
            Tüketim sırasında kuyruk boşalır; yarıda durdurulan bir
            ``drain()`` çağrısı kalan öğeleri kuyrukta bırakır.

        Notes:
            Privacy_Mode kapandığında bu jeneratör tüketilerek bekleyen
            indekslemeler ``memory_index_add`` boru hattına geri verilir.
        """
        while self._deque:
            yield self._deque.popleft()

    def snapshot(self) -> list[Any]:
        """Kuyruğun mevcut içeriğinin sığ kopyasını döner; kuyruk değişmez.

        Yalnızca test ve hata ayıklama için kullanılır; production
        akışında ``drain`` tercih edilmelidir.
        """
        return list(self._deque)

    def clear(self) -> None:
        """Tüm bekleyen öğeleri ve düşürülen sayacı sıfırla."""
        self._deque.clear()
        self._dropped = 0


# ---------------------------------------------------------------------------
# batch_for_embed — saf gruplandırma
# ---------------------------------------------------------------------------


def batch_for_embed(
    items: Iterable[Any],
    batch: int = DEFAULT_EMBED_BATCH,
) -> Iterator[list[Any]]:
    """``items`` iterable'ını ``batch`` boyutlu listelere böl.

    Memory_RAG_Skill'in embedding adımı NVIDIA NIM'in batch çağrısını
    (varsayılan 16, design.md "Embedder") kullanır; bu fonksiyon o
    sözleşmeyi karşılar. Image_Search_Skill ile davranış paylaşımı
    için ``image_search/_internal.batch_for_embed`` ile tutarlıdır.

    Args:
        items: Herhangi bir iterable (chunk listesi, embed input'ları,
            vs.).
        batch: Parça boyutu; ``> 0`` olmalı.

    Yields:
        En çok ``batch`` eleman içeren listeler. Son parça daha küçük
        olabilir; girdi boşsa hiçbir şey yield edilmez.

    Raises:
        ValueError: ``batch`` pozitif tamsayı değilse.

    Notes:
        Saf, deterministik. ``items`` bir generator ise tek geçişlik
        olur; çağıran taraf liste'ye dökmek isteyebilir.
    """
    _validate_positive_int("batch", batch)
    bucket: list[Any] = []
    for item in items:
        bucket.append(item)
        if len(bucket) >= batch:
            yield bucket
            bucket = []
    if bucket:
        yield bucket
