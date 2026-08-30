import config
from processing.stt import transcribe
from processing.orchestrator import ConversationOrchestrator
from processing.tts import speak
from utils.logger import get_logger

logger = get_logger(__name__)

_PHRASES = {
    "en": {
        "trouble_hearing": "I'm having trouble hearing you. Please try calling back later.",
        "could_not_transcribe": "Could not transcribe audio. Please try again.",
        "trouble_understanding": "I'm having trouble understanding you. Please try calling back later.",
        "didn_t_catch_that": "I didn't quite catch that. Please speak clearly and try again."
    },
    "hi": {
        "trouble_hearing": "मुझे आपको सुनने में परेशानी हो रही है। कृपया बाद में कॉल करें।",
        "could_not_transcribe": "ऑडियो ट्रांसक्राइब नहीं किया जा सका। कृपया पुनः प्रयास करें।",
        "trouble_understanding": "मुझे आपकी बात समझने में परेशानी हो रही है। कृपया बाद में कॉल करें।",
        "didn_t_catch_that": "मुझे सुनाई नहीं दिया। कृपया स्पष्ट रूप से बोलें और पुनः प्रयास करें।"
    },
    "kn": {
        "trouble_hearing": "ನನಗೆ ಕೇಳಿಸುತ್ತಿಲ್ಲ. ದಯವಿಟ್ಟು ನಂತರ ಮತ್ತೆ ಕರೆ ಮಾಡಿ.",
        "could_not_transcribe": "ಆಡಿಯೋವನ್ನು ಲಿಪ್ಯಂತರ ಮಾಡಲು ಸಾಧ್ಯವಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ.",
        "trouble_understanding": "ನನಗೆ ಅರ್ಥವಾಗುತ್ತಿಲ್ಲ. ದಯವಿಟ್ಟು ನಂತರ ಮತ್ತೆ ಕರೆ ಮಾಡಿ.",
        "didn_t_catch_that": "ನನಗೆ ಕೇಳಿಸಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಸ್ಪಷ್ಟವಾಗಿ ಮಾತನಾಡಿ ಮತ್ತೆ ಪ್ರಯತ್ನಿಸಿ."
    },
    "te": {
        "trouble_hearing": "నాకు మీ మాటలు వినపడటం లేదు. దయచేసి తర్వాత మళ్లీ కాల్ చేయండి.",
        "could_not_transcribe": "ఆడియోను లిప్యంతరీకరించడం సాధ్యం కాలేదు. దయచేసి మళ్లీ ప్రయత్నించండి.",
        "trouble_understanding": "మీ మాటలు నాకు అర్థం కాలేదు. దయచేసి తర్వాత మళ్లీ కాల్ చేయండి.",
        "didn_t_catch_that": "నాకు సరిగ్గా వినపడలేదు. దయచేసి స్పష్టంగా మళ్లీ చెప్పండి."
    }
}


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
        lang_code = "en"
        lang_name = "English"
        print("\nSelected: Auto-Detect (Defaulting to English)")

    auto_detect = (lang_choice == "0" or lang_choice == "")

    dm = ConversationOrchestrator(language=lang_name)

    # Initial greeting
    _print_divider()
    greeting = dm.get_greeting(lang_code)
    print(f"\n[AI]: {greeting}\n")
    speak(greeting, language=lang_code or "en")

    retry_count = 0
    
    while not dm.is_finished:
        print("\n[Listening...]")

        # ── Stage 1: Record + Transcribe ─────────────────────────────────────
        logger.info("Pipeline started")
        stt_result = transcribe(language=lang_code)
        
        # Ensure lang_code defaults to 'en' for dict lookup if auto_detect fails
        safe_lang_code = lang_code if lang_code in _PHRASES else "en"

        if not stt_result["text"]:
            retry_count += 1
            if retry_count >= 3:
                msg = _PHRASES[safe_lang_code]["trouble_hearing"]
                print(f"\n[AI]: {msg}\n")
                speak(msg, language=lang_code or "en")
                break
            msg = _PHRASES[safe_lang_code]["could_not_transcribe"]
            print(f"\n[AI]: {msg}")
            speak(msg, language=lang_code or "en")
            continue

        if stt_result["confidence"] < 0.15:  # tiny model produces lower scores than large-v3
            retry_count += 1
            if retry_count >= 3:
                msg = _PHRASES[safe_lang_code]["trouble_understanding"]
                print(f"\n[AI]: {msg}\n")
                speak(msg, language=lang_code or "en")
                break
            msg = _PHRASES[safe_lang_code]["didn_t_catch_that"]
            print(f"\n[AI]: {msg}")
            speak(msg, language=lang_code or "en")
            continue

        # If user selected Auto-Detect, update the language dynamically.
        # Only accept the detected language if it's one we support AND
        # the tiny model is confident enough (prob > 0.85) to avoid
        # misclassifying Indian-accent English as Swahili / French / etc.
        if auto_detect:
            detected_lang = stt_result.get("language")
            lang_prob      = stt_result.get("lang_prob", 0)
            _SUPPORTED_ISO = {"en", "hi", "kn", "te"}
            if detected_lang and detected_lang in _SUPPORTED_ISO:
                iso_to_name = {"en": "English", "hi": "Hindi", "kn": "Kannada", "te": "Telugu"}
                lang_code = detected_lang
                dm.memory.language = iso_to_name.get(detected_lang, "English")


        print(f"\n[You]: {stt_result['text']}")

        # ── Stage 2 & 3: Orchestrator (LLM understand + reply) ───────────────
        retry_count = 0  # Reset on successful transcription
        ai_response = dm.process(stt_result["text"])
        
        _print_divider()
        print(f"\n[AI]: {ai_response}\n")
        speak(ai_response, language=lang_code or "en")

    print("\nConversation ended. Goodbye!")

if __name__ == "__main__":
    main()
