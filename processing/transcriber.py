import speech_recognition as sr


def transcribe_audio(filename="test_output.wav") -> str:
    """
    Transcribe audio from a WAV file to text using Google's speech recognition API.
    
    Args:
        filename: Path to the WAV file to transcribe (default: test_output.wav)
    
    Returns:
        Transcribed text as a string, or empty string if transcription fails
    """
    recognizer = sr.Recognizer()
    
    try:
        with sr.AudioFile(filename) as source:
            audio = recognizer.record(source)
        
        text = recognizer.recognize_google(audio)
        return text
    
    except sr.UnknownValueError:
        print("Error: Audio could not be understood. Please speak clearly.")
        return ""
    
    except sr.RequestError as e:
        print(f"Error: Could not request results from Google Speech Recognition API; {e}")
        return ""
    
    except FileNotFoundError:
        print(f"Error: Audio file '{filename}' not found.")
        return ""
    
    except Exception as e:
        print(f"Error: An unexpected error occurred during transcription: {e}")
        return ""


if __name__ == "__main__":
    text = transcribe_audio()
    print(f"Transcribed: {text}")