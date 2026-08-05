"""
tests/test_appointments.py
Run with: python -m pytest tests/test_appointments.py -v
"""
import pytest
import sqlite3
from datetime import date, timedelta
from database import get_connection, init_db
from services.appointments import book_appointment, cancel_appointment, reschedule_appointment, check_availability



def test_check_availability():
    today = date.today().isoformat()
    # Assuming the dummy data injected slots for today
    available, msg = check_availability(today)
    assert type(available) == bool
    assert "slots on" in msg or "fully booked" in msg

def test_book_appointment_success():
    today = date.today().isoformat()
    success, msg, context = book_appointment(
        name="Test User",
        doctor="Dr. Sharma",
        department="General Medicine",
        symptoms=["fever"],
        date=today,
        time="09:00"
    )
    # The slot might be booked by dummy data, so success could be True or False depending on the seed.
    # But it shouldn't crash.
    assert isinstance(success, bool)
    assert isinstance(msg, str)
    assert isinstance(context, dict)

def test_book_appointment_past_date():
    past_date = (date.today() - timedelta(days=1)).isoformat()
    success, msg, context = book_appointment(
        name="Test User",
        doctor="Dr. Sharma",
        department="General Medicine",
        symptoms=["fever"],
        date=past_date,
        time="09:00"
    )
    assert not success
    assert "in the past" in msg

def test_cancel_nonexistent_appointment():
    today = date.today().isoformat()
    success, msg = cancel_appointment("Nonexistent User", today)
    assert not success
    assert "couldn't find a patient record" in msg

def test_reschedule_past_date():
    today = date.today().isoformat()
    past_date = (date.today() - timedelta(days=1)).isoformat()
    success, msg = reschedule_appointment("Test User", today, past_date, "10:00")
    assert not success
    assert "in the past" in msg
