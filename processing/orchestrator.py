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
        "date": "What is the current appointment date?",
        "time": "What new time would you like?",
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
            # ⑧ Use predefined template — zero LLM calls for slot questions (Problem 6)
            intent_templates = _SLOT_TEMPLATES.get(self.memory.intent, {})
            reply = intent_templates.get(next_missing)
            if not reply:
                reply = llm_client.generate_reply(self.memory, None, self.memory.language)
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
            else:
                # Booking failed — search for alternatives (Problem 5)
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
                ents.get("date"),
                ents.get("time"),
            )
            backend_result = {"success": success, "message": msg}
            self._finished = True

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

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _commit_reply(self, reply: str) -> str:
        """Persist the AI reply to DB and memory, then return it."""
        self.memory.add_turn("assistant", reply)
        self.memory.last_question = reply
        save_conversation(self.call_id, "AI", reply)
        return reply
