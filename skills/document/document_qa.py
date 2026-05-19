"""Document_QA skill tool — doküman soru-cevap.

Design.md § 8 ve Requirements § 20'ye karşılık gelir.

Sorumluluklar
-------------
* ≤200 sayfa: tek prompt'la Gemini gemini-2.5-flash (Req 20.3).
* >200 sayfa: kullanıcıyı bilgilendir, chunk'lara böl, her chunk'a
  nvidia_text_task ile özet, son adımda birleştir + soru çağrısı (Req 20.4).
* execution_mode="background" ile kayıt; sonuç Result_Announcer üzerinden
  duyurulur (Req 20.5).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from skills.document.readers import read as read_document
from skills.document.chunker import chunk as chunk_text

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)

# Yaklaşık token sınırları
_SINGLE_PASS_PAGE_LIMIT = 200
_CHUNK_TOKEN_SIZE = 8000
_CHARS_PER_TOKEN = 4  # kaba tahmin


def _chars_to_pages(char_count: int, chars_per_page: int = 2000) -> int:
    return max(1, char_count // chars_per_page)


def document_qa(file_path: str, question: str) -> str:
    """Doküman dosyasını oku ve soruyu yanıtla.

    Parameters
    ----------
    file_path:
        Okunacak dosyanın tam yolu (.pdf, .docx, .txt, .md).
    question:
        Dokümana sorulacak soru.

    Returns
    -------
    str
        Yanıt metni.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Dosya bulunamadı: {file_path}"

    # Dokümanı oku
    try:
        text = read_document(path)
    except Exception as exc:
        return f"Dosya okunamadı ({path.name}): {exc}"

    if not text.strip():
        return "Dosya boş veya metin içermiyor."

    estimated_pages = _chars_to_pages(len(text))
    log.info(
        "document_qa: %s — ~%d sayfa, %d karakter",
        path.name,
        estimated_pages,
        len(text),
    )

    if estimated_pages <= _SINGLE_PASS_PAGE_LIMIT:
        return _single_pass(text, question, path.name)
    else:
        return _chunked_pass(text, question, path.name, estimated_pages)


def _single_pass(text: str, question: str, filename: str) -> str:
    """≤200 sayfa: Gemini ile tek seferde yanıtla."""
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            f"Aşağıdaki dokümanı oku ve soruyu yanıtla.\n\n"
            f"Dosya: {filename}\n\n"
            f"Doküman:\n{text[:120000]}\n\n"
            f"Soru: {question}"
        )
        response = model.generate_content(prompt)
        return response.text
    except ImportError:
        return _fallback_answer(text, question)
    except Exception as exc:
        log.warning("document_qa single_pass hatası: %s", exc)
        return f"Yanıt üretilemedi: {exc}"


def _chunked_pass(text: str, question: str, filename: str, pages: int) -> str:
    """
    >200 sayfa: chunk'lara böl, her chunk'u özetle, sonra birleştir.
    """
    max_chunk_chars = _CHUNK_TOKEN_SIZE * _CHARS_PER_TOKEN
    chunks = chunk_text(text, max_chunk_size=max_chunk_chars)

    log.info(
        "document_qa: %s — %d chunk ile özetleme modu (%d sayfa).",
        filename,
        len(chunks),
        pages,
    )

    summaries: list[str] = []
    for i, chunk in enumerate(chunks):
        summary = _summarize_chunk(chunk, i + 1, len(chunks))
        summaries.append(summary)

    combined = "\n\n---\n\n".join(summaries)

    # Son adım: birleşik özet üzerinden soruyu yanıtla
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
        prompt = (
            f"Aşağıda '{filename}' adlı büyük dokümanın bölüm özetleri var.\n\n"
            f"{combined}\n\n"
            f"Bu özetlere dayanarak şu soruyu yanıtla: {question}"
        )
        response = model.generate_content(prompt)
        return (
            f"[{pages} sayfalık doküman {len(chunks)} bölümde özetlendi]\n\n"
            + response.text
        )
    except Exception as exc:
        log.warning("document_qa chunked final pass hatası: %s", exc)
        return f"Yanıt üretilemedi: {exc}"


def _summarize_chunk(chunk: str, idx: int, total: int) -> str:
    """Tek chunk'u özetle (nvidia_text_task veya Gemini)."""
    prompt = (
        f"Aşağıdaki metin bölümünü ({idx}/{total}) kısaca özetle:\n\n{chunk}"
    )
    # nvidia_text_task'i doğrudan çağırmak yerine Gemini kullan
    # (background task içinde çalıştığı için iç içe dispatch gerekmez)
    try:
        import google.generativeai as genai
        model = genai.GenerativeModel("gemini-2.5-flash")
        response = model.generate_content(prompt)
        return response.text
    except Exception as exc:
        log.warning("document_qa chunk %d/%d özetleme hatası: %s", idx, total, exc)
        return chunk[:500] + "..."


def _fallback_answer(text: str, question: str) -> str:
    """google.generativeai yoksa basit keyword arama."""
    q_words = set(question.lower().split())
    lines = text.split("\n")
    relevant = [
        line for line in lines
        if any(w in line.lower() for w in q_words) and len(line.strip()) > 20
    ]
    if relevant:
        return "İlgili bölümler:\n" + "\n".join(relevant[:10])
    return "Soruyla ilgili içerik bulunamadı."


document_qa.__tool__ = {
    "declaration": {
        "name": "document_qa",
        "description": (
            "Bir doküman dosyasını (.pdf, .docx, .txt, .md) okur ve "
            "kullanıcının sorusunu yanıtlar. Büyük dosyalar otomatik olarak "
            "bölümlere ayrılır."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "file_path": {
                    "type": "STRING",
                    "description": "Okunacak dosyanın tam yolu.",
                },
                "question": {
                    "type": "STRING",
                    "description": "Dokümana sorulacak soru.",
                },
            },
            "required": ["file_path", "question"],
        },
    },
    "execution_mode": "background",
}

__all__ = ["document_qa"]
