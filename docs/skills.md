# Skill Yazma Rehberi

Bu rehber JARVIS v2 için yeni bir skill (yetenek paketi) nasıl yazılır adım adım açıklar.

## Skill Nedir?

Skill, bir veya daha fazla tool (araç) içeren modüler bir Python paketidir. Her skill:
- `skills/{isim}/` klasöründe yaşar
- `__skill__.py` veya `skill.yaml` ile kendini tanımlar
- `tools.py` içinde tool fonksiyonlarını barındırır
- Plugin_Host tarafından çalışma zamanında otomatik keşfedilir ve yüklenir

## Adım Adım: Yeni Skill Oluşturma

### 1. Klasör Yapısını Oluştur

```
skills/
  benim_skilim/
    __init__.py
    __skill__.py
    tools.py
```

### 2. `__init__.py` Oluştur

```python
"""Benim skill açıklamam."""
```

### 3. `__skill__.py` — Manifesto Tanımla

```python
from runtime.types import SkillManifest

MANIFEST = SkillManifest(
    name="benim_skilim",
    version="0.1.0",
    enabled=True,
    entry_module="skills.benim_skilim.tools",
    tools=[
        "benim_toolum",
        "ikinci_toolum",
    ],
    description="Bu skill şunu yapar.",
    requires=["requests"],  # pip bağımlılıkları (opsiyonel)
)

__all__ = ["MANIFEST"]
```

### 4. `tools.py` — Tool Fonksiyonlarını Yaz

Her tool fonksiyonu:
- Normal bir Python fonksiyonu olmalı
- `__tool__` metadata dict'i taşımalı
- `declaration` (Gemini schema) ve `execution_mode` içermeli

```python
def benim_toolum(sorgu: str) -> str:
    """Tool açıklaması."""
    # İşlemi yap
    return f"Sonuç: {sorgu}"

benim_toolum.__tool__ = {
    "declaration": {
        "name": "benim_toolum",
        "description": "Bu tool şunu yapar. Kullanıcı X dediğinde kullan.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "sorgu": {
                    "type": "STRING",
                    "description": "Arama sorgusu.",
                },
            },
            "required": ["sorgu"],
        },
    },
    "execution_mode": "inline",  # veya "background"
}


def ikinci_toolum(dosya_yolu: str, soru: str) -> str:
    """Uzun süren işlem — background modunda çalışır."""
    import time
    time.sleep(5)  # Uzun işlem simülasyonu
    return f"{dosya_yolu} için yanıt: {soru}"

ikinci_toolum.__tool__ = {
    "declaration": {
        "name": "ikinci_toolum",
        "description": "Uzun süren bir işlem yapar.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "dosya_yolu": {"type": "STRING", "description": "Dosya yolu."},
                "soru": {"type": "STRING", "description": "Soru."},
            },
            "required": ["dosya_yolu", "soru"],
        },
    },
    "execution_mode": "background",  # Arka planda çalışır, Voice_Core'u bloklamaz
}

__all__ = ["benim_toolum", "ikinci_toolum"]
```

## Declaration Şeması

Gemini'nin kendi tip sistemi kullanılır (JSON Schema Draft-7 değil):

| Tip | Açıklama |
|-----|----------|
| `STRING` | Metin |
| `NUMBER` | Sayı (int veya float) |
| `BOOLEAN` | true/false |
| `OBJECT` | İç içe nesne |
| `ARRAY` | Liste |

### Parametresiz Tool

```python
def basit_tool() -> str:
    return "Merhaba"

basit_tool.__tool__ = {
    "declaration": {
        "name": "basit_tool",
        "description": "Parametresiz basit bir tool.",
        # "parameters" alanı atlanabilir
    },
    "execution_mode": "inline",
}
```

## `execution_mode` Seçimi

| Mod | Ne Zaman Kullanılır | Örnek |
|-----|---------------------|-------|
| `"inline"` | < 2 saniye süren işlemler | sys_info, get_weather |
| `"background"` | Uzun süren, IO ağırlıklı işlemler | video_object_detect, document_qa |

