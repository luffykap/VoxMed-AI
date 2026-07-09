# config.py – Central configuration for VoxMed AI
import os
from pathlib import Path

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
AUDIO_DURATION = 5          # Default recording duration in seconds
TEMP_AUDIO_FILE = str(OUTPUT_DIR / "temp_recording.wav")
SILENCE_THRESHOLD = 500     # RMS amplitude below which audio is considered silent
SILENCE_DURATION  = 2.0     # Seconds of continuous silence before recording stops

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

# ── Admin API ──────────────────────────────────────────────────
API_HOST = "127.0.0.1"
API_PORT = 8000
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "voxmed123")
