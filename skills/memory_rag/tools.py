"""Memory_RAG skill tool implementations.

İçerdiği handler'lar:

- :func:`memory_index_add` — Bir metin parçasını (kaynak, kimlik, metin,
  opsiyonel etiketler) chunk'lara böler, NVIDIA embedding modeli ile
  vektörleştirir ve Vector_Store'a kalıcı olarak yazar. ``background``
  modda çalışır.

- :func:`memory_rag_query` — Doğal dil sorusunu embedding'e çevirir,
  Vector_Store'dan top-k en alakalı chunk'ı alır ve NVIDIA
  ``llama3-chatqa-1.5-70b`` modeli ile Türkçe yanıt üretir. ``background``
  modda çalışır.

- :func:`memory_rag_forget` — Belirtilen kaynak veya kimliğe ait tüm
  vektörleri Vector_Store'dan siler. ``inline`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, dosya boyutunu
   kontrol eder, Privacy_Mode durumunu okur.
2. **Model_Router / NVIDIA API çağrısı** — gerçek HTTP isteği bu katmanda
   yapılır; embedding için 3x exponential backoff uygulanır.
3. **Türkçe yanıt formatlama** — ``format_rag_answer`` ile kullanıcı dostu
   tek paragraf üretilir.

Privacy_Mode aktifken ``conversation`` kaynaklı yeni indekslemeler
``PendingIndexQueue``'ya alınır; Privacy_Mode kapanınca arka planda drain
başlatılır (Req 4.7, 16.8). 10 MB üzeri dosyalar stream okunur ve 1000
chunk üst sınırı uygulanır (Req 4.10).
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

NAMESPACE = "memory_rag"
EMBED_MODEL = "nvidia/nv-embedqa-e5-v5"
CHAT_MODEL = "nvidia/llama3-chatqa-1.5-70b"
NVIDIA_EMBED_ENDPOINT = "https://integrate.api.nvidia.com/v1/embeddings"
NVIDIA_CHAT_ENDPOINT = "https://integrate.api.nvidia.com/v1/chat/completions"

# 10 MB dosya boyutu eşiği (Req 4.10)
FILE_SIZE_THRESHOLD_BYTES = 10 * 1024 * 1024

# Exponential backoff parametreleri (Req 4.8)
MAX_EMBED_RETRIES = 3
BACKOFF_BASE_SEC = 1.0

# ---------------------------------------------------------------------------
# PendingIndexEntry — Privacy_Mode kuyruğu için veri yapısı
# ---------------------------------------------------------------------------


@dataclass
class PendingIndexEntry:
    """Privacy_Mode aktifken kuyruğa alınan indeksleme isteği."""

    source: str
    id: str
    text: str
    tags: list[str]
    enqueued_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Modül düzeyinde PendingIndexQueue ve Privacy_Mode drain thread yönetimi
# ---------------------------------------------------------------------------

from skills.memory_rag._internal import PendingIndexQueue  # noqa: E402

_pending_queue: PendingIndexQueue = PendingIndexQueue()
_drain_lock = threading.Lock()
_drain_thread: threading.Thread | None = None


def _get_pending_queue() -> PendingIndexQueue:
    """Modül düzeyinde PendingIndexQueue örneğini döner."""
    return _pending_queue


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA API anahtarı
# ---------------------------------------------------------------------------


def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: Privacy_Mode erişimi
# ---------------------------------------------------------------------------


def _privacy_is_active() -> bool:
    """Privacy_Mode aktif mi? main.py'de wire edilmemişse False döner."""
    try:
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "is_active"):
            return bool(pm.is_active())
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Yardımcı: Vector_Store erişimi
# ---------------------------------------------------------------------------


def _get_vector_store():
    """Paylaşılan Vector_Store örneğini döner."""
    from memory.vector_store import VectorStore
    from pathlib import Path
    db_path = Path(__file__).resolve().parent.parent.parent / "memory" / "vector_store.db"
    return VectorStore(db_path)


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA embedding çağrısı (3x exponential backoff)
# ---------------------------------------------------------------------------


