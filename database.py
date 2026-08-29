"""
database.py
-----------
Database connection, schema initialization, and persistence helpers.

Driver selection:
  - PostgreSQL (psycopg2) when DATABASE_URL env var is set  → production / Render
  - SQLite (sqlite3)       when DATABASE_URL is absent      → local development fallback

All callers use get_connection() which returns a connection whose cursor supports
dict-like row access (RealDictCursor for psycopg2, Row factory for sqlite3).
"""
import os
import config
from utils.logger import get_logger
from datetime import date, timedelta

logger = get_logger(__name__)

# ── Backend selection ─────────────────────────────────────────────────────────

_USE_POSTGRES = bool(config.DATABASE_URL)

_schema_ensured = False
_cleanup_done   = False

# ── Thin compatibility wrapper ────────────────────────────────────────────────
# psycopg2 uses %s placeholders; sqlite3 uses ?.
# All SQL in this file uses ? — when running against PostgreSQL we swap them.

def _adapt_sql(sql: str) -> str:
    """Replace SQLite-style ? placeholders with PostgreSQL %s."""
    if _USE_POSTGRES:
        return sql.replace("?", "%s")
    return sql


def _adapt_params(params):
    """Ensure params is a tuple (psycopg2 requires sequences, not lists)."""
    if params is None:
        return ()
    return tuple(params)


# ── Connection factory ────────────────────────────────────────────────────────

def get_connection():
    """
    Return an open database connection.

    PostgreSQL (when DATABASE_URL is set):
        Uses psycopg2 with RealDictCursor so rows behave like dicts.
    SQLite (fallback):
        Uses sqlite3 with Row factory so rows behave like dicts.

    Callers are responsible for calling conn.close() when done.
    Schema is ensured (CREATE IF NOT EXISTS) once per process on first call.
    """
    global _schema_ensured

    if _USE_POSTGRES:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        conn = psycopg2.connect(config.DATABASE_URL, cursor_factory=RealDictCursor)
        conn.autocommit = False  # explicit transaction control matches sqlite3 behaviour

        if not _schema_ensured:
            _ensure_schema(conn)
            _schema_ensured = True

        return conn
    else:
        import sqlite3
        conn = sqlite3.connect(str(config.DB_PATH))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = 1")
        if config.DB_ECHO:
            conn.set_trace_callback(print)

        if not _schema_ensured:
            _ensure_schema(conn)
            _schema_ensured = True

        return conn


# ── Schema helpers ────────────────────────────────────────────────────────────

def _table_exists(conn, table_name: str) -> bool:
    """Check if a table exists — works for both PostgreSQL and SQLite."""
    if _USE_POSTGRES:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name = %s",
            (table_name.lower(),),
        )
        return cursor.fetchone() is not None
    else:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cursor.fetchone() is not None


def _ensure_schema(conn) -> None:
    """
    Idempotently create any tables that are missing.
    Uses CREATE TABLE IF NOT EXISTS — never drops existing data.
    Runs once per process (guarded by _schema_ensured).
    """
    _create_tables_if_missing(conn)

    # Seed SymptomMappings if the table was just empty
    cursor = conn.cursor()
    if _USE_POSTGRES:
        cursor.execute("SELECT 1 FROM SymptomMappings LIMIT 1")
    else:
        cursor.execute("SELECT 1 FROM SymptomMappings LIMIT 1")
    if not cursor.fetchone():
        _seed_symptom_mappings(conn)

    _cleanup_past_slots(conn)


