"""
processing/orchestrator.py
--------------------------
ConversationOrchestrator – replaces the old DialogueManager.

Key differences from the old approach:
- No hardcoded state machine (GREETING / GET_DATE / etc.)
- The LLM infers conversational state from the last 3 turns in memory.
- process() takes ONLY user_text (not a pre-computed nlp_result).
- All AppointmentService calls are IDENTICAL to the old implementation.
- Database persistence (create_call, save_conversation) is identical.
"""
from __future__ import annotations

from typing import Any
import re

import config
from database import create_call, end_call, save_conversation, save_ai_log, get_all_departments, get_all_doctors
from processing.memory import ConversationMemory, REQUIRED_ENTITIES
from services import llm_client
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Module-level conversation-control constants ─────────────────────────────────

# Phrases that signal the user wants to STOP the current flow and reset.
# Checked with regex — no extra LLM call needed. Multilingual support added.
_ABORT_PHRASES = re.compile(
    r"\b(cancel (it|that|this|everything|all)|i want to cancel|never mind|nevermind|"
    r"forget it|don'?t want|do not want|i changed my mind|start over|"
    r"i don'?t want to|not anymore|stop this|abort|scratch that|let'?s stop|"
    r"ruko|band karo|raddu chey|cancel chey|bekeda|cancel maadu)\b",
    re.IGNORECASE,
)

# Phrases that signal the user wants to END the call entirely.
_FAREWELL_PHRASES = re.compile(
    r"\b(goodbye|good bye|bye|end (it|the call|this)|i('?ll just)? end it|"
    r"i don'?t (want|need) (any )?help|no (more )?help|i('?m)? (done|leaving|going)|"
    r"hang up|stop the call|disconnect|i'?ll call later|call you later|"
    r"alvida|namaste|dhanyavad|dhanyavadagalu|dhanyavaadalu|bye bye)\b",
    re.IGNORECASE,
)

# LLM intents that signal the user wants out of the current flow
_ABORT_INTENTS = {"deny", "out_of_scope", "general_chat"}

# Intents that are valid mid-flow switches (user genuinely changed their mind)
_SWITCHABLE_INTENTS = {
    "book_appointment", "cancel_appointment", "reschedule_appointment",
    "check_availability",
}

# Confirmation yes/no patterns (no LLM needed for simple affirm/deny)
_CONFIRM_YES = re.compile(
    r"\b(yes|yeah|yep|yup|correct|confirm|confirmed|ok|okay|sure|go ahead|book it|do it|that'?s right|right|perfect|sounds good|"
    r"haan|han|ji haan|sari|avunu|houdu|sari)\b",
    re.IGNORECASE,
)
_CONFIRM_NO = re.compile(
    r"\b(no|nope|nah|wrong|incorrect|change|wait|actually|different|not that|not right|cancel that|let me|i want to change|"
    r"nahi|na|illa|bede|bedi|kadu|vaddu)\b",
    re.IGNORECASE,
)

