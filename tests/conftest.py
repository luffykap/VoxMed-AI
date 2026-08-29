import os
import tempfile
import pytest
import config
import database


@pytest.fixture(autouse=True)
def isolated_db():
    """
    Provides a fresh, isolated SQLite database for each test.

    Strategy (unchanged from before):
    - Create a temp SQLite file.
    - Point config.DB_PATH at it.
    - Directly build the schema (bypassing the INIT_DB guard that protects
      production from accidental drops — tests always need a clean DB).
    - Reset the module-level flags so get_connection() re-runs schema init
      for the new temp file.
    """
    # Only applies when running locally against the SQLite fallback.
    # If DATABASE_URL is set the tests would hit the real PostgreSQL DB,
    # so we skip isolation (the developer is responsible in that case).
    if database._USE_POSTGRES:
        yield
        return

    fd, temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)

    # Save original
    original_db_path = config.DB_PATH

    # Override DB path and reset process-level flags
    config.DB_PATH = temp_db_path
    database._schema_ensured = False  # force schema rebuild on next get_connection()
    database._cleanup_done = True     # skip past-slot cleanup (no slots yet)

    # Build the schema directly — safe call, never drops tables
    conn = database.get_connection()
    # Seed symptom mappings if not already done
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM SymptomMappings LIMIT 1")
    if not cursor.fetchone():
        database._seed_symptom_mappings(conn)
    conn.close()

    yield

    # Restore and cleanup
    config.DB_PATH = original_db_path
    database._schema_ensured = False  # reset so next test rebuilds cleanly
    database._cleanup_done = False
    try:
        os.remove(temp_db_path)
    except OSError:
        pass
