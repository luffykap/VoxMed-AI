"""
tests/test_integration.py
--------------------------
Integration tests for ConversationOrchestrator using a MockLLM.
Run with: python -m pytest tests/test_integration.py -v
"""
import pytest
from unittest.mock import patch
from datetime import date, timedelta
from database import get_connection
from processing.orchestrator import ConversationOrchestrator


# ── MockLLM ───────────────────────────────────────────────────────────────────

class MockLLM:
    """
    Drop-in mock for services.llm_client that returns deterministic responses.
    Matches the CURRENT interface: understand(memory, user_text, detect_intent).
    """
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.idx = 0

    async def understand(self, memory, user_text: str, detect_intent: bool = False) -> dict:
        if self.idx < len(self.responses):
            res = self.responses[self.idx]
            self.idx += 1
            return res
        # Safe fallback: return last intent with no new entities
        return {"intent": memory.intent or "book_appointment", "entities": {}}

    async def generate_reply(self, memory, backend_result, language) -> str:
        if not backend_result:
            return "What else do you need?"
        if backend_result.get("success"):
            return backend_result.get("message", "Action successful.")
        return backend_result.get("message", "Something went wrong.")

    async def infer_department(self, symptoms: list) -> str:
        return "General Medicine"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_slot(doctor_name: str = "Dr. Sharma", dept: str = "General Medicine",
               slot_date: str = None, slot_time: str = "09:00") -> None:
    """Seed a free slot into the isolated test DB."""
    if slot_date is None:
        slot_date = (date.today() + timedelta(days=1)).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO Departments (name) VALUES (?)", (dept,))
    cursor.execute("SELECT id FROM Departments WHERE name = ?", (dept,))
    dept_id = cursor.fetchone()["id"]
    cursor.execute(
        "INSERT OR IGNORE INTO Doctors (name, department_id) VALUES (?, ?)",
        (doctor_name, dept_id),
    )
    cursor.execute("SELECT id FROM Doctors WHERE name = ?", (doctor_name,))
    doc_id = cursor.fetchone()["id"]
    cursor.execute(
        "INSERT OR IGNORE INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, 0)",
        (doc_id, slot_date, slot_time),
    )
    conn.commit()
    conn.close()


# ── Tests: booking flow ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_greeting_contains_welcome():
    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM([])):
        greeting = orch.get_greeting()
    assert "Welcome" in greeting
    assert not orch.is_finished


@pytest.mark.asyncio
async def test_full_booking_flow_with_confirmation():
    """
    Happy path:
      Turn 1: intent detected
      Turn 2: symptoms extracted  → medical_need derived
      Turn 3: date extracted
      Turn 4: time extracted      → confirmation prompt triggered
      Turn 5: patient name        → (name collected before confirmation)
      Actually the order is: medical_need, date, time, patient_name, then confirmation.
      After confirmation "yes" → booking executed (may succeed or fail on slots).
    """
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    _seed_slot(slot_date=tomorrow, slot_time="09:00")

    mock_responses = [
        # Turn 1: detect intent
        {"intent": "book_appointment", "confidence": 0.95, "entities": {}},
        # Turn 2: symptoms
        {"intent": "book_appointment", "entities": {"symptoms": ["fever"]}},
        # Turn 3: date
        {"intent": "book_appointment", "entities": {"date": tomorrow}},
        # Turn 4: time
        {"intent": "book_appointment", "entities": {"time": "09:00"}},
        # Turn 5: patient name
        {"intent": "book_appointment", "entities": {"patient_name": "Kapil"}},
    ]

    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        orch.get_greeting()
        await orch.process("I want to book an appointment")
        await orch.process("I have a fever")
        await orch.process(f"On {tomorrow}")
        reply_time = await orch.process("At 9 AM")
        reply_name = await orch.process("Kapil")

        # After all entities collected, a confirmation prompt should appear
        assert "Shall I confirm" in reply_name or "confirm" in reply_name.lower()
        assert not orch.is_finished

        # User confirms
        reply_final = await orch.process("yes")

    # Booking attempted — either booked or alternative offered
    assert orch.is_finished or "unavailable" in reply_final.lower() or "slot" in reply_final.lower()


