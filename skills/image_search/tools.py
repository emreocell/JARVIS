"""Image_Search skill tool implementations.

İçerdiği handler'lar:

- :func:`image_index_build` — Verilen klasördeki desteklenen görselleri
  (`.jpg`, `.jpeg`, `.png`, `.webp`) ``nvidia/nvclip`` modeli ile
  embedding'e çevirir ve ``Vector_Store`` içindeki ``image_search``
  namespace'ine yazar. Hash tabanlı dedupe ile daha önce indekslenmiş
  ve değişmemiş görseller atlanır. 5000 üzerinde görsel içeren
  klasörlerde her 500 görselde bir Türkçe ilerleme duyurusu yapılır.
  Erişilemeyen dosyalar atlanır; sonunda atlanan sayı raporlanır.
  ``background`` modda çalışır.

- :func:`image_search` — Doğal dil sorgusu için NVCLIP text embedding'i
  üretir ve ``Vector_Store``'da top-k (varsayılan k=10) en yakın
  görselin tam yollarını skorlarıyla birlikte döner.
  ``background`` modda çalışır.

Üç katmanlı tasarım (design.md § Skill paketlerinin iç tasarımı):

1. **Saf girdi doğrulama** — argümanları normalize eder, klasör/dosya
   yolunu kontrol eder, Privacy_Mode kontrolü yapar.
2. **NVIDIA NIM çağrısı** — ``nvidia/nvclip`` modeli ile image/text
   embedding üretir.
3. **Türkçe yanıt formatlama** — sonuçları kullanıcı dostu paragrafa
   çevirir.

Privacy_Mode aktifken yeni indeksleme durdurulur; mevcut indekste
arama açıktır (Req 10.8). Erişilemeyen dosyalar sessizce atlanır ve
sonunda atlanan sayı raporlanır (Req 10.7).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sabitler
# ---------------------------------------------------------------------------

NVCLIP_MODEL = "nvidia/nvclip"
NVIDIA_EMBED_ENDPOINT = "https://integrate.api.nvidia.com/v1/embeddings"
IMAGE_SEARCH_NAMESPACE = "image_search"
IMAGE_SOURCE = "image"
PROGRESS_INTERVAL = 500   # Her 500 görselde bir ilerleme duyurusu
LARGE_FOLDER_THRESHOLD = 5000  # Bu eşiğin üzerinde ilerleme duyurusu yapılır
DEFAULT_TOP_K = 10
DEFAULT_EMBED_BATCH = 8


# ---------------------------------------------------------------------------
# Yardımcı: NVIDIA API anahtarı
# ---------------------------------------------------------------------------

def _nvidia_api_key() -> str:
    from app_config import get_app_config_value
    return str(get_app_config_value("nvidia_api_key", "") or "").strip()


# ---------------------------------------------------------------------------
# Yardımcı: embed_batch config değeri
# ---------------------------------------------------------------------------

def _embed_batch() -> int:
    from app_config import get_app_config_value
    cfg = get_app_config_value("image_search", {}) or {}
    try:
        return int(cfg.get("embed_batch", DEFAULT_EMBED_BATCH))
    except (TypeError, ValueError):
        return DEFAULT_EMBED_BATCH


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
    from pathlib import Path as _Path
    db_path = _Path(__file__).resolve().parent.parent.parent / "memory" / "vector_store.db"
    return VectorStore(db_path)


# ---------------------------------------------------------------------------
# Yardımcı: Dosya hash'i
# ---------------------------------------------------------------------------

def _file_sha256(path: str) -> str | None:
    """Dosyanın SHA-256 hash'ini döner; erişilemezse None."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Yardımcı: Mevcut indekslenmiş hash'leri al
# ---------------------------------------------------------------------------

def _get_indexed_hashes(vector_store) -> set[str]:
    """Vector_Store'daki image_search namespace'indeki tüm hash'leri döner."""
    try:
        with vector_store._connect() as conn:
            rows = conn.execute(
                "SELECT file_hash FROM vectors WHERE namespace = ? AND file_hash IS NOT NULL",
                (IMAGE_SEARCH_NAMESPACE,),
            ).fetchall()
        return {row["file_hash"] for row in rows if row["file_hash"]}
    except Exception as exc:
        log.warning("_get_indexed_hashes: hash'ler alınamadı: %s", exc)
        return set()


# ---------------------------------------------------------------------------
# Yardımcı: NVCLIP image embedding
# ---------------------------------------------------------------------------

