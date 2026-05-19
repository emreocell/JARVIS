"""Pure helpers for :mod:`skills.image_search`.

Bu modül Image_Search_Skill'in property-tabanlı testle (Hypothesis)
doğrulanan saf fonksiyonlarını içerir. HTTP isteği, NVCLIP çağrısı,
``Vector_Store`` insert, ``Result_Announcer`` duyurusu ve ``logging``
tarafı dışarıda tutulur; bu modül yalnızca veri dönüşümü yapar.

Sözleşme
========

* :func:`dedupe_by_hash` — bir ``(path, hash)`` listesini, daha önce
  indekslenmiş hash kümesine göre ayıklar; çıktı sırası girdiyle
  korunur ve aynı hash listede birden çok kez geçerse yalnızca ilk
  kayıt yer alır.
* :func:`knn_search` — sorgu embedding'iyle aday embedding'lerin
  cosine benzerliğini hesaplar ve azalan sıraya göre ilk ``top_k``
  adayı döndürür. Eşit skorlarda kararlı sıralama için ``id`` baz
  alınır.
* :func:`walk_supported` — verilen klasörü yürür ve desteklenen
  uzantıları (``.jpg/.jpeg/.png/.webp``) deterministik biçimde
  yield eder. ``os.walk`` kullanır; sabit bir dosya sistemi varsayımı
  altında etkin biçimde saftır (log/cache yok).
* :func:`batch_for_embed` — herhangi bir iterable'ı ``batch``
  boyutunda parçalara böler; son parça daha küçük olabilir.

Tüm fonksiyonlar yan etkisizdir; aynı girdi için her zaman aynı çıktıyı
üretirler.
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable, Iterator, Sequence


__all__ = [
    "SUPPORTED_EXTENSIONS",
    "dedupe_by_hash",
    "knn_search",
    "walk_supported",
    "batch_for_embed",
]


SUPPORTED_EXTENSIONS: tuple[str, ...] = (".jpg", ".jpeg", ".png", ".webp")
"""Image_Search_Skill'in indekslediği dosya uzantıları (Req 10.2).

