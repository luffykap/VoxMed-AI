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
_psycopg2_patched = False

# ── Thin compatibility wrapper ────────────────────────────────────────────────
def _adapt_sql(sql: str) -> str:
    """Replace SQLite-style ? placeholders with PostgreSQL %s."""
    if isinstance(sql, bytes):
        # psycopg2.extras.execute_batch passes pre-merged bytes (already %s-style)
        return sql
    if _USE_POSTGRES:
        sql = sql.replace("LIKE ? COLLATE NOCASE", "ILIKE %s")
        sql = sql.replace("= ? COLLATE NOCASE", "ILIKE %s")
        sql = sql.replace("COLLATE NOCASE", "")
        sql = sql.replace("is_booked = 0", "is_booked = FALSE")
        sql = sql.replace("is_booked = 1", "is_booked = TRUE")
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
    global _psycopg2_patched

    if _USE_POSTGRES:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        if not _psycopg2_patched:
            original_execute = RealDictCursor.execute
            original_executemany = RealDictCursor.executemany
            original_fetchone = RealDictCursor.fetchone
            original_fetchall = RealDictCursor.fetchall

            def patched_execute(self, query, vars=None):
                return original_execute(self, _adapt_sql(query), _adapt_params(vars) if vars else None)
                
            def patched_executemany(self, query, vars_list):
                return original_executemany(self, _adapt_sql(query), [_adapt_params(v) for v in vars_list])

            import datetime
            def _cast_dates(row):
                if row:
                    for k, v in row.items():
                        if isinstance(v, datetime.time):
                            row[k] = str(v)[:5]
                        elif isinstance(v, datetime.date):
                            row[k] = str(v)
                return row

            def patched_fetchone(self):
                return _cast_dates(original_fetchone(self))

            def patched_fetchall(self):
                rows = original_fetchall(self)
                if rows:
                    for r in rows:
                        _cast_dates(r)
                return rows

            RealDictCursor.execute = patched_execute
            RealDictCursor.executemany = patched_executemany
            RealDictCursor.fetchone = patched_fetchone
            RealDictCursor.fetchall = patched_fetchall
            _psycopg2_patched = True

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
    _migrate_multi_hospital(conn)
    _seed_default_hospitals(conn)
    _ensure_hospital_doctors_and_slots(conn)

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
            CREATE TABLE IF NOT EXISTS Hospitals (
                id            SERIAL PRIMARY KEY,
                name          TEXT NOT NULL UNIQUE,
                city          TEXT DEFAULT 'Bengaluru',
                address       TEXT,
                phone         TEXT,
                opening_hours TEXT DEFAULT 'Monday to Saturday, 9 AM to 8 PM',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Departments (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                hospital_id INTEGER NOT NULL DEFAULT 1 REFERENCES Hospitals(id),
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (hospital_id, name)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Doctors (
                id            SERIAL PRIMARY KEY,
                name          TEXT NOT NULL,
                department_id INTEGER NOT NULL REFERENCES Departments(id),
                hospital_id   INTEGER NOT NULL DEFAULT 1 REFERENCES Hospitals(id),
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
                id          SERIAL PRIMARY KEY,
                patient_id  INTEGER NOT NULL REFERENCES Patients(id),
                doctor_id   INTEGER NOT NULL REFERENCES Doctors(id),
                slot_id     INTEGER NOT NULL REFERENCES Slots(id),
                hospital_id INTEGER NOT NULL DEFAULT 1 REFERENCES Hospitals(id),
                status      TEXT NOT NULL DEFAULT 'BOOKED',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS Calls (
                id          SERIAL PRIMARY KEY,
                patient_id  INTEGER REFERENCES Patients(id),
                hospital_id INTEGER DEFAULT 1 REFERENCES Hospitals(id),
                start_time  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time    TIMESTAMP,
                status      TEXT NOT NULL DEFAULT 'ONGOING'
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
            CREATE TABLE IF NOT EXISTS Hospitals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                city TEXT DEFAULT 'Bengaluru',
                address TEXT,
                phone TEXT,
                opening_hours TEXT DEFAULT 'Monday to Saturday, 9 AM to 8 PM',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS Departments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                hospital_id INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (hospital_id) REFERENCES Hospitals (id),
                UNIQUE (hospital_id, name)
            );
            CREATE TABLE IF NOT EXISTS Doctors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                department_id INTEGER NOT NULL,
                hospital_id INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (department_id) REFERENCES Departments (id),
                FOREIGN KEY (hospital_id) REFERENCES Hospitals (id)
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
                hospital_id INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'BOOKED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (patient_id) REFERENCES Patients (id),
                FOREIGN KEY (doctor_id) REFERENCES Doctors (id),
                FOREIGN KEY (slot_id) REFERENCES Slots (id),
                FOREIGN KEY (hospital_id) REFERENCES Hospitals (id)
            );
            CREATE TABLE IF NOT EXISTS Calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id INTEGER,
                hospital_id INTEGER DEFAULT 1,
                start_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                end_time TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'ONGOING',
                FOREIGN KEY (patient_id) REFERENCES Patients (id),
                FOREIGN KEY (hospital_id) REFERENCES Hospitals (id)
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


DEFAULT_HOSPITALS = [
    ("Apollo Hospital", "Bengaluru", "154/11, Bannerghatta Road", "+918026304050", "Monday to Saturday, 8 AM to 9 PM"),
    ("Manipal Hospital", "Bengaluru", "98, HAL Old Airport Road", "+918025024444", "Monday to Sunday, 9 AM to 8 PM"),
    ("Fortis Hospital", "Bengaluru", "14, Cunningham Road", "+918041994444", "Monday to Saturday, 9 AM to 7 PM"),
]


def _migrate_multi_hospital(conn) -> None:
    """Safely add hospital_id columns to existing tables if migrating an older DB."""
    cursor = conn.cursor()
    tables_with_hospital = ["Departments", "Doctors", "Appointments", "Calls"]
    for table in tables_with_hospital:
        try:
            if _USE_POSTGRES:
                cursor.execute(
                    f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS hospital_id INTEGER DEFAULT 1"
                )
                conn.commit()
            else:
                cursor.execute(f"PRAGMA table_info({table})")
                cols = [r["name"] for r in cursor.fetchall()]
                if "hospital_id" not in cols:
                    cursor.execute(
                        f"ALTER TABLE {table} ADD COLUMN hospital_id INTEGER DEFAULT 1"
                    )
                    conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.debug("Migration check for %s: %s", table, exc)

    # Drop legacy single-hospital UNIQUE(name) constraint on PostgreSQL Departments table
    if _USE_POSTGRES:
        try:
            cursor.execute("ALTER TABLE Departments DROP CONSTRAINT IF EXISTS departments_name_key")
            conn.commit()
        except Exception as exc:
            conn.rollback()
            logger.debug("Drop departments_name_key check: %s", exc)


def _seed_default_hospitals(conn) -> None:
    """Ensure the default hospitals exist so hospital_id=1 foreign keys are always valid."""
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM Hospitals LIMIT 1")
    if cursor.fetchone():
        return
    for name, city, address, phone, hours in DEFAULT_HOSPITALS:
        if _USE_POSTGRES:
            cursor.execute(
                "INSERT INTO Hospitals (name, city, address, phone, opening_hours) "
                "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (name) DO NOTHING",
                (name, city, address, phone, hours),
            )
        else:
            cursor.execute(
                "INSERT OR IGNORE INTO Hospitals (name, city, address, phone, opening_hours) "
                "VALUES (?, ?, ?, ?, ?)",
                (name, city, address, phone, hours),
            )
    conn.commit()
    logger.info("Seeded %d default hospitals", len(DEFAULT_HOSPITALS))


def _ensure_hospital_doctors_and_slots(conn) -> None:
    """
    When migrating an existing single-hospital DB (e.g. NeonDB on Render) to multi-hospital,
    Hospital 1 already has doctors, while Hospitals 2 and 3 have 0 doctors.
    Seed departments, doctors, and 7-day slots for any hospital missing doctors,
    when the DB already has initial data (leaving empty unit-test DBs untouched).
    """
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM Doctors LIMIT 1")
    if not cursor.fetchone():
        return

    departments = [
        "General Medicine", "Cardiology", "Dermatology", "Neurology",
        "Orthopedics", "ENT", "Ophthalmology", "Pediatrics",
        "Gynecology", "Psychiatry", "Gastroenterology",
    ]
    doctor_data_by_hospital = {
        2: {
            "General Medicine": ["Dr. Deepti", "Dr. Rahul", "Dr. Sharma"],
            "Cardiology":       ["Dr. Joshi", "Dr. Reddy"],
            "Dermatology":      ["Dr. Kapoor", "Dr. Sneha"],
            "Neurology":        ["Dr. Vivek", "Dr. Rao"],
            "Orthopedics":      ["Dr. Thomas", "Dr. Roy"],
            "ENT":              ["Dr. Ramesh", "Dr. George"],
            "Ophthalmology":    ["Dr. Neha", "Dr. Khan"],
            "Pediatrics":       ["Dr. Ritu", "Dr. Das"],
            "Gynecology":       ["Dr. Swathi", "Dr. Fernandes"],
            "Psychiatry":       ["Dr. Sonia", "Dr. Nikhil"],
        },
        3: {
            "General Medicine": ["Dr. Vikram", "Dr. Sunita", "Dr. Sharma"],
            "Cardiology":       ["Dr. Malhotra", "Dr. Hegde"],
            "Dermatology":      ["Dr. Chawla", "Dr. Divya"],
            "Neurology":        ["Dr. Kulkarni", "Dr. Shetty"],
            "Orthopedics":      ["Dr. Chauhan", "Dr. Nambiar"],
            "ENT":              ["Dr. Deshmukh", "Dr. Pai"],
            "Ophthalmology":    ["Dr. Chatterjee", "Dr. Gowda"],
            "Pediatrics":       ["Dr. Banerjee", "Dr. Kamath"],
            "Gynecology":       ["Dr. Srinivasan", "Dr. Shenoy"],
            "Psychiatry":       ["Dr. Gokhale", "Dr. Bhat"],
        },
    }

    today = date.today()
    times = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]

    for h_id, doc_map in doctor_data_by_hospital.items():
        try:
            cursor.execute(
                _adapt_sql("SELECT 1 FROM Doctors WHERE hospital_id = ? LIMIT 1"),
                _adapt_params([h_id]),
            )
            if cursor.fetchone():
                continue

            for d in departments:
                cursor.execute(
                    _adapt_sql("SELECT id FROM Departments WHERE name = ? AND hospital_id = ?"),
                    _adapt_params([d, h_id]),
                )
                if not cursor.fetchone():
                    cursor.execute(
                        _adapt_sql("INSERT INTO Departments (name, hospital_id) VALUES (?, ?)"),
                        _adapt_params([d, h_id]),
                    )
            conn.commit()

            cursor.execute(
                _adapt_sql("SELECT id, name FROM Departments WHERE hospital_id = ?"),
                _adapt_params([h_id]),
            )
            dept_ids = {r["name"]: r["id"] for r in cursor.fetchall()}

            new_doc_ids = []
            for dept, docs in doc_map.items():
                d_id = dept_ids.get(dept)
                if not d_id:
                    continue
                for doc in docs:
                    if _USE_POSTGRES:
                        cursor.execute(
                            "INSERT INTO Doctors (name, department_id, hospital_id) VALUES (%s, %s, %s) RETURNING id",
                            (doc, d_id, h_id),
                        )
                        new_doc_ids.append(cursor.fetchone()["id"])
                    else:
                        cursor.execute(
                            "INSERT INTO Doctors (name, department_id, hospital_id) VALUES (?, ?, ?)",
                            (doc, d_id, h_id),
                        )
                        new_doc_ids.append(cursor.lastrowid)
            conn.commit()

            slot_rows = []
            for doc_id in new_doc_ids:
                for day_offset in range(7):
                    d_date = (today + timedelta(days=day_offset)).isoformat()
                    for t in times:
                        slot_rows.append((doc_id, d_date, t, False if _USE_POSTGRES else 0))

            if _USE_POSTGRES:
                from psycopg2.extras import execute_batch
                execute_batch(
                    cursor,
                    "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (%s, %s, %s, %s)",
                    slot_rows,
                    page_size=500,
                )
            else:
                cursor.executemany(
                    "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, ?)",
                    slot_rows,
                )
            conn.commit()
            logger.info("Auto-seeded %d doctors and %d slots for hospital_id=%d", len(new_doc_ids), len(slot_rows), h_id)
        except Exception as exc:
            conn.rollback()
            logger.warning("Auto-seed skipped for hospital_id=%d: %s", h_id, exc)


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
        "Hospitals",
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
    _seed_default_hospitals(conn)
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

    # 0. Hospitals
    _seed_default_hospitals(conn)
    cursor.execute("SELECT id, name FROM Hospitals ORDER BY id")
    hospitals = [dict(r) for r in cursor.fetchall()]

    # 1. Departments per hospital
    departments = [
        "General Medicine", "Cardiology", "Dermatology", "Neurology",
        "Orthopedics", "ENT", "Ophthalmology", "Pediatrics",
        "Gynecology", "Psychiatry", "Gastroenterology",
    ]
    for h in hospitals:
        for d in departments:
            cursor.execute(
                _adapt_sql("INSERT INTO Departments (name, hospital_id) VALUES (?, ?)"),
                _adapt_params([d, h["id"]]),
            )
    conn.commit()

    cursor.execute("SELECT id, name, hospital_id FROM Departments")
    dept_ids = {(row["hospital_id"], row["name"]): row["id"] for row in cursor.fetchall()}

    # 2. Doctors per hospital (Hospital 1 gets primary list; Hospitals 2 & 3 get distinct + shared doctors)
    doctor_data_by_hospital = {
        1: {
            "General Medicine": ["Dr. Sharma", "Dr. Meera", "Dr. Nair"],
            "Cardiology":       ["Dr. Gupta", "Dr. Patel"],
            "Dermatology":      ["Dr. Priya", "Dr. Arjun"],
            "Neurology":        ["Dr. Verma", "Dr. Ananya"],
            "Orthopedics":      ["Dr. Singh", "Dr. Kiran"],
            "ENT":              ["Dr. Bose", "Dr. Ahmed"],
            "Ophthalmology":    ["Dr. Iyer", "Dr. Joseph"],
            "Pediatrics":       ["Dr. Anita", "Dr. Kavya"],
            "Gynecology":       ["Dr. Pooja", "Dr. Lakshmi"],
            "Psychiatry":       ["Dr. Menon", "Dr. Harish"],
        },
        2: {
            "General Medicine": ["Dr. Deepti", "Dr. Rahul", "Dr. Sharma"],
            "Cardiology":       ["Dr. Joshi", "Dr. Reddy"],
            "Dermatology":      ["Dr. Kapoor", "Dr. Sneha"],
            "Neurology":        ["Dr. Vivek", "Dr. Rao"],
            "Orthopedics":      ["Dr. Thomas", "Dr. Roy"],
            "ENT":              ["Dr. Ramesh", "Dr. George"],
            "Ophthalmology":    ["Dr. Neha", "Dr. Khan"],
            "Pediatrics":       ["Dr. Ritu", "Dr. Das"],
            "Gynecology":       ["Dr. Swathi", "Dr. Fernandes"],
            "Psychiatry":       ["Dr. Sonia", "Dr. Nikhil"],
        },
        3: {
            "General Medicine": ["Dr. Vikram", "Dr. Sunita", "Dr. Sharma"],
            "Cardiology":       ["Dr. Malhotra", "Dr. Hegde"],
            "Dermatology":      ["Dr. Chawla", "Dr. Divya"],
            "Neurology":        ["Dr. Kulkarni", "Dr. Shetty"],
            "Orthopedics":      ["Dr. Chauhan", "Dr. Nambiar"],
            "ENT":              ["Dr. Deshmukh", "Dr. Pai"],
            "Ophthalmology":    ["Dr. Chatterjee", "Dr.gowda"],
            "Pediatrics":       ["Dr. Banerjee", "Dr. Kamath"],
            "Gynecology":       ["Dr. Srinivasan", "Dr. Shenoy"],
            "Psychiatry":       ["Dr. Gokhale", "Dr. Bhat"],
        },
    }
    for h in hospitals:
        h_id = h["id"]
        doc_map = doctor_data_by_hospital.get(h_id, doctor_data_by_hospital[1])
        for dept, doctors in doc_map.items():
            d_id = dept_ids.get((h_id, dept))
            if not d_id:
                continue
            for doc in doctors:
                cursor.execute(
                    _adapt_sql("INSERT INTO Doctors (name, department_id, hospital_id) VALUES (?, ?, ?)"),
                    _adapt_params([doc, d_id, h_id]),
                )
    conn.commit()

    cursor.execute("SELECT id, hospital_id FROM Doctors")
    doc_rows = [dict(row) for row in cursor.fetchall()]

    # 3. Slots
    today = date.today()
    times = ["09:00", "10:00", "11:00", "12:00", "14:00", "15:00", "16:00", "17:00"]
    for doc_row in doc_rows:
        doc_id = doc_row["id"]
        for day_offset in range(14):
            d_date = (today + timedelta(days=day_offset)).isoformat()
            for t in times:
                is_booked = True if random.random() < 0.25 else False
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
        _adapt_sql(
            "SELECT s.id, s.doctor_id, d.hospital_id "
            "FROM Slots s JOIN Doctors d ON s.doctor_id = d.id "
            "WHERE s.is_booked = ?"
        ),
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
                "INSERT INTO Appointments (patient_id, doctor_id, slot_id, hospital_id, status) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            _adapt_params([p_id, slot["doctor_id"], slot["id"], slot["hospital_id"], status]),
        )
    conn.commit()

    # 6. Calls
    hospital_ids = [h["id"] for h in hospitals]
    for _ in range(30):
        p_id = random.choice(patient_ids)
        h_id = random.choice(hospital_ids)
        cursor.execute(
            _adapt_sql("INSERT INTO Calls (patient_id, hospital_id, status) VALUES (?, ?, ?)"),
            _adapt_params([p_id, h_id, "COMPLETED"]),
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


# ── Hospital helpers ──────────────────────────────────────────────────────────

def get_all_hospitals() -> list[dict]:
    """Return all hospitals ordered by ID."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id, name, city, address, phone, opening_hours FROM Hospitals ORDER BY id")
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows
    except Exception:
        logger.exception("get_all_hospitals failed")
        return []


def get_hospital_by_id(hospital_id: int | None) -> dict | None:
    """Return a single hospital dict by ID (defaults to first hospital if not found)."""
    hospitals = get_all_hospitals()
    if not hospitals:
        return None
    if hospital_id is not None:
        for h in hospitals:
            if h["id"] == int(hospital_id):
                return h
    return hospitals[0]


def resolve_hospital(query: str | int | None) -> dict | None:
    """
    Resolve a hospital from either:
      - Keypad digit / 1-based index or ID (e.g. 1, "1", "2", "3")
      - Spoken number word in English/Hindi/Kannada/Telugu ("one", "first", "ek", "ondu", "okati")
      - Spoken hospital name or partial match (e.g. "Apollo", "Manipal Hospital", "Fortis",
        or even in native script like "अपोलो", "मणिपाल", "फोर्टिस")
    Returns the matched hospital dict or None if no match.
    """
    if query is None:
        return None

    hospitals = get_all_hospitals()
    if not hospitals:
        return None

    if isinstance(query, int):
        for h in hospitals:
            if h["id"] == query:
                return h
        if 1 <= query <= len(hospitals):
            return hospitals[query - 1]
        return None

    raw = str(query).strip()
    if not raw:
        return None

    # 1. Direct digit check ("1", "2", "3")
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(hospitals):
            return hospitals[idx - 1]
        for h in hospitals:
            if h["id"] == idx:
                return h

    lower = raw.lower()

    # 2. Native script & transliterated hospital keyword match
    native_keywords = {
        "apollo": ["apollo", "apolo", "अपोलो", "ಅಪೋಲೋ", "ಅಪೊಲೊ", "అపోలో"],
        "manipal": ["manipal", "मणिपाल", "मनिपाल", "ಮಣಿಪಾಲ್", "ಮಣಿಪಾಲ", "మణిపాల్"],
        "fortis": ["fortis", "फोर्टिस", "ಫೋರ್ಟಿಸ್", "ఫోర్టిస్"],
    }
    for h in hospitals:
        h_lower = h["name"].lower()
        # Direct substring match
        if h_lower in lower or lower in h_lower:
            return h
        # Match main brand word (e.g. "apollo" in "Apollo Hospital")
        brand = h_lower.replace("hospital", "").replace("clinic", "").strip()
        if brand and brand in lower:
            return h
        for key, variants in native_keywords.items():
            if key in h_lower and any(v in lower for v in variants):
                return h

    # 3. Spoken number words ("press 1", "option two", "first", "ek", "do", "teen")
    spoken_numbers = {
        1: ["1", "one", "first", "ek", "एक", "ಒಂದು", "ఒకటి"],
        2: ["2", "two", "second", "do", "दो", "ಎರಡು", "రెండు"],
        3: ["3", "three", "third", "teen", "तीन", "ಮೂರು", "మూడు"],
        4: ["4", "four", "fourth", "char", "चार", "ನಾಲ್ಕು", "నాలుగు"],
    }
    words = lower.replace(".", " ").replace(",", " ").split()
    for idx, tokens in spoken_numbers.items():
        if idx <= len(hospitals) and any(t in words for t in tokens):
            return hospitals[idx - 1]

    # 4. Fuzzy match against hospital names
    import difflib
    best_h = None
    best_score = 0.0
    clean_query = lower.replace("hospital", "").replace("clinic", "").strip()
    for h in hospitals:
        brand = h["name"].lower().replace("hospital", "").replace("clinic", "").strip()
        score = difflib.SequenceMatcher(None, clean_query, brand).ratio()
        if score > best_score and score > 0.55:
            best_score = score
            best_h = h
    return best_h


# ── Persistence helpers ───────────────────────────────────────────────────────

def create_call(hospital_id: int = 1) -> int | None:
    """Insert a new ONGOING call and return its id."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if _USE_POSTGRES:
            cursor.execute(
                "INSERT INTO Calls (hospital_id, status) VALUES (%s, 'ONGOING') RETURNING id",
                (hospital_id or 1,),
            )
            call_id = cursor.fetchone()["id"]
        else:
            cursor.execute(
                "INSERT INTO Calls (hospital_id, status) VALUES (?, 'ONGOING')",
                (hospital_id or 1,),
            )
            call_id = cursor.lastrowid
        conn.commit()
        conn.close()
        logger.info("Call created | call_id=%d | hospital_id=%s", call_id, hospital_id)
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
    department: str,
    date: str | None = None,
    time: str | None = None,
    hospital_id: int | None = None,
) -> dict | None:
    """
    Find the first free slot for any doctor in the given department (and hospital).
    Optionally filter by date and/or time.
    Returns dict with keys: doctor_id, doctor_name, slot_id, slot_date, slot_time.
    Returns None if nothing is available.
    """
    try:
        conn = get_connection()
        cursor = conn.cursor()

        if _USE_POSTGRES:
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
            if hospital_id:
                query += " AND d.hospital_id = %s"
                params.append(hospital_id)
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
            if hospital_id:
                query += " AND d.hospital_id = ?"
                params.append(hospital_id)
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