def _embed_images_nvclip(
    image_paths: list[str],
    api_key: str,
) -> list[list[float]] | None:
    """Verilen görsel yolları için NVCLIP image embedding'leri üretir.

    Görseller base64 olarak encode edilip NVIDIA NIM endpoint'ine gönderilir.
    Başarısızlıkta None döner.
    """
    import requests as _requests

    if not image_paths:
        return []

    inputs = []
    for path in image_paths:
        try:
            with open(path, "rb") as f:
                img_bytes = f.read()
            ext = os.path.splitext(path)[1].lower().lstrip(".")
            if ext == "jpg":
                ext = "jpeg"
            b64 = base64.b64encode(img_bytes).decode("ascii")
            data_url = f"data:image/{ext};base64,{b64}"
            inputs.append(data_url)
        except OSError as exc:
            log.warning("_embed_images_nvclip: dosya okunamadı %s: %s", path, exc)
            inputs.append(None)

    # None olan girişleri filtrele; indeks eşlemesini koru
    valid_inputs = [inp for inp in inputs if inp is not None]
    if not valid_inputs:
        return None

    try:
        payload: dict[str, Any] = {
            "model": NVCLIP_MODEL,
            "input": valid_inputs,
            "input_type": "image",
            "encoding_format": "float",
        }
        response = _requests.post(
            NVIDIA_EMBED_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=120,
        )
        if response.status_code >= 400:
            detail = response.text.strip()[:400]
            log.error(
                "_embed_images_nvclip: NVIDIA API hatası (%d): %s",
                response.status_code,
                detail,
            )
            return None

        data = response.json()
        embeddings_data = data.get("data") or []
        # Sıralı embedding listesi döner
        result = [item["embedding"] for item in sorted(embeddings_data, key=lambda x: x.get("index", 0))]
        return result

    except Exception as exc:
        log.error("_embed_images_nvclip: istek başarısız: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Yardımcı: NVCLIP text embedding
# ---------------------------------------------------------------------------

def _embed_text_nvclip(
    text: str,
    api_key: str,
) -> list[float] | None:
    """Verilen metin için NVCLIP text embedding üretir."""
    import requests as _requests

    try:
        payload: dict[str, Any] = {
            "model": NVCLIP_MODEL,
            "input": [text],
            "input_type": "query",
            "encoding_format": "float",
        }
        response = _requests.post(
            NVIDIA_EMBED_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=60,
        )
        if response.status_code >= 400:
            detail = response.text.strip()[:400]
            log.error(
                "_embed_text_nvclip: NVIDIA API hatası (%d): %s",
                response.status_code,
                detail,
            )
            return None

        data = response.json()
        embeddings_data = data.get("data") or []
        if not embeddings_data:
            return None
        return embeddings_data[0]["embedding"]

    except Exception as exc:
        log.error("_embed_text_nvclip: istek başarısız: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Yardımcı: Result_Announcer ilerleme duyurusu
# ---------------------------------------------------------------------------

def _announce_progress(message: str) -> None:
    """Result_Announcer üzerinden ilerleme duyurusu yapar."""
    try:
        import main as _main  # type: ignore[import]
        announcer = getattr(_main, "result_announcer", None)
        if announcer is not None and hasattr(announcer, "announce"):
            announcer.announce(message)
            return
    except Exception:
        pass
    # Fallback: sadece log'a yaz
    log.info("İlerleme: %s", message)


# ---------------------------------------------------------------------------
# Ana handler: image_index_build
# ---------------------------------------------------------------------------

def image_index_build(folder: str, force_reindex: bool = False) -> str:
    """Verilen klasördeki görselleri NVCLIP ile indeksler.

    Klasördeki tüm desteklenen görseller (`.jpg`, `.jpeg`, `.png`, `.webp`)
    için NVCLIP image embedding üretir ve ``Vector_Store`` içindeki
    ``image_search`` namespace'ine yazar.

    Hash tabanlı dedupe: daha önce indekslenmiş ve değişmemiş görseller
    atlanır (Req 10.6). ``force_reindex=True`` ile tüm görseller yeniden
    işlenir.

    5000 üzerinde görsel içeren klasörlerde her 500 görselde bir Türkçe
    ilerleme duyurusu yapılır (Req 10.5).

    Erişilemeyen dosyalar atlanır; sonunda atlanan sayı raporlanır (Req 10.7).

    Privacy_Mode aktifken yeni indeksleme durdurulur (Req 10.8).
    """
    # --- Privacy_Mode kontrolü ---
    if _privacy_is_active():
        return (
            "Gizlilik modu aktif olduğu için yeni görsel indeksleme "
            "durduruldu. Gizlilik modunu kapatarak tekrar deneyin."
        )

    # --- API anahtarı kontrolü ---
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için görsel indeksleme "
            "yapılamıyor."
        )

    # --- Klasör kontrolü ---
    folder_path = Path(folder)
    if not folder_path.exists() or not folder_path.is_dir():
        return (
            f"Klasör bulunamadı veya erişilemiyor: {folder}. "
            "Lütfen geçerli bir klasör yolu girin."
        )

    # --- Saf yardımcıları kullan ---
    from skills.image_search._internal import walk_supported, dedupe_by_hash, batch_for_embed
    from memory.vector_store import VectorRow

    vector_store = _get_vector_store()
    embed_batch_size = _embed_batch()

    # Tüm desteklenen görselleri topla
    all_paths = list(walk_supported(folder_path))
    total_found = len(all_paths)

    if total_found == 0:
        return (
            f"'{folder}' klasöründe desteklenen görsel bulunamadı "
            "(.jpg, .jpeg, .png, .webp)."
        )

    # Mevcut indekslenmiş hash'leri al
    existing_hashes: set[str] = set() if force_reindex else _get_indexed_hashes(vector_store)

    # Hash hesapla ve dedupe uygula
    skipped_access = 0
    paths_with_hashes: list[tuple[str, str]] = []

    for path in all_paths:
        file_hash = _file_sha256(path)
        if file_hash is None:
            skipped_access += 1
            log.warning("image_index_build: dosyaya erişilemiyor, atlanıyor: %s", path)
            continue
        paths_with_hashes.append((path, file_hash))

    # Dedupe: daha önce indekslenmiş hash'leri çıkar
    new_paths_with_hashes = dedupe_by_hash(paths_with_hashes, existing_hashes)
    skipped_dedupe = len(paths_with_hashes) - len(new_paths_with_hashes)

    if not new_paths_with_hashes:
        msg = (
            f"'{folder}' klasöründeki tüm {total_found} görsel zaten "
            f"güncel indekste mevcut."
        )
        if skipped_access > 0:
            msg += f" ({skipped_access} dosyaya erişilemedi.)"
        return msg

    # Büyük klasör için ilerleme duyurusu yapılacak mı?
    announce_progress = total_found > LARGE_FOLDER_THRESHOLD

    # Batch embed ve Vector_Store upsert
    indexed_count = 0
    failed_embed = 0
    new_paths = [p for p, _ in new_paths_with_hashes]
    hash_map = {p: h for p, h in new_paths_with_hashes}

    for batch_paths in batch_for_embed(new_paths, embed_batch_size):
        embeddings = _embed_images_nvclip(batch_paths, api_key)

        if embeddings is None or len(embeddings) != len(batch_paths):
            # Batch başarısız: bu batch'teki görselleri atla
            failed_embed += len(batch_paths)
            log.warning(
                "image_index_build: batch embed başarısız, %d görsel atlandı",
                len(batch_paths),
            )
            continue

        # Vector_Store'a yaz
        rows = []
        for path, embedding in zip(batch_paths, embeddings):
            file_hash = hash_map.get(path, "")
            row = VectorRow(
                namespace=IMAGE_SEARCH_NAMESPACE,
                id=file_hash,  # Hash'i ID olarak kullan (dedupe için)
                source=IMAGE_SOURCE,
                model=NVCLIP_MODEL,
                embedding=embedding,
                text=None,
                file_path=path,
                file_hash=file_hash,
                metadata={"original_path": path},
            )
            rows.append(row)

        try:
            vector_store.upsert_many(rows)
            indexed_count += len(rows)
        except Exception as exc:
            log.error("image_index_build: Vector_Store upsert hatası: %s", exc)
            failed_embed += len(batch_paths)
            continue

        # İlerleme duyurusu: her 500 görselde bir (büyük klasörler için)
        if announce_progress and indexed_count > 0 and indexed_count % PROGRESS_INTERVAL == 0:
            progress_msg = (
                f"Görsel indeksleme devam ediyor: {indexed_count} görsel işlendi, "
                f"toplam {len(new_paths)} yeni görsel var."
            )
            _announce_progress(progress_msg)

    # --- Sonuç raporu ---
    parts = []

    if indexed_count > 0:
        parts.append(f"{indexed_count} yeni görsel başarıyla indekslendi")

    if skipped_dedupe > 0:
        parts.append(f"{skipped_dedupe} görsel zaten indekste mevcut (atlandı)")

    if skipped_access > 0:
        parts.append(f"{skipped_access} dosyaya erişilemedi (atlandı)")

    if failed_embed > 0:
        parts.append(f"{failed_embed} görsel için embedding üretilemedi (atlandı)")

    if not parts:
        return f"'{folder}' klasöründe işlenecek yeni görsel bulunamadı."

    summary = "; ".join(parts) + "."
    return f"'{folder}' klasörü indekslendi: {summary}"


image_index_build.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "image_index_build",
        "description": (
            "Verilen klasordeki gorselleri NVCLIP ile indeksler ve yerel "
            "arama icin Vector_Store'a kaydeder. Kullanici 'fotograflarimi "
            "indeksle', 'resim klasorumu tara', 'gorsel arama icin hazirla' "
            "gibi komutlar verdiginde kullan. Hash tabanlı dedupe ile "
            "degismemis gorseller yeniden islenmez. Privacy modu aktifken "
            "calistirilmaz."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "folder": {
                    "type": "STRING",
                    "description": (
                        "Indekslenecek klasorun tam yolu. "
                        "Ornek: 'C:/Users/Kullanici/Pictures'"
                    ),
                },
                "force_reindex": {
                    "type": "BOOLEAN",
                    "description": (
                        "True ise daha once indekslenmis gorseller de "
                        "yeniden islenir. Varsayilan: False."
                    ),
                },
            },
            "required": ["folder"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/nvclip",
        "fallback": [],
    },
}


