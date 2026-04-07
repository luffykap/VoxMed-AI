# VoxMed AI – Full-Fledged Implementation Plan

> **Project**: VoxMed AI – Multilingual Voice-Based Appointment Booking System  
> **Strategy**: Local-First Development → Cloud API Migration in Phase 10  
> **Created**: 2026-04-07  

---

## 📍 Current State Analysis

| Component | Status | File |
|---|---|---|
| Audio Recorder | ✅ Done | `input/recorder.py` – Records 5s of mic audio to WAV |
| Audio Transcriber | ✅ Done | `processing/transcriber.py` – Converts WAV to text via Google STT |
| Entry Point | ❌ Empty | `main.py` – 0 lines of code |
| Dependencies | ⚠️ Partial | `requirements.txt` – Only `pyaudio`, `SpeechRecognition` |
| NLP / Intent | ❌ Missing | No intent detection or entity extraction |
| Dialogue Manager | ❌ Missing | No conversation state or follow-up logic |
| Database | ❌ Missing | No database, no schema, no models |
| Appointment Engine | ❌ Missing | No booking/cancel/reschedule logic |
| TTS (Voice Response) | ❌ Missing | No text-to-speech |
| Admin Dashboard | ❌ Missing | No web UI |
| Tests | ❌ Missing | No test suite |

**Bottom line**: We are at roughly **10% of Phase 1**. The recorder and transcriber are decent starting points, but everything else — the brain of the system — is yet to be built.

---

## 🏗️ Target Architecture

```
User Layer
├── 🎤 Microphone Input
├── 🔊 Speaker Output
└── 🖥️ Admin Dashboard (Web)

Audio Layer
├── recorder.py     → Capture microphone audio
├── stt.py          → Speech-to-Text (offline/online)
└── tts.py          → Text-to-Speech (offline/online)

AI Layer
├── nlp.py          → Intent Detection + Entity Extraction
└── dialogue.py     → Dialogue State Machine (conversation manager)

Backend Layer
├── appointments.py → Booking Engine (slot check, book, cancel, reschedule)
├── database.py     → SQLite Database (doctors, slots, appointments)
└── api/server.py   → FastAPI REST API (for Admin Dashboard)
```

---

## 📦 Final Project Structure

```
VoxMed-AI/
├── main.py                    # CLI orchestrator (local conversation loop)
├── config.py                  # Centralized configuration & constants
├── database.py                # SQLite setup, models, seed data
├── requirements.txt           # All dependencies
├── PLAN.md                    # This file
├── .env                       # API keys (gitignored)
├── .gitignore
│
├── input/
│   ├── __init__.py
│   └── recorder.py            # ✅ EXISTS – Mic audio capture
│
├── processing/
│   ├── __init__.py
│   ├── transcriber.py         # ✅ EXISTS – Will be refactored into stt.py
│   ├── stt.py                 # Speech-to-Text (replaces transcriber.py)
│   ├── tts.py                 # Text-to-Speech (pyttsx3 → cloud later)
│   ├── nlp.py                 # Intent detection + Entity extraction
│   └── dialogue.py            # Dialogue state machine
│
├── services/
│   ├── __init__.py
│   └── appointments.py        # Appointment booking engine
│
├── api/
│   ├── __init__.py
│   └── server.py              # FastAPI REST API for admin dashboard
│
├── admin/
│   ├── index.html             # Admin dashboard UI
│   ├── style.css
│   └── app.js
│
├── utils/
│   ├── __init__.py
│   ├── logger.py              # Centralized logging
│   └── validators.py          # Input validation helpers
│
├── tests/
│   ├── test_stt.py
│   ├── test_nlp.py
│   ├── test_dialogue.py
│   ├── test_appointments.py
│   └── test_integration.py
│
└── output/                    # Temp audio files, logs
    └── __init__.py
```

---

## 🚀 Phased Implementation Plan

### ☐ PHASE 1: Project Foundation *(~1 session)*

**WHY**: A solid foundation prevents refactoring hell later. We need clean structure, config management, logging, and a working dev environment before writing any AI logic.

**Tasks:**
1. Restructure folders to match the target structure above
2. Create `config.py` with all constants (audio params, DB path, supported languages)
3. Create `utils/logger.py` for structured logging of all interactions
4. Update `requirements.txt` with all local dependencies:
   - `SpeechRecognition`, `pyaudio`, `pyttsx3`, `fastapi`, `uvicorn`, `python-dotenv`
5. Create a Python virtual environment and install dependencies
6. Update `.gitignore` (add `.env`, `*.db`, `output/*.wav`)
7. Wire up `main.py` as a basic CLI loop: Record → Transcribe → Print

