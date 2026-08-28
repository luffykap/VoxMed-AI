"""
processing/stt.py
-----------------
Speech-to-Text with automatic provider selection.

Local mode  (CLI / main.py):
    transcribe(language)  →  records mic → returns {text, language, confidence}
    Uses local faster-whisper (no API key required).

Cloud mode  (Twilio webhook):
    transcribe_audio_bytes(audio_bytes, language)  →  returns {text, language, confidence}
    Uses OpenAI Whisper API if OPENAI_API_KEY is set, otherwise falls back
    to the local faster-whisper model by writing the bytes to a temp file.

Provider selection:
    OPENAI_API_KEY set  →  OpenAI Whisper API   (cloud, Twilio-optimised)
    OPENAI_API_KEY not set  →  faster-whisper   (local CPU inference)
"""
from __future__ import annotations

import io
import math
import os
import tempfile
from pathlib import Path

import config
from utils.logger import get_logger

logger = get_logger(__name__)

# ── Lazy-initialise the local Whisper model ───────────────────────────────────
# We only load it if cloud Whisper is NOT configured, to avoid wasting RAM/CPU
# when running in production with an OpenAI key.

_local_model = None


def _get_local_model():
    """Return the cached faster-whisper model, loading it on first call."""
    global _local_model
    if _local_model is None:
        from faster_whisper import WhisperModel

        logger.info(
            "Loading local Whisper model: %s (device: %s, compute: %s)",
            config.WHISPER_MODEL,
            config.WHISPER_DEVICE,
            config.WHISPER_COMPUTE_TYPE,
        )
        _local_model = WhisperModel(
            config.WHISPER_MODEL,
            device=config.WHISPER_DEVICE,
            compute_type=config.WHISPER_COMPUTE_TYPE,
        )
    return _local_model


# ── Shared helpers ────────────────────────────────────────────────────────────


def _local_transcribe_file(filepath: str, language: str | None) -> dict:
    """
    Run local faster-whisper on an audio file.
    Returns {text, language, confidence}.
    """
    model = _get_local_model()

    segments, info = model.transcribe(
        filepath,
        beam_size=5,
        language=language,
        task="translate",
    )

    detected_lang = info.language
    lang_prob = info.language_probability
    text = ""
    logprobs: list[float] = []

    for segment in segments:
        text += segment.text + " "
        logprobs.append(segment.avg_logprob)

    text = text.strip()

    if logprobs:
        avg_logprob = sum(logprobs) / len(logprobs)
        confidence = round(math.exp(avg_logprob), 4)
    else:
        confidence = 0.0

    logger.info(
        "Local STT | lang=%s (prob=%.2f) | confidence=%.2f | chars=%d",
        detected_lang,
        lang_prob,
        confidence,
        len(text),
    )
    return {"text": text, "language": detected_lang, "confidence": confidence}


def _cloud_transcribe_bytes(audio_bytes: bytes, language: str | None) -> dict:
    """
    Send audio bytes to OpenAI Whisper API and return {text, language, confidence}.
    Falls back to local transcription on any API error.
    """
    import httpx

    api_key = config.OPENAI_API_KEY
    model = config.OPENAI_WHISPER_MODEL

    # Build a multipart request — OpenAI requires the file as a named upload
    audio_file = ("audio.wav", io.BytesIO(audio_bytes), "audio/wav")

    data: dict = {"model": model}
    if language:
        data["language"] = language

    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {api_key}"},
                files={"file": audio_file},
                data=data,
            )
        response.raise_for_status()
        result = response.json()
        text = result.get("text", "").strip()
        detected_lang = result.get("language", language or "en")

        # OpenAI Whisper API doesn't return a per-segment confidence score,
        # so we use a fixed high-confidence value for successful API calls.
        confidence = 0.9

        logger.info(
            "Cloud STT (OpenAI Whisper) | lang=%s | confidence=%.2f | chars=%d",
            detected_lang,
            confidence,
            len(text),
        )
        return {"text": text, "language": detected_lang, "confidence": confidence}

    except Exception as exc:
        logger.warning(
            "OpenAI Whisper API failed (%s) — falling back to local faster-whisper", exc
        )
        # Fallback: write bytes to a temp WAV and run local model
        return _bytes_via_local_fallback(audio_bytes, language)


def _bytes_via_local_fallback(audio_bytes: bytes, language: str | None) -> dict:
    """Write audio_bytes to a temp WAV file and transcribe with local model."""
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp.write(audio_bytes)
        tmp_path = tmp.name
    try:
        return _local_transcribe_file(tmp_path, language)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ── Public API ────────────────────────────────────────────────────────────────


def transcribe(language: str | None = None) -> dict:
    """
    Capture audio from the microphone and transcribe it.
    Used by the local CLI (main.py). Always uses local faster-whisper.

    Returns: {text, language, confidence}
    """
    from input.recorder import record_audio

    logger.info("Mic transcription started (local faster-whisper)")

    try:
        record_audio(
            filename=config.TEMP_AUDIO_FILE,
            max_duration=config.AUDIO_DURATION,
            silence_threshold=config.SILENCE_THRESHOLD,
            silence_duration=config.SILENCE_DURATION,
        )
        logger.info("Audio captured — transcribing...")
        return _local_transcribe_file(config.TEMP_AUDIO_FILE, language)

    except Exception:
        logger.exception("Unexpected error during microphone transcription")
        return {"text": "", "language": "unknown", "confidence": 0.0}


def transcribe_audio_bytes(audio_bytes: bytes, language: str | None = None) -> dict:
    """
    Transcribe raw audio bytes (e.g. from a Twilio recording download).

    Provider selection:
        OPENAI_API_KEY set  →  OpenAI Whisper API (cloud)
        Otherwise           →  local faster-whisper (via temp file)

    Returns: {text, language, confidence}
    """
    if not audio_bytes:
        logger.warning("transcribe_audio_bytes: received empty audio_bytes")
        return {"text": "", "language": language or "en", "confidence": 0.0}

    logger.info(
        "transcribe_audio_bytes | size=%d bytes | provider=%s",
        len(audio_bytes),
        "openai_whisper" if config.OPENAI_API_KEY else "local_faster_whisper",
    )

    try:
        if config.OPENAI_API_KEY:
            return _cloud_transcribe_bytes(audio_bytes, language)
        else:
            return _bytes_via_local_fallback(audio_bytes, language)
    except Exception:
        logger.exception("transcribe_audio_bytes: unexpected error")
        return {"text": "", "language": language or "en", "confidence": 0.0}