**Background mod:** Tool_Runtime görevi Task_Manager'a gönderir ve Voice_Core'a hemen `"Görev başlatıldı"` yanıtı döner. Görev tamamlandığında Result_Announcer sonucu sesli olarak duyurur.

## Skill'i Devre Dışı Bırakma

`config/api_keys.json` içinde:

```json
{
  "disabled_skills": ["benim_skilim", "vision"]
}
```

## Tam Örnek: Hava Durumu Skill'i

```python
# skills/hava/tools.py

import requests

def hava_durumu(sehir: str) -> str:
    """Belirtilen şehir için hava durumu bilgisi döner."""
    try:
        # Gerçek API çağrısı burada olur
        return f"{sehir}: 22°C, Güneşli"
    except Exception as exc:
        return f"Hava durumu alınamadı: {exc}"

hava_durumu.__tool__ = {
    "declaration": {
        "name": "hava_durumu",
        "description": (
            "Belirtilen şehir için güncel hava durumu bilgisi döner. "
            "Kullanıcı hava sorarsa kullan."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "sehir": {
                    "type": "STRING",
                    "description": "Hava durumu sorgulanacak şehir adı.",
                },
            },
            "required": ["sehir"],
        },
    },
    "execution_mode": "inline",
}

__all__ = ["hava_durumu"]
```

```python
# skills/hava/__skill__.py

from runtime.types import SkillManifest

MANIFEST = SkillManifest(
    name="hava",
    version="0.1.0",
    enabled=True,
    entry_module="skills.hava.tools",
    tools=["hava_durumu"],
    description="Hava durumu bilgisi sağlar.",
)

__all__ = ["MANIFEST"]
```

## İpuçları

- Tool adları benzersiz olmalı — aynı isimde iki tool varsa ikincisi yüklenmez.
- `description` alanı Gemini'nin tool'u ne zaman çağıracağını belirler; açıklayıcı yazın.
- Background tool'lar `BackgroundTask.cancel_event` ile iptal sinyalini dinleyebilir.
- Privacy Mode aktifken clipboard ve log işlemleri otomatik devre dışı kalır.

## Tool `route` Metadata (NIM/Gemini Yönlendirmesi)

JARVIS v2 + NVIDIA Skill Pack ile birlikte gelen `Model_Router`, her tool çağrısının hangi sağlayıcıya (Gemini birincil, Gemini ikincil veya NVIDIA NIM) ve hangi modele gideceğine karar verir. Skill yazarı, tool'unun tercih ettiği rotayı ve fallback zincirini `__tool__` sözlüğüne **opsiyonel** `route` alanı ekleyerek bildirir. Alan tanımlanmazsa Plugin_Host varsayılan rota mantığına devreder ve `config/api_keys.json → model_router.default_routes` tablosundan tool kategorisine düşen rotayı uygular.

### Şema

```python
{
    "provider": "gemini_primary" | "gemini_secondary" | "nvidia",
    "model": "<sağlayıcının kabul ettiği model adı>",
    "fallback": [   # opsiyonel, sıralı, aynı yapıdaki rotalar
        {"provider": "...", "model": "..."},
        ...
    ],
}
```

Doğrulama kuralları (Plugin_Host `_validate_route` tarafından uygulanır):

- `provider` alanı yalnızca üç sabit değerden biri olabilir.
- `model` boş olmayan bir string olmalıdır.
- `fallback` ya yoktur ya da liste olur; her eleman aynı şemayı sağlar (iç içe geçişlerde de).
- Geçersiz `route` alanı **yalnızca o tool'un kayıtdan düşmesine** neden olur; skill yüklemesi devam eder ve Türkçe uyarı log'u üretilir.

### Örnek: NVIDIA birincil + Gemini ikincil fallback

```python
def memory_rag_query(question: str, top_k: int = 5) -> str:
    """Anlamsal hafızada arar ve Türkçe tek paragraflık yanıt döner."""
    ...

memory_rag_query.__tool__ = {
    "declaration": {
        "name": "memory_rag_query",
        "description": "Geçmiş konuşmalar ve indekslenmiş notlar üzerinde RAG sorgusu çalıştır.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "question": {"type": "STRING", "description": "Doğal dil sorusu."},
                "top_k": {"type": "NUMBER", "description": "Geri getirilecek alıntı sayısı."},
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
```

