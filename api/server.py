from fastapi import FastAPI, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import secrets
import config
from database import get_connection
from pathlib import Path

app = FastAPI(title="VoxMed Admin API")

# Setup CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

security = HTTPBasic()

def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    current_username_bytes = credentials.username.encode("utf8")
    correct_username_bytes = config.ADMIN_USERNAME.encode("utf8")
    is_correct_username = secrets.compare_digest(
        current_username_bytes, correct_username_bytes
    )
    
    current_password_bytes = credentials.password.encode("utf8")
    correct_password_bytes = config.ADMIN_PASSWORD.encode("utf8")
    is_correct_password = secrets.compare_digest(
        current_password_bytes, correct_password_bytes
    )
    
    if not (is_correct_username and is_correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

class SlotCreate(BaseModel):
    doctor_id: int
    slot_date: str
    slot_time: str
    is_booked: bool = False

@app.get("/api/appointments")
def get_appointments(username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT a.id, p.name as patient_name, p.phone as patient_phone, 
               d.name as doctor_name, s.slot_date, s.slot_time, a.status
        FROM Appointments a
        JOIN Patients p ON a.patient_id = p.id
        JOIN Doctors d ON a.doctor_id = d.id
        JOIN Slots s ON a.slot_id = s.id
        ORDER BY s.slot_date DESC, s.slot_time DESC
    ''')
    appointments = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return appointments

@app.delete("/api/appointments/{appointment_id}")
def delete_appointment(appointment_id: int, username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute("SELECT slot_id FROM Appointments WHERE id = ?", (appointment_id,))
    row = cursor.fetchone()
    
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Appointment not found")
        
    slot_id = row['slot_id']
    
    cursor.execute("UPDATE Slots SET is_booked = 0 WHERE id = ?", (slot_id,))
    cursor.execute("UPDATE Appointments SET status = 'CANCELED' WHERE id = ?", (appointment_id,))
    
    conn.commit()
    conn.close()
    return {"message": "Appointment canceled successfully"}

@app.get("/api/doctors")
def get_doctors(username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT d.id, d.name, dept.name as department
        FROM Doctors d
        JOIN Departments dept ON d.department_id = dept.id
        ORDER BY d.name
    ''')
    doctors = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return doctors

@app.get("/api/slots")
def get_slots(doctor_id: int = None, date: str = None, username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    
    query = '''
        SELECT s.id, d.name as doctor_name, s.slot_date, s.slot_time, s.is_booked
        FROM Slots s
        JOIN Doctors d ON s.doctor_id = d.id
        WHERE 1=1
    '''
    params = []
    
    if doctor_id:
        query += " AND s.doctor_id = ?"
        params.append(doctor_id)
        
    if date:
        query += " AND s.slot_date = ?"
        params.append(date)
        
    query += " ORDER BY s.slot_date, s.slot_time"
    
    cursor.execute(query, params)
    slots = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return slots

@app.post("/api/slots")
def create_slot(slot: SlotCreate, username: str = Depends(verify_credentials)):
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO Slots (doctor_id, slot_date, slot_time, is_booked) VALUES (?, ?, ?, ?)",
            (slot.doctor_id, slot.slot_date, slot.slot_time, slot.is_booked)
        )
        conn.commit()
    except Exception as e:
        conn.close()
        raise HTTPException(status_code=400, detail=str(e))
        
    conn.close()
    return {"message": "Slot created successfully"}

@app.get("/api/dashboard-stats")
def get_dashboard_stats(username: str = Depends(verify_credentials)):
    from datetime import date as _date
    today = _date.today().isoformat()
    
    conn = get_connection()
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT COUNT(a.id) as count
        FROM Appointments a
        JOIN Slots s ON a.slot_id = s.id
        WHERE s.slot_date = ? AND a.status = 'BOOKED'
    ''', (today,))
    today_appointments = cursor.fetchone()['count']
    
    cursor.execute("SELECT COUNT(id) as count FROM Doctors")
    total_doctors = cursor.fetchone()['count']
    
    cursor.execute("SELECT COUNT(id) as count FROM Appointments WHERE status = 'BOOKED'")
    total_active_appointments = cursor.fetchone()['count']
    
    conn.close()
    return {
        "today_appointments": today_appointments,
        "total_doctors": total_doctors,
        "total_active_appointments": total_active_appointments
    }

# Mount static files for the admin dashboard
admin_dir = Path(__file__).resolve().parent.parent / "admin"
if admin_dir.exists():
    app.mount("/", StaticFiles(directory=str(admin_dir), html=True), name="admin")
