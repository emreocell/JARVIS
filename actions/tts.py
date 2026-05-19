"""
TTS (Text-to-Speech) — Windows pyttsx3 kullanır.
Alp Ünlü tarafından yapılmıştır — @alppunlu
Windows uyarlaması

----------------------------------------------------------------------
JARVIS v2 mimarisindeki konumu
----------------------------------------------------------------------
Bu modül **bilinçli olarak** ``actions/`` altında kalır ve yeni
``skills/`` paketine taşınmaz. Sebep:

* TTS, Gemini Live tarafından sağlanmayan ortamlarda Voice_Core
  (``main.py::JarvisLive``) için *içsel* bir yedek/yardımcı katmandır.
  LLM'in kullanabileceği bir "tool" değildir.
* Bu nedenle modülde ``__tool__`` metadata'sı **yoktur** ve hiçbir
  ``skills/*/__skill__.py`` MANIFEST'i bu modüle referans vermez.
* ``runtime.plugin_host.PluginHost.discover([Path("skills")])`` yalnız
  ``skills/`` ağacını gezdiği için bu dosya hiçbir zaman
  Tool_Runtime'a kaydolmaz; dolayısıyla Gemini'ye tool olarak da
  açılmaz.
* Tüketici tek bir noktadır: Voice_Core (gelecekte gerekirse)
  ``from actions.tts import speak_text`` şeklinde içe aktarır. Public
  API (``speak_text``, ``get_available_voices``) bu içsel kullanım
  garantisi altında stabildir.

Yeni bir Voice_Core dış görünüş ekleneceği zaman da burada bulunan
``speak_text(text, on_done=None, blocking=False)`` imzası korunmalıdır.
İlgili tasarım kararı: ``.kiro/specs/jarvis-v2-upgrade/tasks.md`` Görev
5.11 ve Requirement 18.1.
"""

import threading
import pyttsx3


# pyttsx3 motoru (global, tekrar kullanım için)
_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    """Singleton TTS motoru oluştur."""
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                try:
                    _engine = pyttsx3.init()
                    # Windows'ta Türkçe ses ayarla (varsa)
                    voices = _engine.getProperty('voices')
                    for voice in voices:
                        # Türkçe ses ara
                        if 'turkish' in voice.name.lower() or 'türk' in voice.name.lower():
                            _engine.setProperty('voice', voice.id)
                            break
                        # İngilizce ses varsayılan
                        elif 'english' in voice.name.lower():
                            _engine.setProperty('voice', voice.id)
                except Exception:
                    _engine = None
    return _engine


def speak_text(text: str, on_done=None, blocking: bool = False):
    """
    Metni sesli olarak okur.
    on_done: okuma bitince çağrılacak fonksiyon (opsiyonel)
    blocking: True ise bitene kadar bekler
    """
    if not text or not text.strip():
        if on_done:
            on_done()
        return

    # Çok uzun metinleri kısalt (TTS için)
    max_len = 500
    if len(text) > max_len:
        text = text[:max_len] + "..."

    def _run():
        try:
            engine = _get_engine()
            if engine:
                engine.say(text)
                engine.runAndWait()
            else:
                # Fallback: Windows SAPI
                import subprocess
                subprocess.run(
                    ['powershell', '-Command', f'Add-Type -AssemblyName System.Speech; $synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; $synth.Speak("{text}")'],
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except Exception:
            pass
        if on_done:
            on_done()

    if blocking:
        _run()
    else:
        threading.Thread(target=_run, daemon=True).start()


def get_available_voices() -> list[str]:
    """Windows'taki mevcut sesleri listeler."""
    try:
        engine = _get_engine()
        if engine:
            voices = engine.getProperty('voices')
            return [v.name for v in voices]
    except Exception:
        pass
    return []