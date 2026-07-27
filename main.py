import config
from processing.stt import transcribe
from processing.orchestrator import ConversationOrchestrator
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
        print(f"\nSelected: {config.SUPPORTED_LANGUAGES[lang_choice]['name']}")
    else:
        lang_code = None
        print("\nSelected: Auto-Detect")

    dm = ConversationOrchestrator(language=lang_code or "en")

    # Initial greeting
    _print_divider()
    print(f"\n[AI]: {dm.get_greeting()}\n")

    while not dm.is_finished:
        print("\nPress Enter to start speaking...")
        input()

        # ── Stage 1: Record + Transcribe ─────────────────────────────────────
        logger.info("Pipeline started")
        stt_result = transcribe(language=lang_code)

        if not stt_result["text"]:
            print("\n[AI]: Could not transcribe audio. Please try again.")
            continue

        if stt_result["confidence"] < 0.4:
            print("\n[AI]: Low confidence transcription. Please speak clearly and try again.")
            continue

        print(f"\n[You]: {stt_result['text']}")

        # ── Stage 2 & 3: Orchestrator (LLM understand + reply) ───────────────
        ai_response = dm.process(stt_result["text"])
        
        _print_divider()
        print(f"\n[AI]: {ai_response}\n")

    print("\nConversation ended. Goodbye!")

if __name__ == "__main__":
    main()