def find_doctor_slots(doctor_name: str, department: str, hospital_id: int | None = None) -> dict:
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
            query = (
                "SELECT d.id, d.name, dept.name AS dept_name "
                "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
                "WHERE d.name ILIKE %s"
            )
            params: list = [f"%{doctor_name}%"]
            if hospital_id:
                query += " AND d.hospital_id = %s"
                params.append(hospital_id)
            cursor.execute(query, tuple(params))
        else:
            query = (
                "SELECT d.id, d.name, dept.name AS dept_name "
                "FROM Doctors d JOIN Departments dept ON d.department_id = dept.id "
                "WHERE d.name LIKE ? COLLATE NOCASE"
            )
            params = [f"%{doctor_name}%"]
            if hospital_id:
                query += " AND d.hospital_id = ?"
                params.append(hospital_id)
            cursor.execute(query, tuple(params))

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


def get_all_departments(hospital_id: int | None = None) -> list[str]:
    """Return a list of all active departments (optionally filtered by hospital_id)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if hospital_id:
            cursor.execute(
                _adapt_sql("SELECT DISTINCT name FROM Departments WHERE hospital_id = ? ORDER BY name"),
                _adapt_params([hospital_id]),
            )
        else:
            cursor.execute("SELECT DISTINCT name FROM Departments ORDER BY name")
        rows = cursor.fetchall()
        conn.close()
        return [r["name"] for r in rows]
    except Exception:
        logger.exception("get_all_departments failed")
        return []


def get_all_doctors(hospital_id: int | None = None) -> list[dict]:
    """Return a list of all active doctors with their departments (optionally filtered by hospital_id)."""
    try:
        conn = get_connection()
        cursor = conn.cursor()
        if hospital_id:
            cursor.execute(
                _adapt_sql(
                    "SELECT d.name AS doctor_name, dept.name AS dept_name, h.name AS hospital_name "
                    "FROM Doctors d "
                    "JOIN Departments dept ON d.department_id = dept.id "
                    "LEFT JOIN Hospitals h ON d.hospital_id = h.id "
                    "WHERE d.hospital_id = ? "
                    "ORDER BY dept_name, doctor_name"
                ),
                _adapt_params([hospital_id]),
            )
        else:
            cursor.execute(
                "SELECT d.name AS doctor_name, dept.name AS dept_name, h.name AS hospital_name "
                "FROM Doctors d "
                "JOIN Departments dept ON d.department_id = dept.id "
                "LEFT JOIN Hospitals h ON d.hospital_id = h.id "
                "ORDER BY dept_name, doctor_name"
            )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        logger.exception("get_all_doctors failed")
        return []
