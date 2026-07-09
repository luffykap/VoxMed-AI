import speech_recognition as sr

import config
from utils.logger import get_logger

logger = get_logger(__name__)

_recognizer = sr.Recognizer()

_DEFAULT_LANG = config.SUPPORTED_LANGUAGES[config.DEFAULT_LANGUAGE]["code"]


def transcribe(language: str = _DEFAULT_LANG) -> dict:
    """
    Capture audio from the microphone and transcribe it.

    Parameters
    ----------
    language : BCP-47 code from config.SUPPORTED_LANGUAGES, e.g. "hi-IN".
               Defaults to config.DEFAULT_LANGUAGE.

    Returns
    -------
    dict with keys: text, language, confidence.
    """
    logger.info("Transcription started | language=%s", language)

    try:
        with sr.Microphone() as source:
            logger.info("Adjusting for ambient noise...")
            _recognizer.adjust_for_ambient_noise(source, duration=2)
            logger.info("Listening...")
            audio = _recognizer.listen(source)

        logger.info("Audio captured | transcribing...")

        result = _recognizer.recognize_google(
            audio,
            language=language,
            show_all=True,
        )

        if not result or "alternative" not in result:
            logger.warning("No transcription result returned")
            return {"text": "", "language": language, "confidence": 0.0}

        best       = result["alternative"][0]
        text       = best.get("transcript", "")
        confidence = round(best.get("confidence", 0.0), 4)

        logger.info(
            "Transcription completed | lang=%s | confidence=%.2f | text_length=%d",
            language, confidence, len(text),
        )

        return {"text": text, "language": language, "confidence": confidence}

    except sr.UnknownValueError:
        logger.warning("Speech not understood | language=%s", language)
        return {"text": "", "language": language, "confidence": 0.0}

    except sr.RequestError as e:
        logger.error("Speech API request failed | error=%s", str(e))
        return {"text": "", "language": language, "confidence": 0.0}

    except Exception:
        logger.exception("Unexpected error during transcription")
        return {"text": "", "language": language, "confidence": 0.0}
