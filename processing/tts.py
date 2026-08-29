"""
processing/tts.py
-----------------
Text-to-Speech with a cross-platform provider chain.

Provider selection (tried in order):
  1. ElevenLabs API  — if ELEVENLABS_API_KEY is set
  2. gTTS + pygame   — cross-platform fallback
  3. pyttsx3         — fully offline, last resort

speak(text, language)     → plays audio through speakers
synthesize_bytes(text, language) → returns raw MP3 bytes for Twilio
"""
from __future__ import annotations

import io

import config
from utils.logger import get_logger

logger = get_logger(__name__)


# ── ElevenLabs ────────────────────────────────────────────────────────────────

def _elevenlabs_bytes(text: str, language: str) -> bytes | None:
    """
    Call ElevenLabs TTS API and return raw MP3 bytes.
    Returns None on any failure so the caller can fall back.
    """
    api_key = config.ELEVENLABS_API_KEY
    if not api_key:
        return None

    voice_id = config.ELEVENLABS_VOICE_ID

    try:
        import httpx

        url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
        headers = {
            "xi-api-key": api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        payload = {
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        }

        with httpx.Client(timeout=20) as client:
            response = client.post(
                url,
                headers=headers,
                json=payload,
            )

        response.raise_for_status()

        logger.info(
            "ElevenLabs TTS | lang=%s | chars=%d | bytes=%d",
            language,
            len(text),
            len(response.content),
        )

        return response.content

    except Exception as exc:
        logger.warning(
            "ElevenLabs TTS failed (%s) — falling back",
            exc,
        )
        return None


# ── gTTS + pygame ─────────────────────────────────────────────────────────────

_GTTS_LANG_MAP: dict[str, str] = {
    "en": "en",
    "hi": "hi",
    "kn": "kn",
    "te": "te",
}


def _gtts_bytes(text: str, language: str) -> bytes | None:
    """
    Generate MP3 bytes using Google Text-to-Speech.
    Returns None on failure.
    """
    try:
        from gtts import gTTS

        gtts_lang = _GTTS_LANG_MAP.get(language, "en")

        tts = gTTS(
            text=text,
            lang=gtts_lang,
            slow=False,
        )

        buf = io.BytesIO()
        tts.write_to_fp(buf)
        buf.seek(0)

        mp3_bytes = buf.read()

        logger.info(
            "gTTS | lang=%s | chars=%d | bytes=%d",
            language,
            len(text),
            len(mp3_bytes),
        )

        return mp3_bytes

    except Exception as exc:
        logger.warning(
            "gTTS failed (%s) — falling back",
            exc,
        )
        return None


def _play_mp3_bytes(mp3_bytes: bytes) -> None:
    """
    Play MP3 bytes through system speakers using pygame.
    """
    try:
        import pygame

        pygame.mixer.init()
        pygame.mixer.music.load(io.BytesIO(mp3_bytes))
        pygame.mixer.music.play()

        while pygame.mixer.music.get_busy():
            pygame.time.wait(50)

        pygame.mixer.quit()

    except Exception as exc:
        logger.warning(
            "pygame playback failed (%s)",
            exc,
        )


# ── pyttsx3 ──────────────────────────────────────────────────────────────────

def _pyttsx3_speak(text: str) -> None:
    """Last-resort offline TTS using pyttsx3."""
    try:
        import pyttsx3

        engine = pyttsx3.init()
        engine.setProperty("rate", 150)
        engine.setProperty("volume", 1.0)

        engine.say(text)
        engine.runAndWait()

        logger.info(
            "pyttsx3 TTS | chars=%d",
            len(text),
        )

    except Exception as exc:
        logger.warning(
            "pyttsx3 speak failed (%s) — printing only",
            exc,
        )
        print(f"[TTS fallback]: {text}")


# ── Public API ────────────────────────────────────────────────────────────────

def speak(text: str, language: str = "en") -> None:
    """
    Speak the given text using the best available TTS provider.

    Provider chain:
      1. ElevenLabs
      2. gTTS + pygame
      3. pyttsx3
    """
    if not text or not text.strip():
        return

    logger.info(
        "TTS speak | lang=%s | chars=%d",
        language,
        len(text),
    )

    # 1. ElevenLabs
    mp3_bytes = _elevenlabs_bytes(text, language)

    if mp3_bytes:
        _play_mp3_bytes(mp3_bytes)
        return

    # 2. gTTS + pygame
    mp3_bytes = _gtts_bytes(text, language)

    if mp3_bytes:
        _play_mp3_bytes(mp3_bytes)
        return

    # 3. pyttsx3
    _pyttsx3_speak(text)


def synthesize_bytes(text: str, language: str = "en") -> bytes:
    """
    Generate TTS audio and return raw MP3 bytes WITHOUT playing them.

    Used by the Twilio webhook.
    """
    if not text or not text.strip():
        return b""

    logger.info(
        "TTS synthesize_bytes | lang=%s | chars=%d",
        language,
        len(text),
    )

    # 1. ElevenLabs
    mp3_bytes = _elevenlabs_bytes(text, language)

    if mp3_bytes:
        return mp3_bytes

    # 2. gTTS
    mp3_bytes = _gtts_bytes(text, language)

    if mp3_bytes:
        return mp3_bytes

    logger.warning(
        "synthesize_bytes: all cloud providers failed — returning empty bytes"
    )

    return b""