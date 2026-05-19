"""
Vector_Store — SQLite + numpy tabanlı kalıcı vektör depo.

Memory_RAG_Skill ve Image_Search_Skill tarafından paylaşılan, üçüncü taraf
FAISS/Chroma bağımlılığı olmadan tek bir SQLite dosyasında namespace bazlı
çoklu vektör koleksiyonu tutan kalıcı katman.

Tasarım kararları (bkz. design.md → Vector_Store):

- Tek tablo (``vectors``); ``namespace`` alanı koleksiyonları (memory_rag,
  image_search vb.) ayırır. ``PRIMARY KEY (namespace, id)`` aynı kimliğin
  yeniden yazılmasına (upsert) izin verir.
- Embedding ``float32`` numpy array olarak ``.tobytes()`` ile BLOB'a yazılır;
  ``dim`` alanı ile birlikte saklanır ve okunduğunda ``np.frombuffer`` ile
  geri çözülür.
- ``knn_search`` tüm aynı boyutlu vektörleri belleğe alıp numpy ile cosine
  similarity hesaplar. 50K vektör altı yerel kullanıcı veri seti için yeterli
  hızdır; gelecekte FAISS'e migrate edilebilir.
- Veri tabanı yolu enjekte edilebilir (test için ``tmp_path``).
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# Şema
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vectors (
    namespace   TEXT NOT NULL,
    id          TEXT NOT NULL,
    source      TEXT NOT NULL,
    model       TEXT NOT NULL,
    embedding   BLOB NOT NULL,
    dim         INTEGER NOT NULL,
    text        TEXT,
    file_path   TEXT,
    file_hash   TEXT,
    created_at  REAL NOT NULL,
    metadata    TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (namespace, id)
);
CREATE INDEX IF NOT EXISTS idx_vectors_source ON vectors(namespace, source);
CREATE INDEX IF NOT EXISTS idx_vectors_hash   ON vectors(namespace, file_hash);
"""


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VectorRow:
    """``upsert_many`` için tek satırı temsil eder.

    ``embedding`` float32'e dönüştürülerek BLOB olarak yazılır; ``metadata``
    JSON-serileştirilebilir bir dict olmalıdır. ``created_at`` verilmezse
    çağrı anındaki ``time.time()`` kullanılır.
    """

    namespace: str
    id: str
    source: str
    model: str
    embedding: Sequence[float]
    text: str | None = None
    file_path: str | None = None
    file_hash: str | None = None
    metadata: dict | None = None
    created_at: float | None = None


@dataclass(frozen=True)
class SearchHit:
    """``knn_search`` sonucu."""

    id: str
    score: float
    source: str
    model: str
    text: str | None
    file_path: str | None
    file_hash: str | None
    metadata: dict


# ---------------------------------------------------------------------------
# Yardımcılar (saf)
# ---------------------------------------------------------------------------


def _embedding_to_blob(vec: Sequence[float]) -> tuple[bytes, int]:
    """Sequence'ı float32 numpy array'e çevirip ``(bytes, dim)`` döner."""
    arr = np.asarray(vec, dtype=np.float32)
    if arr.ndim != 1:
        raise ValueError("embedding 1 boyutlu olmalı")
    if arr.size == 0:
        raise ValueError("embedding boş olamaz")
    return arr.tobytes(), int(arr.shape[0])


def _blob_to_embedding(blob: bytes, dim: int) -> np.ndarray:
    """BLOB'tan float32 numpy array geri çözer (kopya alır)."""
    return np.frombuffer(blob, dtype=np.float32, count=dim).copy()


