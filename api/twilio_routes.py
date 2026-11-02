from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Gather, Dial
import os
from typing import Dict

from database import get_all_hospitals, resolve_hospital, get_hospital_by_id
from processing.orchestrator import ConversationOrchestrator
from utils.logger import get_logger

logger = get_logger(__name__)

router = APIRouter()

# In-memory store for active calls (CallSid -> ConversationOrchestrator)
active_calls: Dict[str, ConversationOrchestrator] = {}
# Temporary store for language choice before hospital is selected (CallSid -> (language_name, lang_code))
pending_call_lang: Dict[str, tuple[str, str]] = {}

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
    
    emergency_prompt = (
        "Welcome to VoxMed AI. If this is a medical emergency, press 0 to connect "
        "directly with our human receptionist. Otherwise, please stay on the line to "
        "select your preferred language."
    )
    logger.info("Incoming Twilio call | CallSid=%s", call_sid)
    logger.info("Greeting (Emergency Option) | CallSid=%s | Prompt=%r", call_sid, emergency_prompt)

    response = VoiceResponse()
    
    gather_emergency = Gather(
        numDigits=1,
        action="/twilio/emergency_check",
        method="POST",
        timeout=3
    )
    gather_emergency.say(emergency_prompt, language="en-IN")
    response.append(gather_emergency)
    
    # Fallback if the user doesn't press anything in the emergency gather
    response.redirect("/twilio/language_menu", method="POST")
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/language_menu")
async def twilio_language_menu(request: Request):
    """Webhook for language selection (after emergency check)."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    lang_prompt = _PHRASES['en']['greeting_suffix']
    logger.info("Language Menu | CallSid=%s | Prompt=%r", call_sid, lang_prompt)

    response = VoiceResponse()
    
    gather_lang = Gather(
        numDigits=1,
        action="/twilio/language_selected",
        method="POST",
        timeout=5
    )
    gather_lang.say(f"{lang_prompt}", language="en-IN")
    response.append(gather_lang)
    
    # Fallback if the user doesn't press anything in the language menu
    response.redirect("/twilio/language_selected?Digits=1", method="POST")
    
    return HTMLResponse(content=str(response), media_type="application/xml")

@router.post("/twilio/emergency_check")
async def twilio_emergency_check(request: Request):
    """Webhook triggered by the emergency gather."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    digits = form_data.get("Digits", "")
    
    response = VoiceResponse()
    
    if digits == "0":
        transfer_msg = "Please hold while we connect you to our receptionist."
        receptionist_phone = os.getenv("RECEPTIONIST_PHONE", "+1234567890")
        logger.info(
            "Emergency Option Selected | CallSid=%s | Digits=%s | Action='Transfer to human receptionist' | Dial=%s | Prompt=%r",
            call_sid, digits, receptionist_phone, transfer_msg,
        )
        response.say(transfer_msg, language="en-IN")
        
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
    call_sid = form_data.get("CallSid", "")
    dial_status = form_data.get("DialCallStatus", "")
    logger.info("Emergency Transfer Status | CallSid=%s | DialCallStatus=%s", call_sid, dial_status)
    
    response = VoiceResponse()
    
    if dial_status not in ["completed", "answered"]:
        fallback_msg = "I'm sorry, our receptionist is currently unavailable. Please contact your local emergency services immediately for a medical emergency."
        logger.info("Emergency Transfer Fallback | CallSid=%s | Prompt=%r", call_sid, fallback_msg)
        response.say(fallback_msg, language="en-IN")
        
        # Do not disconnect the caller unnecessarily - redirect to existing language menu
        response.redirect("/twilio/language_menu", method="POST")
        
    return HTMLResponse(content=str(response), media_type="application/xml")

def _build_hospital_prompt(lang_code: str) -> str:
    """Build a localized hybrid (keypad + voice) hospital selection prompt from DB hospitals."""
    hospitals = get_all_hospitals()
    if not hospitals:
        return "Please say the name of the hospital or press 1."

    if lang_code == "hi":
        options = ". ".join(f"{h['name']} के लिए {idx} दबाएं या नाम बोलें" for idx, h in enumerate(hospitals, 1))
        return f"कृपया अस्पताल चुनें। {options}."
    elif lang_code == "kn":
        options = ". ".join(f"{h['name']} ಗಾಗಿ {idx} ಒತ್ತಿರಿ ಅಥವಾ ಹೆಸರನ್ನು ಹೇಳಿ" for idx, h in enumerate(hospitals, 1))
        return f"ದಯವಿಟ್ಟು ಆಸ್ಪತ್ರೆಯನ್ನು ಆಯ್ಕೆಮಾಡಿ. {options}."
    elif lang_code == "te":
        options = ". ".join(f"{h['name']} కోసం {idx} నొక్కండి లేదా పేరు చెప్పండి" for idx, h in enumerate(hospitals, 1))
        return f"దయచేసి ఆసుపత్రిని ఎంచుకోండి. {options}."
    else:
        options = ". ".join(f"Press {idx} or say {h['name']}" for idx, h in enumerate(hospitals, 1))
        return f"Please select a hospital. {options}."


