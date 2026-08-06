"""
database.py
-----------
SQLite database connection, schema initialization, and dummy data generation.
"""
import sqlite3
import config
from utils.logger import get_logger
from datetime import date, timedelta

logger = get_logger(__name__)

_schema_ensured = False
_cleanup_done   = False

def get_connection() -> sqlite3.Connection:
    """Returns a connection to the SQLite database."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    # Enable foreign keys
    conn.execute("PRAGMA foreign_keys = 1")
    if config.DB_ECHO:
        conn.set_trace_callback(print)
    # Ensure newer tables exist and clean up past data — once per process
    global _schema_ensured
    if not _schema_ensured:
        _ensure_schema(conn)
        _schema_ensured = True
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Create tables that may be missing from an older voxmed.db
    WITHOUT dropping or altering existing tables.
    Also prunes past slots/appointments on first connection each session.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='SymptomMappings'"
    )
    if not cursor.fetchone():
        cursor.execute('''
            CREATE TABLE SymptomMappings (
                symptom TEXT PRIMARY KEY,
                department_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Seed from config.SYMPTOM_TO_DEPARTMENT
        symptom_inserts = [
            (symptom.lower(), dept)
            for symptom, (dept, _) in config.SYMPTOM_TO_DEPARTMENT.items()
        ]
        cursor.executemany(
            "INSERT OR IGNORE INTO SymptomMappings (symptom, department_name) VALUES (?, ?)",
            symptom_inserts,
        )
        conn.commit()
        logger.info("Created missing SymptomMappings table and seeded %d rows", len(symptom_inserts))

    # Clean up past data once per process
    _cleanup_past_slots(conn)


def _cleanup_past_slots(conn: sqlite3.Connection) -> None:
    """
    Remove slots from past dates and archive their appointments.
    Runs once per process (guarded by _cleanup_done).
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    from datetime import date as _date
    today = _date.today().isoformat()

    cursor = conn.cursor()

    # Mark ALL appointments whose slot is in the past as COMPLETED
    # (covers BOOKED, RESCHEDULED, CANCELED — any status still pointing to a past slot)
    cursor.execute(
        """
        UPDATE Appointments SET status = 'COMPLETED'
        WHERE slot_id IN (
            SELECT id FROM Slots WHERE slot_date < ?
        )
        """,
        (today,),
    )
    completed = cursor.rowcount

    # Delete past Slots only if they are not referenced by Appointments
    cursor.execute(
        "DELETE FROM Slots WHERE slot_date < ? AND id NOT IN (SELECT slot_id FROM Appointments)", 
        (today,)
    )
    deleted_slots = cursor.rowcount

    conn.commit()
    if deleted_slots:
        logger.info(
            "Past-slot cleanup: archived %d appointments, deleted %d past slots",
            completed, deleted_slots,
        )


