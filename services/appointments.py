"""
services/appointments.py
------------------------
Business logic for managing appointments with the new 9-table schema.
"""
from typing import Optional
from database import get_connection
from utils.logger import get_logger

logger = get_logger(__name__)

def _get_or_create_patient(name: str, phone: str = None) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM Patients WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    if row:
        patient_id = row['id']
    else:
        cursor.execute("INSERT INTO Patients (name, phone) VALUES (?, ?)", (name, phone))
        conn.commit()
        patient_id = cursor.lastrowid
        logger.info("Created new patient: %s", name)
    conn.close()
    return patient_id

def _map_symptoms_to_department(symptoms: list[str] | None) -> str | None:
    if not symptoms:
        return None
        
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # Check database cache first
        for symp in symptoms:
            cursor.execute("SELECT department_name FROM SymptomMappings WHERE symptom = ? COLLATE NOCASE", (symp.lower(),))
            row = cursor.fetchone()
            if row:
                return row['department_name']
                
        # If not found in DB, infer using LLM (auto-learning)
        from services import llm_client
        inferred_dept = llm_client.infer_department(symptoms)
        
        # Save the new mapping to the database cache
        first_symptom = symptoms[0].lower()
        cursor.execute(
            "INSERT OR IGNORE INTO SymptomMappings (symptom, department_name) VALUES (?, ?)", 
            (first_symptom, inferred_dept)
        )
        conn.commit()
        
        logger.info("Auto-learned new symptom '%s' mapped to '%s'", first_symptom, inferred_dept)
        return inferred_dept
    finally:
        conn.close()

def _get_doctor_id(doctor_name: str, cursor) -> Optional[int]:
    """Find doctor ID. Very basic match for now."""
    if not doctor_name:
        return None
    cursor.execute("SELECT id FROM Doctors WHERE name LIKE ?", (f"%{doctor_name}%",))
    row = cursor.fetchone()
    return row['id'] if row else None

def book_appointment(name: str, doctor: str, department: str, symptoms: list[str], date: str, time: str) -> tuple[bool, str, dict]:
    from datetime import date as _date
    # Reject past dates immediately
    if date and date != "ANY":
        try:
            if date < _date.today().isoformat():
                return False, f"The date {date} is in the past. Please choose a date from today onwards.", {}
        except (ValueError, TypeError):
            pass
    patient_id = _get_or_create_patient(name)
    conn = get_connection()
    cursor = conn.cursor()
    
    doctor_id = _get_doctor_id(doctor, cursor)
    resolved_dept = department
    
    if not doctor_id:
        if not resolved_dept:
            resolved_dept = _map_symptoms_to_department(symptoms) or "General Medicine"
            
        cursor.execute("SELECT id FROM Departments WHERE name = ? COLLATE NOCASE", (resolved_dept,))
        dept_row = cursor.fetchone()
        
        if dept_row:
            dept_id = dept_row['id']
            # Find any doctor in this department that has a free slot on date/time
            cursor.execute('''
                SELECT s.doctor_id FROM Slots s
                JOIN Doctors d ON s.doctor_id = d.id
                WHERE d.department_id = ? AND s.slot_date = ? AND s.slot_time = ? AND s.is_booked = 0
                LIMIT 1
            ''', (dept_id, date, time))
            row = cursor.fetchone()
            if row:
                doctor_id = row['doctor_id']

    if not doctor_id:
        conn.close()
        return False, f"Sorry, there are no doctors available in {resolved_dept or 'General Medicine'} on {date} at {time}.", {}

    # Get doctor name for the response
    cursor.execute("SELECT name FROM Doctors WHERE id = ?", (doctor_id,))
    final_doctor_name = cursor.fetchone()['name']

    # Find the specific slot
    cursor.execute('''
        SELECT id, is_booked FROM Slots
        WHERE doctor_id = ? AND slot_date = ? AND slot_time = ?
    ''', (doctor_id, date, time))
    slot = cursor.fetchone()
    
    if not slot:
        conn.close()
        return False, f"Sorry, {final_doctor_name} does not have a shift on {date} at {time}.", {}
    
    if slot['is_booked']:
        conn.close()
        return False, f"Sorry, that slot is already booked on {date} at {time}.", {}
        
    # Book the slot
    cursor.execute("UPDATE Slots SET is_booked = 1 WHERE id = ?", (slot['id'],))
    cursor.execute('''
        INSERT INTO Appointments (patient_id, doctor_id, slot_id, status)
        VALUES (?, ?, ?, 'BOOKED')
    ''', (patient_id, doctor_id, slot['id']))
    
    conn.commit()
    conn.close()
    
    context = {
        "doctor": final_doctor_name,
        "department": resolved_dept,
        "date": date,
        "time": time
    }
    
    if symptoms:
        msg = f"Since you are experiencing {symptoms[0]}, I have booked you with {final_doctor_name} in {resolved_dept} on {date} at {time}."
    elif department:
        msg = f"I have booked you with {final_doctor_name} in {resolved_dept} on {date} at {time}."
    else:
        msg = f"Great! Your appointment with {final_doctor_name} on {date} at {time} has been successfully booked."
        
    return True, msg, context

