"""
chat_test.py
------------
Interactive text test for the VoxMed AI conversation flow.
Type your patient messages at the prompt; the AI replies in text
and optionally speaks them aloud via pyttsx3.

Usage:
    python chat_test.py                   # text only
    python chat_test.py --tts             # text + voice output
    python chat_test.py --lang hi --tts   # Hindi replies with voice

Commands during the conversation:
    quit / exit / q   → end the session
    /reset            → start a fresh conversation
    /memory           → print current memory state (intent, entities, turns)
    /entities         → print only extracted entities so far
    /tts              → toggle voice output on/off mid-conversation
"""

import argparse
import sys

# ── Bootstrap so imports resolve from the project root ────────────────────────
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config  # loads .env via load_dotenv()
from processing.orchestrator import ConversationOrchestrator

# TTS is optional — if the module fails to load, voice output is silently disabled.
try:
    from processing.tts import speak as _speak
    _TTS_AVAILABLE = True
except Exception:
    _TTS_AVAILABLE = False
    def _speak(text, language="en"):  # no-op fallback
        pass

def speak(text: str, language: str = "en") -> None:
    if _TTS_AVAILABLE:
        _speak(text, language=language)

# ── Colours (graceful fallback on Windows without ANSI) ───────────────────────
try:
    import colorama
    colorama.init()
    AI  = "\033[96m"   # cyan
    YOU = "\033[93m"   # yellow
    SYS = "\033[90m"   # grey
    RST = "\033[0m"
except ImportError:
    AI = YOU = SYS = RST = ""


def _print_memory(dm: ConversationOrchestrator) -> None:
    m = dm.memory
    print(f"\n{SYS}── Memory ──────────────────────────────────────────────")
    print(f"  intent   : {m.intent}")
    print(f"  entities : {m.entities}")
    print(f"  missing  : {m.missing_entities()}")
    print(f"  language : {m.language}")
    print(f"  turns    : {len(m.turns)}")
    for i, t in enumerate(m.turns):
        role = t['role'].upper()
        print(f"    [{i}] {role}: {t['content'][:80]}")
    print(f"────────────────────────────────────────────────────{RST}\n")


def run_session(language: str = "en", tts_enabled: bool = False) -> None:
    print(f"\n{SYS}=== VoxMed AI  –  Text {'+ Voice' if tts_enabled else 'Only'} Mode ==={RST}")
    providers = " -> ".join(config.LLM_PROVIDER_CHAIN)
    print(f"{SYS}Language: {language}  |  Providers: {providers}  |  TTS: {'ON' if tts_enabled else 'OFF'}{RST}")
    print(f"{SYS}Commands: /reset  /memory  /entities  /tts  quit{RST}\n")

    dm = ConversationOrchestrator(language=language)
    greeting = dm.get_greeting()
    print(f"{AI}[AI]: {greeting}{RST}\n")
    if tts_enabled:
        speak(greeting, language=language)

    while not dm.is_finished:
        try:
            raw = input(f"{YOU}[You]: {RST}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{SYS}Session interrupted.{RST}")
            break

        if not raw:
            continue

        # ── Debug / control commands ──────────────────────────────────────────
        lower = raw.lower()
        if lower in ("quit", "exit", "q"):
            print(f"{SYS}Exiting.{RST}")
            break

        if lower == "/reset":
            print(f"{SYS}Starting fresh session…{RST}\n")
            run_session(language, tts_enabled=tts_enabled)
            return

        if lower == "/tts":
            tts_enabled = not tts_enabled
            state = "ON" if tts_enabled else "OFF"
            print(f"{SYS}Voice output toggled: {state}{RST}\n")
            continue

        if lower in ("/memory", "/mem"):
            _print_memory(dm)
            continue

        if lower in ("/entities", "/ents"):
            print(f"\n{SYS}Entities: {dm.memory.entities}{RST}\n")
            continue

        # ── Normal conversation turn ──────────────────────────────────────────
        print(f"{SYS}  [thinking…]{RST}", end="\r")
        response = dm.process(raw)
        print(" " * 20, end="\r")   # clear the "thinking" line
        print(f"{AI}[AI]: {response}{RST}\n")
        if tts_enabled:
            speak(response, language=language)

    if dm.is_finished:
        print(f"\n{SYS}Conversation completed. Run the script again to start a new one.{RST}")


def main() -> None:
    parser = argparse.ArgumentParser(description="VoxMed AI text-mode chat tester")
    parser.add_argument(
        "--lang", default="en",
        help="Reply language code passed to Stage 2 (e.g. en, hi, kn, te). Default: en"
    )
    parser.add_argument(
        "--tts", action="store_true", default=False,
        help="Enable text-to-speech voice output for AI replies"
    )
    args = parser.parse_args()

    # Warn if no provider has an API key configured
    has_any_key = any(
        config.LLM_PROVIDERS.get(name, {}).get("api_key")
        for name in config.LLM_PROVIDER_CHAIN
    )
    if not has_any_key:
        print(
            f"\n{SYS}WARNING: No LLM API keys set -- falling back to regex NLP.\n"
            f"   Add at least one provider key (e.g. GEMINI_API_KEY) to .env for full LLM mode.{RST}\n"
        )

    run_session(language=args.lang, tts_enabled=args.tts)


if __name__ == "__main__":
    main()