Karşılaştırma her zaman küçük harfe normalize edilmiş uzantı üzerinden
yapılır; yani ``photo.JPG`` da kabul edilir.
"""


def dedupe_by_hash(
    paths_with_hashes: Iterable[tuple[str, str]],
    existing_hash_set: Iterable[str] | None = None,
) -> list[tuple[str, str]]:
    """``(path, hash)`` listesinden tekrar eden ve daha önce
    indekslenmiş kayıtları çıkarır.

    İki ayrı dedupe katmanı uygulanır:

    1. ``existing_hash_set`` içindeki hash'ler atlanır — bu hash daha
       önce başka bir indeksleme turunda işlenmiştir
       (Req 10.6: "dosya hash'i değişmemişse embedding üretimini atlar").
    2. Aynı hash girdi listesinde birden çok kez geçerse yalnızca ilk
       görülen ``(path, hash)`` çifti çıktıda kalır. Bu sayede aynı
       içeriğe sahip iki farklı yol için tek embedding hesaplanır.

    Args:
        paths_with_hashes: ``(absolute_path, content_hash)`` çiftlerinin
            iterable'ı. Hash genelde dosya içeriğinin SHA-256'sıdır,
            fakat bu fonksiyon hash'in formatını yorumlamaz.
        existing_hash_set: Daha önce indekslenmiş hash'lerin kümesi.
            ``None`` ise boş set olarak değerlendirilir.

    Returns:
        Embedding hesaplanması gereken ``(path, hash)`` çiftlerinin
        sırası korunmuş listesi. Yeni hash sayısı her zaman ≤ distinct
        input hash sayısıdır (Property 18, invariant 1).

    Notes:
        Saf, deterministik, yan etkisiz. Aynı girdi için her zaman aynı
        çıktıyı üretir; mevcut hash kümesi tüm girdi hash'lerini
        içeriyorsa boş liste döner (Property 18, invariant 2).
    """
    seen: set[str] = set(existing_hash_set or ())
    result: list[tuple[str, str]] = []
    for path, file_hash in paths_with_hashes:
        if file_hash in seen:
            continue
        seen.add(file_hash)
        result.append((path, file_hash))
    return result


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """İki vektör için cosine similarity. Sıfır-norm güvenli.

    Numpy bağımlılığı eklememek için saf Python'da yazılmıştır; tipik
    NVCLIP boyutu (768 veya 1024) için yeterince hızlıdır ve PBT'de
    deterministiklik garantilenir.
    """
    if len(a) != len(b):
        raise ValueError(
            f"embedding boyutu uyumsuz: {len(a)} vs {len(b)}"
        )
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def knn_search(
    query_embedding: Sequence[float],
    candidates: Iterable[tuple[str, Sequence[float]]],
    top_k: int = 10,
) -> list[tuple[str, float]]:
    """Saf cosine top-k araması.

    Args:
        query_embedding: NVCLIP text embedding'i (Req 10.3).
        candidates: ``(id, embedding)`` çiftleri. ``id`` genelde dosya
            yoludur fakat fonksiyon yorumlamaz; tek koşul karşılaştırılabilir
            (string) olmasıdır.
        top_k: Döndürülecek en üst skor sayısı; ≤ 0 ise boş liste döner.

    Returns:
        ``(id, score)`` listesinin azalan skor sırasında ilk ``top_k``
        elemanı. Eşit skorlu adaylar arasında kararlı bir sıralama
        garanti edilir: önce skor azalan, ardından ``id`` artan.

    Notes:
        Saf, deterministik, yan etkisiz. ``query_embedding`` veya bir
        adayın normu sıfırsa cosine 0 olarak değerlendirilir; sıralamaya
        ``id`` artan kararlılığı uygulanır.
    """
    if top_k <= 0:
        return []
    scored: list[tuple[str, float]] = []
    for cid, emb in candidates:
        score = _cosine_similarity(query_embedding, emb)
        scored.append((cid, score))
    # Stable sort: önce id artan, sonra skor azalan — Python'un sort'u
    # kararlı olduğu için iki aşamalı sıralama eşit skorlarda id artan
    # düzeni korur.
    scored.sort(key=lambda item: item[0])
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:top_k]


def walk_supported(folder: str | os.PathLike[str]) -> Iterator[str]:
    """``folder`` altındaki desteklenen görselleri deterministik
    sırada yield eder (Req 10.2).

    Args:
        folder: Klasör yolu. Yol yoksa veya bir dosyaysa boş iterator
            döner; çağıran taraf erişim hatasını ayrıca raporlamak
            isteyebilir (Req 10.7) fakat bu fonksiyon sessiz kalır.

    Yields:
        Mutlak dosya yolları. Uzantı karşılaştırması küçük harfe
        normalize edilir; gizli dosyalar (``.``) atlanmaz çünkü
        kullanıcı kendi klasörüne mantıklı bir şey koyduğunu varsayar.

    Notes:
        ``os.walk`` kullanır; ``topdown=True`` ve dizin/dosya isimleri
        sıralanır. Sabit bir dosya sisteminde aynı klasör için her
        çağrı aynı sırayı verir — log, cache veya başka yan etki yoktur.
    """
    try:
        # os.walk has no order guarantee across platforms; sort her seviyede.
        for root, dirs, files in os.walk(folder, topdown=True):
            dirs.sort()
            for name in sorted(files):
                ext = os.path.splitext(name)[1].lower()
                if ext in SUPPORTED_EXTENSIONS:
                    yield os.path.join(root, name)
    except OSError:
        # Erişilemeyen klasörler çağıran katmanın sorumluluğunda; pure
        # generator olarak sessiz biter.
        return


def batch_for_embed(
    items: Iterable[Any],
    batch: int = 8,
) -> Iterator[list[Any]]:
    """``items`` iterable'ını ``batch`` boyutlu listelere böler.

    Args:
        items: Herhangi bir iterable (path listesi, hash çiftleri,
            embedding kayıtları, vb.).
        batch: Parça boyutu. ``<= 0`` durumunda ``ValueError``.

    Yields:
        En çok ``batch`` eleman içeren listeler. Son parça daha küçük
        olabilir; girdi boşsa hiçbir şey yield edilmez.

    Notes:
        Saf, deterministik. ``image_search`` config'inden
        ``embed_batch`` (varsayılan 8) ile çağrılması beklenir
        (Req 18.4).
    """
    if batch <= 0:
        raise ValueError(f"batch must be positive, got {batch}")
    chunk: list[Any] = []
    for item in items:
        chunk.append(item)
        if len(chunk) >= batch:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
