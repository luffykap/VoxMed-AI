"""
One-off: copy voxmed.db (SQLite) into the PostgreSQL database in DATABASE_URL.

Usage:
    DATABASE_URL="postgresql://..." python scripts/migrate_sqlite_to_postgres.py

Existing rows in the target tables are replaced. Original ids are preserved
and sequences are reset afterwards.
"""
import os
import sqlite3
import sys

import psycopg2
from psycopg2.extras import execute_values

# Parents first so foreign keys are satisfied.
TABLES = [
    "Departments", "Doctors", "Patients", "Slots", "Appointments",
    "Calls", "Conversations", "AI_Logs", "Feedback", "SymptomMappings",
]

url = os.environ.get("DATABASE_URL")
if not url:
    sys.exit("DATABASE_URL is not set")

db_path = os.path.join(os.path.dirname(__file__), "..", "voxmed.db")
src = sqlite3.connect(db_path)
src.row_factory = sqlite3.Row
dst = psycopg2.connect(url)
cur = dst.cursor()

cur.execute("TRUNCATE " + ", ".join(f'"{t.lower()}"' for t in TABLES) + " RESTART IDENTITY CASCADE")

for table in TABLES:
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = %s AND data_type = 'boolean'",
        (table.lower(),),
    )
    bool_cols = {r[0] for r in cur.fetchall()}

    rows = src.execute(f"SELECT * FROM {table}").fetchall()
    if not rows:
        print(f"{table}: 0 rows")
        continue
    cols = rows[0].keys()
    data = [
        tuple(bool(r[c]) if (c in bool_cols and r[c] is not None) else r[c] for c in cols)
        for r in rows
    ]
    execute_values(
        cur,
        f'INSERT INTO {table.lower()} ({", ".join(cols)}) VALUES %s',
        data,
        page_size=500,
    )
    if "id" in cols:
        cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table.lower()}', 'id'), "
            f"COALESCE((SELECT MAX(id) FROM {table.lower()}), 1))"
        )
    print(f"{table}: {len(data)} rows")

dst.commit()
dst.close()
print("Done.")
