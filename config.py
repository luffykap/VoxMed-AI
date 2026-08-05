# config.py – Central configuration for VoxMed AI
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()  # loads .env from the project root into os.environ

# ── Project Paths ──────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = BASE_DIR / "voxmed.db"

# ── Audio Settings ─────────────────────────────────────────────
AUDIO_CHUNK = 1024          # Number of frames per buffer
AUDIO_FORMAT_NAME = "int16" # Will be mapped to pyaudio format
AUDIO_CHANNELS = 1          # Mono audio (1 channel)
AUDIO_RATE = 44100          # Sample rate in Hz
AUDIO_DURATION = 50         # Default recording duration in seconds
TEMP_AUDIO_FILE = str(OUTPUT_DIR / "temp_recording.wav")
SILENCE_THRESHOLD = 500     # RMS amplitude below which audio is considered silent
SILENCE_DURATION  = 6.0     # Seconds of continuous silence before recording stops

# ── Language Settings ──────────────────────────────────────────
SUPPORTED_LANGUAGES = {
    "1": {"code": "en-US", "name": "English", "tts_lang": "en"},
    "2": {"code": "hi-IN", "name": "Hindi",   "tts_lang": "hi"},
    "3": {"code": "kn",    "name": "Kannada", "tts_lang": "kn"},
    "4": {"code": "te",    "name": "Telugu",  "tts_lang": "te"},
}
DEFAULT_LANGUAGE = "1"  # English

# ── Whisper (faster-whisper) ───────────────────────────────────
WHISPER_MODEL        = "large-v3"  # Options: large-v3, turbo, medium, small
WHISPER_DEVICE       = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

# ── Database Settings ──────────────────────────────────────────
DB_ECHO = False  # Set True to log all SQL queries (for debugging)

# ── Logging ────────────────────────────────────────────────────
LOG_FILE = str(BASE_DIR / "output" / "voxmed.log")
LOG_LEVEL = "INFO"

# ── Symptom → Department mapping ─────────────────────────────
# Each symptom keyword maps to (department_name, priority).
# When symptoms span multiple departments, highest total priority wins.
# Priority scale: higher number = more specific / urgent department.
SYMPTOM_TO_DEPARTMENT: dict[str, tuple[str, int]] = {
    # General Medicine (priority 1 — catch-all)
    "fever":           ("General Medicine", 1),
    "cold":            ("General Medicine", 1),
    "cough":           ("General Medicine", 1),
    "body ache":       ("General Medicine", 1),
    "weakness":        ("General Medicine", 1),
    "fatigue":         ("General Medicine", 1),
    "headache":        ("General Medicine", 1),
    "vomiting":        ("General Medicine", 1),
    "nausea":          ("General Medicine", 1),
    "diarrhea":        ("General Medicine", 1),
    "stomach ache":    ("General Medicine", 1),
    "loss of appetite": ("General Medicine", 1),
    # Cardiology (priority 3)
    "chest pain":      ("Cardiology", 3),
    "palpitations":    ("Cardiology", 3),
    "heart palpitation": ("Cardiology", 3),
    "breathlessness":  ("Cardiology", 3),
    "shortness of breath": ("Cardiology", 3),
    "irregular heartbeat": ("Cardiology", 3),
    # Dermatology (priority 2)
    "skin rash":       ("Dermatology", 2),
    "itching":         ("Dermatology", 2),
    "acne":            ("Dermatology", 2),
    "eczema":          ("Dermatology", 2),
    "psoriasis":       ("Dermatology", 2),
    "hair loss":       ("Dermatology", 2),
    # Orthopedics (priority 2)
    "joint pain":      ("Orthopedics", 2),
    "fracture":        ("Orthopedics", 2),
    "back pain":       ("Orthopedics", 2),
    "knee pain":       ("Orthopedics", 2),
    "bone pain":       ("Orthopedics", 2),
    "muscle pain":     ("Orthopedics", 2),
    # Ophthalmology (priority 2)
    "eye pain":        ("Ophthalmology", 2),
    "blurred vision":  ("Ophthalmology", 2),
    "red eye":         ("Ophthalmology", 2),
    "watery eyes":     ("Ophthalmology", 2),
    # ENT (priority 2)
    "ear pain":        ("ENT", 2),
    "hearing loss":    ("ENT", 2),
    "sore throat":     ("ENT", 2),
    "runny nose":      ("ENT", 2),
    "nasal congestion": ("ENT", 2),
    # Neurology (priority 3)
    "seizure":         ("Neurology", 3),
    "numbness":        ("Neurology", 3),
    "memory loss":     ("Neurology", 3),
    "dizziness":       ("Neurology", 2),
    "migraine":        ("Neurology", 2),
    # Gastroenterology (priority 2)
    "bloating":        ("Gastroenterology", 2),
    "acid reflux":     ("Gastroenterology", 2),
    "constipation":    ("Gastroenterology", 2),
    "abdominal pain":  ("Gastroenterology", 2),
}
DEFAULT_DEPARTMENT = "General Medicine"

# ── Admin API ──────────────────────────────────────────────────
API_HOST = "127.0.0.1"
API_PORT = 8000
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "voxmed123")

# ── LLM — shared settings ──────────────────────────────────────
LLM_TEMPERATURE               = 0.1
LLM_MAX_TOKENS_UNDERSTANDING  = 250
LLM_MAX_TOKENS_RESPONSE       = 80
LLM_TIMEOUT                   = 30

# ── LLM — per-provider credentials ────────────────────────────
# Each entry: (env_var_name, base_url, model)
LLM_PROVIDERS: dict[str, dict] = {
    "gemini": {
        "api_key":  os.getenv("GEMINI_API_KEY", ""),
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model":    "gemini-3.6-flash",
    },
    "groq": {
        "api_key":  os.getenv("GROQ_API_KEY", ""),
        "base_url": "https://api.groq.com/openai/v1",
        "model":    "llama-3.3-70b-versatile",
    },
    "cerebras": {
        "api_key":  os.getenv("CEREBRAS_API_KEY", ""),
        "base_url": "https://api.cerebras.ai/v1",
        "model":    "gpt-oss-120b",
    },
    "mistral": {
        "api_key":  os.getenv("MISTRAL_API_KEY", ""),
        "base_url": "https://api.mistral.ai/v1",
        "model":    "mistral-small-latest",
    },
    "openrouter": {
        "api_key":  os.getenv("OPENROUTER_API_KEY", ""),
        "base_url": "https://openrouter.ai/api/v1",
        "model":    "google/gemma-4-26b-a4b-it:free",
    },
}

# Failover order: Gemini → Groq → Cerebras → Mistral → OpenRouter
LLM_PROVIDER_CHAIN: list[str] = ["gemini", "groq", "cerebras", "mistral", "openrouter"]