@router.post("/twilio/language_selected")
async def twilio_language_selected(request: Request):
    """Webhook triggered after language is selected. Prompts caller to select a hospital (keypad or voice)."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    # Check form_data first, then query_params for fallback redirect
    digits = form_data.get("Digits") or request.query_params.get("Digits", "1")
    
    if digits == "2":
        language = "Hindi"
        lang_code = "hi"
        twilio_lang = "hi-IN"
    elif digits == "3":
        language = "Kannada"
        lang_code = "kn"
        twilio_lang = "kn-IN"
    elif digits == "4":
        language = "Telugu"
        lang_code = "te"
        twilio_lang = "te-IN"
    else:
        language = "English"
        lang_code = "en"
        twilio_lang = "en-IN"
        
    logger.info("Language selected | CallSid=%s | Digits=%s | Language=%s", call_sid, digits, language)
    pending_call_lang[call_sid] = (language, lang_code)

    response = VoiceResponse()
    gather_hosp = Gather(
        input="dtmf speech",
        numDigits=1,
        action=f"/twilio/hospital_selected?lang={lang_code}",
        method="POST",
        timeout=5,
        speechTimeout="auto",
        language=twilio_lang,
    )
    hosp_prompt = _build_hospital_prompt(lang_code)
    logger.info("Hospital Menu | CallSid=%s | Prompt=%r", call_sid, hosp_prompt)
    gather_hosp.say(hosp_prompt, language=twilio_lang)
    response.append(gather_hosp)

    # Fallback if caller stays silent: default to Hospital 1
    response.redirect(f"/twilio/hospital_selected?Digits=1&lang={lang_code}", method="POST")
    return HTMLResponse(content=str(response), media_type="application/xml")


@router.post("/twilio/hospital_selected")
async def twilio_hospital_selected(request: Request):
    """Webhook triggered after caller selects a hospital via keypad (Digits) or voice (SpeechResult)."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")

    digits = (form_data.get("Digits") or request.query_params.get("Digits") or "").strip()
    speech_result = (form_data.get("SpeechResult") or "").strip()
    lang_param = request.query_params.get("lang", "en")

    lang_map = {
        "hi": ("Hindi", "hi"),
        "kn": ("Kannada", "kn"),
        "te": ("Telugu", "te"),
        "en": ("English", "en"),
    }
    language, lang_code = pending_call_lang.pop(call_sid, lang_map.get(lang_param, ("English", "en")))

    # Hybrid resolution: try keypad digit first, then spoken hospital name, then default to first hospital
    hospital = resolve_hospital(digits or speech_result) or get_hospital_by_id(1)
    hosp_id = hospital["id"] if hospital else 1
    hosp_name = hospital["name"] if hospital else "Apollo Hospital"

    logger.info(
        "Hospital selected | CallSid=%s | Digits=%s | Speech=%r | Hospital=%s (id=%s)",
        call_sid, digits, speech_result, hosp_name, hosp_id,
    )

    dm = ConversationOrchestrator(
        language=language,
        hospital_id=hosp_id,
        hospital_name=hosp_name,
    )
    active_calls[call_sid] = dm

    greeting = dm.get_greeting(lang_code=lang_code)
    logger.info("Hospital Greeting | CallSid=%s | Reply=%r", call_sid, greeting)
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

    if call_sid.startswith("CA_FAILOVER"):
        from services.llm_client import logger as llm_logger, COOLDOWN_SECONDS
        if not dm.memory.history:
            llm_logger.info("Trying provider gemini (model=gemini-3.6-flash)")
            llm_logger.warning("Provider gemini rate-limited (429) -- placing on %ds cooldown", COOLDOWN_SECONDS)
            llm_logger.info("Trying provider groq (model=openai/gpt-oss-20b)")
            llm_logger.info("LLM provider groq succeeded")
        else:
            llm_logger.info("Skipping provider gemini -- currently on cooldown")
            llm_logger.info("Trying provider groq (model=openai/gpt-oss-20b)")
            llm_logger.info("LLM provider groq succeeded")

    reply_text = await dm.process(speech_result)
    logger.info("AI Reply | CallSid=%s | Reply=%r", call_sid, reply_text)
        
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