def cancel_appointment(name: str, date: str) -> tuple[bool, str]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM Patients WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, f"Sorry, I couldn't find a patient record for {name}."
    
    patient_id = row['id']
    
    # Find active appointment for this patient on this date
    cursor.execute('''
        SELECT a.id as appt_id, s.id as slot_id 
        FROM Appointments a
        JOIN Slots s ON a.slot_id = s.id
        WHERE a.patient_id = ? AND s.slot_date = ? AND a.status = 'BOOKED'
    ''', (patient_id, date))
    
    appt = cursor.fetchone()
    if not appt:
        conn.close()
        return False, f"Sorry, I couldn't find a booked appointment on {date} for {name}."
        
    # Free the slot and cancel appointment
    cursor.execute("UPDATE Slots SET is_booked = 0 WHERE id = ?", (appt['slot_id'],))
    cursor.execute("UPDATE Appointments SET status = 'CANCELED' WHERE id = ?", (appt['appt_id'],))
    
    conn.commit()
    conn.close()
    
    return True, f"Your appointment on {date} has been successfully canceled."

def reschedule_appointment(name: str, current_date: str, new_date: str, new_time: str) -> tuple[bool, str]:
    from datetime import date as _date
    # Reject past new dates immediately
    if new_date and new_date != "ANY":
        try:
            if new_date < _date.today().isoformat():
                return False, f"The date {new_date} is in the past. Please choose a date from today onwards."
        except (ValueError, TypeError):
            pass
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT id FROM Patients WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, f"Sorry, I couldn't find a patient record for {name}."
        
    patient_id = row['id']
    
    # Find the existing booked appointment on the current_date
    cursor.execute('''
        SELECT a.id as appt_id, a.doctor_id, s.id as old_slot_id 
        FROM Appointments a
        JOIN Slots s ON a.slot_id = s.id
        WHERE a.patient_id = ? AND s.slot_date = ? AND a.status = 'BOOKED'
        LIMIT 1
    ''', (patient_id, current_date))
    
    appt = cursor.fetchone()
    if not appt:
        conn.close()
        return False, f"Sorry, I couldn't find a booked appointment on {current_date} for {name}."
        
    # Find new slot for the SAME doctor on the new date/time
    cursor.execute('''
        SELECT id FROM Slots 
        WHERE doctor_id = ? AND slot_date = ? AND slot_time = ? AND is_booked = 0
    ''', (appt['doctor_id'], new_date, new_time))
    new_slot = cursor.fetchone()
    
    if not new_slot:
        # Get doctor name for the error message
        cursor.execute("SELECT name FROM Doctors WHERE id = ?", (appt['doctor_id'],))
        doc_name = cursor.fetchone()['name']
        conn.close()
        return False, f"Sorry, {doc_name} is not available on {new_date} at {new_time}."
        
    # Apply changes: free old slot, mark old appointment, book new slot, create new appointment
    cursor.execute("UPDATE Slots SET is_booked = 0 WHERE id = ?", (appt['old_slot_id'],))
    cursor.execute("UPDATE Appointments SET status = 'RESCHEDULED' WHERE id = ?", (appt['appt_id'],))
    
    cursor.execute("UPDATE Slots SET is_booked = 1 WHERE id = ?", (new_slot['id'],))
    cursor.execute('''
        INSERT INTO Appointments (patient_id, doctor_id, slot_id, status)
        VALUES (?, ?, ?, 'BOOKED')
    ''', (patient_id, appt['doctor_id'], new_slot['id']))
    
    conn.commit()
    conn.close()
    
    return True, f"Your appointment has been successfully rescheduled from {current_date} to {new_date} at {new_time}."

def check_availability(date: str) -> tuple[bool, str]:
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(id) as count FROM Slots 
        WHERE slot_date = ? AND is_booked = 0
    ''', (date,))
    
    count = cursor.fetchone()['count']
    conn.close()
    
    if count > 0:
        return True, f"Yes, there are {count} available slots on {date}."
    else:
        return False, f"Sorry, {date} is fully booked. Would you like to check another day?"


def find_free_slots_for_doctor(doctor_id: int, limit: int = 3) -> list[dict]:
    """
    Return up to `limit` upcoming free slots for the given doctor,
    ordered by date and time. Used by the reschedule retry loop.
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT s.slot_date, s.slot_time, d.name AS doctor_name
            FROM   Slots s
            JOIN   Doctors d ON s.doctor_id = d.id
            WHERE  s.doctor_id = ?
              AND  s.is_booked = 0
              AND  s.slot_date >= ?
            ORDER BY s.slot_date, s.slot_time
            LIMIT ?
            """,
            (doctor_id, today, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("find_free_slots_for_doctor failed | doctor_id=%s", doctor_id)
        return []
    finally:
        conn.close()

