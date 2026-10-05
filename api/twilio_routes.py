from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
import os
from typing import Dict

from processing.orchestrator import ConversationOrchestrator
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

# In-memory store for active calls (CallSid -> ConversationOrchestrator)
active_calls: Dict[str, ConversationOrchestrator] = {}

def get_twiml_response(reply_text: str, is_finished: bool, lang_code: str = "en") -> str:
    """Helper to generate TwiML for a reply and gather more input if needed."""
    response = VoiceResponse()
    
    twilio_lang = "en-US"
    if lang_code.startswith("hi"):
        twilio_lang = "hi-IN"
    elif lang_code.startswith("kn"):
        twilio_lang = "kn-IN" 
    elif lang_code.startswith("te"):
        twilio_lang = "te-IN"
    
    # Speak the AI's reply
    response.say(reply_text, language=twilio_lang)

    if not is_finished:
        gather = Gather(
            input="speech",
            action="/twilio/process",
            method="POST",
            timeout=5,
            speechTimeout="0.8",
            language=twilio_lang
        )
        response.append(gather)
        # If the user stays silent, Twilio will continue past the Gather.
        response.say("Are you still there?", language=twilio_lang)
        response.redirect("/twilio/process_silence")
    else:
        response.hangup()

    return str(response)

_PHRASES = {
    "en": {
        "session_expired": "Sorry, the session has expired. Please call back.",
        "no_audio": "I didn't quite catch that. Could you repeat?",
        "silence": "Please let me know how I can help.",
        "greeting_suffix": "Press 1 for English. Hindi ke liye 2 dabaye. Kannada gaagi 3 otti. Telugu kosam 4 nokkandi."
    },
    "hi": {
        "session_expired": "क्षमा करें, सत्र समाप्त हो गया है। कृपया वापस कॉल करें।",
        "no_audio": "मुझे सुनाई नहीं दिया। कृपया दोहराएं।",
        "silence": "कृपया मुझे बताएं कि मैं आपकी कैसे मदद कर सकता हूं।",
    },
    "kn": {
        "session_expired": "ಕ್ಷಮಿಸಿ, ಸೆಷನ್ ಮುಗಿದಿದೆ. ದಯವಿಟ್ಟು ಮತ್ತೆ ಕರೆ ಮಾಡಿ.",
        "no_audio": "ನನಗೆ ಕೇಳಿಸಲಿಲ್ಲ. ದಯವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳಿ.",
        "silence": "ದಯವಿಟ್ಟು ನಾನು ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು ಎಂದು ತಿಳಿಸಿ.",
    },
    "te": {
        "session_expired": "క్షమించండి, సెషన్ ముగిసింది. దయచేసి తిరిగి కాల్ చేయండి.",
        "no_audio": "నాకు వినపడలేదు. దయచేసి మళ్ళీ చెప్పండి.",
        "silence": "దయచేసి నేను మీకు ఎలా సహాయం చేయగలనో చెప్పండి.",
    }
}

