import os
import tempfile
import pytest
import config
import database

@pytest.fixture(autouse=True)
def isolated_db():
    """Provides a fresh, isolated database for each test."""
    fd, temp_db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    
    # Save original
    original_db_path = config.DB_PATH
    
    # Override
    config.DB_PATH = temp_db_path
    # We set _schema_ensured to True so get_connection() doesn't try to run
    # cleanup queries (like UPDATE Appointments) before init_db() creates the tables.
    database._schema_ensured = True
    database._cleanup_done = True
    
    # Init fresh DB
    database.init_db()
    
    yield
    
    # Restore and cleanup
    config.DB_PATH = original_db_path
    try:
        os.remove(temp_db_path)
    except OSError:
        pass
