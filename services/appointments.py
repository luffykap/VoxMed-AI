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


async def _map_symptoms_to_department(symptoms: list[str] | None) -> str | None:
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
        inferred_dept = await llm_client.infer_department(symptoms)

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

_CLINIC_TZ = "Asia/Kolkata"


def _slot_request_error(date: str, time: str) -> Optional[str]:
    """Validate a requested date/time. Returns an error message, or None if OK."""
    from datetime import datetime, date as _date
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(_CLINIC_TZ))
    except Exception:  # tzdata missing — fall back to server local time
        now = datetime.now()

    if not date or str(date).upper() == "ANY":
        return "Please tell me a specific date for the appointment."
    if not time or str(time).upper() == "ANY":
        return "Please tell me a specific time for the appointment."

    try:
        _date.fromisoformat(date)
    except (ValueError, TypeError):
        return f"I couldn't understand the date {date}. Please give a valid date."

    if date < now.date().isoformat():
        return f"The date {date} is in the past. Please choose a date from today onwards."
    if date == now.date().isoformat() and time < now.strftime("%H:%M"):
        return f"The time {time} has already passed today. Please choose a later time."
    return None


def _claim_slot(cursor, slot_id: int) -> bool:
    """
    Atomically mark a slot as booked. Returns True only if THIS call flipped it
    from free to booked, so two concurrent bookings cannot both succeed.
    """
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ? AND is_booked = ?"),
        _adapt_params([True if _USE_POSTGRES else 1, slot_id, False if _USE_POSTGRES else 0]),
    )
    return cursor.rowcount == 1


async def book_appointment(name: str, doctor: str, department: str, symptoms: list[str], date: str, time: str) -> tuple[bool, str, dict]:
    err = _slot_request_error(date, time)
    if err:
        return False, err, {}

    patient_id = _get_or_create_patient(name)
    conn = get_connection()
    try:
        cursor = conn.cursor()

        doctor_id = _get_doctor_id(doctor, cursor)
        resolved_dept = department

        if not doctor_id:
            # If department is missing, try to infer from symptoms using LLM
            if not department and symptoms:
                resolved_dept = await _map_symptoms_to_department(symptoms) or "General Medicine"

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
            return False, f"Sorry, {final_doctor_name} does not have a shift on {date} at {time}.", {}

        # Atomic claim — protects against concurrent double-booking
        if not _claim_slot(cursor, slot["id"]):
            conn.rollback()
            return False, f"Sorry, that slot is already booked on {date} at {time}.", {}

        cursor.execute(
            _adapt_sql(
                "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, 'BOOKED')"
            ),
            _adapt_params([patient_id, doctor_id, slot["id"]]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
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


def _find_booked_appointments(cursor, name: str, date: str) -> list:
    """
    Find BOOKED appointments on `date` for any patient whose name matches
    `name` (after the same cleaning used at booking time), earliest first.
    """
    clean = _clean_patient_name(name)
    if _USE_POSTGRES:
        name_clause = "lower(p.name) = lower(%s)"
    else:
        name_clause = "p.name = ? COLLATE NOCASE"
    cursor.execute(
        _adapt_sql(f"""
            SELECT a.id AS appt_id, a.patient_id, a.doctor_id, s.id AS slot_id, s.slot_time
            FROM Appointments a
            JOIN Slots s    ON a.slot_id = s.id
            JOIN Patients p ON a.patient_id = p.id
            WHERE {name_clause} AND s.slot_date = ? AND a.status = 'BOOKED'
            ORDER BY s.slot_time
        """),
        _adapt_params([clean, date]),
    )
    return cursor.fetchall()


def cancel_appointment(name: str, date: str) -> tuple[bool, str]:
    conn = get_connection()
    try:
        cursor = conn.cursor()
        appts = _find_booked_appointments(cursor, name, date)
        if not appts:
            return False, f"Sorry, I couldn't find a booked appointment on {date} for {name}."

        appt = appts[0]
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
        return True, f"Your appointment on {date} has been successfully canceled."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reschedule_appointment(name: str, current_date: str, new_date: str, new_time: str) -> tuple[bool, str]:
    err = _slot_request_error(new_date, new_time)
    if err:
        return False, err

    conn = get_connection()
    try:
        cursor = conn.cursor()
        appts = _find_booked_appointments(cursor, name, current_date)
        if not appts:
            return False, f"Sorry, I couldn't find a booked appointment on {current_date} for {name}."

        appt = appts[0]
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
            return False, f"Sorry, {doc_name} is not available on {new_date} at {new_time}."

        # Claim the new slot atomically BEFORE releasing the old one
        if not _claim_slot(cursor, new_slot["id"]):
            conn.rollback()
            return False, f"Sorry, that slot was just taken on {new_date} at {new_time}."

        cursor.execute(
            _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
            _adapt_params([booked_false, appt["slot_id"]]),
        )
        cursor.execute(
            _adapt_sql("UPDATE Appointments SET status = 'RESCHEDULED' WHERE id = ?"),
            _adapt_params([appt["appt_id"]]),
        )
        cursor.execute(
            _adapt_sql(
                "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) "
                "VALUES (?, ?, ?, 'BOOKED')"
            ),
            _adapt_params([appt["patient_id"], appt["doctor_id"], new_slot["id"]]),
        )
        conn.commit()
        return True, f"Your appointment has been successfully rescheduled from {current_date} to {new_date} at {new_time}."
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

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
