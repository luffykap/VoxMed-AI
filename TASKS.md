# VoxMed AI – Task Progress Tracker

> Last updated: 2026-07-09  
> Current Phase: **Phase 7 – Text-to-Speech**  

---

## ✅ PHASE 1: Project Foundation

### Done
- [x] Created folder structure: `services/`, `api/`, `admin/`, `tests/` with `__init__.py`
- [x] Created `config.py` – centralized constants (paths, audio, languages, DB, API)
- [x] Created `utils/logger.py` – timestamped logging to terminal + file
- [x] Created `utils/validators.py` – validate name, date, time, phone
- [x] Updated `requirements.txt` – all dependencies with pinned versions
- [x] Updated `.gitignore` – added `.env`, `*.db`, `output/*.log`
- [x] Created `.env` – local credentials (gitignored, never pushed)
- [x] Committed and pushed to GitHub
- [x] Created stub files for processing and services (Step 7)
- [x] Wired up `main.py` for end-to-end audio pipeline (Step 8)

## ✅ PHASE 2: Microphone Integration
### Done
- [x] Refactored `input/recorder.py` with silence detection
- [x] Created `processing/stt.py` (replaced transcriber.py)
- [x] Supported live mic + language parameter

## ✅ PHASE 3: Multilingual STT
### Done
- [x] Auto language detection
- [x] Handle mixed English/Hindi input
- [x] Retry on unclear audio

## ✅ PHASE 4: NLP – Intent + Entities
### Done
- [x] `processing/nlp.py`
- [x] 4 intents: book, cancel, reschedule, inquiry
- [x] 4 entities: name, doctor, date, time

## ✅ PHASE 5: Dialogue Manager
### Done
- [x] `processing/dialogue.py`
- [x] State machine: GREETING → INTENT_CAPTURE → SLOT_FILLING → CONFIRMATION → ACTION → FAREWELL

## ✅ PHASE 6: Appointments + Database
### Done
- [x] `database.py` with SQLite schema
- [x] `services/appointments.py` – booking engine

## ✅ PHASE 7: Text-to-Speech
### Done
- [x] `processing/tts.py` using pyttsx3
- [x] Integrated into `main.py`

## ✅ PHASE 8: Admin Dashboard
### Done
- [x] `api/server.py` – FastAPI REST endpoints
- [x] `admin/` – HTML/CSS/JS web UI

## ✅ PHASE 9: Testing & Edge Cases
### Done
- [x] Full test suite in `tests/`
- [x] Edge case handling

## ✅ PHASE 10: Deployment & API Migration
### Done
- [x] Updated `config.py` – added Twilio, OpenAI Whisper, ElevenLabs, PUBLIC_BASE_URL keys
- [x] Updated `.env` – Phase 10 placeholder keys with comments
- [x] `processing/stt.py` – added `transcribe_audio_bytes()` with OpenAI Whisper API → local faster-whisper fallback
- [x] `processing/tts.py` – rewritten with provider chain: ElevenLabs → gTTS+pygame → pyttsx3; added `synthesize_bytes()` for Twilio
- [x] `api/twilio_webhook.py` – Twilio Voice webhook (incoming, gather, status, audio serving)
- [x] `api/server.py` – mounted Twilio router, added `/health`, slowapi rate limiting, tightened CORS
- [x] `requirements.txt` – added twilio, openai, elevenlabs, gtts, pygame, slowapi
- [x] `Procfile` – Railway/Render start command
- [x] `railway.json` – Railway deployment config with health check
- [x] `Dockerfile` – multi-stage production container