### Davranış özeti

- **Health gating:** Bir sağlayıcı son 60 saniyede iki ardışık başarısızlık aldıysa zincir kaydırılır (`Health_Probe`).
- **Retry:** 5xx / Timeout / `ConnectionError` durumlarında her rota için en fazla iki yeniden deneme.
- **Rate limit (429) özel kuralı:** Bir Gemini rotası 429 dönerse istek yalnızca diğer Gemini rotasına bir kez sıçrar; **NVIDIA'ya düşülmez** (intent çağrılarında anlamsız).
- **Auth (401/403):** İlgili sağlayıcı oturum boyunca devre dışı bırakılır.
- **Cache:** Aynı `(tool_name, request)` çifti için son 30 saniye içinde başarılı yanıt LRU önbellekten döner (kapasite 32). `model_router.disable_cache=true` ile kapatılabilir.
- **NVIDIA anahtarı yok:** `nvidia_api_key` boşsa NVIDIA bağımlısı tüm yeni skill'ler (memory_rag, doc_intel, reasoning, translate, safety, creative, image_search, audio_structured, embodied) Plugin_Host tarafından otomatik atılır; mevcut Gemini-only tool'lar etkilenmez.

## NVIDIA NIM Skill Kataloğu

Aşağıdaki 9 skill, NVIDIA NIM modellerini Model_Router üzerinden kullanır. Hepsi `requires=["nvidia_api_key"]` manifestine sahiptir; anahtar yoksa otomatik olarak devre dışı kalırlar. Privacy_Mode aktifken her skill'in kalıcı yan etkileri (vector store yazımı, log dosyası, clipboard tüketimi, ekran görüntüsü diske kayıt) durdurulur; salt-okunur sorgular çalışmaya devam eder.

### Safety_Skill (`skills/safety/`)

PII maskeleme, içerik güvenliği, konu kontrolü ve deepfake tespiti için süreç çapı koruyucu katmanı.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `pii_mask` | `inline` | `nvidia/gliner-pii` | Metni `[PII:tip]` formatında maskeler; idempotent. Conversation_Logger ve Clipboard_Manager tarafından senkron çağrılabilir (`safety.pii.mask`). |
| `content_safety_check` | `inline` | `meta/llama-guard-4-12b` (fb: `nvidia/llama-3.1-nemoguard-8b-content-safety`) | LLM yanıtının kullanıcıya iletilmeden önce güvenli olup olmadığını döner. |
| `topic_control_check` | `inline` | `nvidia/llama-3.1-nemoguard-8b-topic-control` | Sorgu konusunu `safety.allowed_topics` listesine göre kabul/red eder. |
| `deepfake_detect` | `background` | `nvidia/ai-synthetic-video-detector` | Video dosyası için sentetik olma yüzdesi döner. |

Konfig: `safety.enforce_content_safety` (varsayılan `true`), `safety.fail_closed` (varsayılan `false`), `safety.allowed_topics`. `enforce_content_safety=false` iken denetim atlanır, sadece "warn" log'u düşer.

### Memory_RAG_Skill (`skills/memory_rag/`)

Konuşma logları, notlar ve dosyalar için yerel vektör tabanı + RAG sorgu motoru.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `memory_index_add` | `background` | `nvidia/nv-embedqa-e5-v5` | Metni 800 karakterlik chunk'lara böler, embed eder, `Vector_Store`'a upsert eder. Embed hatasında 3x exponential backoff. |
| `memory_rag_query` | `background` | `nvidia/llama3-chatqa-1.5-70b` (fb: `meta/llama-3.1-70b-instruct`, `gemini_secondary/models/gemini-2.5-pro`) | top-k chunk'ı (varsayılan k=5) alır, prompt'a kaynak alıntı ekler, Türkçe tek paragraf yanıt + parantez içinde kaynak başlıkları döner. |
| `memory_rag_forget` | `inline` | — | Belirli kaynak veya kimlikleri Vector_Store'dan siler; idempotent. |