# Predefined slot-filling questions — deterministic, no LLM call needed.
# Python decides which question to ask (Problem 3); LLM only formats complex replies.
_SLOT_TEMPLATES = {
    "english": {
        "book_appointment": {
            "medical_need": "What symptoms are you experiencing? Do you have any specific doctor you want to see?",
            "date": "What date would you prefer for the appointment?",
            "time": "What time would you like?",
            "patient_name": "Please provide the patient's name.",
        },
        "cancel_appointment": {
            "patient_name": "Please provide the patient's name.",
            "date": "What is the appointment date?",
        },
        "reschedule_appointment": {
            "patient_name": "Please provide the patient's name.",
            "current_date": "What is the date of your current appointment?",
            "new_date": "What new date would you like to reschedule to?",
            "new_time": "What time would you prefer on the new date?",
        },
        "check_availability": {
            "date": "Which date would you like to check?",
        },
    },
    "hindi": {
        "book_appointment": {
            "medical_need": "आप किन लक्षणों का अनुभव कर रहे हैं? क्या आप किसी विशेष डॉक्टर को दिखाना चाहते हैं?",
            "date": "आप किस तारीख को अपॉइंटमेंट बुक करना चाहते हैं?",
            "time": "आप किस समय अपॉइंटमेंट चाहते हैं?",
            "patient_name": "कृपया मरीज का नाम बताएं।",
        },
        "cancel_appointment": {
            "patient_name": "कृपया मरीज का नाम बताएं।",
            "date": "अपॉइंटमेंट की तारीख क्या है?",
        },
        "reschedule_appointment": {
            "patient_name": "कृपया मरीज का नाम बताएं।",
            "current_date": "आपके वर्तमान अपॉइंटमेंट की तारीख क्या है?",
            "new_date": "आप किस नई तारीख को अपॉइंटमेंट चाहते हैं?",
            "new_time": "आप नई तारीख पर किस समय अपॉइंटमेंट चाहते हैं?",
        },
        "check_availability": {
            "date": "आप किस तारीख की जांच करना चाहते हैं?",
        },
    },
    "kannada": {
        "book_appointment": {
            "medical_need": "ನೀವು ಯಾವ ರೋಗಲಕ್ಷಣಗಳನ್ನು ಅನುಭವಿಸುತ್ತಿದ್ದೀರಿ? ನೀವು ಯಾವುದೇ ನಿರ್ದಿಷ್ಟ ವೈದ್ಯರನ್ನು ಭೇಟಿಯಾಗಲು ಬಯಸುವಿರಾ?",
            "date": "ನೀವು ಯಾವ ದಿನಾಂಕದಂದು ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ಬುಕ್ ಮಾಡಲು ಬಯಸುತ್ತೀರಿ?",
            "time": "ನೀವು ಯಾವ ಸಮಯವನ್ನು ಬಯಸುತ್ತೀರಿ?",
            "patient_name": "ದಯವಿಟ್ಟು ರೋಗಿಯ ಹೆಸರನ್ನು ಒದಗಿಸಿ.",
        },
        "cancel_appointment": {
            "patient_name": "ದಯವಿಟ್ಟು ರೋಗಿಯ ಹೆಸರನ್ನು ಒದಗಿಸಿ.",
            "date": "ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ದಿನಾಂಕ ಯಾವುದು?",
        },
        "reschedule_appointment": {
            "patient_name": "ದಯವಿಟ್ಟು ರೋಗಿಯ ಹೆಸರನ್ನು ಒದಗಿಸಿ.",
            "current_date": "ನಿಮ್ಮ ಪ್ರಸ್ತುತ ಅಪಾಯಿಂಟ್‌ಮೆಂಟ್ ದಿನಾಂಕ ಯಾವುದು?",
            "new_date": "ನೀವು ಯಾವ ಹೊಸ ದಿನಾಂಕಕ್ಕೆ ಬದಲಾಯಿಸಲು ಬಯಸುತ್ತೀರಿ?",
            "new_time": "ಹೊಸ ದಿನಾಂಕದಂದು ನೀವು ಯಾವ ಸಮಯವನ್ನು ಬಯಸುತ್ತೀರಿ?",
        },
        "check_availability": {
            "date": "ನೀವು ಯಾವ ದಿನಾಂಕವನ್ನು ಪರಿಶೀಲಿಸಲು ಬಯಸುತ್ತೀರಿ?",
        },
    },
    "telugu": {
        "book_appointment": {
            "medical_need": "మీరు ఏ లక్షణాలను ఎదుర్కొంటున్నారు? మీరు నిర్దిష్ట వైద్యుడిని కలవాలనుకుంటున్నారా?",
            "date": "మీరు ఏ తేదీన అపాయింట్‌మెంట్ బుక్ చేయాలనుకుంటున్నారు?",
            "time": "మీకు ఏ సమయం కావాలి?",
            "patient_name": "దయచేసి రోగి పేరును తెలపండి.",
        },
        "cancel_appointment": {
            "patient_name": "దయచేసి రోగి పేరును తెలపండి.",
            "date": "అపాయింట్‌మెంట్ తేదీ ఏమిటి?",
        },
        "reschedule_appointment": {
            "patient_name": "దయచేసి రోగి పేరును తెలపండి.",
            "current_date": "మీ ప్రస్తుత అపాయింట్‌మెంట్ తేదీ ఏమిటి?",
            "new_date": "మీరు ఏ కొత్త తేదీకి మార్చాలనుకుంటున్నారు?",
            "new_time": "కొత్త తేదీలో మీరు ఏ సమయం కోరుకుంటున్నారు?",
        },
        "check_availability": {
            "date": "మీరు ఏ తేదీని తనిఖీ చేయాలనుకుంటున్నారు?",
        },
    }
}


