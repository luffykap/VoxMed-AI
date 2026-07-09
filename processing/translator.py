from deep_translator import GoogleTranslator
from utils.logger import get_logger

logger = get_logger(__name__)

def translate_to_english(text: str, source_lang_code: str) -> str:
    """Translates text from source language to English."""
    if not text:
        return ""
        
    # Extract language prefix (e.g. 'kn' from 'kn-IN')
    lang_prefix = source_lang_code.split('-')[0]
    
    # English needs no translation
    if lang_prefix == "en":
        return text
        
    logger.info("Translation started | from=%s | text_length=%s", lang_prefix, len(text))
    
    try:
        translator = GoogleTranslator(source=lang_prefix, target='en')
        translated = translator.translate(text)
        
        logger.info("Translation successful | result=%s", translated)
        return translated
    except Exception as e:
        logger.exception("Translation failed | error=%s", str(e))
        return text  # Fallback to original text if translation fails
