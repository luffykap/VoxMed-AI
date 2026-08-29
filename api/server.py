"""
api/server.py
-------------
VoxMed AI FastAPI application.

Endpoints
---------
  Admin API  (HTTP Basic auth required):
    GET    /api/appointments
    DELETE /api/appointments/{id}
    GET    /api/doctors
    GET    /api/slots
    POST   /api/slots
    GET    /api/dashboard-stats

  Twilio Voice webhook (Phase 10):
    POST   /twilio/incoming   — initial call handler
    POST   /twilio/gather     — speech result handler
    POST   /twilio/status     — call status updates
    GET    /twilio/audio/{fn} — synthesised audio file serving

  Utilities:
    GET    /health            — liveness check (used by Railway/Render)
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler  # type: ignore
from slowapi.errors import RateLimitExceeded  # type: ignore
from slowapi.util import get_remote_address  # type: ignore

import config
from database import get_connection, _USE_POSTGRES, _adapt_sql, _adapt_params

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address)

app = FastAPI(title="VoxMed Admin API", version="1.0.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── CORS ─────────────────────────────────────────────────────────────────────
# In production PUBLIC_BASE_URL is set to the deployed domain.
# Locally it defaults to http://localhost:8000.
_allowed_origins = [config.PUBLIC_BASE_URL]
if "localhost" not in config.PUBLIC_BASE_URL:
    _allowed_origins.append("http://localhost:8000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Twilio webhook router ─────────────────────────────────────────────────────
from api.twilio_webhook import router as twilio_router  # noqa: E402

app.include_router(twilio_router)

# ── Auth ──────────────────────────────────────────────────────────────────────
security = HTTPBasic()


def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    ok_user = secrets.compare_digest(
        credentials.username.encode("utf8"),
        config.ADMIN_USERNAME.encode("utf8"),
    )
    ok_pass = secrets.compare_digest(
        credentials.password.encode("utf8"),
        config.ADMIN_PASSWORD.encode("utf8"),
    )
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ── Health check ──────────────────────────────────────────────────────────────

@app.get("/health", tags=["utility"])
def health_check():
    """Liveness probe used by Railway / Render health checks."""
    return {"status": "ok", "service": "VoxMed AI"}


# ── Admin models ──────────────────────────────────────────────────────────────

class SlotCreate(BaseModel):
    doctor_id: int
    slot_date: str
    slot_time: str
    is_booked: bool = False


# ── Admin API endpoints ───────────────────────────────────────────────────────

@app.get("/api/appointments", tags=["admin"])
@limiter.limit("60/minute")
def get_appointments(request: Request, username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT a.id, p.name as patient_name, p.phone as patient_phone,
               d.name as doctor_name, s.slot_date, s.slot_time, a.status
        FROM Appointments a
        JOIN Patients p ON a.patient_id = p.id
        JOIN Doctors d ON a.doctor_id = d.id
        JOIN Slots s ON a.slot_id = s.id
        ORDER BY s.slot_date DESC, s.slot_time DESC
    """)
    appointments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return appointments


@app.delete("/api/appointments/{appointment_id}", tags=["admin"])
@limiter.limit("30/minute")
def delete_appointment(
    request: Request,
    appointment_id: int,
    username: str = Depends(verify_credentials),
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        _adapt_sql("SELECT slot_id FROM Appointments WHERE id = ?"),
        _adapt_params([appointment_id]),
    )
    row = cursor.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Appointment not found")

    slot_id = row["slot_id"]
    booked_false = False if _USE_POSTGRES else 0
    cursor.execute(
        _adapt_sql("UPDATE Slots SET is_booked = ? WHERE id = ?"),
        _adapt_params([booked_false, slot_id]),
    )
    cursor.execute(
        _adapt_sql("UPDATE Appointments SET status = 'CANCELED' WHERE id = ?"),
        _adapt_params([appointment_id]),
    )
    conn.commit()
    conn.close()
    return {"message": "Appointment canceled successfully"}


@app.get("/api/doctors", tags=["admin"])
@limiter.limit("60/minute")
def get_doctors(request: Request, username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id, d.name, dept.name as department
        FROM Doctors d
        JOIN Departments dept ON d.department_id = dept.id
        ORDER BY d.name
    """)
    doctors = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return doctors


@app.get("/api/slots", tags=["admin"])
@limiter.limit("60/minute")
def get_slots(
    request: Request,
    doctor_id: int = None,
    date: str = None,
    username: str = Depends(verify_credentials),
):
    conn = get_connection()
    cursor = conn.cursor()

    query = """
        SELECT s.id, d.name as doctor_name, s.slot_date, s.slot_time, s.is_booked
        FROM Slots s
        JOIN Doctors d ON s.doctor_id = d.id
        WHERE 1=1
    """
    params = []

    if doctor_id:
        query += _adapt_sql(" AND s.doctor_id = ?")
        params.append(doctor_id)
    if date:
        query += _adapt_sql(" AND s.slot_date = ?")
        params.append(date)

    query += " ORDER BY s.slot_date, s.slot_time"
    cursor.execute(query, params)
    slots = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return slots


@app.post("/api/slots", tags=["admin"])
@limiter.limit("30/minute")
def create_slot(
    request: Request,
    slot: SlotCreate,
    username: str = Depends(verify_credentials),
):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            _adapt_sql(
                "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, ?)"
            ),
            _adapt_params([slot.doctor_id, slot.slot_date, slot.slot_time, slot.is_booked]),
        )
        conn.commit()
    except Exception as exc:
        conn.close()
        raise HTTPException(status_code=400, detail=str(exc))
    conn.close()
    return {"message": "Slot created successfully"}


@app.get("/api/dashboard-stats", tags=["admin"])
@limiter.limit("60/minute")
def get_dashboard_stats(request: Request, username: str = Depends(verify_credentials)):
    from datetime import date as _date

    today = _date.today().isoformat()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        _adapt_sql("""
        SELECT COUNT(a.id) as count
        FROM Appointments a
        JOIN Slots s ON a.slot_id = s.id
        WHERE s.slot_date = ? AND a.status = 'BOOKED'
        """),
        _adapt_params([today]),
    )
    today_appointments = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(id) as count FROM Doctors")
    total_doctors = cursor.fetchone()["count"]

    cursor.execute("SELECT COUNT(id) as count FROM Appointments WHERE status = 'BOOKED'")
    total_active_appointments = cursor.fetchone()["count"]

    conn.close()
    return {
        "today_appointments": today_appointments,
        "total_doctors": total_doctors,
        "total_active_appointments": total_active_appointments,
    }


# ── Static admin dashboard ────────────────────────────────────────────────────
# Mounted last so it doesn't shadow the /api/* and /twilio/* routes.
admin_dir = Path(__file__).resolve().parent.parent / "admin"
if admin_dir.exists():
    app.mount("/", StaticFiles(directory=str(admin_dir), html=True), name="admin")
