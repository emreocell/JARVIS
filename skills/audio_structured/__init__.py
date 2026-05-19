"""Audio_Structured_Skill — toplantı ve telefon görüşmesi yapılandırma.

Bu paket :mod:`skills.vision`'da bulunan mevcut ``audio_to_table`` tool'unu
**bozmadan**, NVIDIA NIM Skill Paketi spec'inin Req 11 maddelerini
karşılayan iki yeni background tool eklemek için ayrı bir skill paketi
olarak tasarlandı (bkz. design.md § Audio_Structured_Skill):

* ``meeting_to_actions`` — toplantı kaydını ``participants`` ve
  ``action_items`` (her biri ``owner`` ve ``due`` alanları opsiyonel)
  içeren JSON yapıya dönüştürür.
* ``call_to_crm`` — telefon görüşmesini ``customer``, ``intent``,
  ``next_step`` ve ``summary`` alanlarını içeren CRM-uyumlu JSON
  çıktıya çevirir.

Skill'in iç yardımcıları (chunk'lama, payload normalize) yan etkisiz
saf fonksiyonlar olarak :mod:`skills.audio_structured._internal`
altında durur ve Hypothesis ile property-based test edilebilir.
"""
