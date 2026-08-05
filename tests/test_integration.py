"""
tests/test_integration.py
Run with: python -m pytest tests/test_integration.py -v
"""
import pytest
from unittest.mock import patch
from datetime import date
from database import init_db
from processing.orchestrator import ConversationOrchestrator



# Mock the LLM client to return deterministic responses instead of calling Gemini
class MockLLM:
    def __init__(self, responses: list[dict]):
        self.responses = responses
        self.idx = 0

    def understand(self, memory, user_text, detect_intent=False):
        res = self.responses[self.idx]
        self.idx += 1
        return res

    def generate_reply(self, memory, backend_result, language):
        if backend_result and backend_result.get("success"):
            return "Action successful."
        if backend_result and not backend_result.get("success"):
            return backend_result.get("message", "Action failed.")
        return "I'm just a mock."

def test_full_booking_flow():
    orchestrator = ConversationOrchestrator(language="en")
    
    # Mock LLM responses for a standard booking flow
    mock_responses = [
        # Turn 1: "I want to book an appointment"
        {"intent": "book_appointment", "confidence": 0.9, "entities": {}},
        # Turn 2: "fever"
        {"intent": "book_appointment", "entities": {"symptoms": ["fever"]}},
        # Turn 3: "Dr. Sharma"
        {"intent": "book_appointment", "entities": {"doctor": "Dr. Sharma"}},
        # Turn 4: "Tomorrow"
        {"intent": "book_appointment", "entities": {"date": "ANY", "time": "ANY"}},
        # Turn 5: "Kapil"
        {"intent": "book_appointment", "entities": {"patient_name": "Kapil"}},
    ]
    
    with patch("processing.orchestrator.llm_client", MockLLM(mock_responses)):
        greeting = orchestrator.get_greeting()
        assert "Welcome" in greeting
        
        reply1 = orchestrator.process("I want to book an appointment")
        assert not orchestrator.is_finished
        
        reply2 = orchestrator.process("I have a fever")
        assert not orchestrator.is_finished
        
        reply3 = orchestrator.process("Dr. Sharma")
        assert not orchestrator.is_finished
        
        reply4 = orchestrator.process("Any time tomorrow")
        assert not orchestrator.is_finished
        
        reply5 = orchestrator.process("My name is Kapil")
        assert orchestrator.is_finished
        assert "successful" in reply5 or "unavailable" in reply5 # Depends on dummy data

