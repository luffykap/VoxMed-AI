"""
processing/tts.py
-----------------
Text-to-Speech using edge-tts (streaming) and macOS ffplay.
"""
import os
import subprocess
import asyncio
import edge_tts
import config
from utils.logger import get_logger

logger = get_logger(__name__)

async def _speak_streaming(text: str, voice: str) -> None:
    comm = edge_tts.Communicate(text, voice)
    # Using ffplay for instant playback by reading from standard input
    proc = subprocess.Popen(["ffplay", "-i", "pipe:0", "-nodisp", "-autoexit", "-loglevel", "quiet"], stdin=subprocess.PIPE)
    
    try:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                proc.stdin.write(chunk["data"])
    except Exception as exc:
        logger.exception("Error during edge-tts streaming: %s", exc)
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.wait()

def speak(text: str, language: str = "en") -> None:
    """
    Speak the given text aloud using edge-tts.
    Streams the audio directly to ffplay for instant playback without waiting for full generation.
    """
    if not text or not text.strip():
        return

    try:
        logger.info("TTS speaking (lang=%s, length=%d chars) via edge-tts", language, len(text))
        
        # Map our internal language codes to edge-tts voices
        lang_map = {
            "en": "en-US-AriaNeural",
            "hi": "hi-IN-SwaraNeural",
            "kn": "kn-IN-SapnaNeural",
            "te": "te-IN-ShrutiNeural"
        }
        
        voice = lang_map.get(language, "en-US-AriaNeural")
        
        # Run the async streaming function
        asyncio.run(_speak_streaming(text, voice))
            
    except Exception as exc:
        logger.exception("TTS speak failed: %s", exc)

