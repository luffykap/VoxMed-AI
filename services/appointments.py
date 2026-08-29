"""
services/appointments.py
------------------------
Business logic for managing appointments with the new 9-table schema.
"""
from typing import Optional
from database import get_connection, _USE_POSTGRES, _adapt_sql, _adapt_params
from utils.logger import get_logger

logger = get_logger(__name__)

def _clean_patient_name(raw: str) -> str:
    """
    Normalise a patient name coming from STT/LLM:
    - Strip surrounding whitespace
    - Remove common filler prefixes ("My name is", "It's", etc.)
    - Deduplicate repeated words (e.g. "Couple Couple" → "Couple")
    - Title-case the result
    """
    import re
    name = raw.strip()
    # Remove filler prefixes
    name = re.sub(
        r"^(it'?s|its|the (patient'?s? )?name is|name is|patient is|"
        r"my name is|i am|i'?m|call me|for|the name is)\s+",
        "", name, flags=re.IGNORECASE,
    ).strip()
    # Deduplicate consecutive repeated words: "Couple Couple" → "Couple"
    words = name.split()
    deduped: list[str] = []
    for w in words:
        if not deduped or w.lower() != deduped[-1].lower():
            deduped.append(w)
    name = " ".join(deduped)
    return name.title()

def _get_or_create_patient(name: str, phone: str = None) -> int:
    clean = _clean_patient_name(name)
    conn = get_connection()
    cursor = conn.cursor()

    if _USE_POSTGRES:
        cursor.execute(
            "SELECT id FROM Patients WHERE lower(name) = lower(%s)", (clean,)
        )
    else:
        cursor.execute("SELECT id FROM Patients WHERE name LIKE ? COLLATE NOCASE", (clean,))

    row = cursor.fetchone()
    if row:
        patient_id = row["id"]
    else:
        if _USE_POSTGRES:
            cursor.execute(
                "INSERT INTO Patients (name, phone) VALUES (%s, %s) RETURNING id",
                (clean, phone),
            )
            patient_id = cursor.fetchone()["id"]
        else:
            cursor.execute(
                "INSERT INTO Patients (name, phone) VALUES (?, ?)", (clean, phone)
            )
            patient_id = cursor.lastrowid
        conn.commit()
        logger.info("Created new patient: %s", clean)
    conn.close()
    return patient_id


def _map_symptoms_to_department(symptoms: list[str] | None) -> str | None:
    if not symptoms:
        return None

    conn = get_connection()
    cursor = conn.cursor()

    try:
        for symp in symptoms:
            if _USE_POSTGRES:
                cursor.execute(
                    "SELECT department_name FROM SymptomMappings "
                    "WHERE lower(symptom) = lower(%s)",
                    (symp.lower(),),
                )
            else:
                cursor.execute(
                    "SELECT department_name FROM SymptomMappings "
                    "WHERE symptom = ? COLLATE NOCASE",
                    (symp.lower(),),
                )
            row = cursor.fetchone()
            if row:
                return row["department_name"]

        # Not found in DB — infer via LLM and cache
        from services import llm_client
        inferred_dept = llm_client.infer_department(symptoms)

        first_symptom = symptoms[0].lower()
        if _USE_POSTGRES:
            cursor.execute(
                "INSERT INTO SymptomMappings (symptom, department_name) VALUES (%s, %s) "
                "ON CONFLICT (symptom) DO NOTHING",
                (first_symptom, inferred_dept),
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO SymptomMappings (symptom, department_name) VALUES (?, ?)",
                (first_symptom, inferred_dept),
            )
        conn.commit()

        logger.info("Auto-learned new symptom '%s' mapped to '%s'", first_symptom, inferred_dept)
        return inferred_dept
    finally:
        conn.close()