def _create_tables_if_missing(conn) -> None:
    """Issue CREATE TABLE IF NOT EXISTS for every table in the schema."""
    cursor = conn.cursor()

    if _USE_POSTGRES:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS Departments (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Doctors (
                id            SERIAL PRIMARY KEY,
                name          TEXT NOT NULL,
                department_id INTEGER NOT NULL REFERENCES Departments(id),
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Patients (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                phone      TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Slots (
                id        SERIAL PRIMARY KEY,
                doctor_id INTEGER NOT NULL REFERENCES Doctors(id),
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                is_booked BOOLEAN NOT NULL DEFAULT FALSE,
                UNIQUE (doctor_id, slot_date, slot_time)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Appointments (
                id         SERIAL PRIMARY KEY,
                patient_id INTEGER NOT NULL REFERENCES Patients(id),
                doctor_id  INTEGER NOT NULL REFERENCES Doctors(id),
                slot_id    INTEGER NOT NULL REFERENCES Slots(id),
                status     TEXT NOT NULL DEFAULT 'BOOKED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Calls (
                id         SERIAL PRIMARY KEY,
                patient_id INTEGER REFERENCES Patients(id),
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time   TIMESTAMP,
                status     TEXT NOT NULL DEFAULT 'ONGOING'
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Conversations (
                id              SERIAL PRIMARY KEY,
                call_id         INTEGER NOT NULL REFERENCES Calls(id),
                speaker         TEXT NOT NULL,
                transcript      TEXT NOT NULL,
                detected_intent TEXT,
                timestamp       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS AI_Logs (
                id        SERIAL PRIMARY KEY,
                module    TEXT NOT NULL,
                level     TEXT NOT NULL,
                message   TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Feedback (
                id             SERIAL PRIMARY KEY,
                appointment_id INTEGER NOT NULL REFERENCES Appointments(id),
                rating         INTEGER,
                comments       TEXT,
                timestamp      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS SymptomMappings (
                symptom         TEXT PRIMARY KEY,
                department_name TEXT NOT NULL,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ]
        for stmt in statements:
            cursor.execute(stmt)
    else:
        # SQLite: use executescript (not available in psycopg2)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS Departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS Doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (department_id) REFERENCES Departments (id)
            );
            CREATE TABLE IF NOT EXISTS Patients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                phone TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS Slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doctor_id INTEGER NOT NULL,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                is_booked BOOLEAN NOT NULL DEFAULT 0,
                FOREIGN KEY (doctor_id) REFERENCES Doctors (id),
                UNIQUE(doctor_id, slot_date, slot_time)
            );
            CREATE TABLE IF NOT EXISTS Appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER NOT NULL,
                doctor_id INTEGER NOT NULL,
                slot_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'BOOKED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (patient_id) REFERENCES Patients (id),
                FOREIGN KEY (doctor_id) REFERENCES Doctors (id),
                FOREIGN KEY (slot_id) REFERENCES Slots (id)
            );
            CREATE TABLE IF NOT EXISTS Calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'ONGOING',
                FOREIGN KEY (patient_id) REFERENCES Patients (id)
            );
            CREATE TABLE IF NOT EXISTS Conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id INTEGER NOT NULL,
                speaker TEXT NOT NULL,
                transcript TEXT NOT NULL,
                detected_intent TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (call_id) REFERENCES Calls (id)
            );
            CREATE TABLE IF NOT EXISTS AI_Logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                module TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS Feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                appointment_id INTEGER NOT NULL,
                rating INTEGER,
                comments TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (appointment_id) REFERENCES Appointments (id)
            );
            CREATE TABLE IF NOT EXISTS SymptomMappings (
                symptom TEXT PRIMARY KEY,
                department_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)

    conn.commit()
    logger.info("Schema ensured (CREATE IF NOT EXISTS) | backend=%s",
                "postgresql" if _USE_POSTGRES else "sqlite")


def _seed_symptom_mappings(conn) -> None:
    """Insert the built-in symptom→department mappings if the table is empty."""
    cursor = conn.cursor()
    rows = [
        (symptom.lower(), dept)
        for symptom, (dept, _) in config.SYMPTOM_TO_DEPARTMENT.items()
    ]
    if _USE_POSTGRES:
        from psycopg2.extras import execute_batch
        execute_batch(
            cursor,
            "INSERT INTO SymptomMappings (symptom, department_name) VALUES (%s, %s) "
            "ON CONFLICT (symptom) DO NOTHING",
            rows,
        )
    else:
        cursor.executemany(
            "INSERT OR IGNORE INTO SymptomMappings (symptom, department_name) VALUES (?, ?)",
            rows,
        )
    conn.commit()
    logger.info("Seeded %d rows into SymptomMappings", len(rows))


def _cleanup_past_slots(conn) -> None:
    """
    Archive past appointments as COMPLETED and delete orphaned past slots.
    Runs once per process (guarded by _cleanup_done).
    """
    global _cleanup_done
    if _cleanup_done:
        return
    _cleanup_done = True

    today = date.today().isoformat()
    cursor = conn.cursor()

    cursor.execute(
        _adapt_sql("""
        UPDATE Appointments SET status = 'COMPLETED'
        WHERE slot_id IN (
            SELECT id FROM Slots WHERE slot_date < ?
        )
        """),
        _adapt_params([today]),
    )
    completed = cursor.rowcount

    cursor.execute(
        _adapt_sql(
            "DELETE FROM Slots WHERE slot_date < ? "
            "AND id NOT IN (SELECT slot_id FROM Appointments)"
        ),
        _adapt_params([today]),
    )
    deleted_slots = cursor.rowcount

    conn.commit()
    if deleted_slots:
        logger.info(
            "Past-slot cleanup: archived %d appointments, deleted %d past slots",
            completed, deleted_slots,
        )


# ── init_db — GUARDED against production use ─────────────────────────────────

def init_db():
    """
    Initializes the database schema.

    SAFETY GUARD:
        DROP TABLE statements are only executed when the environment variable
        INIT_DB=true is set explicitly. Without it this function falls back to
        safe CREATE TABLE IF NOT EXISTS so it can never destroy production data.

    When called with INIT_DB=true (e.g. in tests or first-time local setup) it
    drops all tables, recreates them, and seeds dummy data.
    """
    destructive = os.getenv("INIT_DB", "").lower() == "true"

    if not destructive:
        # Safe path: just ensure the schema exists without dropping anything.
        logger.info(
            "init_db() called without INIT_DB=true — running safe CREATE IF NOT EXISTS"
        )
        conn = get_connection()
        conn.close()
        return

    # ── Destructive path (INIT_DB=true only) ─────────────────────────────────
    logger.warning(
        "init_db() running in DESTRUCTIVE mode (INIT_DB=true) — dropping all tables"
    )
    conn = get_connection()
    cursor = conn.cursor()

    tables = [
        "Feedback", "AI_Logs", "Conversations", "Calls",
        "Appointments", "Slots", "Doctors", "Departments", "Patients", "SymptomMappings",
    ]

    if _USE_POSTGRES:
        for table in tables:
            cursor.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    else:
        for table in tables:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")

    conn.commit()

    # Reset the flag so _ensure_schema re-runs after the drop
    global _schema_ensured
    _schema_ensured = False

    _create_tables_if_missing(conn)
    logger.info("Database schema initialized (destructive path).")

    _inject_dummy_data(conn)
    conn.commit()
    conn.close()
    logger.info("Dummy data injected.")


def _inject_dummy_data(conn):
    import random
    logger.info("Injecting dummy data for testing...")
    random.seed(42)

    cursor = conn.cursor()

    # 1. Departments
    departments = [
        "General Medicine", "Cardiology", "Dermatology", "Neurology",
        "Orthopedics", "ENT", "Ophthalmology", "Pediatrics",
        "Gynecology", "Psychiatry", "Gastroenterology",
    ]
    for d in departments:
        cursor.execute(_adapt_sql("INSERT INTO Departments (name) VALUES (?)"), _adapt_params([d]))
    conn.commit()

    cursor.execute("SELECT id, name FROM Departments")
    dept_ids = {row["name"]: row["id"] for row in cursor.fetchall()}

    # 2. Doctors
    doctor_data = {
        "General Medicine": ["Dr. Sharma", "Dr. Meera", "Dr. Deepti", "Dr. Rahul", "Dr. Nair"],
        "Cardiology":       ["Dr. Gupta", "Dr. Patel", "Dr. Joshi", "Dr. Reddy"],
        "Dermatology":      ["Dr. Priya", "Dr. Arjun", "Dr. Kapoor", "Dr. Sneha"],
        "Neurology":        ["Dr. Verma", "Dr. Ananya", "Dr. Vivek", "Dr. Rao"],
        "Orthopedics":      ["Dr. Singh", "Dr. Kiran", "Dr. Thomas", "Dr. Roy"],
        "ENT":              ["Dr. Bose", "Dr. Ahmed", "Dr. Ramesh", "Dr. George"],
        "Ophthalmology":    ["Dr. Iyer", "Dr. Joseph", "Dr. Neha", "Dr. Khan"],
        "Pediatrics":       ["Dr. Anita", "Dr. Kavya", "Dr. Ritu", "Dr. Das"],
        "Gynecology":       ["Dr. Pooja", "Dr. Lakshmi", "Dr. Swathi", "Dr. Fernandes"],
        "Psychiatry":       ["Dr. Menon", "Dr. Harish", "Dr. Sonia", "Dr. Nikhil"],
    }
    for dept, doctors in doctor_data.items():
        for doc in doctors:
            cursor.execute(
                _adapt_sql("INSERT INTO Doctors (name, department_id) VALUES (?, ?)"),
                _adapt_params([doc, dept_ids[dept]]),
            )
    conn.commit()

    cursor.execute("SELECT id FROM Doctors")
    doc_id_list = [row["id"] for row in cursor.fetchall()]

    # 3. Slots
    today = date.today()
    times = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
    for doc_id in doc_id_list:
        for day_offset in range(30):
            d_date = (today + timedelta(days=day_offset)).isoformat()
            for t in times:
                is_booked = True if random.random() < 0.3 else False
                cursor.execute(
                    _adapt_sql(
                        "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, ?)"
                    ),
                    _adapt_params([doc_id, d_date, t, is_booked]),
                )
    conn.commit()

    # 4. Patients
    patient_names = [
        "Aditi", "Rahul", "Priya", "Ankit", "Neha", "Karan", "Sneha", "Rohit", "Aarav", "Diya",
        "Rohan", "Meera", "Vikram", "Sonia", "Kunal", "Riya", "Nikhil", "Pooja", "Arjun", "Kavita",
        "Siddharth", "Aisha", "Varun", "Simran", "Amit", "Kritika", "Raj", "Tanya", "Akash", "Shruti",
        "Manish", "Pallavi", "Gaurav", "Nisha", "Deepak", "Anjali", "Sameer", "Preeti", "Ravi", "Swati",
    ]
    for name in patient_names:
        phone = f"98{random.randint(10000000, 99999999)}"
        cursor.execute(
            _adapt_sql("INSERT INTO Patients (name, phone) VALUES (?, ?)"),
            _adapt_params([name, phone]),
        )
    conn.commit()

    cursor.execute("SELECT id FROM Patients")
    patient_ids = [row["id"] for row in cursor.fetchall()]

    # 5. Appointments
    cursor.execute(
        _adapt_sql("SELECT id, doctor_id FROM Slots WHERE is_booked = ?"),
        _adapt_params([True if _USE_POSTGRES else 1]),
    )
    booked_slots = cursor.fetchall()
    selected_slots = random.sample(booked_slots, min(60, len(booked_slots)))
    statuses = ["BOOKED", "COMPLETED", "CANCELED", "RESCHEDULED"]
    for slot in selected_slots:
        p_id = random.choice(patient_ids)
        status = random.choice(statuses)
        cursor.execute(
            _adapt_sql(
                "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, ?)"
            ),
            _adapt_params([p_id, slot["doctor_id"], slot["id"], status]),
        )
    conn.commit()

    # 6. Calls
    for _ in range(30):
        p_id = random.choice(patient_ids)
        cursor.execute(
            _adapt_sql("INSERT INTO Calls (patient_id, status) VALUES (?, ?)"),
            _adapt_params([p_id, "COMPLETED"]),
        )
    conn.commit()

    cursor.execute("SELECT id FROM Calls")
    call_ids = [row["id"] for row in cursor.fetchall()]

    # 7. Conversations
    conv_rows = []
    for call_id in call_ids:
        conv_rows.extend([
            (call_id, "Patient", "I have fever.", "book_appointment"),
            (call_id, "AI",      "What symptoms are you experiencing?", None),
            (call_id, "Patient", "Fever and headache.", "provide_symptoms"),
            (call_id, "AI",      "General Medicine would be appropriate.", None),
            (call_id, "Patient", "Tomorrow morning.", "provide_date_time"),
            (call_id, "AI",      "Dr Sharma is available.", None),
        ])
    for row in conv_rows:
        cursor.execute(
            _adapt_sql(
                "INSERT INTO Conversations (call_id, speaker, transcript, detected_intent) "
                "VALUES (?, ?, ?, ?)"
            ),
            _adapt_params(row),
        )
    conn.commit()

    # 8. AI Logs
    modules = ["STT", "NLP", "DM", "TTS"]
    levels  = ["INFO", "DEBUG", "WARN"]
    log_messages = [
        "Intent detected", "Department inferred", "Appointment booked",
        "Doctor unavailable", "Slot suggested", "Audio parsed",
        "Speech synthesized", "Database updated",
    ]
    for _ in range(200):
        cursor.execute(
            _adapt_sql("INSERT INTO AI_Logs (module, level, message) VALUES (?, ?, ?)"),
            _adapt_params([
                random.choice(modules),
                random.choice(levels),
                random.choice(log_messages),
            ]),
        )
    conn.commit()

    # 9. Feedback
    cursor.execute(
        _adapt_sql("SELECT id FROM Appointments WHERE status = ?"),
        _adapt_params(["COMPLETED"]),
    )
    completed_appointments = cursor.fetchall()
    feedback_comments = [
        "Excellent doctor", "Very helpful", "Long waiting time", "Satisfied", "Needs improvement",
    ]
    for appt in completed_appointments:
        cursor.execute(
            _adapt_sql(
                "INSERT INTO Feedback (appointment_id, rating, comments) VALUES (?, ?, ?)"
            ),
            _adapt_params([appt["id"], random.randint(1, 5), random.choice(feedback_comments)]),
        )
    conn.commit()

    # 10. SymptomMappings
    _seed_symptom_mappings(conn)


if __name__ == "__main__":
    init_db()


# ── Persistence helpers ───────────────────────────────────────────────────────

def create_call() -> int | None:
    """Insert a new ONGOING call and return its id."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if _USE_POSTGRES:
            cursor.execute("INSERT INTO Calls (status) VALUES ('ONGOING') RETURNING id")
            call_id = cursor.fetchone()["id"]
        else:
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
        conn.cursor().execute(
            _adapt_sql(
                "UPDATE Calls SET status='COMPLETED', end_time=CURRENT_TIMESTAMP WHERE id=?"
            ),
            _adapt_params([call_id]),
        )
        conn.commit()
        conn.close()
        logger.info("Call ended | call_id=%d", call_id)
    except Exception:
        logger.exception("end_call failed | call_id=%s", call_id)


def save_conversation(
    call_id: int, speaker: str, transcript: str, detected_intent: str | None = None
) -> None:
    """Insert one turn into Conversations."""
    if call_id is None:
        return
    try:
        conn = get_connection()
        conn.cursor().execute(
            _adapt_sql(
                "INSERT INTO Conversations (call_id, speaker, transcript, detected_intent) "
                "VALUES (?, ?, ?, ?)"
            ),
            _adapt_params([call_id, speaker, transcript, detected_intent]),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception(
            "save_conversation failed | call_id=%s | speaker=%s", call_id, speaker
        )


def save_ai_log(module: str, level: str, message: str) -> None:
    """Insert one row into AI_Logs."""
    try:
        conn = get_connection()
        conn.cursor().execute(
            _adapt_sql("INSERT INTO AI_Logs (module, level, message) VALUES (?, ?, ?)"),
            _adapt_params([module, level, message]),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("save_ai_log failed | module=%s", module)


# ── Availability query helpers ────────────────────────────────────────────────

def find_available_slot(
    department: str, date: str | None = None, time: str | None = None
) -> dict | None:
    """
    Find the first free slot for any doctor in the given department.
    Optionally filter by date and/or time.
    Returns dict with keys: doctor_id, doctor_name, slot_id, slot_date, slot_time.
    Returns None if nothing is available.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()

        if _USE_POSTGRES:
            # PostgreSQL: case-insensitive comparison via ILIKE
            query = """
                SELECT d.id AS doctor_id, d.name AS doctor_name,
                       s.id AS slot_id, s.slot_date, s.slot_time
                FROM Slots s
                JOIN Doctors d    ON s.doctor_id    = d.id
                JOIN Departments dept ON d.department_id = dept.id
                WHERE dept.name ILIKE %s
                  AND s.is_booked = FALSE
            """
            params: list = [department]
            if date:
                query += " AND s.slot_date = %s"
                params.append(date)
            if time:
                query += " AND s.slot_time = %s"
                params.append(time)
            query += " ORDER BY s.slot_date, s.slot_time LIMIT 1"
            cursor.execute(query, params)
        else:
            query = """
                SELECT d.id AS doctor_id, d.name AS doctor_name,
                       s.id AS slot_id, s.slot_date, s.slot_time
                FROM Slots s
                JOIN Doctors d    ON s.doctor_id    = d.id
                JOIN Departments dept ON d.department_id = dept.id
                WHERE dept.name = ? COLLATE NOCASE
                  AND s.is_booked = 0
            """
            params = [department]
            if date:
                query += " AND s.slot_date = ?"
                params.append(date)
            if time:
                query += " AND s.slot_time = ?"
                params.append(time)
            query += " ORDER BY s.slot_date, s.slot_time LIMIT 1"
            cursor.execute(query, params)

        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        logger.exception("find_available_slot failed | department=%s", department)
        return None


def find_nearest_slot(
    doctor_id: int, preferred_date: str, preferred_time: str | None
) -> dict | None:
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
        cursor = conn.cursor()

        cursor.execute(
            _adapt_sql("SELECT name FROM Doctors WHERE id = ?"),
            _adapt_params([doctor_id]),
        )
        doctor_name = cursor.fetchone()["name"]

        booked_val = False if _USE_POSTGRES else 0

        # 1. Same date, later time
        if preferred_date:
            params: list = [doctor_id, preferred_date, booked_val]
            time_clause = ""
            if preferred_time:
                time_clause = _adapt_sql(" AND slot_time > ?")
                params.append(preferred_time)
            cursor.execute(
                _adapt_sql(
                    "SELECT id AS slot_id, slot_date, slot_time FROM Slots "
                    "WHERE doctor_id = ? AND slot_date = ? AND is_booked = ?"
                ) + time_clause + " ORDER BY slot_time LIMIT 1",
                _adapt_params(params),
            )
            row = cursor.fetchone()
            if row:
                conn.close()
                return {**dict(row), "doctor_name": doctor_name}

        # 2. Next available date
        cursor.execute(
            _adapt_sql(
                "SELECT id AS slot_id, slot_date, slot_time FROM Slots "
                "WHERE doctor_id = ? AND is_booked = ? "
                "AND slot_date > ? "
                "ORDER BY slot_date, slot_time LIMIT 1"
            ),
            _adapt_params([doctor_id, booked_val, preferred_date or ""]),
        )
        row = cursor.fetchone()
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
        "exists":     bool,
        "wrong_dept": bool,
        "doctor_id":  int | None,
        "doctor_name": str | None,
        "slots":      list[dict],
      }
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()

        booked_val = False if _USE_POSTGRES else 0

        if _USE_POSTGRES:
            cursor.execute(
                "SELECT d.id, d.name, dept.name AS dept_name "
                "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
                "WHERE d.name ILIKE %s",
                (f"%{doctor_name}%",),
            )
        else:
            cursor.execute(
                "SELECT d.id, d.name, dept.name AS dept_name "
                "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
                "WHERE d.name LIKE ? COLLATE NOCASE",
                (f"%{doctor_name}%",),
            )

        row = cursor.fetchone()

        if not row:
            conn.close()
            return {
                "exists": False, "wrong_dept": False,
                "doctor_id": None, "doctor_name": None, "slots": [],
            }

        doctor_id   = row["id"]
        actual_name = row["name"]
        actual_dept = row["dept_name"]
        wrong_dept  = actual_dept.lower() != department.lower()

        cursor.execute(
            _adapt_sql(
                "SELECT id AS slot_id, slot_date, slot_time FROM Slots "
                "WHERE doctor_id = ? AND is_booked = ? "
                "ORDER BY slot_date, slot_time"
            ),
            _adapt_params([doctor_id, booked_val]),
        )
        slots = [dict(s) for s in cursor.fetchall()]
        conn.close()
        return {
            "exists":      True,
            "wrong_dept":  wrong_dept,
            "doctor_id":   doctor_id,
            "doctor_name": actual_name,
            "actual_dept": actual_dept,
            "slots":       slots,
        }
    except Exception:
        logger.exception("find_doctor_slots failed | doctor=%s", doctor_name)
        return {
            "exists": False, "wrong_dept": False,
            "doctor_id": None, "doctor_name": None, "slots": [],
        }


def get_all_departments() -> list[str]:
    """Return a list of all active departments."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM Departments ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        return [r["name"] for r in rows]
    except Exception:
        logger.exception("get_all_departments failed")
        return []


def get_all_doctors() -> list[dict]:
    """Return a list of all active doctors with their departments."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT d.name AS doctor_name, dept.name AS dept_name "
            "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
            "ORDER BY dept_name, doctor_name"
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_doctors failed")
        return []
