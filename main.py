from input.recorder import record_audio
from processing.transcriber import transcribe_audio

def main():
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