Konfig: `memory_rag.top_k`, `memory_rag.chunk_chars`, `memory_rag.chunk_overlap`, `memory_rag.embed_batch`. 10 MB üstü dosyalar stream + 1000 chunk üst sınırı ile işlenir. Privacy_Mode aktifken conversation log kaynaklı eklemeler `PendingIndexQueue`'a alınır (kapasite 5000), Privacy_Mode kapanınca arka planda drain edilir.

### Doc_Intel_Skill (`skills/doc_intel/`)

PDF/fatura/makbuz parse, chart okuma, uzun ekran görüntüsü özetleme.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `doc_parse` | `background` | `nvidia/nemotron-parse` (fb: `nvidia/nemoretriever-parse`) | Yapılandırılmış JSON döner: `vendor`, `total`, `currency`, `date`, `line_items`. Privacy_Mode kapalıysa `logs/doc_intel/{timestamp}.json` dosyasına yazar. |
| `chart_read` | `background` | `google/deplot` | Chart görselini tabloya çevirir + Türkçe açıklama. |
| `screenshot_summarize` | `background` | `microsoft/kosmos-2` (fb: `adept/fuyu-8b`) | Uzun ekran görüntüsünü en fazla üç paragrafta Türkçe özetler. |

Görsel uzun kenarı 4096 px'i aşarsa gönderim öncesi oran korunarak ölçeklendirilir. Dosya bulunamaz/okunamazsa modele istek **gönderilmez**, kullanıcıya Türkçe tek paragraflık hata döner. PDF'lerde PyMuPDF eksikse Türkçe açıklamayla `RuntimeError` üretilir.

### Reasoning_Skill (`skills/reasoning/`)

Çok adımlı planlama ve dinamik rutin üretimi.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `plan_generate` | `background` | `nvidia/llama-3.3-nemotron-super-49b-v1.5` (ultra opsiyonel: `nvidia/llama-3.1-nemotron-ultra-253b-v1` veya `qwen/qwen3-next-80b-a3b-thinking`) | Doğal dil hedefinden Routine_Engine ile uyumlu adım listesi üretir. `derin düşün` / `ultra reasoning` tetikleyicisiyle ultra modele yükselir. |
| `plan_explain` | `inline` | (router üzerinden) | Üretilmiş planı Türkçe paragrafla açıklar. |
| `plan_save` | `background` | — | Dinamik planı `routines.json`'a yazar; mevcut adla çakışırsa onay tool çağrısı bekler. |

`RoutinePlanParser` (saf, yan etkisiz) ham model çıktısını `Routine` / `RoutineStep` listesine çevirir; Plugin_Host'ta kayıtlı olmayan tool adlarını düşürür ve Türkçe uyarıyla `dropped_steps` döner. Bozuk JSON yanıtı, ham metni `dropped_steps`'e koyup `routine.steps == []` ile döner.

### Translate_Skill (`skills/translate/`)

Gerçek zamanlı çeviri ve OCR + çeviri.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `translate_text` | `inline` | `nvidia/riva-translate-4b-instruct-v1.1` | Metni hedef dile çevirir; Türkçe tek paragraflık girişle birlikte (orijinal + çeviri) döner. |
| `translate_screen` | `background` | `nvidia/riva-translate-4b-instruct-v1.1` | Aktif pencerenin ekran görüntüsünü mevcut Vision boru hattıyla yakalar, OCR uygular, çevirir. OCR boş → "Ekranda çevrilebilir metin bulunamadı". |

Kaynak dil belirtilmediyse otomatik tespit yapılır. Hedef dil yoksa `translate.default_target` kullanılır (varsayılan `en`). Privacy_Mode aktifken clipboard kaynaklı çağrılar durur; kullanıcı doğrudan diktiklerin çevirisi devam eder.

### Creative_Skill (`skills/creative/`)

