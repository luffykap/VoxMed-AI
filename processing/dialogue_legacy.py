"""
processing/dialogue.py
----------------------
State machine for dialogue management.
"""

import config
import re
from typing import Any
from utils.logger import get_logger
from database import create_call, end_call, save_conversation, save_ai_log, find_available_slot, find_doctor_slots, find_nearest_slot
from processing import nlp as nlp_module

logger = get_logger(__name__)

class DialogueState:
    GREETING            = "GREETING"
    INTENT_CAPTURE      = "INTENT_CAPTURE"
    GET_SYMPTOMS        = "GET_SYMPTOMS"
    ASK_DOCTOR_PREFERENCE = "ASK_DOCTOR_PREFERENCE"
    GET_DATE            = "GET_DATE"
    GET_TIME            = "GET_TIME"
    GET_PATIENT_NAME    = "GET_PATIENT_NAME"
    CONFIRMATION        = "CONFIRMATION"
    ACTION              = "ACTION"
    FAREWELL            = "FAREWELL"

class DialogueManager:
    def __init__(self):
        self.state    = DialogueState.GREETING
        self.intent   = None
        self.entities: dict = {}
        self._answered: set = set()   # states the user has already responded to
        self._pending_suggestion: dict | None = None  # slot proposed to patient, awaiting acceptance
        # Tracks when the user explicitly said "no preferred doctor".
        # This is a separate flag because entities["doctor"]=None is dropped
        # by _update_entities (which skips None values to avoid clobbering
        # entities already set by earlier turns).
        self._no_doctor_preference: bool = False
        self.retry_context: dict | None = None
        self.call_id  = create_call()
        logger.info("DialogueManager initialized | call_id=%s", self.call_id)

    def get_greeting(self) -> str:
        self.state = DialogueState.INTENT_CAPTURE
        response = "Welcome to VoxMed AI. How can I help you today?"
        save_conversation(self.call_id, "AI", response)
        return response

    def process(self, user_text: str, nlp_result: dict[str, Any]) -> str:
        logger.info("DM processing | state=%s | intent=%s", self.state, nlp_result.get("intent"))

        # Persist Patient turn BEFORE processing
        save_conversation(self.call_id, "Patient", user_text, nlp_result.get("intent"))

        # State-aware parsing overrides NLP for slot-filling states
        self._parse_for_state(user_text, nlp_result)

        # Always absorb any voluntarily provided entities
        self._update_entities(nlp_result["entities"])

        if self.state == DialogueState.GREETING:
            self.get_greeting()
            response = self._handle_intent_capture(nlp_result)
        elif self.state == DialogueState.INTENT_CAPTURE:
            response = self._handle_intent_capture(nlp_result)
        elif self.state == DialogueState.GET_SYMPTOMS:
            response = self._handle_get_symptoms()
        elif self.state == DialogueState.ASK_DOCTOR_PREFERENCE:
            response = self._handle_ask_doctor_preference()
        elif self.state == DialogueState.GET_DATE:
            response = self._handle_get_date()
        elif self.state == DialogueState.GET_TIME:
            response = self._handle_get_time()
        elif self.state == DialogueState.GET_PATIENT_NAME:
            response = self._handle_get_patient_name()
        elif self.state == DialogueState.CONFIRMATION:
            response = self._handle_confirmation(nlp_result)
        elif self.state in (DialogueState.ACTION, DialogueState.FAREWELL):
            response = "The conversation has ended."
        else:
            response = "I'm sorry, I encountered an error."

        # Persist AI turn IMMEDIATELY after generating response
        save_conversation(self.call_id, "AI", response)
        return response

    # Phrases that mean the patient has no preferred doctor.
    # Covers bare "No", "None", "Anyone", "Recommend one", "Whichever",
    # "I don't have one", "It doesn't matter", "You choose", "Up to you", etc.
    _NO_DOCTOR_PHRASES = re.compile(
        r"\b(no|none|don'?t|do not|not|any|whoever|whichever|recommend|assign|"
        r"no preference|no preferred|doesn'?t matter|you choose|up to you|"
        r"anyone|i don'?t have|doesn'?t matter|either one|any one|go ahead|"
        r"whatever|your choice|your call|i don'?t mind)\b",
        re.IGNORECASE,
    )

    # Filler phrases to strip before treating the whole response as a name
    _NAME_FILLERS = re.compile(
        r"^(it'?s|its|the (patient'?s? )?name is|name is|patient is|"
        r"my name is|i am|i'?m|call me|for)\s+",
        re.IGNORECASE,
    )

    def _parse_for_state(self, user_text: str, nlp_result: dict) -> None:
        """
        For slot-filling states, directly parse the user response instead of
        relying on NLP patterns that expect full sentences.
        Mutates nlp_result['entities'] in-place so _update_entities picks it up.
        """
        state = self.state
        ents  = nlp_result["entities"]
        text  = user_text.strip()

        if state == DialogueState.GET_PATIENT_NAME:
            name = self._NAME_FILLERS.sub("", text).strip()
            if name:
                ents["patient_name"] = name.title()
                save_ai_log("DM", "INFO", f"State-parse GET_PATIENT_NAME -> {ents['patient_name']}")

        elif state == DialogueState.GET_DATE:
            date_val = nlp_module.extract_date(text)
            if date_val:
                ents["date"] = date_val
                save_ai_log("DM", "INFO", f"State-parse GET_DATE -> {date_val}")

        elif state == DialogueState.GET_TIME:
            time_val = nlp_module.extract_time(text)
            if time_val:
                ents["time"] = time_val
                save_ai_log("DM", "INFO", f"State-parse GET_TIME -> {time_val}")

        elif state == DialogueState.GET_SYMPTOMS:
            raw = [s.strip() for s in re.split(r",|\band\b", text, flags=re.IGNORECASE) if s.strip()]
            if raw:
                ents["symptoms"] = raw
                ents["medical_need"] = True
                save_ai_log("DM", "INFO", f"State-parse GET_SYMPTOMS -> {raw}")

        elif state == DialogueState.ASK_DOCTOR_PREFERENCE:
            if self._NO_DOCTOR_PHRASES.search(text):
                # User expressed no preference. We set a dedicated flag rather
                # than ents["doctor"]=None because _update_entities silently
                # drops None values (correct behaviour for NLP-provided entities
                # but wrong here where None is intentional).
                self._no_doctor_preference = True
                save_ai_log("DM", "INFO", "State-parse ASK_DOCTOR_PREFERENCE -> no preference (flag set)")
            else:
                m = re.search(r"\b(?:dr\.?|doctor)\s+([A-Za-z]+)", text, re.IGNORECASE)
                if m:
                    ents["doctor"] = f"Dr. {m.group(1).capitalize()}"
                else:
                    bare = self._NAME_FILLERS.sub("", text).strip()
                    if bare:
                        ents["doctor"] = f"Dr. {bare.split()[0].capitalize()}"
                save_ai_log("DM", "INFO", f"State-parse ASK_DOCTOR_PREFERENCE -> {ents.get('doctor')}")

    def _update_entities(self, new_entities: dict):
        for k, v in new_entities.items():
            if v is not None:
                self.entities[k] = v

    # ── Workflow state handlers ───────────────────────────────────────────────

    def _handle_intent_capture(self, nlp_result: dict) -> str:
        self.intent = nlp_result["intent"]
        save_ai_log("NLP", "INFO", f"Intent={self.intent}")

        if self.intent == "general_inquiry":
            self.state = DialogueState.FAREWELL
            save_ai_log("DM", "INFO", "State -> FAREWELL")
            return "For general inquiries, our clinic is open Monday to Saturday, 9 AM to 8 PM. Is there anything else you need?"

        if self.intent != "book_appointment":
            # Non-booking intents go straight to confirmation
            self.state = DialogueState.CONFIRMATION
            save_ai_log("DM", "INFO", "State -> CONFIRMATION")
            return self._generate_confirmation_prompt()

        # Booking: advance through workflow, skipping already-filled slots
        return self._advance_booking_flow(from_state=DialogueState.INTENT_CAPTURE)

    def _advance_booking_flow(self, from_state: str) -> str:
        """Walk the booking workflow in order, skipping slots already filled or answered."""
        # If doctor is already known we can skip the doctor preference question entirely
        if self.entities.get("doctor") and self.entities.get("department"):
            self._answered.add(DialogueState.ASK_DOCTOR_PREFERENCE)
        steps = [
            (DialogueState.GET_SYMPTOMS,           lambda: not self.entities.get("symptoms")),
            (DialogueState.ASK_DOCTOR_PREFERENCE,  lambda: not self.entities.get("doctor")),
            (DialogueState.GET_DATE,               lambda: not self.entities.get("date")),
            (DialogueState.GET_TIME,               lambda: not self.entities.get("time")),
            (DialogueState.GET_PATIENT_NAME,       lambda: not self.entities.get("patient_name")),
        ]
        for state, needs_asking in steps:
            if state in self._answered:
                continue          # user already responded to this step, skip
            if needs_asking():
                self.state = state
                save_ai_log("DM", "INFO", f"State -> {state}")
                return self._question_for(state)

        # All slots filled — go to confirmation
        self.state = DialogueState.CONFIRMATION
        save_ai_log("DM", "INFO", "State -> CONFIRMATION")
        return self._generate_confirmation_prompt()

    def _question_for(self, state: str) -> str:
        return {
            DialogueState.GET_SYMPTOMS:         "What symptoms are you experiencing?",
            DialogueState.ASK_DOCTOR_PREFERENCE: "Do you have a preferred doctor? If not, I can assign one for you.",
            DialogueState.GET_DATE:             "What date would you prefer for the appointment?",
            DialogueState.GET_TIME:             "What time works best for you?",
            DialogueState.GET_PATIENT_NAME:     "May I have the patient's name please?",
        }[state]

    def _infer_department(self, symptoms: list[str]) -> str:
        """Score departments by summing priorities of matched symptom keywords."""
        scores: dict[str, int] = {}
        lower_symptoms = [s.lower() for s in symptoms]
        for keyword, (dept, priority) in config.SYMPTOM_TO_DEPARTMENT.items():
            if any(keyword in s for s in lower_symptoms):
                scores[dept] = scores.get(dept, 0) + priority
        if not scores:
            return config.DEFAULT_DEPARTMENT
        return max(scores, key=lambda d: scores[d])

    def _handle_get_symptoms(self) -> str:
        self._answered.add(DialogueState.GET_SYMPTOMS)
        symptoms = self.entities.get("symptoms")
        if symptoms:
            dept = self._infer_department(symptoms)
            self.entities["department"] = dept
            save_ai_log("DM", "INFO", f"Department inferred: {dept} from symptoms={symptoms}")
            dept_msg = f"Based on your symptoms, {dept} would be the most appropriate department."
            return dept_msg + " " + self._advance_booking_flow(from_state=DialogueState.GET_SYMPTOMS)
        return self._question_for(DialogueState.GET_SYMPTOMS)

    def _handle_ask_doctor_preference(self) -> str:
        self._answered.add(DialogueState.ASK_DOCTOR_PREFERENCE)
        doctor     = self.entities.get("doctor")
        department = self.entities.get("department", config.DEFAULT_DEPARTMENT)
        date       = self.entities.get("date")
        time       = self.entities.get("time")

        # Honour the explicit "no preference" flag set by _parse_for_state.
        # When the user says "No" / "Anyone" / "Recommend one" in this state,
        # _no_doctor_preference is True and we should auto-assign — NOT treat
        # it as a denial of the entire conversation.
        if self._no_doctor_preference or not doctor:
            # Patient has no preference — find the first available slot in the department
            slot = find_available_slot(department, date, time)
            if slot:
                self._apply_suggestion(slot)
                save_ai_log("DM", "INFO", f"Auto-assigned slot: {slot['doctor_name']} on {slot['slot_date']} at {slot['slot_time']}")
                return (
                    f"I found an available appointment with {slot['doctor_name']} "
                    f"on {slot['slot_date']} at {slot['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
            # No slot on the requested date — try any date in the department
            slot = find_available_slot(department)
            if slot:
                self._apply_suggestion(slot)
                save_ai_log("DM", "INFO", f"No slot on requested date; nearest: {slot['doctor_name']} {slot['slot_date']}")
                return (
                    f"There are no slots on {date or 'that date'} in {department}. "
                    f"The nearest available appointment is with {slot['doctor_name']} "
                    f"on {slot['slot_date']} at {slot['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
            # Truly no slots anywhere — ask for a different date
            save_ai_log("DM", "WARN", f"No slots found in {department}")
            self.entities.pop("date", None)
            self.entities.pop("time", None)
            self._answered.discard(DialogueState.GET_DATE)
            self._answered.discard(DialogueState.GET_TIME)
            self.state = DialogueState.GET_DATE
            self.retry_context = {
                "state": DialogueState.ASK_DOCTOR_PREFERENCE,
                "doctor": doctor,
                "department": department,
                "reason": "no_slots"
            }
            return f"I'm sorry, there are currently no available slots in {department}. Could you suggest a different date?"

        # Patient named a doctor — validate and check availability
        info = find_doctor_slots(doctor, department)

        if not info["exists"]:
            save_ai_log("DM", "WARN", f"Doctor not found: {doctor}")
            # Fall back to auto-assign in the same department
            slot = find_available_slot(department, date, time)
            if slot:
                self.entities.pop("doctor", None)  # clear the unknown doctor
                self._apply_suggestion(slot)
                return (
                    f"I couldn't find a doctor named {doctor} in our system. "
                    f"However, {slot['doctor_name']} is available on {slot['slot_date']} at {slot['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
            return f"I couldn't find a doctor named {doctor} and there are no other available slots in {department} right now."

        if info["wrong_dept"]:
            # Doctor exists but in a different department — inform and reassign
            save_ai_log("DM", "INFO", f"{doctor} is in {info['actual_dept']}, not {department}")
            self.entities["department"] = info["actual_dept"]

        if not info["slots"]:
            # Doctor exists but fully booked — suggest another doctor in same department
            save_ai_log("DM", "WARN", f"{info['doctor_name']} has no free slots")
            alt = find_available_slot(department, date, time)
            if alt:
                self.entities.pop("doctor", None)
                self._apply_suggestion(alt)
                return (
                    f"{info['doctor_name']} has no available slots. "
                    f"However, {alt['doctor_name']} is available on {alt['slot_date']} at {alt['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
            return f"{info['doctor_name']} is fully booked and there are no other available slots in {department} right now."

        # Doctor exists and has slots — pick the best matching one
        best = self._best_slot(info["slots"], date, time)
        if best:
            best["doctor_name"] = info["doctor_name"]
            self.entities["doctor"] = info["doctor_name"]
            self._apply_suggestion(best)
            # Exact slot match
            if date and time and best["slot_date"] == date and best["slot_time"] == time:
                save_ai_log("DM", "INFO", f"Exact slot confirmed: {info['doctor_name']} {best['slot_date']} {best['slot_time']}")
                return (
                    f"{info['doctor_name']} is available on {best['slot_date']} at {best['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
            # Nearest available slot (requested slot was taken)
            nearest = find_nearest_slot(info["doctor_id"], date, time)
            if nearest:
                nearest["doctor_name"] = info["doctor_name"]
                self._apply_suggestion(nearest)
                save_ai_log("DM", "INFO", f"Nearest slot suggested: {info['doctor_name']} {nearest['slot_date']} {nearest['slot_time']}")
                return (
                    f"The requested slot is unavailable. The nearest available appointment with "
                    f"{info['doctor_name']} is on {nearest['slot_date']} at {nearest['slot_time']}. "
                    + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
                )
        # Doctor fully booked — offer another in same department
        save_ai_log("DM", "WARN", f"{info['doctor_name']} has no free slots")
        alt = find_available_slot(department, date, time)
        if alt:
            self.entities.pop("doctor", None)
            self._apply_suggestion(alt)
            return (
                f"{info['doctor_name']} has no available slots. "
                f"However, {alt['doctor_name']} is available on {alt['slot_date']} at {alt['slot_time']}. "
                + self._advance_booking_flow(from_state=DialogueState.ASK_DOCTOR_PREFERENCE)
            )
        return f"{info['doctor_name']} is fully booked and there are no other available slots in {department} right now."

    def _apply_suggestion(self, slot: dict) -> None:
        """Store a found slot into entities so downstream steps are skipped."""
        self.entities["date"]   = slot["slot_date"]
        self.entities["time"]   = slot["slot_time"]
        self.entities["doctor"] = slot["doctor_name"]
        self._answered.update({DialogueState.GET_DATE, DialogueState.GET_TIME})

    def _best_slot(self, slots: list[dict], preferred_date: str | None, preferred_time: str | None) -> dict:
        """Return the slot that best matches preferred date/time, else the earliest."""
        if preferred_date and preferred_time:
            for s in slots:
                if s["slot_date"] == preferred_date and s["slot_time"] == preferred_time:
                    return s
        if preferred_date:
            for s in slots:
                if s["slot_date"] == preferred_date:
                    return s
        return slots[0]  # earliest available

    def _handle_get_date(self) -> str:
        self._answered.add(DialogueState.GET_DATE)
        if self.entities.get("date"):
            if self.retry_context and self.retry_context.get("state") == DialogueState.ASK_DOCTOR_PREFERENCE:
                self.state = DialogueState.ASK_DOCTOR_PREFERENCE
                self.retry_context = None
                return self._handle_ask_doctor_preference()

            # If we arrived here after a no-slots redirect, re-run doctor preference
            if DialogueState.ASK_DOCTOR_PREFERENCE in self._answered:
                self._answered.discard(DialogueState.ASK_DOCTOR_PREFERENCE)
            return self._advance_booking_flow(from_state=DialogueState.GET_DATE)
        return self._question_for(DialogueState.GET_DATE)

    def _handle_get_time(self) -> str:
        self._answered.add(DialogueState.GET_TIME)
        if self.entities.get("time"):
            if self.retry_context and self.retry_context.get("state") == DialogueState.ASK_DOCTOR_PREFERENCE:
                self.state = DialogueState.ASK_DOCTOR_PREFERENCE
                self.retry_context = None
                return self._handle_ask_doctor_preference()

            return self._advance_booking_flow(from_state=DialogueState.GET_TIME)
        return self._question_for(DialogueState.GET_TIME)

    def _handle_get_patient_name(self) -> str:
        self._answered.add(DialogueState.GET_PATIENT_NAME)
        if self.entities.get("patient_name"):
            return self._advance_booking_flow(from_state=DialogueState.GET_PATIENT_NAME)
        return self._question_for(DialogueState.GET_PATIENT_NAME)

    def _handle_confirmation(self, nlp_result: dict) -> str:
        raw = nlp_result.get("_raw_text", "").strip().lower()
        intent = nlp_result["intent"]
        if raw in ("yes", "y", "yeah", "yep", "correct", "sure", "ok", "okay", "हाँ", "हां"):
            intent = "affirm"
        elif raw in ("no", "n", "nope", "nahi", "नहीं"):
            intent = "deny"

        if intent == "affirm":
            self.state = DialogueState.ACTION
            return self._execute_action()

        # Check whether the user is making a partial correction rather than a full restart
        # A correction contains at least one new entity (date, time, doctor, name)
        new_ents = nlp_result["entities"]
        correction_keys = {k for k in ("date", "time", "doctor", "patient_name") if new_ents.get(k)}

        if correction_keys:
            # Partial update — only overwrite what the user explicitly changed
            for k in correction_keys:
                self.entities[k] = new_ents[k]
                save_ai_log("DM", "INFO", f"Correction applied: {k}={new_ents[k]}")
            # If date or time changed, re-run availability to keep doctor/slot consistent
            if correction_keys & {"date", "time", "doctor"}:
                dept = self.entities.get("department", config.DEFAULT_DEPARTMENT)
                doctor = self.entities.get("doctor")
                date   = self.entities.get("date")
                time   = self.entities.get("time")
                if doctor:
                    info = find_doctor_slots(doctor, dept)
                    if info["exists"] and info["slots"]:
                        best = self._best_slot(info["slots"], date, time)
                        if best:
                            best["doctor_name"] = info["doctor_name"]
                            self._apply_suggestion(best)
                    else:
                        slot = find_available_slot(dept, date, time)
                        if slot:
                            self._apply_suggestion(slot)
                else:
                    slot = find_available_slot(dept, date, time)
                    if slot:
                        self._apply_suggestion(slot)
            return self._generate_confirmation_prompt()

        if intent == "deny":
            # Pure denial with no correction — full restart
            self.state = DialogueState.INTENT_CAPTURE
            self.intent = None
            self.entities = {}
            self._answered = set()
            self._pending_suggestion = None
            self._no_doctor_preference = False  # reset preference flag on restart
            self.retry_context = None
            return "Alright, let's start over. What would you like to do?"

        return "Please confirm with yes or no, or tell me what you'd like to change. " + self._generate_confirmation_prompt()

    def _generate_confirmation_prompt(self) -> str:
        if self.intent == "book_appointment":
            doc  = self.entities.get("doctor") or "To be assigned"
            dept = self.entities.get("department") or "General Medicine"
            symp = self.entities.get("symptoms")
            symp_str = ", ".join(symp) if symp else "Not specified"
            name = self.entities.get("patient_name") or "Not provided"
            date = self.entities.get("date") or "Not specified"
            time = self.entities.get("time") or "Not specified"
            return (
                f"Please confirm the following appointment details:\n"
                f"  Patient   : {name}\n"
                f"  Department: {dept}\n"
                f"  Doctor    : {doc}\n"
                f"  Date      : {date}\n"
                f"  Time      : {time}\n"
                f"  Symptoms  : {symp_str}\n"
                f"Is everything correct?"
            )
        elif self.intent == "cancel_appointment":
            return (
                f"Please confirm:\n"
                f"  Cancel appointment on {self.entities.get('date')} "
                f"for {self.entities.get('patient_name')}.\n"
                f"Is that correct?"
            )
        elif self.intent == "reschedule_appointment":
            return (
                f"Please confirm:\n"
                f"  Reschedule appointment for {self.entities.get('patient_name')} "
                f"to {self.entities.get('date')} at {self.entities.get('time')}.\n"
                f"Is that correct?"
            )
        elif self.intent == "check_availability":
            return f"You want to check availability for {self.entities.get('date')}. Is that correct?"
        return "Is this correct?"

    def _execute_action(self) -> str:
        self.state = DialogueState.FAREWELL
        save_ai_log("DM", "INFO", f"State changed: CONFIRMATION -> FAREWELL | Executing intent={self.intent}")
        from services import appointments
        
        if self.intent == "book_appointment":
            success, msg, context = appointments.book_appointment(
                name=self.entities.get("patient_name"),
                doctor=self.entities.get("doctor"),
                department=self.entities.get("department"),
                symptoms=self.entities.get("symptoms", []),
                date=self.entities.get("date"),
                time=self.entities.get("time")
            )
            return msg
            
        elif self.intent == "cancel_appointment":
            success, msg = appointments.cancel_appointment(
                self.entities.get("patient_name"),
                self.entities.get("date")
            )
            return msg
            
        elif self.intent == "reschedule_appointment":
            success, msg = appointments.reschedule_appointment(
                self.entities.get("patient_name"),
                self.entities.get("date"),
                self.entities.get("time")
            )
            return msg
            
        elif self.intent == "check_availability":
            success, msg = appointments.check_availability(
                self.entities.get("date")
            )
            return msg
            
        end_call(self.call_id)
        return "Action completed successfully."
