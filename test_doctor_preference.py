"""
Temporary test script for doctor-preference fix.
Run with:  python test_doctor_preference.py
"""
import sys
sys.path.insert(0, ".")
from processing.dialogue import DialogueManager
from processing import nlp as nlp_module


def simulate(label: str, turns: list[str]) -> None:
    print(f"\n=== {label} ===")
    dm = DialogueManager()
    greeting = dm.get_greeting()
    print(f"AI: {greeting}")
    for user_text in turns:
        print(f"User: {user_text}")
        nlp_result = nlp_module.analyse(user_text)
        nlp_result["_raw_text"] = user_text
        response = dm.process(user_text, nlp_result)
        print(f"AI: {response}")


simulate("Test 1: 'No' -> auto-assign -> name -> confirm -> book", [
    "I want to book an appointment.",
    "fever",
    "No",
    "John Smith",
    "yes",
])

simulate("Test 2: 'Anyone is fine'", [
    "I want to book an appointment.",
    "fever",
    "Anyone is fine",
])

simulate("Test 3: 'Recommend one'", [
    "I want to book an appointment.",
    "fever",
    "Recommend one",
])

simulate("Test 4: 'I don't have one'", [
    "I want to book an appointment.",
    "fever",
    "I don't have one",
])

simulate("Test 5: specific doctor (Dr. Gupta for chest pain)", [
    "I want to book an appointment.",
    "chest pain",
    "Dr. Gupta",
])

simulate("Test 6: Full 'No' flow — confirm 'No' in confirmation should restart, not break", [
    "I want to book an appointment.",
    "fever",
    "No",
    "Jane Doe",
    "No",   # <-- deny at confirmation → full restart
])
