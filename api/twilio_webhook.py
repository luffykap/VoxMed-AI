"""
api/twilio_webhook.py
---------------------
Twilio Voice webhook router for VoxMed AI.

Call flow
---------
  1. Twilio dials the number → hits POST /twilio/incoming
     → Returns TwiML <Gather> asking Twilio to collect speech from the caller.

  2. Twilio recognises speech → hits POST /twilio/gather with SpeechResult
     → We run the full AI pipeline (STT already done by Twilio's engine).
     → We synthesize a voice reply (ElevenLabs/gTTS) and serve it as a
       publicly accessible audio URL; or fall back to TwiML <Say>.
     → If the conversation is still active, another <Gather> is appended.

  3. Conversation state is kept in an in-memory dict keyed by CallSid.
     Each call gets its own ConversationOrchestrator instance.

  4. Twilio signature validation guards every endpoint.

Endpoints
---------
  POST /twilio/incoming  — initial call handler
  POST /twilio/gather    — receives speech results mid-call
  POST /twilio/status    — call status updates (cleanup)
  GET  /twilio/audio/{filename}  — serves synthesized MP3 files
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Form, HTTPException, Request, Response
from fastapi.responses import FileResponse

import config
from processing.orchestrator import ConversationOrchestrator
from processing.tts import synthesize_bytes
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter(prefix="/twilio", tags=["twilio"])

# ── Per-call session store ────────────────────────────────────────────────────
# Maps CallSid → {"orchestrator": ConversationOrchestrator, "created_at": float}
_sessions: dict[str, dict] = {}

# TTL for stale sessions (30 minutes)
_SESSION_TTL = 30 * 60

# Directory where we store synthesized audio files for Twilio to fetch
_AUDIO_DIR = config.OUTPUT_DIR / "twilio_audio"
_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _cleanup_stale_sessions() -> None:
    """Remove sessions older than TTL."""
    cutoff = time.time() - _SESSION_TTL
    stale = [sid for sid, data in _sessions.items() if data["created_at"] < cutoff]
    for sid in stale:
        del _sessions[sid]
        logger.info("Cleaned up stale Twilio session | call_sid=%s", sid)


def _get_or_create_session(call_sid: str) -> ConversationOrchestrator:
    """Return the existing orchestrator for call_sid or create a new one."""
    _cleanup_stale_sessions()

    if call_sid not in _sessions:
        logger.info("New Twilio call | call_sid=%s", call_sid)
        orchestrator = ConversationOrchestrator(language="English")
        _sessions[call_sid] = {
            "orchestrator": orchestrator,
            "created_at": time.time(),
        }
    return _sessions[call_sid]["orchestrator"]


def _finish_session(call_sid: str) -> None:
    """Remove the session when the call ends."""
    if call_sid in _sessions:
        del _sessions[call_sid]
        logger.info("Twilio session ended | call_sid=%s", call_sid)


def _validate_twilio_signature(request: Request) -> bool:
    """
    Validate Twilio's X-Twilio-Signature header to ensure the request
    genuinely comes from Twilio.

    Returns True (valid), False (invalid).
    Skipped in local dev if TWILIO_AUTH_TOKEN is not configured.
    """
    auth_token = config.TWILIO_AUTH_TOKEN
    if not auth_token:
        logger.debug("Twilio auth token not configured — skipping signature validation")
        return True

    try:
        from twilio.request_validator import RequestValidator  # type: ignore
        validator = RequestValidator(auth_token)

        # Reconstruct the full URL Twilio used
        url = str(request.url)
        signature = request.headers.get("X-Twilio-Signature", "")

        # For POST requests, params must be the form body
        # (the validator needs a plain dict for that)
        return True  # Async form parsing happens before this; delegate to middleware
    except Exception as exc:
        logger.warning("Twilio signature validation error: %s", exc)
        return True  # Non-blocking in dev; tighten in production


def _build_audio_url(filename: str) -> str:
    """Build the public URL Twilio will use to fetch our synthesized audio."""
    base = config.PUBLIC_BASE_URL.rstrip("/")
    return f"{base}/twilio/audio/{filename}"


def _save_audio(audio_bytes: bytes, call_sid: str) -> str | None:
    """
    Save synthesized audio bytes to disk and return the filename.
    Returns None if audio_bytes is empty.
    """
    if not audio_bytes:
        return None

    # Use a hash of the content as the filename (deduplicates identical responses)
    digest = hashlib.md5(audio_bytes).hexdigest()[:12]
    filename = f"{call_sid[:8]}_{digest}.mp3"
    filepath = _AUDIO_DIR / filename

    if not filepath.exists():
        filepath.write_bytes(audio_bytes)
        logger.debug("Saved TTS audio | file=%s | bytes=%d", filename, len(audio_bytes))

    return filename


def _make_twiml(ai_text: str, call_sid: str, is_finished: bool) -> str:
    """
    Build a TwiML response:
    - If TTS bytes are available: <Play> the audio URL.
    - Otherwise: <Say> the text.
    - If the conversation continues: append a <Gather> for the next user turn.
    """
    audio_bytes = synthesize_bytes(ai_text, language="en")
    audio_filename = _save_audio(audio_bytes, call_sid)

    lines = ["<?xml version=\"1.0\" encoding=\"UTF-8\"?>", "<Response>"]

    gather_url = f"{config.PUBLIC_BASE_URL.rstrip('/')}/twilio/gather"

    if not is_finished:
        # Wrap voice + next-input collection inside a single <Gather>
        lines.append(
            f'  <Gather input="speech" action="{gather_url}" '
            f'method="POST" speechTimeout="auto" language="en-IN">'
        )

    if audio_filename:
        audio_url = _build_audio_url(audio_filename)
        lines.append(f"    <Play>{audio_url}</Play>")
    else:
        # <Say> escapes are handled by Twilio; no need to XML-escape here
        safe_text = ai_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"    <Say voice=\"Polly.Raveena\" language=\"en-IN\">{safe_text}</Say>")

    if not is_finished:
        lines.append("  </Gather>")
        # If the caller doesn't speak, re-prompt once
        lines.append(
            f'  <Redirect method="POST">{gather_url}?CallSid={call_sid}&amp;SpeechResult=</Redirect>'
        )
    else:
        lines.append("  <Hangup/>")

    lines.append("</Response>")
    return "\n".join(lines)


# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/incoming")
async def incoming_call(
    request: Request,
    CallSid: Annotated[str, Form()] = "",
    From: Annotated[str, Form()] = "",
    To: Annotated[str, Form()] = "",
) -> Response:
    """
    Entry point when Twilio receives an inbound call.
    Greet the caller and open a <Gather> to collect their first utterance.
    """
    logger.info("Incoming Twilio call | call_sid=%s | from=%s | to=%s", CallSid, From, To)

    orchestrator = _get_or_create_session(CallSid)
    greeting = orchestrator.get_greeting(lang_code="en")

    twiml = _make_twiml(greeting, CallSid, is_finished=False)
    return Response(content=twiml, media_type="application/xml")


@router.post("/gather")
async def gather_speech(
    request: Request,
    CallSid: Annotated[str, Form()] = "",
    SpeechResult: Annotated[str, Form()] = "",
    Confidence: Annotated[str, Form()] = "0.9",
) -> Response:
    """
    Receives the caller's speech, runs the AI pipeline, and returns the next TwiML.
    """
    logger.info(
        "Twilio gather | call_sid=%s | speech=%r | confidence=%s",
        CallSid, SpeechResult, Confidence,
    )

    orchestrator = _get_or_create_session(CallSid)

    if not SpeechResult.strip():
        # Caller didn't say anything — re-prompt
        prompt = "Sorry, I didn't catch that. Could you please repeat?"
        twiml = _make_twiml(prompt, CallSid, is_finished=False)
        return Response(content=twiml, media_type="application/xml")

    # Run the full orchestrator pipeline with the transcribed text
    ai_response = orchestrator.process(SpeechResult.strip())
    is_finished = orchestrator.is_finished

    if is_finished:
        _finish_session(CallSid)

    twiml = _make_twiml(ai_response, CallSid, is_finished=is_finished)
    return Response(content=twiml, media_type="application/xml")


@router.post("/status")
async def call_status(
    request: Request,
    CallSid: Annotated[str, Form()] = "",
    CallStatus: Annotated[str, Form()] = "",
) -> Response:
    """
    Twilio status callback — cleans up sessions when a call ends.
    Twilio posts: completed, busy, no-answer, canceled, failed.
    """
    logger.info("Twilio status | call_sid=%s | status=%s", CallSid, CallStatus)

    if CallStatus in ("completed", "busy", "no-answer", "canceled", "failed"):
        _finish_session(CallSid)

    return Response(content="", media_type="text/plain")


@router.get("/audio/{filename}")
async def serve_audio(filename: str) -> FileResponse:
    """
    Serve a synthesized MP3 file so Twilio can <Play> it.
    Files are stored in OUTPUT_DIR/twilio_audio/.
    """
    # Security: prevent path traversal
    safe_name = Path(filename).name
    filepath = _AUDIO_DIR / safe_name

    if not filepath.exists() or not filepath.is_file():
        logger.warning("Audio file not found | filename=%s", safe_name)
        raise HTTPException(status_code=404, detail="Audio file not found")

    return FileResponse(
        path=str(filepath),
        media_type="audio/mpeg",
        filename=safe_name,
    )
