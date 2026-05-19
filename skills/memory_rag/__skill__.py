"""Memory_RAG skill manifest.

Yayınlanan tool'lar:

- ``memory_index_add`` — Bir metin parçasını, notu veya dosyayı anlamsal
  hafızaya ekler. Metin chunk'lara bölünür, ``nvidia/nv-embedqa-e5-v5``
  modeli ile vektörleştirilir ve Vector_Store'a kalıcı olarak yazılır.
  ``background`` modda çalışır (Req 4.1, 4.2, 4.3, 4.4).

- ``memory_rag_query`` — Doğal dil sorusunu Vector_Store'da arar ve
  ``nvidia/llama3-chatqa-1.5-70b`` modeli ile Türkçe yanıt üretir.
  Fallback: ``meta/llama-3.1-70b-instruct`` → ``gemini_secondary/models/gemini-2.5-pro``.
  ``background`` modda çalışır (Req 4.5, 4.6).

- ``memory_rag_forget`` — Belirtilen kaynak veya kimliğe ait vektörleri
  Vector_Store'dan siler. ``inline`` modda çalışır (Req 4.9).

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

Privacy_Mode aktifken ``conversation`` kaynaklı yeni indekslemeler
``PendingIndexQueue``'ya alınır; Privacy_Mode kapanınca arka planda drain
başlatılır (Req 4.7, 16.8).

10 MB üzeri dosyalar stream okunur ve 1000 chunk üst sınırı uygulanır
(Req 4.10).

NVIDIA API anahtarı yoksa Plugin_Host bu skill'i otomatik olarak devre
dışı bırakır (Req 17.4).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="memory_rag",
    version="1.0.0",
    enabled=True,
    entry_module="skills.memory_rag.tools",
    tools=[
        "memory_index_add",
        "memory_rag_query",
        "memory_rag_forget",
    ],
    description=(
        "Anlamsal hafıza ve RAG (Retrieval-Augmented Generation) skill'i. "
        "Konuşma loglarını, notları ve dosyaları vektörleştirip kalıcı olarak "
        "saklar; doğal dil sorularıyla ilgili bilgileri bulur ve NVIDIA modeli "
        "ile Türkçe yanıt üretir."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
