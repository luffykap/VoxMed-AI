import config
from processing.stt import transcribe
from processing.orchestrator import ConversationOrchestrator
from processing.tts import speak
from utils.logger import get_logger

logger = get_logger(__name__)


def _print_divider():
    print("-" * 50)


def main():
    print("=== VoxMed AI ===")
    print("Select Language:")
    print("[0] Auto-Detect")
    for key, lang_info in config.SUPPORTED_LANGUAGES.items():
        print(f"[{key}] {lang_info['name']}")

    lang_choice = input("Choice [0]: ").strip()

    if lang_choice in config.SUPPORTED_LANGUAGES:
        lang_code = config.SUPPORTED_LANGUAGES[lang_choice]["code"].split("-")[0]
        lang_name = config.SUPPORTED_LANGUAGES[lang_choice]["name"]
        print(f"\nSelected: {lang_name}")
    else:
        lang_code = None
        lang_name = "English"
        print("\nSelected: Auto-Detect")

    auto_detect = (lang_code is None)

    dm = ConversationOrchestrator(language=lang_name)

    # Initial greeting
    _print_divider()
    greeting = dm.get_greeting()
    print(f"\n[AI]: {greeting}\n")
    speak(greeting, language=lang_code or "en")

    retry_count = 0
    
    while not dm.is_finished:
        print("\n[Listening...]")

        # ── Stage 1: Record + Transcribe ─────────────────────────────────────
        logger.info("Pipeline started")
        stt_result = transcribe(language=lang_code)

        if not stt_result["text"]:
            retry_count += 1
            if retry_count >= 3:
                msg = "I'm having trouble hearing you. Please try calling back later."
                print(f"\n[AI]: {msg}\n")
                speak(msg, language=lang_code or "en")
                break
            msg = "Could not transcribe audio. Please try again."
            print(f"\n[AI]: {msg}")
            speak(msg, language=lang_code or "en")
            continue

        if stt_result["confidence"] < 0.4:
            retry_count += 1
            if retry_count >= 3:
                msg = "I'm having trouble understanding you. Please try calling back later."
                print(f"\n[AI]: {msg}\n")
                speak(msg, language=lang_code or "en")
                break
            msg = "I didn't quite catch that. Please speak clearly and try again."
            print(f"\n[AI]: {msg}")
            speak(msg, language=lang_code or "en")
            continue

        # If user selected Auto-Detect, update the language dynamically
        if auto_detect:
            detected_lang = stt_result.get("language")
            if detected_lang:
                iso_to_name = {"en": "English", "hi": "Hindi", "kn": "Kannada", "te": "Telugu"}
                lang_code = detected_lang
                dm.memory.language = iso_to_name.get(detected_lang, "English")

        print(f"\n[You]: {stt_result['text']}")

        # ── Stage 2 & 3: Orchestrator (LLM understand + reply) ───────────────
        ai_response = dm.process(stt_result["text"])
        
        _print_divider()
        print(f"\n[AI]: {ai_response}\n")
        speak(ai_response, language=lang_code or "en")

    print("\nConversation ended. Goodbye!")

if __name__ == "__main__":
    main()