@router.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Webhook triggered when a new call connects."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    logger.info("Incoming Twilio call | CallSid=%s", call_sid)

    response = VoiceResponse()
    
    gather_emergency = Gather(
        numDigits=1,
        action="/twilio/emergency_check",
        method="POST",
        timeout=3
    )
    gather_emergency.say(
        "Welcome to VoxMed AI. If this is a medical emergency, press 0 to connect directly with our human receptionist. Otherwise, please stay on the line to select your preferred language.",
        language="en-IN"
    )
    response.append(gather_emergency)
    
    # Fallback if the user doesn't press anything in the emergency gather
    response.redirect("/twilio/language_menu", method="POST")
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/language_menu")
async def twilio_language_menu(request: Request):
    """Webhook for language selection (after emergency check)."""
    response = VoiceResponse()
    
    gather_lang = Gather(
        numDigits=1,
        action="/twilio/language_selected",
        method="POST",
        timeout=5
    )
    gather_lang.say(f"{_PHRASES['en']['greeting_suffix']}", language="en-IN")
    response.append(gather_lang)
    
    # Fallback if the user doesn't press anything in the language menu
    response.redirect("/twilio/language_selected?Digits=1", method="POST")
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/emergency_check")
async def twilio_emergency_check(request: Request):
    """Webhook triggered by the emergency gather."""
    form_data = await request.form()
    digits = form_data.get("Digits", "")
    
    response = VoiceResponse()
    
    if digits == "0":
        response.say("Please hold while we connect you to our receptionist.", language="en-IN")
        
        receptionist_phone = os.getenv("RECEPTIONIST_PHONE", "+1234567890")
        
        # Dial with action to handle failure
        dial = Dial(
            action="/twilio/emergency_transfer_status",
            method="POST",
            timeout=20
        )
        dial.number(receptionist_phone)
        response.append(dial)
    else:
        # User pressed something else (e.g. 1), so redirect to language selection
        # By appending Digits to query params, language_selected will pick it up
        response.redirect(f"/twilio/language_selected?Digits={digits}", method="POST")
        
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/emergency_transfer_status")
async def twilio_emergency_transfer_status(request: Request):
    """Webhook triggered when the dial to receptionist ends or fails."""
    form_data = await request.form()
    dial_status = form_data.get("DialCallStatus", "")
    
    response = VoiceResponse()
    
    if dial_status not in ["completed", "answered"]:
        response.say("I'm sorry, our receptionist is currently unavailable. Please contact your local emergency services immediately for a medical emergency.", language="en-IN")
        
        # Do not disconnect the caller unnecessarily - redirect to existing language menu
        response.redirect("/twilio/language_menu", method="POST")
        
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/language_selected")
async def twilio_language_selected(request: Request):
    """Webhook triggered after language is selected."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    # Check form_data first, then query_params for fallback redirect
    digits = form_data.get("Digits") or request.query_params.get("Digits", "1")
    
    if digits == "2":
        language = "Hindi"
        lang_code = "hi"
    elif digits == "3":
        language = "Kannada"
        lang_code = "kn"
    elif digits == "4":
        language = "Telugu"
        lang_code = "te"
    else:
        language = "English"
        lang_code = "en"
        
    logger.info("Language selected | CallSid=%s | Digits=%s | Language=%s", call_sid, digits, language)

    dm = ConversationOrchestrator(language=language)
    active_calls[call_sid] = dm

    greeting = dm.get_greeting(lang_code=lang_code)
    
    twiml = get_twiml_response(greeting, dm.is_finished, lang_code=lang_code)
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/twilio/process")
async def twilio_process(request: Request):
    """Webhook triggered after Twilio Gathers speech."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    speech_result = form_data.get("SpeechResult", "").strip()
    
    logger.info("Received speech from Twilio | CallSid=%s | Text=%s", call_sid, speech_result)

    dm = active_calls.get(call_sid)
    
    lang_code = "en"
    if dm and dm.memory.language:
        lang_name = dm.memory.language.lower()
        if "hindi" in lang_name:
            lang_code = "hi"
        elif "kannada" in lang_name:
            lang_code = "kn"
        elif "telugu" in lang_name:
            lang_code = "te"

    if not dm:
        logger.warning("CallSid not found in active calls. | CallSid=%s", call_sid)
        response = VoiceResponse()
        response.say(_PHRASES["en"]["session_expired"])
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    if not speech_result:
        reply_text = _PHRASES[lang_code]["no_audio"]
        twiml = get_twiml_response(reply_text, dm.is_finished, lang_code=lang_code)
        return HTMLResponse(content=twiml, media_type="application/xml")

    reply_text = await dm.process(speech_result)
        
    twiml = get_twiml_response(reply_text, dm.is_finished, lang_code=lang_code)
    
    if dm.is_finished:
        active_calls.pop(call_sid, None)
        
    return HTMLResponse(content=twiml, media_type="application/xml")

@router.post("/twilio/process_silence")
async def twilio_process_silence(request: Request):
    """Webhook triggered if user stays silent after Gather."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    dm = active_calls.get(call_sid)
    if not dm:
        response = VoiceResponse()
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    lang_code = "en"
    if dm.memory.language:
        lang_name = dm.memory.language.lower()
        if "hindi" in lang_name:
            lang_code = "hi"
        elif "kannada" in lang_name:
            lang_code = "kn"
        elif "telugu" in lang_name:
            lang_code = "te"

    reply_text = _PHRASES[lang_code]["silence"]
    twiml = get_twiml_response(reply_text, dm.is_finished, lang_code=lang_code)
    return HTMLResponse(content=twiml, media_type="application/xml")
