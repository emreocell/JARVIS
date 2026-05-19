"""Translate skill — gerçek zamanlı çeviri ve OCR çevirisi.

Bu paket NVIDIA `riva-translate-4b-instruct-v1.1` modelini Model_Router
üzerinden çağıran ``translate_text`` (inline) ve ``translate_screen``
(background) tool'larını yayımlar.

Saf yardımcılar (girdi normalize, Türkçe yanıt biçimleme, dil ipucu
heuristiği) :mod:`skills.translate._internal` içindedir ve PBT'ye
uygundur (yan etkisiz, deterministik).
"""
