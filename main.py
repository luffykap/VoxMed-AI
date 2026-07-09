from processing.stt import transcribe


def main():
    print("=== VoxMed AI ===")
    print("Select Language:")
    for key, lang_info in config.SUPPORTED_LANGUAGES.items():
        print(f"[{key}] {lang_info['name']}")
    
    lang_choice = input(f"Choice [{config.DEFAULT_LANGUAGE}]: ").strip()
    if lang_choice not in config.SUPPORTED_LANGUAGES:
        lang_choice = config.DEFAULT_LANGUAGE
        
    lang_code = config.SUPPORTED_LANGUAGES[lang_choice]["code"]
    lang_name = config.SUPPORTED_LANGUAGES[lang_choice]["name"]
    print(f"\nSelected language: {lang_name}")

    while True:
        print("\nPress Enter to start recording...")
        input()

        record_audio()

        text = transcribe_audio()

        print(f"\nYou said: {text}\n")

        choice = input("Record again? (y/n): ").strip().lower()

        if choice != "y":
            print("Goodbye!")
            break


if __name__ == "__main__":
    main()
