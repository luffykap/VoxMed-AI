# VoxMed AI – Task Progress Tracker

> Last updated: 2026-05-22  
> Current Phase: **Phase 1 – Project Foundation**  

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

### 🔲 Remaining in Phase 1 (Pick up here next time)

**Step 7 – Create stub files** ← NEXT UP
> Run this in terminal to create all empty module files:
```bash
cd /Users/kapilpal/voxmed/VoxMed-AI

# Processing stubs
touch processing/stt.py
touch processing/tts.py
touch processing/nlp.py
touch processing/dialogue.py

# Service and API stubs
touch services/appointments.py
touch api/server.py

# Verify all files exist
find . -name "*.py" | grep -v __pycache__ | grep -v .git | sort
```

---

**Step 8 – Wire up `main.py`** ← Phase 1 final milestone
> Connect recorder → transcriber → print in a simple CLI loop.
> This is the first time the full audio pipeline runs end-to-end!
> Code will be provided when we reach this step.

---

## 🔲 PHASE 2: Microphone Integration (Not Started)
- Refactor `input/recorder.py` with silence detection
- Create `processing/stt.py` (replace transcriber.py)
- Support live mic + language parameter

## 🔲 PHASE 3: Multilingual STT (Not Started)
- Auto language detection
- Handle mixed English/Hindi input
- Retry on unclear audio

## 🔲 PHASE 4: NLP – Intent + Entities (Not Started)
- `processing/nlp.py`
- 4 intents: book, cancel, reschedule, inquiry
- 4 entities: name, doctor, date, time

## 🔲 PHASE 5: Dialogue Manager (Not Started)
- `processing/dialogue.py`
- State machine: GREETING → INTENT_CAPTURE → SLOT_FILLING → CONFIRMATION → ACTION → FAREWELL

## 🔲 PHASE 6: Appointments + Database (Not Started)
- `database.py` with SQLite schema
- `services/appointments.py` – booking engine

## 🔲 PHASE 7: Text-to-Speech (Not Started)
- `processing/tts.py` using pyttsx3

## 🔲 PHASE 8: Admin Dashboard (Not Started)
- `api/server.py` – FastAPI REST endpoints
- `admin/` – HTML/CSS/JS web UI

## 🔲 PHASE 9: Testing & Edge Cases (Not Started)
- Full test suite in `tests/`
- Edge case handling

## 🔲 PHASE 10: Deployment & API Migration (Not Started)
- Twilio integration
- Cloud STT/TTS APIs
- Production deployment
