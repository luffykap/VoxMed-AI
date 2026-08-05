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

import config
from database import create_call, end_call, save_conversation, save_ai_log
from processing.memory import ConversationMemory, REQUIRED_ENTITIES
from services import llm_client
from utils.logger import get_logger

logger = get_logger(__name__)


# Predefined slot-filling questions — deterministic, no LLM call needed.
# Python decides which question to ask (Problem 3); LLM only formats complex replies.
_SLOT_TEMPLATES = {
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
        logger.info("ConversationOrchestrator initialized | call_id=%s | lang=%s",
                    self.call_id, language)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def is_finished(self) -> bool:
        return self._finished

    def get_greeting(self) -> str:
        greeting = "Welcome to VoxMed AI. How can I help you today?"
        save_conversation(self.call_id, "AI", greeting)
        self.memory.add_turn("assistant", greeting)
        self.memory.last_question = greeting
        return greeting

    def process(self, user_text: str) -> str:
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

        # ② Determine mode (Problem 1 — intent lock)
        detect_intent = self.memory.intent is None

        # ③ LLM Stage 1
        understanding = llm_client.understand(self.memory, user_text, detect_intent=detect_intent)

        # ④ Handle LLM unavailability (Problem 4 — never fall back to legacy NLP)
        if understanding.get("_api_unavailable"):
            logger.warning("LLM unavailable — preserving memory, asking user to retry")
            reply = "The system is momentarily unavailable. Please repeat your message."
            return self._commit_reply(reply)

        entities = understanding.get("entities") or {}

        # ⑤ Set or lock intent (Problem 1)
        if detect_intent:
            self.memory.intent = understanding.get("intent", "general_inquiry")
            confidence = understanding.get("confidence", 0.8)
            save_ai_log("NLP", "INFO",
                        f"Intent detected: {self.memory.intent} confidence={confidence:.2f}")
        else:
        # Intent remains locked once detected.
        # Ignore LLM intent_switch for now. Python owns the conversation workflow.
            if understanding.get("intent_switch"):
                logger.warning(
                "LLM suggested an intent switch, but intent switching is currently ignored. "
                "Current intent=%s",
                self.memory.intent,
            )

            save_ai_log(
                "NLP",
                "INFO",
                f"Entities extracted (intent locked={self.memory.intent}): {list(entities.keys())}",
            )

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
            if not target_date or target_date == "ANY":
                target_date = _date.today().isoformat()
            
            dept = self.memory.entities.get("department", config.DEFAULT_DEPARTMENT)
            
            conn = get_connection()
            cursor = conn.cursor()
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
            conn.close()
            
            if slots:
                times = ", ".join(s["slot_time"] for s in slots)
                msg = f"On {target_date}, we have free slots at {times}."
            else:
                msg = f"Sorry, there are no available slots in {dept} on {target_date}."
            
            reply = llm_client.generate_reply(self.memory, {"success": bool(slots), "message": msg}, self.memory.language)
            
            # Steer back to the booking flow
            if missing:
                intent_templates = _SLOT_TEMPLATES.get(self.memory.intent, {})
                slot_question = intent_templates.get(next_missing)
                if slot_question:
                    if self.memory.language and self.memory.language.lower() not in ("en", "english"):
                        slot_question = llm_client.generate_reply(self.memory, {"success": True, "message": f"Ask the user: {slot_question}"}, self.memory.language)
                    reply = f"{reply} {slot_question}"
                    
            save_ai_log("DM", "INFO", f"Side query handled. reply_len={len(reply)}")
            return self._commit_reply(reply)

        if missing:
            next_missing = missing[0]
            # ⑧ Use predefined template — zero LLM calls for slot questions (Problem 6)
            intent_templates = _SLOT_TEMPLATES.get(self.memory.intent, {})
            reply = intent_templates.get(next_missing)
            if not reply:
                reply = llm_client.generate_reply(self.memory, None, self.memory.language)
            elif self.memory.language and self.memory.language.lower() not in ("en", "english"):
                # Use LLM to translate the static slot question to the requested language
                reply = llm_client.generate_reply(self.memory, {"success": True, "message": f"Ask the user: {reply}"}, self.memory.language)
                
            save_ai_log("DM", "INFO",
                        f"Slot-filling: next={next_missing} | template={next_missing in _SLOT_TEMPLATES}")
            return self._commit_reply(reply)

        # ⑨ All entities present — execute intent
        reply = self._execute_intent()
        return self._commit_reply(reply)

    # ── Intent execution ──────────────────────────────────────────────────────

    def _execute_intent(self) -> str:
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
                    dept = appointments._map_symptoms_to_department(ents.get("symptoms"))
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
            success, msg, context = appointments.book_appointment(
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
                alternatives = self._find_alternative_slots()
                if alternatives:
                    alt_list = "; ".join(
                        f"{a['slot_date']} at {a['slot_time']} with {a.get('doctor_name', 'available doctor')}"
                        for a in alternatives
                    )
                    backend_result = {
                        "success": False,
                        "message": f"That slot is unavailable. Alternatives: {alt_list}. Which would you prefer?",
                    }
                    # Problem 3: never pop existing entities — only clear the
                    # specific conflicting slot fields so the user can pick a new one.
                    # Use None assignment so update_entities can overwrite them next turn.
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

        else:
            backend_result = {"success": True, "message": ""}

        save_ai_log("DM", "INFO",
                    f"Executed intent={intent} | success={backend_result.get('success')}")

        reply = llm_client.generate_reply(
            memory=self.memory,
            backend_result=backend_result,
            language=self.memory.language,
        )

        if self._finished:
            end_call(self.call_id)

        return reply

    def _find_alternative_slots(self) -> list[dict]:
        """Search the next 7 days for up to 3 available slots in the same department."""
        from database import get_connection
        from datetime import date as _date, timedelta

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

        for day_offset in range(1, 8):
            d = (_date.today() + timedelta(days=day_offset)).isoformat()
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
