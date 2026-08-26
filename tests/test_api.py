"""
tests/test_api.py
-----------------
Smoke tests for the FastAPI admin API (api/server.py).
Run with:  python -m pytest tests/test_api.py -v
"""
import pytest
from fastapi.testclient import TestClient
from datetime import date
import base64

import config
import database
from api.server import app


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    """TestClient wrapping the FastAPI app."""
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def auth_headers():
    """Basic-auth headers for admin:voxmed123."""
    credentials = base64.b64encode(
        f"{config.ADMIN_USERNAME}:{config.ADMIN_PASSWORD}".encode()
    ).decode()
    return {"Authorization": f"Basic {credentials}"}


def _seed_doctor_and_slot(cursor, conn) -> tuple[int, int]:
    """Insert a department, doctor, and one free slot. Returns (doctor_id, slot_id)."""
    cursor.execute("INSERT OR IGNORE INTO Departments (name) VALUES ('General Medicine')")
    cursor.execute("SELECT id FROM Departments WHERE name = 'General Medicine'")
    dept_id = cursor.fetchone()["id"]
    cursor.execute(
        "INSERT INTO Doctors (name, department_id) VALUES (?, ?)",
        ("Dr. Test", dept_id),
    )
    doctor_id = cursor.lastrowid
    slot_date = date.today().isoformat()
    cursor.execute(
        "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, 0)",
        (doctor_id, slot_date, "10:00"),
    )
    slot_id = cursor.lastrowid
    conn.commit()
    return doctor_id, slot_id


# ── Auth tests ────────────────────────────────────────────────────────────────

def test_unauthenticated_returns_401(client):
    for path in ("/api/appointments", "/api/doctors", "/api/dashboard-stats"):
        assert client.get(path).status_code == 401


def test_wrong_password_returns_401(client):
    bad = base64.b64encode(b"admin:wrongpassword").decode()
    resp = client.get("/api/appointments", headers={"Authorization": f"Basic {bad}"})
    assert resp.status_code == 401


# ── Dashboard stats ───────────────────────────────────────────────────────────

def test_dashboard_stats_shape(client, auth_headers):
    resp = client.get("/api/dashboard-stats", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    for key in ("today_appointments", "total_doctors", "total_active_appointments"):
        assert key in data
        assert isinstance(data[key], int)


def test_dashboard_stats_counts_after_seed(client, auth_headers):
    conn = database.get_connection()
    cursor = conn.cursor()
    doctor_id, slot_id = _seed_doctor_and_slot(cursor, conn)
    cursor.execute("INSERT INTO Patients (name) VALUES ('Stats Patient')")
    patient_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, 'BOOKED')",
        (patient_id, doctor_id, slot_id),
    )
    cursor.execute("UPDATE Slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    conn.commit()
    conn.close()
    data = client.get("/api/dashboard-stats", headers=auth_headers).json()
    assert data["total_doctors"] >= 1
    assert data["total_active_appointments"] >= 1


# ── Doctors ───────────────────────────────────────────────────────────────────

def test_get_doctors_returns_list(client, auth_headers):
    resp = client.get("/api/doctors", headers=auth_headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_get_doctors_contains_seeded(client, auth_headers):
    conn = database.get_connection()
    cursor = conn.cursor()
    _seed_doctor_and_slot(cursor, conn)
    conn.close()
    names = [d["name"] for d in client.get("/api/doctors", headers=auth_headers).json()]
    assert "Dr. Test" in names


# ── Slots ─────────────────────────────────────────────────────────────────────

def test_get_slots_returns_list(client, auth_headers):
    assert client.get("/api/slots", headers=auth_headers).status_code == 200


def test_get_slots_filtered_by_doctor(client, auth_headers):
    conn = database.get_connection()
    cursor = conn.cursor()
    doctor_id, _ = _seed_doctor_and_slot(cursor, conn)
    conn.close()
    slots = client.get(f"/api/slots?doctor_id={doctor_id}", headers=auth_headers).json()
    assert len(slots) >= 1
    assert all(s["doctor_name"] == "Dr. Test" for s in slots)


def test_create_slot_via_api(client, auth_headers):
    conn = database.get_connection()
    cursor = conn.cursor()
    doctor_id, _ = _seed_doctor_and_slot(cursor, conn)
    conn.close()
    resp = client.post(
        "/api/slots",
        headers=auth_headers,
        json={"doctor_id": doctor_id, "slot_date": date.today().isoformat(), "slot_time": "14:00"},
    )
    assert resp.status_code == 200
    assert "successfully" in resp.json()["message"]


# ── Appointments ──────────────────────────────────────────────────────────────

def test_get_appointments_returns_list(client, auth_headers):
    assert client.get("/api/appointments", headers=auth_headers).status_code == 200


def test_cancel_appointment_via_api(client, auth_headers):
    conn = database.get_connection()
    cursor = conn.cursor()
    doctor_id, slot_id = _seed_doctor_and_slot(cursor, conn)
    cursor.execute("INSERT INTO Patients (name) VALUES ('Cancel Test')")
    patient_id = cursor.lastrowid
    cursor.execute(
        "INSERT INTO Appointments (patient_id, doctor_id, slot_id, status) VALUES (?, ?, ?, 'BOOKED')",
        (patient_id, doctor_id, slot_id),
    )
    appt_id = cursor.lastrowid
    cursor.execute("UPDATE Slots SET is_booked = 1 WHERE id = ?", (slot_id,))
    conn.commit()
    conn.close()

    resp = client.delete(f"/api/appointments/{appt_id}", headers=auth_headers)
    assert resp.status_code == 200
    assert "canceled" in resp.json()["message"].lower()

    # Verify slot freed
    conn = database.get_connection()
    row = conn.execute("SELECT is_booked FROM Slots WHERE id = ?", (slot_id,)).fetchone()
    conn.close()
    assert row["is_booked"] == 0


def test_cancel_nonexistent_returns_404(client, auth_headers):
    resp = client.delete("/api/appointments/999999", headers=auth_headers)
    assert resp.status_code == 404