def _embed_texts_with_backoff(texts: list[str], api_key: str) -> list[list[float]] | None:
    """NVIDIA embedding API'sini çağır; hata durumunda 3x exponential backoff uygula.

    Returns:
        Embedding listesi (her metin için bir float listesi) veya tüm
        denemeler başarısız olursa ``None``.
    """
    import requests as _requests

    last_exc: Exception | None = None
    for attempt in range(MAX_EMBED_RETRIES):
        try:
            response = _requests.post(
                NVIDIA_EMBED_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": EMBED_MODEL,
                    "input": texts,
                    "input_type": "passage",
                    "encoding_format": "float",
                    "truncate": "END",
                },
                timeout=60,
            )
            if response.status_code >= 400:
                detail = response.text.strip()[:300]
                raise RuntimeError(
                    f"NVIDIA embedding API hatası ({response.status_code}): {detail}"
                )
            data = response.json()
            embeddings_data = data.get("data") or []
            # Sort by index to maintain order
            embeddings_data.sort(key=lambda x: x.get("index", 0))
            return [item["embedding"] for item in embeddings_data]
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_EMBED_RETRIES - 1:
                wait = BACKOFF_BASE_SEC * (2 ** attempt)
                log.warning(
                    "memory_index_add: embedding denemesi %d/%d başarısız, "
                    "%.1f sn bekleniyor: %s",
                    attempt + 1,
                    MAX_EMBED_RETRIES,
                    wait,
                    exc,
                )
                time.sleep(wait)
            else:
                log.error(
                    "memory_index_add: tüm %d embedding denemesi başarısız: %s",
                    MAX_EMBED_RETRIES,
                    exc,
                )
    return None


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA chat çağrısı (RAG yanıt üretimi)
# ---------------------------------------------------------------------------


def _call_nvidia_chat(
    messages: list[dict],
    api_key: str,
    model: str = CHAT_MODEL,
    max_tokens: int = 1024,
    temperature: float = 0.2,
    timeout: float = 90.0,
) -> str:
    """NVIDIA chat completion API'sini çağır.

    Fallback zinciri: llama3-chatqa-1.5-70b → meta/llama-3.1-70b-instruct
    → gemini_secondary (models/gemini-2.5-pro).
    """
    import requests as _requests

    models_to_try = [
        ("nvidia", model, api_key),
        ("nvidia", "meta/llama-3.1-70b-instruct", api_key),
    ]

    last_exc: Exception | None = None
    for provider, m, key in models_to_try:
        try:
            response = _requests.post(
                NVIDIA_CHAT_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": m,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=timeout,
            )
            if response.status_code >= 400:
                detail = response.text.strip()[:300]
                raise RuntimeError(
                    f"NVIDIA chat API hatası ({response.status_code}): {detail}"
                )
            data = response.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("NVIDIA chat API boş yanıt döndürdü.")
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
            if text:
                return text
            raise RuntimeError("NVIDIA chat modeli boş metin döndürdü.")
        except Exception as exc:
            last_exc = exc
            log.warning("memory_rag_query: model %r başarısız: %s", m, exc)
            continue

    # Gemini secondary fallback
    try:
        from app_config import get_app_config_value
        gemini_secondary_key = str(
            get_app_config_value("gemini_secondary_api_key", "") or ""
        ).strip()
        if gemini_secondary_key:
            import google.generativeai as genai  # type: ignore[import]
            client = genai.GenerativeModel("models/gemini-2.5-pro")
            # Convert messages to Gemini format
            prompt_parts = []
            for msg in messages:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role == "system":
                    prompt_parts.append(f"[Sistem]: {content}")
                elif role == "user":
                    prompt_parts.append(f"[Kullanıcı]: {content}")
                else:
                    prompt_parts.append(content)
            full_prompt = "\n\n".join(prompt_parts)
            genai.configure(api_key=gemini_secondary_key)
            response = client.generate_content(full_prompt)
            text = response.text.strip() if response.text else ""
            if text:
                return text
    except Exception as exc:
        log.warning("memory_rag_query: Gemini secondary fallback başarısız: %s", exc)

    raise RuntimeError(
        f"Tüm RAG yanıt modelleri başarısız oldu. Son hata: {last_exc}"
    )


# ---------------------------------------------------------------------------
# Yardımcı: Dosyadan metin okuma (stream + 10 MB eşiği)
# ---------------------------------------------------------------------------


