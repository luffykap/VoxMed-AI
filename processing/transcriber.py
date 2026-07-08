import speech_recognition as sr

from utils.logger import get_logger

logger = get_logger(__name__)


def transcribe_audio(filename="test_output.wav") -> str:
    recognizer = sr.Recognizer()

    logger.info("Transcription started | file=%s", filename)

    try:
        with sr.AudioFile(filename) as source:
            audio = recognizer.record(source)

        logger.info("Audio loaded successfully | file=%s", filename)

        text = recognizer.recognize_google(audio)

        if not text.strip():
            logger.warning("Empty transcription result | file=%s", filename)
            return ""

        logger.info(
            "Transcription successful | file=%s | text_length=%s",
            filename,
            len(text)
        )

        return text

    except sr.UnknownValueError:
        logger.warning(
            "Speech not understood | file=%s | possible unclear audio",
            filename
        )
        return ""

    except sr.RequestError as e:
        logger.error(
            "Google Speech API request failed | file=%s | error=%s",
            filename,
            str(e)
        )
        return ""

    except FileNotFoundError:
        logger.error("Audio file not found | file=%s", filename)
        return ""

    except Exception:
        logger.exception("Unexpected error during transcription | file=%s", filename)
        return ""


if __name__ == "__main__":
    text = transcribe_audio()
    print(f"Transcribed: {text}")