# ---------------------------------------------------------------------------
# Ana handler: image_search
# ---------------------------------------------------------------------------

def image_search(query: str, top_k: int = DEFAULT_TOP_K) -> str:
    """Doğal dil sorgusuyla yerel görsel arama yapar.

    Sorgu için NVCLIP text embedding üretir ve ``Vector_Store``'da top-k
    (varsayılan k=10) en yakın görselin tam yollarını skorlarıyla birlikte
    döner (Req 10.3).

    Privacy_Mode aktifken mevcut indeks üzerinde arama açıktır (Req 10.8).
    """
    # --- API anahtarı kontrolü ---
    api_key = _nvidia_api_key()
    if not api_key:
        return (
            "NVIDIA API anahtarı girilmediği için görsel arama "
            "yapılamıyor."
        )

    # --- Sorgu doğrulama ---
    query = (query or "").strip()
    if not query:
        return "Arama sorgusu boş olamaz. Lütfen bir sorgu girin."

    if top_k <= 0:
        top_k = DEFAULT_TOP_K

    # --- Text embedding üret ---
    query_embedding = _embed_text_nvclip(query, api_key)
    if query_embedding is None:
        return (
            "Sorgu için embedding üretilemedi. NVIDIA API bağlantısını "
            "kontrol edin ve tekrar deneyin."
        )

    # --- Vector_Store'da knn_search ---
    vector_store = _get_vector_store()
    try:
        hits = vector_store.knn_search(
            namespace=IMAGE_SEARCH_NAMESPACE,
            query_embedding=query_embedding,
            top_k=top_k,
        )
    except Exception as exc:
        log.error("image_search: knn_search hatası: %s", exc)
        return (
            f"Görsel arama sırasında bir hata oluştu: {exc}. "
            "Lütfen tekrar deneyin."
        )

    if not hits:
        return (
            f"'{query}' sorgusu için indekste eşleşen görsel bulunamadı. "
            "Önce 'image_index_build' ile bir klasörü indekslemeniz gerekebilir."
        )

    # --- Sonuçları formatla ---
    lines = [f"'{query}' sorgusu için en yakın {len(hits)} görsel:\n"]
    for i, hit in enumerate(hits, 1):
        path = hit.file_path or hit.id
        score = hit.score
        lines.append(f"{i}. {path}  (benzerlik: {score:.4f})")

    return "\n".join(lines)


image_search.__tool__ = {  # type: ignore[attr-defined]
    "declaration": {
        "name": "image_search",
        "description": (
            "Dogal dil sorgusuyla yerel gorsel arama yapar. Kullanici "
            "'kedili fotograflari bul', 'deniz manzarasi ara', 'dogum gunu "
            "fotograflarini goster' gibi komutlar verdiginde kullan. "
            "Onceden image_index_build ile indekslenmiş klasorler uzerinde "
            "calisir. Privacy modu aktifken de arama yapilabilir."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {
                    "type": "STRING",
                    "description": (
                        "Aranacak gorsel icerigi. Dogal dil kullanilabilir. "
                        "Ornek: 'kedili fotograflar', 'deniz manzarasi', "
                        "'aile yemegi'"
                    ),
                },
                "top_k": {
                    "type": "INTEGER",
                    "description": (
                        "Donturulecek maksimum sonuc sayisi. "
                        "Varsayilan: 10."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    "execution_mode": "background",
    "route": {
        "provider": "nvidia",
        "model": "nvidia/nvclip",
        "fallback": [],
    },
}


__all__ = ["image_index_build", "image_search"]
