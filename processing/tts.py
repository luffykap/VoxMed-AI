import pyttsx3
import config
from utils.logger import get_logger

logger = get_logger(__name__)

# Initialize the TTS engine once globally
try:
    engine = pyttsx3.init()
    # Configure base properties (rate, volume)
    engine.setProperty('rate', 175)
    engine.setProperty('volume', 1.0)
    logger.info("pyttsx3 TTS engine initialized successfully.")
except Exception as e:
    logger.error("Failed to initialize pyttsx3: %s", e)
    engine = None

def speak(text: str, language: str = "en") -> None:
    """
    Speaks the given text using the local TTS engine.
    Attempts to select a voice matching the requested language.
    """
    if not text.strip():
        return

    if not engine:
        import sys
        if sys.platform == "darwin":
            import subprocess
            logger.info("Using macOS 'say' fallback for TTS.")
            # Map language to a standard Mac voice if possible
            voice_arg = "Samantha"
            if language.lower() == "hi":
                voice_arg = "Lekha"
            try:
                subprocess.run(["say", "-v", voice_arg, text], check=True)
            except Exception as e:
                logger.error("macOS 'say' fallback failed: %s", e)
        else:
            logger.error("TTS engine is not initialized. Cannot speak.")
        return

    # Attempt to find an appropriate voice
    # macOS voices usually have identifiers like 'com.apple.speech.synthesis.voice.lekha'
    target_lang = language.lower()
    
    # Common mappings for macOS / Windows
    voice_hints = {
        "hi": ["lekha", "hindi", "kalpana"],
        "en": ["samantha", "daniel", "english", "zira", "david"]
    }
    
    hints = voice_hints.get(target_lang, [target_lang])
    
    selected_voice_id = None
    voices = engine.getProperty('voices')
    
    for voice in voices:
        voice_str = f"{voice.id} {voice.name} {voice.languages}".lower()
        if any(hint in voice_str for hint in hints):
            selected_voice_id = voice.id
            break
            
    if selected_voice_id:
        engine.setProperty('voice', selected_voice_id)
        
    logger.info("Speaking text (lang=%s, length=%d, voice=%s)", 
                language, len(text), selected_voice_id or "default")
    
    try:
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        logger.error("Error during TTS speak: %s", e)