**Verification**: Run `python main.py`, speak into mic, see transcribed text printed.

---

### ☐ PHASE 2: Local Microphone Integration *(~1 session)*

**WHY**: In production this will be Twilio receiving a phone call. Locally, we simulate this with the laptop microphone. We need the recorder to be smarter — detecting silence to auto-stop instead of a fixed 5-second window.

**Tasks:**
1. Refactor `input/recorder.py`:
   - Add silence detection (stop recording when user stops speaking)
   - Use configurable parameters from `config.py`
   - Return audio data directly (not just save to file)
2. Create `processing/stt.py` by refactoring `transcriber.py`:
   - Support live microphone input (not just file-based)
   - Add language parameter (`en-US`, `hi-IN`)
   - Return structured result: `{ text, language, confidence }`
3. Update `main.py` to use the new modules

**Verification**: Speak in English and Hindi separately, see correct transcriptions.

---

### ☐ PHASE 3: Speech-to-Text with Multilingual Support *(~1 session)*

**WHY**: Multilingual support is a core requirement. The system needs to process English and Hindi, and handle mixed-language input.

**Tasks:**
1. Add language detection in `stt.py` (try both languages, pick highest confidence)
2. Create a language prompt at the start: "Press 1 for English, 2 for Hindi"
3. Handle mixed-language input gracefully (e.g., "Mujhe appointment chahiye for Dr. Sharma")
4. Add error handling for unclear/noisy audio with retry mechanism (max 3 retries)

**Verification**: Test with English, Hindi, and mixed-language sentences.

---

### ☐ PHASE 4: NLP – Intent Detection & Entity Extraction *(~2 sessions)*

**WHY**: This is the "brain" of VoxMed AI. Without it, we're just a transcription tool. The NLP module must understand WHAT the patient wants and extract the key details.

**Intents to support:**
- `book_appointment`
- `cancel_appointment`
- `reschedule_appointment`
- `general_inquiry`

**Entities to extract:**
- `patient_name`
- `doctor_name` / `specialization`
- `date` (handle "tomorrow", "next Monday", "15th April")
- `time` (handle "morning", "3 PM", "afternoon")

**Output format:**
```python
{
  "intent": "book_appointment",
  "confidence": 0.85,
  "entities": {
    "doctor": "Dr. Sharma",
    "date": "2026-04-10",
    "time": "15:00",
    "patient_name": None   # Missing → triggers follow-up question
  },
  "missing_entities": ["patient_name"]
}
```

**Tasks:**
1. Create `processing/nlp.py` with keyword/regex-based approach (no external API)
2. Design with an abstract interface so we can swap in OpenAI/Rasa later
3. Handle both English and Hindi keywords

**Verification**: Feed 20+ test sentences, validate intent + entities are correctly extracted.

---

### ☐ PHASE 5: Dialogue Manager *(~2 sessions)*

**WHY**: Real conversations aren't one-shot. A patient might say "Book an appointment" without specifying a doctor or date. The dialogue manager must track state and ask the right follow-up question.

**State Machine:**
```
GREETING → INTENT_CAPTURE → SLOT_FILLING → CONFIRMATION → ACTION → FAREWELL
```

**Tasks:**
1. Create `processing/dialogue.py` with conversation state machine
2. Implement intelligent slot-filling logic per intent
3. Generate dynamic follow-up prompts for missing entities
4. Add confirmation step before executing any action
5. Support English and Hindi response templates

**Verification**: Simulate multi-turn conversation via text input, verify all state transitions.

---

### ☐ PHASE 6: Appointment System & Database *(~2 sessions)*

**WHY**: This is the core business logic. We need persistent storage for doctors, time slots, and appointments with proper validation to prevent double-bookings.

**Database Schema (SQLite):**
```sql
doctors:      id, name, specialization, language
slots:        id, doctor_id, date, start_time, end_time, is_available
appointments: id, patient_name, patient_phone, doctor_id, slot_id,
              status (booked/cancelled/rescheduled), created_at
```

**Tasks:**
1. Create `database.py` with setup, models, and seed data (5 doctors, 7-day slots)
2. Create `services/appointments.py`:
   - `check_availability(doctor_id, date)` → list of open slots
   - `book_appointment(patient_name, doctor_id, slot_id)` → confirmation
   - `cancel_appointment(appointment_id)` → cancellation
   - `reschedule_appointment(appointment_id, new_slot_id)` → updated booking
3. Prevent duplicate bookings (check before inserting)
4. Wire dialogue manager → appointment service