def _get_doctor_id(doctor_name: str, cursor) -> Optional[int]:
    """Find doctor ID. Uses fuzzy matching to handle STT spelling errors."""
    if not doctor_name:
        return None

    import difflib
    cursor.execute(_adapt_sql("SELECT id, name FROM Doctors"))
    doctors = cursor.fetchall()

    req = doctor_name.lower().replace("dr.", "").replace("dr ", "").strip()

    best_match = None
    best_score = 0

    for row in doctors:
        act = row["name"].lower().replace("dr.", "").replace("dr ", "").strip()
        # Direct substring match (fast path — no score needed)
        if req and act and (req in act or act in req):
            return row["id"]

        score = difflib.SequenceMatcher(None, req, act).ratio()
        if score > best_score and score > 0.6:
            best_score = score
            best_match = row["id"]

    return best_match

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

        if _USE_POSTGRES:
            cursor.execute(
                "SELECT id FROM Departments WHERE lower(name) = lower(%s)", (resolved_dept,)
            )
        else:
            cursor.execute(
                "SELECT id FROM Departments WHERE name = ? COLLATE NOCASE", (resolved_dept,)
            )
        dept_row = cursor.fetchone()

        if dept_row:
            dept_id = dept_row["id"]
            booked_val = False if _USE_POSTGRES else 0
            cursor.execute(
                _adapt_sql("""
                    SELECT s.doctor_id FROM Slots s
                    JOIN Doctors d ON s.doctor_id = d.id
                    WHERE d.department_id = ? AND s.slot_date = ? AND s.slot_time = ? AND s.is_booked = ?
                    LIMIT 1
                """),
                _adapt_params([dept_id, date, time, booked_val]),
            )
            row = cursor.fetchone()
            if row:
                doctor_id = row["doctor_id"]

    if not doctor_id:
        conn.close()
        return False, f"Sorry, there are no doctors available in {resolved_dept or 'General Medicine'} on {date} at {time}.", {}

    # Get doctor name for the response
    cursor.execute(_adapt_sql("SELECT name FROM Doctors WHERE id = ?"), _adapt_params([doctor_id]))
    final_doctor_name = cursor.fetchone()["name"]

    # Find the specific slot
    cursor.execute(
        _adapt_sql("SELECT id, is_booked FROM Slots WHERE doctor_id = ? AND slot_date = ? AND slot_time = ?"),
        _adapt_params([doctor_id, date, time]),
    )
    slot = cursor.fetchone()

    if not slot:
        conn.close()
        return False, f"Sorry, {final_doctor_name} does not have a shift on {date} at {time}.", {}

    if slot["is_booked"]:
        conn.close()
        return False, f"Sorry, that slot is already booked on {date} at {time}.", {}

    # Book the slot
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
        _adapt_params([True if _USE_POSTGRES else 1, slot["id"]]),
    )
    cursor.execute(
        _adapt_sql(
            "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, 'BOOKED')"
        ),
        _adapt_params([patient_id, doctor_id, slot["id"]]),
    )

    conn.commit()
    conn.close()

    context = {
        "doctor": final_doctor_name,
        "department": resolved_dept,
        "date": date,
        "time": time,
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

    if _USE_POSTGRES:
        cursor.execute("SELECT id FROM Patients WHERE lower(name) = lower(%s)", (name,))
    else:
        cursor.execute("SELECT id FROM Patients WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, f"Sorry, I couldn't find a patient record for {name}."

    patient_id = row["id"]

    cursor.execute(
        _adapt_sql("""
            SELECT a.id as appt_id, s.id as slot_id
            FROM Appointments a
            JOIN Slots s ON a.slot_id = s.id
            WHERE a.patient_id = ? AND s.slot_date = ? AND a.status = 'BOOKED'
        """),
        _adapt_params([patient_id, date]),
    )
    appt = cursor.fetchone()
    if not appt:
        conn.close()
        return False, f"Sorry, I couldn't find a booked appointment on {date} for {name}."

    booked_false = False if _USE_POSTGRES else 0
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
        _adapt_params([booked_false, appt["slot_id"]]),
    )
    cursor.execute(
        _adapt_sql("UPDATE Appointments SET status = 'CANCELED' WHERE id = ?"),
        _adapt_params([appt["appt_id"]]),
    )

    conn.commit()
    conn.close()

    return True, f"Your appointment on {date} has been successfully canceled."

def reschedule_appointment(name: str, current_date: str, new_date: str, new_time: str) -> tuple[bool, str]:
    from datetime import date as _date
    if new_date and new_date != "ANY":
        try:
            if new_date < _date.today().isoformat():
                return False, f"The date {new_date} is in the past. Please choose a date from today onwards."
        except (ValueError, TypeError):
            pass

    conn = get_connection()
    cursor = conn.cursor()

    if _USE_POSTGRES:
        cursor.execute("SELECT id FROM Patients WHERE lower(name) = lower(%s)", (name,))
    else:
        cursor.execute("SELECT id FROM Patients WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return False, f"Sorry, I couldn't find a patient record for {name}."

    patient_id = row["id"]

    cursor.execute(
        _adapt_sql("""
            SELECT a.id as appt_id, a.doctor_id, s.id as old_slot_id
            FROM Appointments a
            JOIN Slots s ON a.slot_id = s.id
            WHERE a.patient_id = ? AND s.slot_date = ? AND a.status = 'BOOKED'
            LIMIT 1
        """),
        _adapt_params([patient_id, current_date]),
    )
    appt = cursor.fetchone()
    if not appt:
        conn.close()
        return False, f"Sorry, I couldn't find a booked appointment on {current_date} for {name}."

    booked_false = False if _USE_POSTGRES else 0
    cursor.execute(
        _adapt_sql(
            "SELECT id FROM Slots "
            "WHERE doctor_id = ? AND slot_date = ? AND slot_time = ? AND is_booked = ?"
        ),
        _adapt_params([appt["doctor_id"], new_date, new_time, booked_false]),
    )
    new_slot = cursor.fetchone()

    if not new_slot:
        cursor.execute(
            _adapt_sql("SELECT name FROM Doctors WHERE id = ?"),
            _adapt_params([appt["doctor_id"]]),
        )
        doc_name = cursor.fetchone()["name"]
        conn.close()
        return False, f"Sorry, {doc_name} is not available on {new_date} at {new_time}."

    booked_true = True if _USE_POSTGRES else 1
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
        _adapt_params([booked_false, appt["old_slot_id"]]),
    )
    cursor.execute(
        _adapt_sql("UPDATE Appointments SET status = 'RESCHEDULED' WHERE id = ?"),
        _adapt_params([appt["appt_id"]]),
    )
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
        _adapt_params([booked_true, new_slot["id"]]),
    )
    cursor.execute(
        _adapt_sql(
            "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) "
            "VALUES (?, ?, ?, 'BOOKED')"
        ),
        _adapt_params([patient_id, appt["doctor_id"], new_slot["id"]]),
    )

    conn.commit()
    conn.close()

    return True, f"Your appointment has been successfully rescheduled from {current_date} to {new_date} at {new_time}."

def check_availability(date: str) -> tuple[bool, str]:
    conn = get_connection()
    cursor = conn.cursor()

    booked_false = False if _USE_POSTGRES else 0
    cursor.execute(
        _adapt_sql("SELECT COUNT(id) as count FROM Slots WHERE slot_date = ? AND is_booked = ?"),
        _adapt_params([date, booked_false]),
    )
    count = cursor.fetchone()["count"]
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
    booked_false = False if _USE_POSTGRES else 0
    conn = get_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            _adapt_sql("""
            SELECT s.slot_date, s.slot_time, d.name AS doctor_name
            FROM   Slots s
            JOIN   Doctors d ON s.doctor_id = d.id
            WHERE  s.doctor_id = ?
              AND  s.is_booked = ?
              AND  s.slot_date >= ?
            ORDER BY s.slot_date, s.slot_time
            LIMIT ?
            """),
            _adapt_params([doctor_id, booked_false, today, limit]),
        )
        return [dict(r) for r in cursor.fetchall()]
    except Exception:
        logger.exception("find_free_slots_for_doctor failed | doctor_id=%s", doctor_id)
        return []
    finally:
        conn.close()
