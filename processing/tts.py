"""
processing/tts.py
-----------------
Text-to-Speech using gTTS (Google Text-to-Speech) and macOS afplay.
"""
import os
import subprocess
from gtts import gTTS
import config
from utils.logger import get_logger

logger = get_logger(__name__)

def speak(text: str, language: str = "en") -> None:
    """
    Speak the given text aloud using Google TTS.
    Saves to a temporary MP3 file and plays it using macOS native afplay.
    """
    if not text or not text.strip():
        return

    try:
        logger.info("TTS speaking (lang=%s, length=%d chars) via gTTS", language, len(text))
        
        # Map our internal language codes to gTTS standard codes
        lang_map = {
            "en": "en",
            "hi": "hi",
            "kn": "kn",
            "te": "te"
        }
        gtts_lang = lang_map.get(language, "en")

        # Generate MP3 using Google TTS
        tts = gTTS(text=text, lang=gtts_lang, slow=False)
        temp_audio = str(config.OUTPUT_DIR / "temp_tts.mp3")
        tts.save(temp_audio)
        
        # Play the audio using macOS native afplay
        subprocess.run(["afplay", temp_audio])
        
        # Cleanup
        if os.path.exists(temp_audio):
            os.remove(temp_audio)
            
    except Exception as exc:
        logger.exception("TTS speak failed: %s", exc)

