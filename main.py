from processing.stt import transcribe


def main():
    while True:
        print("\nPress Enter to start recording...")
        input()

        result = transcribe()

        print(f"\nLanguage   : {result['language']}")
        print(f"Confidence : {result['confidence']:.2%}")
        print(f"Transcript : {result['text']}\n")

        choice = input("Record again? (y/n): ").strip().lower()

        if choice != "y":
            print("Goodbye!")
            break


if __name__ == "__main__":
    main()