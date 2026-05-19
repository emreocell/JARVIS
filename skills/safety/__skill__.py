"""Safety skill manifest.

Yayınlanan tool'lar:

- ``pii_mask`` — Metindeki PII alanlarını ``nvidia/gliner-pii`` modeli ile
  tespit eder ve ``[PII:tip]`` formatında maskeler. ``inline`` modda çalışır.
  Conversation_Logger ve Clipboard_Manager tarafından ``skills.safety.pii.mask``
  sarmalayıcısı üzerinden de senkron çağrılabilir (Req 8.7).

- ``content_safety_check`` — LLM yanıtını ``meta/llama-guard-4-12b`` veya
  ``nvidia/llama-3.1-nemoguard-8b-content-safety`` modeli ile denetler.
  ``safety.enforce_content_safety=false`` ise denetim atlanır, yalnızca
  "warn" log üretilir (Req 8.9). ``inline`` modda çalışır.

- ``topic_control_check`` — Kullanıcı sorgusunun ``safety.allowed_topics``
  listesine uygunluğunu ``nvidia/llama-3.1-nemoguard-8b-topic-control``
  modeli ile denetler. ``inline`` modda çalışır.

- ``deepfake_detect`` — Video dosyasını ``nvidia/ai-synthetic-video-detector``
  modeli ile analiz eder; sentetik olma olasılığını yüzde olarak döner.
  ``background`` modda çalışır.

Plugin_Host bu modülü ``MANIFEST`` üzerinden keşfeder ve ``tools``
listesindeki her ad için ``entry_module`` içindeki ``__tool__`` metadata'sını
okur (bkz. ``runtime/plugin_host.py``).

``pii_mask`` özel bir konumda durur: hem Gemini tool'u olarak Voice_Core
tarafından çağrılabilir hem de ``skills.safety.pii.mask`` düz Python
sarmalayıcısı üzerinden Conversation_Logger ve Clipboard_Manager tarafından
senkron çağrılabilir. Skill yüklenmediğinde ``pii.mask`` no-op (identity)
olarak kalır; böylece NVIDIA anahtarsız çalışan kullanıcı için davranış
değişmez (design.md § Safety_Skill).

``safety.fail_closed`` config alanı ``True`` ise NIM endpoint başarısız
olduğunda çağrı reddedilir; ``False`` ise uyarı log'u ile geçişe izin
verilir (Req 8.10).

Privacy_Mode aktifken ``pii_mask`` çağrılarına devam edilir; conversation
log yazımı Privacy_Mode tarafından zaten durdurulduğu için yalnızca
clipboard ve sesli yanıt akışında maskeleme aktif kalır (Req 8.8).
"""

from __future__ import annotations

from runtime.types import SkillManifest


MANIFEST: SkillManifest = SkillManifest(
    name="safety",
    version="1.0.0",
    enabled=True,
    entry_module="skills.safety.tools",
    tools=[
        "pii_mask",
        "content_safety_check",
        "topic_control_check",
        "deepfake_detect",
    ],
    description=(
        "Safety skill'i: PII maskeleme (gliner-pii), içerik güvenliği "
        "denetimi (llama-guard-4-12b / nemoguard-content-safety), konu "
        "kısıtı denetimi (nemoguard-topic-control) ve deepfake tespiti "
        "(ai-synthetic-video-detector). Tüm denetimler NVIDIA NIM "
        "endpoint'leri üzerinden çalışır."
    ),
    requires=["nvidia_api_key"],
)


__all__ = ["MANIFEST"]