def _read_text_from_file(file_path: str) -> str:
    """Dosyadan metin oku; 10 MB üzeri dosyalar stream okunur (Req 4.10)."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"Dosya bulunamadı: {file_path}")
    if not p.is_file():
        raise ValueError(f"Geçerli bir dosya değil: {file_path}")

    file_size = p.stat().st_size
    if file_size > FILE_SIZE_THRESHOLD_BYTES:
        log.info(
            "memory_index_add: dosya boyutu %d bayt (>10 MB), stream okunuyor: %s",
            file_size,
            file_path,
        )
        # Stream okuma: 1 MB'lık parçalar halinde oku
        chunks_text: list[str] = []
        with p.open("r", encoding="utf-8", errors="replace") as f:
            while True:
                chunk = f.read(1024 * 1024)  # 1 MB
                if not chunk:
                    break
                chunks_text.append(chunk)
        return "".join(chunks_text)
    else:
        return p.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Yardımcı: Privacy_Mode kapanınca drain başlat
# ---------------------------------------------------------------------------


def _start_drain_if_needed() -> None:
    """Privacy_Mode kapandığında bekleyen indekslemeleri arka planda drain et."""
    global _drain_thread

    with _drain_lock:
        if _drain_thread is not None and _drain_thread.is_alive():
            return  # Zaten çalışıyor
        queue = _get_pending_queue()
        if not queue:
            return  # Kuyruk boş

        def _drain_worker() -> None:
            log.info(
                "memory_rag: Privacy_Mode kapandı, %d bekleyen indeksleme drain ediliyor.",
                len(queue),
            )
            api_key = _nvidia_api_key()
            if not api_key:
                log.warning("memory_rag drain: NVIDIA API anahtarı eksik, drain iptal.")
                return

            from skills.memory_rag._internal import chunk_text, batch_for_embed
            from memory.vector_store import VectorRow

            vs = _get_vector_store()
            drained = 0
            for entry in queue.drain():
                try:
                    chunks = chunk_text(entry.text)
                    if not chunks:
                        continue
                    for batch in batch_for_embed(chunks):
                        embeddings = _embed_texts_with_backoff(batch, api_key)
                        if embeddings is None:
                            log.warning(
                                "memory_rag drain: embedding başarısız, kayıt atlandı: %s",
                                entry.id,
                            )
                            continue
                        rows = []
                        for i, (chunk_text_val, emb) in enumerate(
                            zip(batch, embeddings)
                        ):
                            chunk_id = f"{entry.id}__chunk_{drained}_{i}"
                            rows.append(
                                VectorRow(
                                    namespace=NAMESPACE,
                                    id=chunk_id,
                                    source=entry.source,
                                    model=EMBED_MODEL,
                                    embedding=emb,
                                    text=chunk_text_val,
                                    metadata={
                                        "original_id": entry.id,
                                        "tags": entry.tags,
                                        "chunk_index": i,
                                    },
                                )
                            )
                        vs.upsert_many(rows)
                    drained += 1
                except Exception as exc:
                    log.error(
                        "memory_rag drain: kayıt işlenirken hata (id=%s): %s",
                        entry.id,
                        exc,
                    )
            log.info("memory_rag drain: %d kayıt işlendi.", drained)

        _drain_thread = threading.Thread(
            target=_drain_worker, daemon=True, name="memory_rag_drain"
        )
        _drain_thread.start()


def _on_privacy_mode_change(active: bool) -> None:
    """Privacy_Mode değişikliğini dinle; kapanınca drain başlat."""
    if not active:
        _start_drain_if_needed()


def _register_privacy_listener() -> None:
    """Privacy_Mode listener'ını kaydet (main.py'de wire edilmişse)."""
    try:
        import main as _main  # type: ignore[import]
        pm = getattr(_main, "privacy", None)
        if pm is not None and hasattr(pm, "on_change"):
            pm.on_change(_on_privacy_mode_change)
    except Exception:
        pass


# Modül yüklendiğinde listener'ı kaydet
try:
    _register_privacy_listener()
except Exception:
    pass


# ---------------------------------------------------------------------------
# Handler: memory_index_add
# ---------------------------------------------------------------------------


def memory_index_add(
    source: str,
    id: str,
    text: str,
    tags: list[str] | None = None,
    *,
    model_router: Any = None,
    privacy_mode: Any = None,
) -> str:
    """Bir metin parçasını chunk'lara böl, vektörleştir ve Vector_Store'a yaz.

    Args:
        source: Kaynak türü (``conversation``, ``note``, ``file``).
        id: Benzersiz kimlik; aynı kimlik için upsert yapılır.
        text: İndekslenecek metin veya dosya yolu (``source="file"`` ise).
        tags: Opsiyonel etiket listesi.
        model_router: Tool_Runtime tarafından enjekte edilen Model_Router
            örneği (opsiyonel; doğrudan NVIDIA API kullanılır).
        privacy_mode: Tool_Runtime tarafından enjekte edilen PrivacyMode
            örneği (opsiyonel; modül düzeyinde fallback kullanılır).

    Returns:
        Türkçe tek paragraflık sonuç mesajı.
    """
    from skills.memory_rag._internal import chunk_text, batch_for_embed
    from memory.vector_store import VectorRow

    tags = tags or []
    source = str(source or "").strip()
    id = str(id or "").strip()

    if not source:
        return "Kaynak türü belirtilmedi."
    if not id:
        return "Kimlik (id) belirtilmedi."
    if not text:
        return "İndekslenecek metin boş."

    # Privacy_Mode kontrolü: conversation kaynağı için kuyruğa al
    privacy_active = (
        privacy_mode.is_active()
        if privacy_mode is not None and hasattr(privacy_mode, "is_active")
        else _privacy_is_active()
    )

    if privacy_active and source == "conversation":
        entry = PendingIndexEntry(
            source=source,
            id=id,
            text=text,
            tags=tags,
        )
        _get_pending_queue().enqueue(entry)
        log.info(
            "memory_index_add: Privacy_Mode aktif, kayıt kuyruğa alındı (id=%s).", id
        )
        return (
            "Gizlilik modu aktif olduğu için bu konuşma kaydı şu an "
            "indekslenemiyor; gizlilik modu kapanınca otomatik olarak "
            "işlenecek."
        )

    # Dosya kaynağı ise metni dosyadan oku
    actual_text = text
    if source == "file":
        try:
            actual_text = _read_text_from_file(text)
        except (FileNotFoundError, ValueError) as exc:
            return f"Dosya okunamadı: {exc}"
        except Exception as exc:
            log.error("memory_index_add: dosya okuma hatası: %s", exc)
            return f"Dosya okunurken beklenmeyen hata oluştu: {exc}"

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için hafıza indeksleme "
            "özelliği kullanılamıyor."
        )

    # Chunk'lara böl (1000 chunk üst sınırı Req 4.10)
    chunks = chunk_text(actual_text)
    if not chunks:
        return f"'{id}' kimlikli metin boş veya yalnızca boşluk içeriyor; indekslenmedi."

    total_chunks = len(chunks)
    log.info(
        "memory_index_add: '%s' için %d chunk üretildi (kaynak=%s).",
        id,
        total_chunks,
        source,
    )

    vs = _get_vector_store()
    upserted = 0
    failed_batches = 0

    for batch_idx, batch in enumerate(batch_for_embed(chunks)):
        embeddings = _embed_texts_with_backoff(batch, api_key)
        if embeddings is None:
            failed_batches += 1
            log.error(
                "memory_index_add: batch %d embedding başarısız (id=%s).",
                batch_idx,
                id,
            )
            continue

        rows = []
        for i, (chunk_text_val, emb) in enumerate(zip(batch, embeddings)):
            global_chunk_idx = batch_idx * 16 + i
            chunk_id = f"{id}__chunk_{global_chunk_idx}"
            rows.append(
                VectorRow(
                    namespace=NAMESPACE,
                    id=chunk_id,
                    source=source,
                    model=EMBED_MODEL,
                    embedding=emb,
                    text=chunk_text_val,
                    metadata={
                        "original_id": id,
                        "tags": tags,
                        "chunk_index": global_chunk_idx,
                    },
                )
            )
        try:
            vs.upsert_many(rows)
            upserted += len(rows)
        except Exception as exc:
            log.error(
                "memory_index_add: Vector_Store upsert hatası (id=%s): %s", id, exc
            )
            failed_batches += 1

    if failed_batches > 0 and upserted == 0:
        return (
            f"'{id}' kimlikli kayıt indekslenemedi: embedding servisi yanıt vermedi. "
            "Lütfen NVIDIA API anahtarınızı ve internet bağlantınızı kontrol edin."
        )

    if failed_batches > 0:
        return (
            f"'{id}' kimlikli kayıt kısmen indekslendi: {upserted} chunk başarıyla "
            f"eklendi, {failed_batches} batch başarısız oldu."
        )

    return (
        f"'{id}' kimlikli kayıt başarıyla indekslendi: {upserted} chunk "
        f"Vector_Store'a eklendi (kaynak: {source})."
    )


memory_index_add.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "memory_index_add",
        "description": (
            "Bir metin parcasini, notu veya dosyayi anlamsal hafizaya ekler. "
            "Metin chunk'lara bolunur, NVIDIA embedding modeli ile vektorlestirilir "
            "ve kalici olarak Vector_Store'a yazilir. Kullanici 'bunu hatirla', "
            "'su notu kaydet', 'bu dosyayi indeksle' gibi komutlar verdiginde kullan. "
            "Arka planda calisir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "source": {
                    "type": "STRING",
                    "description": (
                        "Kaynak turu: 'conversation' (konusma logu), "
                        "'note' (kullanici notu), 'file' (dosya yolu)."
                    ),
                },
                "id": {
                    "type": "STRING",
                    "description": (
                        "Kayit icin benzersiz kimlik. Ayni kimlik tekrar "
                        "gonderilirse mevcut kayit guncellenir (upsert)."
                    ),
                },
                "text": {
                    "type": "STRING",
                    "description": (
                        "Indekslenecek metin icerigi veya 'file' kaynagi "
                        "icin dosya yolu."
                    ),
                },
                "tags": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Opsiyonel etiket listesi (ornek: ['proje-x', 'toplanti']).",
                },
            },
            "required": ["source", "id", "text"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/nv-embedqa-e5-v5",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Handler: memory_rag_query
# ---------------------------------------------------------------------------


def memory_rag_query(
    question: str,
    top_k: int = 5,
    *,
    model_router: Any = None,
    privacy_mode: Any = None,
) -> str:
    """Doğal dil sorusunu Vector_Store'da ara ve NVIDIA ile Türkçe yanıt üret.

    Args:
        question: Kullanıcının doğal dil sorusu.
        top_k: Alınacak en alakalı chunk sayısı (varsayılan 5).
        model_router: Tool_Runtime tarafından enjekte edilen Model_Router
            örneği (opsiyonel).
        privacy_mode: Tool_Runtime tarafından enjekte edilen PrivacyMode
            örneği (opsiyonel).

    Returns:
        Türkçe tek paragraflık yanıt; boş store için özel mesaj.
    """
    from skills.memory_rag._internal import format_rag_answer

    question = str(question or "").strip()
    if not question:
        return "Soru boş bırakıldı."

    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        top_k = 5

    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için hafıza sorgulama "
            "özelliği kullanılamıyor."
        )

    # Soruyu embedding'e çevir
    query_embeddings = _embed_texts_with_backoff([question], api_key)
    if query_embeddings is None:
        return (
            "Sorunuz işlenirken embedding servisi yanıt vermedi. "
            "Lütfen NVIDIA API anahtarınızı ve internet bağlantınızı kontrol edin."
        )

    query_embedding = query_embeddings[0]

    # Vector_Store'dan top-k ara
    vs = _get_vector_store()
    count = vs.count(NAMESPACE)

    if count == 0:
        return "Bilgilerinde eşleşen kayıt bulamadım."

    hits = vs.knn_search(NAMESPACE, query_embedding, top_k=top_k)

    if not hits:
        return "Bilgilerinde eşleşen kayıt bulamadım."

    # RAG prompt'u oluştur
    context_parts: list[str] = []
    sources: list[str] = []

    for i, hit in enumerate(hits, 1):
        chunk_text_val = hit.text or ""
        source_label = hit.source or "bilinmeyen"
        original_id = hit.metadata.get("original_id", hit.id) if hit.metadata else hit.id
        context_parts.append(
            f"[Kaynak {i}: {source_label} / {original_id}]\n{chunk_text_val}"
        )
        source_key = f"{source_label}/{original_id}"
        if source_key not in sources:
            sources.append(source_key)

    context_text = "\n\n".join(context_parts)

    system_prompt = (
        "Sen JARVIS adlı bir yapay zeka asistanısın. "
        "Aşağıdaki bağlam bilgilerini kullanarak kullanıcının sorusunu "
        "Türkçe tek paragrafta yanıtla. "
        "Yalnızca verilen bağlam bilgilerine dayan; bağlamda olmayan "
        "bilgileri uydurmadan 'Bu konuda bilgim yok' de."
    )

    user_message = (
        f"Bağlam bilgileri:\n{context_text}\n\n"
        f"Soru: {question}"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        raw_answer = _call_nvidia_chat(messages, api_key)
    except Exception as exc:
        log.error("memory_rag_query: yanıt üretimi başarısız: %s", exc)
        return (
            "Sorunuz için ilgili bilgiler bulundu ancak yanıt üretilirken "
            f"bir hata oluştu: {exc}"
        )

    return format_rag_answer(raw_answer, sources)


memory_rag_query.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "memory_rag_query",
        "description": (
            "Anlamsal hafizada dogal dil sorgusu yapar. Kullanici 'gecen hafta "
            "X projesi hakkinda ne konustuk', 'su konuda ne biliyorsun', "
            "'hatirliyor musun' gibi sorular sorduğunda kullan. "
            "Vector_Store'dan en alakali bilgileri bulur ve NVIDIA modeli ile "
            "Turkce yanit uretir. Arka planda calisir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {
                    "type": "STRING",
                    "description": "Kullanicinin dogal dil sorusu.",
                },
                "top_k": {
                    "type": "INTEGER",
                    "description": (
                        "Alinacak en alakali chunk sayisi (varsayilan 5). "
                        "Daha kapsamli arama icin arttirilabilir."
                    ),
                },
            },
            "required": ["question"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/llama3-chatqa-1.5-70b",
        "fallback": [
            {"provider": "nvidia", "model": "meta/llama-3.1-70b-instruct"},
            {"provider": "gemini_secondary", "model": "models/gemini-2.5-pro"},
        ],
    },
}


# ---------------------------------------------------------------------------
# Handler: memory_rag_forget
# ---------------------------------------------------------------------------


def memory_rag_forget(
    source: str | None = None,
    id: str | None = None,
    *,
    model_router: Any = None,
    privacy_mode: Any = None,
) -> str:
    """Belirtilen kaynak veya kimliğe ait vektörleri Vector_Store'dan sil.

    Args:
        source: Silinecek kaynak türü (``conversation``, ``note``, ``file``).
            ``id`` ile birlikte veya tek başına kullanılabilir.
        id: Silinecek kaydın kimliği. ``source`` ile birlikte veya tek
            başına kullanılabilir.
        model_router: Tool_Runtime tarafından enjekte edilen Model_Router
            örneği (opsiyonel; bu tool için kullanılmaz).
        privacy_mode: Tool_Runtime tarafından enjekte edilen PrivacyMode
            örneği (opsiyonel; bu tool için kullanılmaz).

    Returns:
        Türkçe tek paragraflık sonuç mesajı.
    """
    source_val = str(source or "").strip() or None
    id_val = str(id or "").strip() or None

    if source_val is None and id_val is None:
        return (
            "Silme işlemi için en az bir kriter belirtilmeli: "
            "'source' (kaynak türü) veya 'id' (kimlik)."
        )

    vs = _get_vector_store()

    try:
        deleted = vs.forget(NAMESPACE, source=source_val, id=id_val)
    except ValueError as exc:
        return f"Silme işlemi başarısız: {exc}"
    except Exception as exc:
        log.error("memory_rag_forget: Vector_Store hatası: %s", exc)
        return f"Silme işlemi sırasında beklenmeyen hata oluştu: {exc}"

    if deleted == 0:
        criteria_parts = []
        if source_val:
            criteria_parts.append(f"kaynak='{source_val}'")
        if id_val:
            criteria_parts.append(f"id='{id_val}'")
        criteria = " ve ".join(criteria_parts)
        return f"Hafızada {criteria} kriterine uyan kayıt bulunamadı."

    criteria_parts = []
    if source_val:
        criteria_parts.append(f"kaynak='{source_val}'")
    if id_val:
        criteria_parts.append(f"id='{id_val}'")
    criteria = " ve ".join(criteria_parts)
    return (
        f"Hafızadan {deleted} kayıt silindi ({criteria})."
    )


memory_rag_forget.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "memory_rag_forget",
        "description": (
            "Anlamsal hafizadan belirli kayitlari siler. Kullanici 'bunu unut', "
            "'su kaynaktan gelen bilgileri sil', 'hafizandan kaldir' gibi "
            "komutlar verdiginde kullan. Inline modda calisir (aninda sonuc verir)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "source": {
                    "type": "STRING",
                    "description": (
                        "Silinecek kaynak turu: 'conversation', 'note', 'file'. "
                        "Bu kaynaktan gelen tum kayitlar silinir."
                    ),
                },
                "id": {
                    "type": "STRING",
                    "description": (
                        "Silinecek kaydın benzersiz kimligi. "
                        "Yalnizca bu kimlige ait chunk'lar silinir."
                    ),
                },
            },
            "required": [],
        },
    },
    "execution_mode": "inline",
    "route": None,
}


__all__ = [
    "memory_index_add",
    "memory_rag_query",
    "memory_rag_forget",
    "PendingIndexEntry",
]