def init_db():
    """Initializes the 10-table database schema."""
    logger.info("Initializing database schema at %s", config.DB_PATH)
    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Drop existing tables if they exist to start fresh with new schema
    tables = [
        "Feedback", "AI_Logs", "Conversations", "Calls", 
        "Appointments", "Slots", "Doctors", "Departments", "Patients", "SymptomMappings"
    ]
    for table in tables:
        cursor.execute(f"DROP TABLE IF EXISTS {table}")

    # 2. Create Tables
    cursor.executescript('''
        CREATE TABLE Departments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE Doctors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            department_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (department_id) REFERENCES Departments (id)
        );

        CREATE TABLE Patients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE Slots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doctor_id INTEGER NOT NULL,
            slot_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            is_booked BOOLEAN NOT NULL DEFAULT 0,
            FOREIGN KEY (doctor_id) REFERENCES Doctors (id),
            UNIQUE(doctor_id, slot_date, slot_time)
        );

        CREATE TABLE Appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER NOT NULL,
            doctor_id INTEGER NOT NULL,
            slot_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'BOOKED', -- BOOKED, CANCELED, RESCHEDULED, COMPLETED
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (patient_id) REFERENCES Patients (id),
            FOREIGN KEY (doctor_id) REFERENCES Doctors (id),
            FOREIGN KEY (slot_id) REFERENCES Slots (id)
        );

        CREATE TABLE Calls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER,
            start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            end_time TIMESTAMP,
            status TEXT NOT NULL DEFAULT 'ONGOING', -- ONGOING, COMPLETED, DROPPED
            FOREIGN KEY (patient_id) REFERENCES Patients (id)
        );

        CREATE TABLE Conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            call_id INTEGER NOT NULL,
            speaker TEXT NOT NULL, -- 'AI' or 'Patient'
            transcript TEXT NOT NULL,
            detected_intent TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (call_id) REFERENCES Calls (id)
        );

        CREATE TABLE AI_Logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            module TEXT NOT NULL, -- 'STT', 'NLP', 'TTS', 'DM'
            level TEXT NOT NULL,
            message TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE Feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            rating INTEGER,
            comments TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (appointment_id) REFERENCES Appointments (id)
        );

        CREATE TABLE SymptomMappings (
            symptom TEXT PRIMARY KEY,
            department_name TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    conn.commit()
    logger.info("Database schema initialized successfully.")

    # 3. Inject Dummy Data
    _inject_dummy_data(cursor)
    conn.commit()
    conn.close()

def _inject_dummy_data(cursor):
    import random
    logger.info("Injecting dummy data for testing...")
    random.seed(42)

    # 1. Departments
    departments = [
        "General Medicine", "Cardiology", "Dermatology", "Neurology",
        "Orthopedics", "ENT", "Ophthalmology", "Pediatrics",
        "Gynecology", "Psychiatry", "Gastroenterology"
    ]
    cursor.executemany("INSERT INTO Departments (name) VALUES (?)", [(d,) for d in departments])

    dept_ids = {row['name']: row['id'] for row in cursor.execute("SELECT id, name FROM Departments")}

    # 2. Doctors
    doctor_data = {
        "General Medicine": ["Dr. Sharma", "Dr. Meera", "Dr. Deepti", "Dr. Rahul", "Dr. Nair"],
        "Cardiology": ["Dr. Gupta", "Dr. Patel", "Dr. Joshi", "Dr. Reddy"],
        "Dermatology": ["Dr. Priya", "Dr. Arjun", "Dr. Kapoor", "Dr. Sneha"],
        "Neurology": ["Dr. Verma", "Dr. Ananya", "Dr. Vivek", "Dr. Rao"],
        "Orthopedics": ["Dr. Singh", "Dr. Kiran", "Dr. Thomas", "Dr. Roy"],
        "ENT": ["Dr. Bose", "Dr. Ahmed", "Dr. Ramesh", "Dr. George"],
        "Ophthalmology": ["Dr. Iyer", "Dr. Joseph", "Dr. Neha", "Dr. Khan"],
        "Pediatrics": ["Dr. Anita", "Dr. Kavya", "Dr. Ritu", "Dr. Das"],
        "Gynecology": ["Dr. Pooja", "Dr. Lakshmi", "Dr. Swathi", "Dr. Fernandes"],
        "Psychiatry": ["Dr. Menon", "Dr. Harish", "Dr. Sonia", "Dr. Nikhil"]
    }

    doc_inserts = []
    for dept, doctors in doctor_data.items():
        d_id = dept_ids[dept]
        for doc in doctors:
            doc_inserts.append((doc, d_id))
    cursor.executemany("INSERT INTO Doctors (name, department_id) VALUES (?, ?)", doc_inserts)

    doc_id_list = [row['id'] for row in cursor.execute("SELECT id FROM Doctors")]

    # 3. Slots
    today = date.today()
    times = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
    slot_inserts = []
    
    for doc_id in doc_id_list:
        for day_offset in range(30):
            d_date = (today + timedelta(days=day_offset)).isoformat()
            for t in times:
                is_booked = 1 if random.random() < 0.3 else 0
                slot_inserts.append((doc_id, d_date, t, is_booked))

    cursor.executemany("INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, ?)", slot_inserts)

    # 4. Patients
    patient_names = [
        "Aditi", "Rahul", "Priya", "Ankit", "Neha", "Karan", "Sneha", "Rohit", "Aarav", "Diya",
        "Rohan", "Meera", "Vikram", "Sonia", "Kunal", "Riya", "Nikhil", "Pooja", "Arjun", "Kavita",
        "Siddharth", "Aisha", "Varun", "Simran", "Amit", "Kritika", "Raj", "Tanya", "Akash", "Shruti",
        "Manish", "Pallavi", "Gaurav", "Nisha", "Deepak", "Anjali", "Sameer", "Preeti", "Ravi", "Swati"
    ]
    patient_inserts = []
    for name in patient_names:
        phone = f"98{random.randint(10000000, 99999999)}"
        patient_inserts.append((name, phone))
    cursor.executemany("INSERT INTO Patients (name, phone) VALUES (?, ?)", patient_inserts)

    patient_ids = [row['id'] for row in cursor.execute("SELECT id FROM Patients")]

    # 5. Appointments
    booked_slots = cursor.execute("SELECT id, doctor_id FROM Slots WHERE is_booked = 1").fetchall()
    selected_slots = random.sample(booked_slots, min(60, len(booked_slots)))
    
    appointment_inserts = []
    statuses = ['BOOKED', 'COMPLETED', 'CANCELED', 'RESCHEDULED']
    for slot in selected_slots:
        p_id = random.choice(patient_ids)
        status = random.choice(statuses)
        appointment_inserts.append((p_id, slot['doctor_id'], slot['id'], status))

    cursor.executemany("INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, ?)", appointment_inserts)

    # 6. Calls
    call_inserts = []
    for _ in range(30):
        p_id = random.choice(patient_ids)
        call_inserts.append((p_id, 'COMPLETED'))
    cursor.executemany("INSERT INTO Calls (patient_id, status) VALUES (?, ?)", call_inserts)
    call_ids = [row['id'] for row in cursor.execute("SELECT id FROM Calls")]

    # 7. Conversations
    conversation_inserts = []
    for call_id in call_ids:
        conversation_inserts.extend([
            (call_id, "Patient", "I have fever.", "book_appointment"),
            (call_id, "AI", "What symptoms are you experiencing?", None),
            (call_id, "Patient", "Fever and headache.", "provide_symptoms"),
            (call_id, "AI", "General Medicine would be appropriate.", None),
            (call_id, "Patient", "Tomorrow morning.", "provide_date_time"),
            (call_id, "AI", "Dr Sharma is available.", None)
        ])
    cursor.executemany(
        "INSERT INTO Conversations (call_id, speaker, transcript, detected_intent) VALUES (?, ?, ?, ?)", 
        conversation_inserts
    )

    # 8. AI Logs
    modules = ['STT', 'NLP', 'DM', 'TTS']
    levels = ['INFO', 'DEBUG', 'WARN']
    log_messages = [
        "Intent detected", "Department inferred", "Appointment booked",
        "Doctor unavailable", "Slot suggested", "Audio parsed",
        "Speech synthesized", "Database updated"
    ]
    log_inserts = []
    for _ in range(200):
        mod = random.choice(modules)
        lvl = random.choice(levels)
        msg = random.choice(log_messages)
        log_inserts.append((mod, lvl, msg))
    cursor.executemany("INSERT INTO AI_Logs (module, level, message) VALUES (?, ?, ?)", log_inserts)

    # 9. Feedback
    completed_appointments = cursor.execute("SELECT id FROM Appointments WHERE status = 'COMPLETED'").fetchall()
    feedback_comments = [
        "Excellent doctor", "Very helpful", "Long waiting time",
        "Satisfied", "Needs improvement"
    ]
    feedback_inserts = []
    for appt in completed_appointments:
        rating = random.randint(1, 5)
        comment = random.choice(feedback_comments)
        feedback_inserts.append((appt['id'], rating, comment))

    cursor.executemany("INSERT INTO Feedback (appointment_id, rating, comments) VALUES (?, ?, ?)", feedback_inserts)

    # 10. Symptom Mappings (Seed from config)
    symptom_inserts = []
    for symptom, (dept, _) in config.SYMPTOM_TO_DEPARTMENT.items():
        symptom_inserts.append((symptom.lower(), dept))
    cursor.executemany("INSERT INTO SymptomMappings (symptom, department_name) VALUES (?, ?)", symptom_inserts)

if __name__ == "__main__":
    init_db()


# ── Persistence helpers ───────────────────────────────────────────────────────

def create_call() -> int | None:
    """Insert a new ONGOING call and return its id."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO Calls (status) VALUES ('ONGOING')")
        call_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info("Call created | call_id=%d", call_id)
        return call_id
    except Exception:
        logger.exception("create_call failed")
        return None


def end_call(call_id: int) -> None:
    """Mark a call as COMPLETED and set end_time."""
    if call_id is None:
        return
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE Calls SET status='COMPLETED', end_time=CURRENT_TIMESTAMP WHERE id=?",
            (call_id,)
        )
        conn.commit()
        conn.close()
        logger.info("Call ended | call_id=%d", call_id)
    except Exception:
        logger.exception("end_call failed | call_id=%s", call_id)


def save_conversation(call_id: int, speaker: str, transcript: str, detected_intent: str | None = None) -> None:
    """Insert one turn into Conversations."""
    if call_id is None:
        return
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO Conversations (call_id, speaker, transcript, detected_intent) VALUES (?, ?, ?, ?)",
            (call_id, speaker, transcript, detected_intent)
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("save_conversation failed | call_id=%s | speaker=%s", call_id, speaker)


def save_ai_log(module: str, level: str, message: str) -> None:
    """Insert one row into AI_Logs."""
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO AI_Logs (module, level, message) VALUES (?, ?, ?)",
            (module, level, message)
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("save_ai_log failed | module=%s", module)


# ── Availability query helpers ────────────────────────────────────────────────

def find_available_slot(department: str, date: str | None = None, time: str | None = None) -> dict | None:
    """
    Find the first free slot for any doctor in the given department.
    Optionally filter by date and/or time.
    Returns dict with keys: doctor_id, doctor_name, slot_id, slot_date, slot_time.
    Returns None if nothing is available.
    """
    try:
        conn = get_connection()
        query = '''
            SELECT d.id AS doctor_id, d.name AS doctor_name,
                   s.id AS slot_id, s.slot_date, s.slot_time
            FROM Slots s
            JOIN Doctors d ON s.doctor_id = d.id
            JOIN Departments dept ON d.department_id = dept.id
            WHERE dept.name = ? COLLATE NOCASE
              AND s.is_booked = 0
        '''
        params: list = [department]
        if date:
            query += " AND s.slot_date = ?"
            params.append(date)
        if time:
            query += " AND s.slot_time = ?"
            params.append(time)
        query += " ORDER BY s.slot_date, s.slot_time LIMIT 1"
        row = conn.execute(query, params).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        logger.exception("find_available_slot failed | department=%s", department)
        return None


def find_nearest_slot(doctor_id: int, preferred_date: str, preferred_time: str | None) -> dict | None:
    """
    Find the nearest free slot for a specific doctor when the exact requested
    slot is unavailable.
    Search order:
      1. Same date, any time after preferred_time (or any time if no preferred_time)
      2. Next available date, earliest time
    Returns dict with keys: slot_id, slot_date, slot_time, doctor_name.
    Returns None if the doctor has no free slots at all.
    """
    try:
        conn = get_connection()
        doctor_name = conn.execute(
            "SELECT name FROM Doctors WHERE id = ?", (doctor_id,)
        ).fetchone()["name"]

        # 1. Same date, later time
        if preferred_date:
            params: list = [doctor_id, preferred_date]
            time_clause = ""
            if preferred_time:
                time_clause = " AND slot_time > ?"
                params.append(preferred_time)
            row = conn.execute(
                f"SELECT id AS slot_id, slot_date, slot_time FROM Slots "
                f"WHERE doctor_id = ? AND slot_date = ? AND is_booked = 0{time_clause} "
                f"ORDER BY slot_time LIMIT 1",
                params
            ).fetchone()
            if row:
                conn.close()
                return {**dict(row), "doctor_name": doctor_name}

        # 2. Next available date
        row = conn.execute(
            "SELECT id AS slot_id, slot_date, slot_time FROM Slots "
            "WHERE doctor_id = ? AND is_booked = 0 "
            "AND slot_date > ? "
            "ORDER BY slot_date, slot_time LIMIT 1",
            (doctor_id, preferred_date or "")
        ).fetchone()
        conn.close()
        return {**dict(row), "doctor_name": doctor_name} if row else None
    except Exception:
        logger.exception("find_nearest_slot failed | doctor_id=%s", doctor_id)
        return None


def find_doctor_slots(doctor_name: str, department: str) -> dict:
    """
    Check whether a named doctor exists in the given department and return
    their available slots.
    Returns:
      {
        "exists":    bool,
        "wrong_dept": bool,          # doctor exists but in a different department
        "doctor_id": int | None,
        "doctor_name": str | None,
        "slots":     list[dict],     # [{slot_id, slot_date, slot_time}, ...]
      }
    """
    try:
        conn = get_connection()
        # Look up doctor by name (partial, case-insensitive)
        row = conn.execute(
            "SELECT d.id, d.name, dept.name AS dept_name "
            "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
            "WHERE d.name LIKE ? COLLATE NOCASE",
            (f"%{doctor_name}%",)
        ).fetchone()

        if not row:
            conn.close()
            return {"exists": False, "wrong_dept": False, "doctor_id": None, "doctor_name": None, "slots": []}

        doctor_id   = row["id"]
        actual_name = row["name"]
        actual_dept = row["dept_name"]
        wrong_dept  = actual_dept.lower() != department.lower()

        slots = [
            dict(s) for s in conn.execute(
                "SELECT id AS slot_id, slot_date, slot_time FROM Slots "
                "WHERE doctor_id = ? AND is_booked = 0 "
                "ORDER BY slot_date, slot_time",
                (doctor_id,)
            ).fetchall()
        ]
        conn.close()
        return {
            "exists":     True,
            "wrong_dept": wrong_dept,
            "doctor_id":  doctor_id,
            "doctor_name": actual_name,
            "actual_dept": actual_dept,
            "slots":      slots,
        }
    except Exception:
        logger.exception("find_doctor_slots failed | doctor=%s", doctor_name)
        return {"exists": False, "wrong_dept": False, "doctor_id": None, "doctor_name": None, "slots": []}

def get_all_departments() -> list[str]:
    """Return a list of all active departments."""
    try:
        conn = get_connection()
        rows = conn.execute("SELECT name FROM Departments ORDER BY name").fetchall()
        conn.close()
        return [r["name"] for r in rows]
    except Exception:
        logger.exception("get_all_departments failed")
        return []

def get_all_doctors() -> list[dict]:
    """Return a list of all active doctors with their departments."""
    try:
        conn = get_connection()
        rows = conn.execute(
            "SELECT d.name AS doctor_name, dept.name AS dept_name "
            "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
            "ORDER BY dept_name, doctor_name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_doctors failed")
        return []
