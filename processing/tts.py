"""
processing/tts.py
-----------------
Text-to-Speech using pyttsx3 (offline, cross-platform).

Creates a fresh engine per speak() call to avoid the Windows SAPI5 bug
where runAndWait() silently fails after the first invocation.
"""
import pyttsx3
from utils.logger import get_logger

logger = get_logger(__name__)

# Voice hint table — checked once, cached for reuse.
_VOICE_HINTS = {
    "hi": ["lekha", "hindi", "kalpana"],
    "kn": ["kannada"],
    "te": ["telugu"],
    "en": ["zira", "david", "samantha", "daniel", "english"],
}


def speak(text: str, language: str = "en") -> None:
    """
    Speak the given text aloud.

    A fresh pyttsx3 engine is created each time because the Windows
    SAPI5 driver's event loop hangs after the first runAndWait().
    """
    if not text or not text.strip():
        return

    try:
        engine = pyttsx3.init()
        engine.setProperty("rate", 175)
        engine.setProperty("volume", 1.0)

        # Try to pick a voice matching the language
        hints = _VOICE_HINTS.get(language.lower(), [language.lower()])
        voices = engine.getProperty("voices")
        for voice in voices:
            voice_str = f"{voice.id} {voice.name}".lower()
            if any(hint in voice_str for hint in hints):
                engine.setProperty("voice", voice.id)
                break

        logger.info("TTS speaking (lang=%s, length=%d chars)", language, len(text))
        engine.say(text)
        engine.runAndWait()
        engine.stop()
    except Exception:
        logger.exception("TTS speak failed")