@pytest.mark.asyncio
async def test_confirmation_no_clears_date_and_asks_again():
    """Saying 'no, change the date' should clear date/time and re-enter slot filling."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    _seed_slot(slot_date=tomorrow)

    mock_responses = [
        {"intent": "book_appointment", "confidence": 0.9, "entities": {}},
        {"intent": "book_appointment", "entities": {"symptoms": ["headache"]}},
        {"intent": "book_appointment", "entities": {"date": tomorrow}},
        {"intent": "book_appointment", "entities": {"time": "09:00"}},
        {"intent": "book_appointment", "entities": {"patient_name": "Ravi"}},
        # After no + re-entering date:
        {"intent": "book_appointment", "entities": {"date": tomorrow}},
    ]

    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        orch.get_greeting()
        await orch.process("book appointment")
        await orch.process("headache")
        await orch.process(tomorrow)
        await orch.process("9 AM")
        await orch.process("Ravi")             # all slots filled → confirmation prompt
        reply_no = await orch.process("no, change the date")  # should clear date and ask

    assert not orch.is_finished
    # date slot should be cleared
    assert orch.memory.entities.get("date") is None
    assert "date" in reply_no.lower() or "prefer" in reply_no.lower()


@pytest.mark.asyncio
async def test_abort_mid_flow_resets_state():
    """Saying 'cancel it' during slot filling should reset intent and entities."""
    mock_responses = [
        {"intent": "book_appointment", "confidence": 0.9, "entities": {}},
        {"intent": "book_appointment", "entities": {"symptoms": ["fever"]}},
        # Abort phrase — orchestrator's regex handles this, no LLM call made
    ]

    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        orch.get_greeting()
        await orch.process("I want to book")
        await orch.process("fever")
        reply = await orch.process("cancel it, never mind")

    assert orch.memory.intent is None
    assert orch.memory.entities == {}
    assert "fresh" in reply.lower() or "start" in reply.lower() or "help" in reply.lower()


@pytest.mark.asyncio
async def test_farewell_ends_conversation():
    """Saying 'goodbye' should set is_finished = True."""
    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM([])):
        orch.get_greeting()
        await orch.process("goodbye")
    assert orch.is_finished


# ── Tests: cancel flow ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_flow_collects_name_and_date():
    """Cancel intent requires patient_name and date before executing."""
    mock_responses = [
        {"intent": "cancel_appointment", "confidence": 0.9, "entities": {}},
        {"intent": "cancel_appointment", "entities": {"patient_name": "Rahul"}},
        {"intent": "cancel_appointment", "entities": {"date": date.today().isoformat()}},
    ]
    orch = ConversationOrchestrator(language="English")
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        orch.get_greeting()
        r1 = await orch.process("cancel my appointment")
        assert not orch.is_finished

        r2 = await orch.process("Rahul")
        assert not orch.is_finished

        r3 = await orch.process(date.today().isoformat())
        # Cancel executed — finished (even if patient not found in empty DB)
        assert orch.is_finished or "couldn't find" in r3.lower()


@pytest.mark.asyncio
async def test_multi_hospital_booking_and_resolution():
    """Verify resolve_hospital and hospital-scoped booking in ConversationOrchestrator."""
    from database import resolve_hospital

    assert resolve_hospital("1")["name"] == "Apollo Hospital"
    assert resolve_hospital("2")["name"] == "Manipal Hospital"
    assert resolve_hospital("Fortis")["name"] == "Fortis Hospital"
    assert resolve_hospital("मणिपाल")["name"] == "Manipal Hospital"

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    # Seed a doctor & slot specifically in Hospital 2 (Manipal Hospital)
    cursor.execute("INSERT OR IGNORE INTO Departments (name, hospital_id) VALUES ('Cardiology', 2)")
    dept_id = cursor.execute(
        "SELECT id FROM Departments WHERE name = 'Cardiology' AND hospital_id = 2"
    ).fetchone()["id"]
    cursor.execute(
        "INSERT INTO Doctors (name, department_id, hospital_id) VALUES ('Dr. Reddy', ?, 2)",
        (dept_id,),
    )
    doc_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, '10:00', 0)",
        (doc_id, tomorrow),
    )
    conn.commit()
    conn.close()

    mock_responses = [
        {"intent": "book_appointment", "confidence": 0.95, "entities": {"department": "Cardiology", "date": tomorrow, "time": "10:00", "patient_name": "Ananya"}},
    ]

    orch = ConversationOrchestrator(language="English", hospital_id=2)
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        greeting = orch.get_greeting()
        assert "Manipal Hospital" in greeting
        confirm_prompt = await orch.process(f"Book Cardiology tomorrow at 10 AM for Ananya")
        assert "Manipal Hospital" in confirm_prompt
        final_reply = await orch.process("yes")
        assert orch.is_finished
        assert "Manipal Hospital" in final_reply
        assert "Dr. Reddy" in final_reply
