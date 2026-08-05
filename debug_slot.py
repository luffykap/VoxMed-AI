import sqlite3, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
con = sqlite3.connect('voxmed.db')
con.row_factory = sqlite3.Row

print("=== AditiAditi Appointments ===")
rows = con.execute("""
    SELECT a.id appt_id, a.slot_id, a.status,
           s.slot_date, s.slot_time, s.is_booked
    FROM Appointments a
    JOIN Patients p ON a.patient_id = p.id
    LEFT JOIN Slots s ON a.slot_id = s.id
    WHERE p.name LIKE '%Aditi%'
    ORDER BY a.id
""").fetchall()
for r in rows:
    print(dict(r))

print("\n=== Slot 456 directly ===")
for r in con.execute("SELECT * FROM Slots WHERE id = 456"):
    print(dict(r))

print("\n=== All Appointments with status CANCELED (latest 10) ===")
for r in con.execute("""
    SELECT a.id, a.slot_id, a.status, p.name patient,
           s.slot_date, s.slot_time, s.is_booked
    FROM Appointments a
    JOIN Patients p ON a.patient_id = p.id
    LEFT JOIN Slots s ON a.slot_id = s.id
    WHERE a.status = 'CANCELED'
    ORDER BY a.id DESC LIMIT 10
"""):
    print(dict(r))

con.close()