class ConversationOrchestrator:
    """
    Drives a single patient call from greeting to farewell.

    Compatible drop-in for the old DialogueManager:
      - get_greeting()          → str
      - process(user_text: str) → str
      - is_finished             → bool  (replaces `state != DialogueState.FAREWELL`)
    """

    def __init__(self, language: str = "en") -> None:
        self.memory   = ConversationMemory(language=language)
        self.call_id  = create_call()
        self._finished = False
        self._reschedule_attempts = 0   # counts consecutive failed reschedule slot tries
        self._awaiting_confirmation = False   # True when waiting for user to confirm booking
        self._confirmation_summary: str = ""  # the summary text shown to user before confirming
        logger.info("ConversationOrchestrator initialized | call_id=%s | lang=%s",
                    self.call_id, language)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_finished(self) -> bool:
        return self._finished

    def get_greeting(self, lang_code: str = None) -> str:
        lang = lang_code or "en"
        if lang.startswith("hi"):
            greeting = "वॉक्समेड एआई में आपका स्वागत है। मैं आज आपकी कैसे मदद कर सकता हूँ?"
        elif lang.startswith("kn"):
            greeting = "VoxMed AI ಗೆ ಸ್ವಾಗತ. ನಾನು ಇಂದು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?"
        elif lang.startswith("te"):
            greeting = "VoxMed AI కి స్వాగతం. నేను ఈరోజు మీకు ఎలా సహాయం చేయగలను?"
        else:
            greeting = "Welcome to VoxMed AI. How can I help you today?"
            
        save_conversation(self.call_id, "AI", greeting)
        self.memory.add_turn("assistant", greeting)
        self.memory.last_question = greeting
        return greeting

    async def process(self, user_text: str) -> str:
        """
        Full pipeline for one user turn.
        Orchestrator owns the workflow; LLM is a helper only.

        Turn flow:
          ① Log user utterance.
          ② Determine LLM mode: detect intent (first turn) or extract entities (later).
          ③ Call LLM Stage 1.
          ④ Handle API unavailability — preserve memory, ask user to retry.
          ⑤ Lock or update intent.
          ⑥ Merge new entities; derive medical_need in Python.
          ⑦ Python computes missing entities.
          ⑧ Use predefined template for next slot question (no LLM call).
          ⑨ When all slots filled: call AppointmentService → Stage 2 LLM reply.
        """
        logger.info("Orchestrator processing | call_id=%s | intent=%s",
                    self.call_id, self.memory.intent)

        # ① Log user turn
        self.memory.add_turn("user", user_text)
        save_conversation(self.call_id, "Patient", user_text)

        # ① Handle pending booking confirmation before any LLM call
        if self._awaiting_confirmation:
            return self._handle_confirmation(user_text)

        # ② Determine mode (Problem 1 — intent lock)
        detect_intent = self.memory.intent is None

        # ③ LLM Stage 1
        understanding = await llm_client.understand(self.memory, user_text, detect_intent=detect_intent)

        # ④ Handle LLM unavailability (Problem 4 — never fall back to legacy NLP)
        if understanding.get("_api_unavailable"):
            logger.warning("LLM unavailable — preserving memory, asking user to retry")
            # Pop the user message that was just added so we don't get consecutive user messages
            if self.memory.turns and self.memory.turns[-1]["role"] == "user":
                self.memory.turns.pop()
            # Do NOT commit this as a standard AI reply so it doesn't pollute context,
            # but we do want the user to hear it.
            return "The system is momentarily unavailable. Please repeat your message."

        entities = understanding.get("entities") or {}

        # ⑤ Set or lock intent (Problem 1)
        if detect_intent:
            self.memory.intent = understanding.get("intent", "general_inquiry")
            confidence = understanding.get("confidence", 0.8)
            save_ai_log("NLP", "INFO",
                        f"Intent detected: {self.memory.intent} confidence={confidence:.2f}")
        else:
            # ── Intent-switch / abort / farewell handling ───────────────────────
            if understanding.get("intent_switch"):
                new_intent      = understanding.get("intent", "")
                abort_by_text   = bool(_ABORT_PHRASES.search(user_text))
                abort_by_intent = new_intent in _ABORT_INTENTS

                if abort_by_text or abort_by_intent:
                    # User wants out — wipe state and offer a fresh start
                    old_intent = self.memory.intent
                    self.memory.intent   = None
                    self.memory.entities = {}
                    self.memory.turns    = []   # clear history so LLM won't re-detect old intent
                    save_ai_log("DM", "INFO",
                                f"Conversation reset: user aborted '{old_intent}'")
                    logger.info("Intent aborted by user | old=%s", old_intent)
                    reply = await llm_client.generate_reply(self.memory, {"success": True, "message": "No problem! Let's start fresh. How can I help you today?"}, self.memory.language)
                    return self._commit_reply(reply)

                elif new_intent and new_intent in _SWITCHABLE_INTENTS:
                    # Legitimate switch (e.g. "actually I want to cancel instead")
                    old_intent = self.memory.intent
                    self.memory.intent   = new_intent
                    self.memory.entities = {}
                    self.memory.turns    = []
                    save_ai_log("DM", "INFO",
                                f"Intent switched: {old_intent} → {new_intent}")
                    logger.info("Intent switched | %s → %s", old_intent, new_intent)

                else:
                    logger.info("Intent switch ignored (new=%s). Keeping=%s",
                                new_intent, self.memory.intent)

            # Fallback abort guard: catches abort phrases even when LLM didn't
            # flag intent_switch (e.g. mid-sentence cancellation like "cancel").
            elif _ABORT_PHRASES.search(user_text):
                old_intent = self.memory.intent
                self.memory.intent   = None
                self.memory.entities = {}
                self.memory.turns    = []
                save_ai_log("DM", "INFO",
                            f"Abort phrase (no intent_switch) — reset from {old_intent}")
                logger.info("Intent aborted by user (fallback regex) | old=%s", old_intent)
                reply = await llm_client.generate_reply(self.memory, {"success": True, "message": "No problem! Let's start over. How can I help you today?"}, self.memory.language)
                return self._commit_reply(reply)

            save_ai_log(
                "NLP",
                "INFO",
                f"Entities extracted (intent={self.memory.intent}): {list(entities.keys())}",
            )

        # Farewell check: user wants to end the call entirely.
        # Runs for BOTH detect_intent=True (no active flow) and locked-intent turns.
        if _FAREWELL_PHRASES.search(user_text):
            save_ai_log("DM", "INFO", "Farewell phrase detected — ending call")
            logger.info("Call ending — farewell phrase detected")
            self._finished = True
            end_call(self.call_id)
            reply = await llm_client.generate_reply(self.memory, {"success": True, "message": "Thank you for calling VoxMed AI. Take care and stay healthy. Goodbye!"}, self.memory.language)
            return self._commit_reply(reply)


        # ⑥ Merge new entities — never overwrites existing (Problem 3)
        self.memory.update_entities(entities)

        # Derive medical_need in Python — LLM never decides this
        if any(self.memory.entities.get(k) for k in ("doctor", "department", "symptoms")):
            self.memory.entities["medical_need"] = True

        # ⑦ Python computes missing entities deterministically (Problem 6)
        missing = self.memory.missing_entities()

        if missing:
            next_missing = missing[0]

        side_query = understanding.get("side_query")
        if side_query == "check_availability":
            from datetime import date as _date
            from database import get_connection
            
            target_date = entities.get("date") or self.memory.entities.get("date")
            
            dept = self.memory.entities.get("department", config.DEFAULT_DEPARTMENT)
            conn = get_connection()
            cursor = conn.cursor()

            if not target_date or target_date == "ANY":
                # Find the next available slots from today onwards
                cursor.execute(
                    """
                    SELECT s.slot_date, s.slot_time, doc.name AS doctor_name
                    FROM Slots s
                    JOIN Doctors doc ON s.doctor_id = doc.id
                    JOIN Departments d ON doc.department_id = d.id
                    WHERE s.slot_date >= ? AND s.is_booked = 0 AND d.name = ? COLLATE NOCASE
                    ORDER BY s.slot_date, s.slot_time
                    LIMIT 3
                    """, (_date.today().isoformat(), dept)
                )
                slots = cursor.fetchall()
                if slots:
                    times = ", ".join(f"{s['slot_time']} on {s['slot_date']}" for s in slots)
                    msg = f"We have free slots at {times}."
                else:
                    msg = f"Sorry, there are no available slots in {dept} coming up."
            else:
                cursor.execute(
                    """
                    SELECT s.slot_time, doc.name AS doctor_name
                    FROM Slots s
                    JOIN Doctors doc ON s.doctor_id = doc.id
                    JOIN Departments d ON doc.department_id = d.id
                    WHERE s.slot_date = ? AND s.is_booked = 0 AND d.name = ? COLLATE NOCASE
                    ORDER BY s.slot_time
                    LIMIT 3
                    """, (target_date, dept)
                )
                slots = cursor.fetchall()
                if slots:
                    times = ", ".join(s["slot_time"] for s in slots)
                    msg = f"On {target_date}, we have free slots at {times}."
                else:
                    msg = f"Sorry, there are no available slots in {dept} on {target_date}."
            
            conn.close()
            
            reply = await llm_client.generate_reply(self.memory, {"success": bool(slots), "message": msg}, self.memory.language)
            
            # Steer back to the booking flow
            if missing:
                next_missing = missing[0]
                lang_key = self.memory.language.lower() if self.memory.language else "english"
                if lang_key not in _SLOT_TEMPLATES: lang_key = "english"
                intent_templates = _SLOT_TEMPLATES[lang_key].get(self.memory.intent, {})
                slot_question = intent_templates.get(next_missing)
                if slot_question:
                    reply = f"{reply} {slot_question}"
                    
            save_ai_log("DM", "INFO", f"Side query handled. reply_len={len(reply)}")
            return self._commit_reply(reply)

        if missing:
            next_missing = missing[0]

            # ⑧a Proactive availability check (booking only):
            #     When date + time are now known but patient_name is still missing,
            #     verify the slot exists BEFORE collecting the name.
            #     This prevents asking for the name, then redirecting because the
            #     slot was unavailable — a confusing UX waste.
            if (
                self.memory.intent == "book_appointment"
                and next_missing == "patient_name"
                and self.memory.entities.get("date")
                and self.memory.entities.get("time")
            ):
                avail_reply = await self._check_slot_before_name()
                if avail_reply:
                    # Slot unavailable — alternatives surfaced; ask user to pick first
                    return self._commit_reply(avail_reply)
                # Slot is available — fall through to ask for name normally

            # ⑧ Use predefined template — zero LLM calls for slot questions (Problem 6)
            lang_key = self.memory.language.lower() if self.memory.language else "english"
            if lang_key not in _SLOT_TEMPLATES: lang_key = "english"
            
            intent_templates = _SLOT_TEMPLATES[lang_key].get(self.memory.intent, {})
            reply = intent_templates.get(next_missing)
            
            if not reply:
                reply = await llm_client.generate_reply(self.memory, None, self.memory.language)
                
            save_ai_log("DM", "INFO",
                        f"Slot-filling: next={next_missing} | template={next_missing in intent_templates}")
            return self._commit_reply(reply)

        # ⑨ All entities present — execute intent
        # For book_appointment: pause for user confirmation first
        if self.memory.intent == "book_appointment" and not self._awaiting_confirmation:
            return await self._request_booking_confirmation()

        reply = await self._execute_intent()
        return self._commit_reply(reply)

    # ── Booking confirmation ──────────────────────────────────────────────────

    async def _request_booking_confirmation(self) -> str:
        """
        Build a human-readable booking summary and ask the user to confirm.
        Sets _awaiting_confirmation = True so the next turn is handled as yes/no.
        """
        ents = self.memory.entities
        doctor   = ents.get("doctor") or "an available doctor"
        date     = ents.get("date") or "?"
        time     = ents.get("time") or "?"
        name     = ents.get("patient_name") or "the patient"
        symptoms = ents.get("symptoms")
        dept     = ents.get("department", config.DEFAULT_DEPARTMENT)

        summary_parts = [f"Booking {doctor}"]
        if symptoms:
            symp_str = ", ".join(symptoms) if isinstance(symptoms, list) else symptoms
            summary_parts.append(f"for {symp_str}")
        else:
            summary_parts.append(f"in {dept}")
        summary_parts.append(f"on {date} at {time} for {name}")

        self._confirmation_summary = " ".join(summary_parts)
        self._awaiting_confirmation = True

        msg = f"{self._confirmation_summary}. Shall I confirm? (Yes / No)"
        logger.info("Awaiting booking confirmation | summary=%s", self._confirmation_summary)
        save_ai_log("DM", "INFO", f"Confirmation requested: {self._confirmation_summary}")
        reply = await llm_client.generate_reply(self.memory, {"success": True, "message": msg}, self.memory.language)
        return self._commit_reply(reply)

    async def _handle_confirmation(self, user_text: str) -> str:
        """
        Process the user's yes/no response to the booking confirmation prompt.
        - Yes  → execute the booking
        - No   → ask what to change, clear that slot
        - Other → re-ask the confirmation question
        """
        if _CONFIRM_YES.search(user_text):
            # User confirmed — execute the booking
            self._awaiting_confirmation = False
            logger.info("Booking confirmed by user")
            save_ai_log("DM", "INFO", "Booking confirmed by user")
            reply = await self._execute_intent()
            return self._commit_reply(reply)

        if _CONFIRM_NO.search(user_text):
            # User wants to change something — figure out which slot to clear
            self._awaiting_confirmation = False
            ents = self.memory.entities

            # Heuristic: check which entity the user mentioned
            text_lower = user_text.lower()
            if any(w in text_lower for w in ("date", "day", "when", "din", "tarikh", "roju", "dina")):
                self.memory.entities["date"] = None
                self.memory.entities["time"] = None
                msg = "No problem. What date would you prefer?"
            elif any(w in text_lower for w in ("time", "hour", "o'clock", "am", "pm", "samay", "baje", "samaya", "vela")):
                self.memory.entities["time"] = None
                msg = "Sure. What time would you prefer?"
            elif any(w in text_lower for w in ("doctor", "dr", "physician", "doctoru", "daaktar")):
                self.memory.entities["doctor"] = None
                msg = "Understood. Which doctor would you like to see?"
            elif any(w in text_lower for w in ("name", "patient", "naam", "peshent", "rogi", "hesaru", "peru")):
                self.memory.entities["patient_name"] = None
                msg = "Of course. What is the patient's name?"
            else:
                # Can't tell what to change — clear date/time and restart slot filling
                self.memory.entities["date"] = None
                self.memory.entities["time"] = None
                msg = "No problem. What changes would you like to make? Let's start with the date."

            logger.info("Booking cancelled by user — re-entering slot filling")
            save_ai_log("DM", "INFO", "Booking cancelled by user, re-entering slot fill")
            reply = await llm_client.generate_reply(self.memory, {"success": True, "message": msg}, self.memory.language)
            return self._commit_reply(reply)

        # Unclear response — repeat the confirmation question
        msg = f"I didn't catch that. {self._confirmation_summary}. Please say Yes to confirm or No to change details."
        reply = await llm_client.generate_reply(self.memory, {"success": True, "message": msg}, self.memory.language)
        return self._commit_reply(reply)

    # ── Intent execution ──────────────────────────────────────────────────────

    async def _execute_intent(self) -> str:
        """
        Dispatch to the correct AppointmentService method.
        All AppointmentService call signatures are unchanged.
        On booking failure: searches for alternatives and continues conversation (Problem 5).
        """
        from services import appointments

        intent = self.memory.intent
        ents   = self.memory.entities

        # Auto-resolve "ANY" date/time
        if intent in ("book_appointment", "reschedule_appointment"):
            if ents.get("date") == "ANY" or ents.get("time") == "ANY":
                from database import find_available_slot
                dept = ents.get("department")
                if not dept and ents.get("symptoms"):
                    dept = await appointments._map_symptoms_to_department(ents.get("symptoms"))
                dept = dept or config.DEFAULT_DEPARTMENT
                
                search_date = None if ents.get("date") == "ANY" else ents.get("date")
                search_time = None if ents.get("time") == "ANY" else ents.get("time")
                
                slot = find_available_slot(dept, search_date, search_time)
                if slot:
                    ents["date"] = slot["slot_date"]
                    ents["time"] = slot["slot_time"]
                    if not ents.get("doctor"):
                        ents["doctor"] = slot["doctor_name"]

        backend_result: dict[str, Any] = {}

        if intent == "book_appointment":
            success, msg, context = await appointments.book_appointment(
                name=ents.get("patient_name"),
                doctor=ents.get("doctor"),
                department=ents.get("department"),
                symptoms=ents.get("symptoms", []),
                date=ents.get("date"),
                time=ents.get("time"),
            )
            if success:
                backend_result = {"success": True, "message": msg, **context}
                self._finished = True
            elif "in the past" in msg:
                # Past-date rejection — clear only date/time and ask again
                self.memory.entities["date"] = None
                self.memory.entities["time"] = None
                backend_result = {"success": False, "message": msg}
                self._finished = False
            else:
                # Slot unavailable — search for alternatives (Problem 5)
                requested_date = ents.get("date")
                alternatives = self._find_alternative_slots(requested_date)
                if alternatives:
                    is_same_date = all(a['slot_date'] == requested_date for a in alternatives)
                    
                    if is_same_date:
                        times = ", ".join(f"{a['slot_time']} with {a.get('doctor_name', 'available doctor')}" for a in alternatives)
                        msg_text = f"That specific time is unavailable. On {requested_date}, we have slots at {times}. Which would you prefer?"
                    else:
                        alt_list = "; ".join(
                            f"{a['slot_date']} at {a['slot_time']} with {a.get('doctor_name', 'available doctor')}"
                            for a in alternatives
                        )
                        msg_text = f"There are no slots available on {requested_date}. The nearest available slots are {alt_list}. Which would you prefer?"
                        
                    backend_result = {
                        "success": False,
                        "message": msg_text,
                    }
                    
                    # Only clear the date if we actually suggested a new date
                    if not is_same_date:
                        self.memory.entities["date"] = None
                    self.memory.entities["time"] = None
                    self._finished = False
                else:
                    backend_result = {"success": False, "message": msg}
                    self._finished = True  # Genuinely no slots available


        elif intent == "cancel_appointment":
            success, msg = appointments.cancel_appointment(
                ents.get("patient_name"),
                ents.get("date"),
            )
            backend_result = {"success": success, "message": msg}
            self._finished = True

        elif intent == "reschedule_appointment":
            success, msg = appointments.reschedule_appointment(
                ents.get("patient_name"),
                ents.get("current_date"),
                ents.get("new_date"),
                ents.get("new_time"),
            )
            if success:
                self._reschedule_attempts = 0
                backend_result = {"success": True, "message": msg}
                self._finished = True
            else:
                self._reschedule_attempts += 1
                # After 2 failed attempts, suggest free slots for the same doctor
                if self._reschedule_attempts >= 2:
                    doctor_id = self._get_doctor_id_from_appointment(
                        ents.get("patient_name"), ents.get("current_date")
                    )
                    if doctor_id:
                        free_slots = appointments.find_free_slots_for_doctor(doctor_id, limit=3)
                        if free_slots:
                            slot_list = "; ".join(
                                f"{s['slot_date']} at {s['slot_time']}"
                                for s in free_slots
                            )
                            doctor_name = free_slots[0].get("doctor_name", "the doctor")
                            alt_msg = (
                                f"{msg} Here are available slots for {doctor_name}: "
                                f"{slot_list}. Which would you prefer?"
                            )
                            backend_result = {"success": False, "message": alt_msg}
                        else:
                            backend_result = {
                                "success": False,
                                "message": f"{msg} No upcoming free slots found for this doctor.",
                            }
                    else:
                        backend_result = {"success": False, "message": msg}
                    self._reschedule_attempts = 0  # reset after showing alternatives
                else:
                    backend_result = {
                        "success": False,
                        "message": f"{msg} Please choose a different date or time.",
                    }
                # Keep patient_name and current_date; only clear the failed new slot fields
                self.memory.entities["new_date"] = None
                self.memory.entities["new_time"] = None
                self._finished = False

        elif intent == "check_availability":
            success, msg = appointments.check_availability(ents.get("date"))
            backend_result = {"success": success, "message": msg}
            # Non-terminal: user may book after checking
            self._finished = False

        elif intent == "general_inquiry":
            backend_result = {
                "success": True,
                "message": "The clinic is open Monday to Saturday, 9 AM to 8 PM.",
            }
            self._finished = True

        elif intent == "medicine_information":
            backend_result = {"success": True, "facts": "We cannot prescribe medicines over the phone. Please consult a doctor in-person for prescriptions.", "is_qa": True}
            self._finished = True

        elif intent == "doctor_information":
            doctors = get_all_doctors()
            facts = "We have the following doctors: " + ", ".join([f"{d['doctor_name']} ({d['dept_name']})" for d in doctors])
            backend_result = {"success": True, "facts": facts, "is_qa": True}
            self._finished = True

        elif intent == "department_information":
            departments = get_all_departments()
            facts = "We offer services in the following departments: " + ", ".join(departments)
            backend_result = {"success": True, "facts": facts, "is_qa": True}
            self._finished = True

        elif intent == "hospital_timings":
            backend_result = {"success": True, "facts": "The clinic is open Monday to Saturday, 9 AM to 8 PM.", "is_qa": True}
            self._finished = True

        elif intent == "insurance_query":
            backend_result = {"success": True, "facts": "We accept most major health insurance plans including Star Health, Apollo Munich, and Max Bupa.", "is_qa": True}
            self._finished = True

        elif intent == "parking_query":
            backend_result = {"success": True, "facts": "We have free valet parking available for all patients at the main entrance.", "is_qa": True}
            self._finished = True

        elif intent == "cost_query":
            backend_result = {"success": True, "facts": "Consultation fees start at 500 rupees, but vary depending on the specialist (up to 1500 rupees).", "is_qa": True}
            self._finished = True

        elif intent == "emergency":
            backend_result = {"success": True, "message": "If this is a medical emergency, please hang up immediately and dial 112 or your local emergency number."}
            self._finished = True

        elif intent == "human_agent":
            backend_result = {"success": True, "message": "Please wait while I connect you to the next available human representative."}
            self._finished = True

        elif intent == "out_of_scope" or intent == "general_chat":
            backend_result = {"success": False, "message": "I am a medical assistant designed to help with appointments and clinic info. How can I assist you with your health needs today?"}
            self._finished = False

        else:
            backend_result = {"success": True, "message": ""}

        save_ai_log("DM", "INFO",
                    f"Executed intent={intent} | success={backend_result.get('success')}")

        reply = await llm_client.generate_reply(
            memory=self.memory,
            backend_result=backend_result,
            language=self.memory.language,
        )

        if self._finished:
            end_call(self.call_id)

        return reply

    async def _check_slot_before_name(self) -> str | None:
        """
        Called when date + time are known but patient_name has not been collected yet.
        Checks whether the requested slot actually exists (is_booked=0).

        Returns:
            None         – slot is free; caller should proceed to ask for name.
            str (reply)  – slot is unavailable; LLM-formatted message with alternatives.
        """
        from services import appointments as _appt
        ents = self.memory.entities
        date = ents.get("date")
        time = ents.get("time")
        doctor = ents.get("doctor")
        dept = ents.get("department", config.DEFAULT_DEPARTMENT)

        # Try to book a dry-run check via the existing slot search
        alternatives = self._find_alternative_slots(date)

        # Check if any alternative exactly matches the requested date+time+doctor
        if doctor:
            # User picked a specific doctor — verify THAT doctor has the slot
            def _doc_match(req: str, act: str) -> bool:
                r = req.lower().replace('dr.', '').replace('dr ', '').strip()
                a = act.lower().replace('dr.', '').replace('dr ', '').strip()
                if not r or not a: return False
                if r in a or a in r: return True
                import difflib
                return difflib.SequenceMatcher(None, r, a).ratio() > 0.6

            exact_match = False
            for a in alternatives:
                if (a["slot_date"] == date and a["slot_time"] == time and 
                    _doc_match(doctor, a.get("doctor_name", ""))):
                    exact_match = True
                    # Update to correct DB spelling so booking won't fail later
                    self.memory.entities["doctor"] = a.get("doctor_name")
                    break
        else:
            # No doctor preference — any matching date+time is fine
            exact_match = any(
                a["slot_date"] == date and a["slot_time"] == time
                for a in alternatives
            )

        if exact_match:
            return None  # Slot is free — no redirect needed

        # Slot unavailable — surface alternatives immediately
        if alternatives:
            is_same_date = all(a["slot_date"] == date for a in alternatives)
            if is_same_date:
                times = ", ".join(
                    f"{a['slot_time']} with {a.get('doctor_name', 'available doctor')}"
                    for a in alternatives
                )
                msg = (f"The slot at {time} on {date} is unavailable. "
                       f"Available slots on {date}: {times}. Which would you prefer?")
            else:
                alt_list = "; ".join(
                    f"{a['slot_date']} at {a['slot_time']} with {a.get('doctor_name', 'available doctor')}"
                    for a in alternatives
                )
                msg = (f"There are no slots available on {date} at {time}. "
                       f"The nearest available slots are: {alt_list}. Which would you prefer?")

            # Clear the unavailable time (and date if we moved dates) so user can re-pick
            if not is_same_date:
                self.memory.entities["date"] = None
            self.memory.entities["time"] = None

            save_ai_log("DM", "INFO",
                        f"Proactive slot check: unavailable date={date} time={time} | alternatives={len(alternatives)}")
            return await llm_client.generate_reply(
                self.memory,
                {"success": False, "message": msg},
                self.memory.language,
            )

        # No alternatives found at all
        save_ai_log("DM", "WARN", f"Proactive slot check: no slots found at all for dept={dept}")
        return await llm_client.generate_reply(
            self.memory,
            {"success": False, "message": f"There are currently no available slots in {dept}. Please try a different date."},
            self.memory.language,
        )

    def _find_alternative_slots(self, requested_date: str | None = None) -> list[dict]:
        """Search for alternatives on the requested date first, then the next 7 days."""
        from database import get_connection
        from datetime import date as _date, timedelta, datetime

        dept = self.memory.entities.get("department", config.DEFAULT_DEPARTMENT)
        alternatives: list[dict] = []

        conn = get_connection()
        cursor = conn.cursor()

        # Get department id
        cursor.execute("SELECT id FROM Departments WHERE name = ? COLLATE NOCASE", (dept,))
        dept_row = cursor.fetchone()
        if not dept_row:
            conn.close()
            logger.info("No department row found for dept=%s", dept)
            return []

        dept_id = dept_row["id"]
        
        start_date_str = requested_date if requested_date else _date.today().isoformat()
        
        # 1. Search on the requested date first
        cursor.execute(
            """
            SELECT s.slot_date, s.slot_time, doc.name AS doctor_name
            FROM   Slots s
            JOIN   Doctors doc ON s.doctor_id = doc.id
            WHERE  doc.department_id = ?
              AND  s.slot_date = ?
              AND  s.is_booked = 0
            ORDER BY s.slot_time
            LIMIT 3
            """,
            (dept_id, start_date_str),
        )
        for row in cursor.fetchall():
            alternatives.append(dict(row))
            
        if alternatives:
            conn.close()
            logger.info("Alternative slots found on requested date: %d for dept=%s", len(alternatives), dept)
            return alternatives

        # 2. If no slots on the requested date, search the next 7 days
        try:
            start_date = datetime.strptime(str(start_date_str), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            start_date = _date.today()

        for day_offset in range(1, 8):
            d = (start_date + timedelta(days=day_offset)).isoformat()
            cursor.execute(
                """
                SELECT s.slot_date, s.slot_time, doc.name AS doctor_name
                FROM   Slots s
                JOIN   Doctors doc ON s.doctor_id = doc.id
                WHERE  doc.department_id = ?
                  AND  s.slot_date = ?
                  AND  s.is_booked = 0
                LIMIT 1
                """,
                (dept_id, d),
            )
            row = cursor.fetchone()
            if row:
                alternatives.append(dict(row))
            if len(alternatives) >= 3:
                break

        conn.close()
        logger.info("Alternative slots found: %d for dept=%s", len(alternatives), dept)
        return alternatives

    def _get_doctor_id_from_appointment(
        self, patient_name: str | None, current_date: str | None
    ) -> int | None:
        """Look up the doctor_id of the patient's existing BOOKED appointment on current_date."""
        if not patient_name or not current_date:
            return None
        try:
            from database import get_connection
            conn = get_connection()
            row = conn.execute(
                """
                SELECT a.doctor_id
                FROM   Appointments a
                JOIN   Patients p ON a.patient_id = p.id
                JOIN   Slots    s ON a.slot_id    = s.id
                WHERE  p.name LIKE ? COLLATE NOCASE
                  AND  s.slot_date = ?
                  AND  a.status = 'BOOKED'
                LIMIT 1
                """,
                (f"%{patient_name}%", current_date),
            ).fetchone()
            conn.close()
            return row["doctor_id"] if row else None
        except Exception:
            logger.exception("_get_doctor_id_from_appointment failed")
            return None

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _commit_reply(self, reply: str) -> str:
        """Persist the AI reply to DB and memory, then return it."""
        self.memory.add_turn("assistant", reply)
        self.memory.last_question = reply
        save_conversation(self.call_id, "AI", reply)
        return reply