def _cosine_scores(query: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    """Query (D,) ile matrix (N, D) arasında cosine similarity döner.

    Sıfır normlu vektörler için skor 0 atanır (NaN yaymaz).
    """
    q_norm = float(np.linalg.norm(query))
    m_norm = np.linalg.norm(matrix, axis=1)
    denom = q_norm * m_norm
    raw = matrix @ query
    safe_denom = np.where(denom == 0.0, 1.0, denom)
    scores = np.where(denom == 0.0, 0.0, raw / safe_denom)
    return scores.astype(np.float32, copy=False)


# ---------------------------------------------------------------------------
# VectorStore
# ---------------------------------------------------------------------------


class VectorStore:
    """SQLite + numpy tabanlı kalıcı vektör depo.

    Her örnek tek bir SQLite dosyası üzerinde çalışır. ``db_path`` test'lerde
    ``tmp_path / "vector_store.db"`` ile enjekte edilebilir; üst klasör
    yoksa otomatik oluşturulur.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA_SQL)
            conn.commit()

    # -- bağlantı yönetimi ---------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Her işlem için yeni bir SQLite bağlantısı açar.

        Kısa ömürlü bağlantı, background tool'ların farklı thread'lerden
        güvenle çağırabilmesini sağlar (SQLite bağlantıları default'ta
        thread-bound'dur).
        """
        conn = sqlite3.connect(str(self._path))
        try:
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    # -- yazma --------------------------------------------------------------

    def upsert_many(self, rows: Iterable[VectorRow | dict]) -> int:
        """Verilen satırları tabloya yazar; ``(namespace, id)`` çakışmasında
        var olan satırı günceller (upsert).

        ``rows`` boş ise 0 döner; aksi halde işlenmiş satır sayısını verir.
        """
        records: list[tuple[Any, ...]] = []
        now = time.time()
        for row in rows:
            if isinstance(row, dict):
                row = VectorRow(**row)
            blob, dim = _embedding_to_blob(row.embedding)
            metadata_text = json.dumps(row.metadata or {}, ensure_ascii=False)
            created_at = row.created_at if row.created_at is not None else now
            records.append(
                (
                    row.namespace,
                    row.id,
                    row.source,
                    row.model,
                    blob,
                    dim,
                    row.text,
                    row.file_path,
                    row.file_hash,
                    float(created_at),
                    metadata_text,
                )
            )
        if not records:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO vectors
                    (namespace, id, source, model, embedding, dim,
                     text, file_path, file_hash, created_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(namespace, id) DO UPDATE SET
                    source     = excluded.source,
                    model      = excluded.model,
                    embedding  = excluded.embedding,
                    dim        = excluded.dim,
                    text       = excluded.text,
                    file_path  = excluded.file_path,
                    file_hash  = excluded.file_hash,
                    created_at = excluded.created_at,
                    metadata   = excluded.metadata
                """,
                records,
            )
            conn.commit()
        return len(records)

    # -- silme --------------------------------------------------------------

    def forget(
        self,
        namespace: str,
        *,
        source: str | None = None,
        id: str | None = None,
    ) -> int:
        """``namespace`` içinde ``source`` veya ``id`` kriterine uyan tüm
        satırları siler. Her ikisi de verilmezse ``ValueError`` fırlatılır
        (toplu wipe için ayrı bir API gerekirse explicit eklenir).

        Silinen satır sayısını döner. Eşleşme yoksa 0 döner (idempotent).
        """
        if source is None and id is None:
            raise ValueError(
                "forget() çağrısı en az 'source' veya 'id' parametresi ister"
            )
        clauses = ["namespace = ?"]
        params: list[Any] = [namespace]
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        if id is not None:
            clauses.append("id = ?")
            params.append(id)
        sql = f"DELETE FROM vectors WHERE {' AND '.join(clauses)}"
        with self._connect() as conn:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return int(cur.rowcount)

    # -- okuma --------------------------------------------------------------

    def knn_search(
        self,
        namespace: str,
        query_embedding: Sequence[float],
        top_k: int,
    ) -> list[SearchHit]:
        """Verilen sorgu embedding'i için cosine similarity en yakın
        ``top_k`` satırı **azalan** skorla döner.

        Yalnızca aynı ``dim``'e sahip satırlar değerlendirilir; namespace
        içinde tutarsız boyut varsa karışmaz.
        """
        if top_k <= 0:
            return []
        query = np.asarray(query_embedding, dtype=np.float32)
        if query.ndim != 1 or query.size == 0:
            raise ValueError("query_embedding boş olmayan 1-D vektör olmalı")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, source, model, embedding, dim, text, file_path,
                       file_hash, metadata
                  FROM vectors
                 WHERE namespace = ? AND dim = ?
                """,
                (namespace, int(query.shape[0])),
            ).fetchall()
        if not rows:
            return []
        matrix = np.stack(
            [_blob_to_embedding(r["embedding"], int(r["dim"])) for r in rows]
        )
        scores = _cosine_scores(query, matrix)
        # top_k satır sayısından büyükse, mevcut satır sayısı kadar dön
        k = min(top_k, len(rows))
        order = np.argsort(-scores, kind="stable")[:k]
        hits: list[SearchHit] = []
        for idx in order:
            r = rows[int(idx)]
            try:
                metadata = json.loads(r["metadata"]) if r["metadata"] else {}
            except json.JSONDecodeError:
                metadata = {}
            hits.append(
                SearchHit(
                    id=r["id"],
                    score=float(scores[int(idx)]),
                    source=r["source"],
                    model=r["model"],
                    text=r["text"],
                    file_path=r["file_path"],
                    file_hash=r["file_hash"],
                    metadata=metadata,
                )
            )
        return hits

    def count(self, namespace: str) -> int:
        """Verilen ``namespace`` içindeki kayıt sayısını döner."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM vectors WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            return int(row["n"]) if row is not None else 0


__all__ = ["VectorStore", "VectorRow", "SearchHit"]
