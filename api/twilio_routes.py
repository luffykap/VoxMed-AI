from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse
from twilio.twiml.voice_response import VoiceResponse, Gather
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
            speechTimeout="auto",
            language=twilio_lang
        )
        response.append(gather)
        # If the user stays silent, Twilio will continue past the Gather.
        response.say("Are you still there?", language=twilio_lang)
        response.redirect("/twilio/process_silence")
    else:
        response.hangup()

    return str(response)

@router.post("/twilio/incoming")
async def twilio_incoming(request: Request):
    """Webhook triggered when a new call connects."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    
    logger.info("Incoming Twilio call | CallSid=%s", call_sid)

    dm = ConversationOrchestrator(language="English")
    active_calls[call_sid] = dm

    greeting = dm.get_greeting(lang_code="en")
    
    twiml = get_twiml_response(greeting, dm.is_finished, lang_code="en")
    return HTMLResponse(content=twiml, media_type="application/xml")


@router.post("/twilio/process")
async def twilio_process(request: Request):
    """Webhook triggered after Twilio Gathers speech."""
    form_data = await request.form()
    call_sid = form_data.get("CallSid", "")
    speech_result = form_data.get("SpeechResult", "").strip()
    
    logger.info("Received speech from Twilio | CallSid=%s | Text=%s", call_sid, speech_result)

    dm = active_calls.get(call_sid)
    if not dm:
        logger.warning("CallSid not found in active calls. | CallSid=%s", call_sid)
        response = VoiceResponse()
        response.say("Sorry, the session has expired. Please call back.")
        response.hangup()
        return HTMLResponse(content=str(response), media_type="application/xml")

    if not speech_result:
        twiml = get_twiml_response("I didn't quite catch that. Could you repeat?", dm.is_finished)
        return HTMLResponse(content=twiml, media_type="application/xml")

    reply_text = dm.process(speech_result)
    
    lang_code = "en"
    if dm.memory.language:
        lang_name = dm.memory.language.lower()
        if "hindi" in lang_name:
            lang_code = "hi"
        elif "kannada" in lang_name:
            lang_code = "kn"
        elif "telugu" in lang_name:
            lang_code = "te"

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

    twiml = get_twiml_response("Please let me know how I can help.", dm.is_finished)
    return HTMLResponse(content=twiml, media_type="application/xml")