Yaratıcı yazım, finansal analiz ve sağlık bilgi yanıtları için uzman modeller.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `creative_write` | `background` | `writer/palmyra-creative-122b` | Blog / sosyal medya / hikaye formatlarında Türkçe çıktı. |
| `financial_analyze` | `background` | `writer/palmyra-fin-70b-32k` | Çıktının başına **kalıcı** "Bu yatırım tavsiyesi değildir" Türkçe uyarısı eklenir. |
| `medical_qa` | `background` | `writer/palmyra-med-70b` | Çıktının başına **kalıcı** "Bu profesyonel tıbbi tavsiye yerine geçmez, bir doktora danışın" uyarısı eklenir. |

Finansal/tıbbi uyarılar `format_with_disclaimer` saf fonksiyonu tarafından her zaman çıktının başına yazılır; kullanıcı `uyarıyı kaldır` dese bile **kaldırılmaz** (yasal gereklilik). 30 sn timeout: Task_Manager iptal sinyali + Türkçe zaman aşımı paragrafı.

### Image_Search_Skill (`skills/image_search/`)

Yerel klasörde sıfır-shot doğal dil görsel arama.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `image_index_build` | `background` | `nvidia/nvclip` | Klasörü yürür, hash ile dedupe yapar, batch (varsayılan 8) embed eder, `Vector_Store`'a `namespace="image_search"` altında yazar. >5000 görselde her 500'de bir Result_Announcer ile Türkçe ilerleme paragrafı. |
| `image_search` | `background` | `nvidia/nvclip` | Sorgu için NVCLIP text embedding üretir, top-k (varsayılan k=10) en yakın görselin tam yollarını ve cosine skorlarını döner. |

Desteklenen uzantılar: `.jpg`, `.jpeg`, `.png`, `.webp`. Erişilemeyen dosyalar atlanır, atlanan sayı sonunda raporlanır. Aynı dosya hash'i + aynı embedding modeli için tekrar embed edilmez (idempotent build). Privacy_Mode aktifken yeni indeksleme durdurulur, mevcut indeks üzerinde arama açıktır.

### Audio_Structured_Skill (`skills/audio_structured/`)

Toplantı ve telefon görüşmesi kayıtlarını yapılandırılmış JSON'a çevirir. Mevcut `actions/nvidia_tools.py` shim'i ve `skills/vision/audio_to_table` tool'u **bozulmadan** korunur; bu paket onlara ek olarak eklenir.

| Tool | Mod | Çıktı Şeması | Özet |
| --- | --- | --- | --- |
| `meeting_to_actions` | `background` | `{participants, action_items[{owner, due}]}` | Mevcut transkripsiyon hattı + Reasoning rotası ile JSON üretir. |
| `call_to_crm` | `background` | `{customer, intent, next_step, summary}` | Telefon görüşmesini CRM-uyumlu yapıya çevirir. |

60 dakika üstü kayıtlar 10 dakikalık parçalara bölünür, sıralı transkribe + tek reasoning isteği. Çıktı, Privacy_Mode kapalıysa `logs/audio_structured/{timestamp}.json` dosyasına kalıcı yazılır. Sesli yanıt için Türkçe tek paragraflık özet de döner. Transkripsiyon başarısız olursa 3x exponential backoff, sonra Türkçe hata paragrafı.

### Embodied_Skill (`skills/embodied/`)

Ekran görüntüsünden GUI agent reasoning.

| Tool | Mod | Varsayılan Rota | Özet |
| --- | --- | --- | --- |
| `gui_next_action` | `background` | `nvidia/cosmos-reason2-8b` | Aktif pencereyi `actions/screen_vision.py` ile yakalar, kullanıcı hedefiyle birlikte modele gönderir, tek paragraflık Türkçe yönerge döner (ör. "sağ üstteki Ayarlar düğmesine tıklayın"). |

Model çıktısında koordinat / bounding box varsa metnin sonunda parantez içinde verilir; **doğrudan tıklama yapılmaz**. Ekran görüntüsü alınamazsa "Ekran görüntüsü alınamadı" döner ve modele istek gönderilmez. Privacy_Mode aktifken görüntü diske **yazılmaz**, yalnızca bellekte tutulur ve isteğe gönderilir.