**Verification**: Book, cancel, and reschedule via CLI. Verify DB state with sqlite3.

---

### ☐ PHASE 7: Text-to-Speech *(~1 session)*

**WHY**: The system must speak back to the patient. We've been printing text so far — now we add voice responses.

**Tasks:**
1. Create `processing/tts.py` using `pyttsx3`:
   - English and Hindi voice selection
   - Configurable speech rate and volume from `config.py`
   - Function: `speak(text, language="en")` → plays audio through speakers
2. Integrate into `main.py` loop: every system response is spoken aloud
3. Fallback: if TTS fails, print the response to terminal

**Verification**: Run a full conversation loop with voice input AND voice output end-to-end.

---

### ☐ PHASE 8: Admin Dashboard *(~2 sessions)*

**WHY**: Doctors and admins need a way to view bookings and manage slots without touching code.

**Tasks:**
1. Create `api/server.py` using FastAPI:
   - `GET /api/appointments` – list all (with filters)
   - `GET /api/appointments/{id}` – single detail
   - `GET /api/doctors` – list doctors
   - `GET /api/slots?doctor_id=X&date=Y` – available slots
   - `POST /api/slots` – add new slot (admin)
   - `DELETE /api/appointments/{id}` – cancel (admin)
   - Basic auth middleware (credentials from `.env`)
2. Create `admin/` web dashboard (HTML + CSS + JS, no framework):
   - Login page
   - Dashboard with today's appointment count
   - Appointment table with search and filter
   - Slot management: add/remove slots per doctor

**Verification**: Start FastAPI server, open dashboard in browser, perform all CRUD operations.

---

### ☐ PHASE 9: Testing & Edge Cases *(~2 sessions)*

**WHY**: A medical system must be reliable. Edge cases can derail real-world usage and erode trust.

**Test Suite (`tests/`):**
- `test_stt.py` – test with sample audio files
- `test_nlp.py` – test intent detection accuracy across 30+ sentences
- `test_dialogue.py` – test state machine transitions
- `test_appointments.py` – test booking logic and duplicate prevention
- `test_integration.py` – end-to-end CLI test

**Edge Cases to Handle:**
- Noisy audio → ask user to repeat (max 3 retries)
- Ambiguous dates ("next week") → ask for specific date
- No available slots → suggest next available slot
- Mid-conversation language switch
- Booking for a past date → validation error
- System timeout (user goes silent)
- API downtime → graceful fallback message

**Verification**: All tests pass. Manual testing of all edge cases documented.

---

### ☐ PHASE 10: Deployment & API Migration *(~2 sessions)*

**WHY**: This is where we go from local prototype to production telephony.

**Tasks:**
1. Swap `input/recorder.py` → Twilio Voice webhook (receive phone audio stream)
2. Swap `processing/stt.py` → OpenAI Whisper API or Google Cloud STT
3. Swap `processing/tts.py` → Google Cloud TTS or Amazon Polly
4. Optionally swap `processing/nlp.py` → OpenAI GPT-4o for richer NLP
5. Deploy FastAPI backend (Railway / Render / AWS)
6. Configure Twilio phone number → webhook URL
7. Add HTTPS, rate limiting, and proper authentication
8. Set up monitoring and alerting

**Verification**: Call the live phone number, complete a full booking flow, verify in admin dashboard.

---

## 📊 Effort Estimate

| Phase | Description | Effort |
|---|---|---|
| Phase 1 | Project Foundation | ~1 session |
| Phase 2 | Mic Integration | ~1 session |
| Phase 3 | Multilingual STT | ~1 session |
| Phase 4 | NLP Brain | ~2 sessions |
| Phase 5 | Dialogue Manager | ~2 sessions |
| Phase 6 | Appointments + DB | ~2 sessions |
| Phase 7 | Text-to-Speech | ~1 session |
| Phase 8 | Admin Dashboard | ~2 sessions |
| Phase 9 | Testing | ~2 sessions |
| Phase 10 | Deployment | ~2 sessions |
| **Total** | | **~16 sessions** |

---

## 🛠️ Local Tech Stack

| Component | Local Tool | Production Equivalent |
|---|---|---|
| Backend | Python scripts + FastAPI | FastAPI on cloud server |
| Database | SQLite | PostgreSQL |
| STT | SpeechRecognition (Google) | OpenAI Whisper / Google Cloud STT |
| TTS | pyttsx3 | Google Cloud TTS / Amazon Polly |
| NLP | Regex/keyword heuristics | OpenAI GPT-4o / Dialogflow |
| Telephony | Laptop microphone/speaker | Twilio Voice API |
