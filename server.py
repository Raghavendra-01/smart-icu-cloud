import asyncio
import sqlite3
import threading
import datetime
import uuid
from flask import Flask, render_template, request, redirect, jsonify, session
from flask_socketio import SocketIO
from aiocoap import resource, Context, Message, CHANGED

# ================= FLASK SETUP =================
app = Flask(__name__)
app.secret_key = "your_secret_key_here"
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# ================= DATABASE HELPER =================
def get_db():
    return sqlite3.connect("hospital.db", check_same_thread=False)

# ================= INITIALIZE DATABASE =================
def init_db():
    conn = get_db()
    c = conn.cursor()

    # ── Patients vitals log ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS patients(
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        bed         INTEGER,
        patient_id  TEXT,
        temperature REAL,
        bp          INTEGER,
        heart_rate  INTEGER,
        spo2        INTEGER,
        saline      INTEGER,
        risk_score  INTEGER,
        timestamp   DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # ── Chat messages ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS chat_messages(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        room      TEXT,
        username  TEXT,
        role      TEXT,
        message   TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # ── Alert thresholds ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS thresholds(
        id   INTEGER PRIMARY KEY,
        temp REAL    DEFAULT 39,
        bp   INTEGER DEFAULT 160,
        spo2 INTEGER DEFAULT 90
    )
    ''')
    c.execute('INSERT OR IGNORE INTO thresholds (id,temp,bp,spo2) VALUES (1,39,160,90)')

    # ── Isolation flags ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS isolation(
        bed    INTEGER PRIMARY KEY,
        active INTEGER DEFAULT 0
    )
    ''')

    # ── Login audit log ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS login_logs(
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        username  TEXT,
        role      TEXT,
        status    TEXT DEFAULT 'SUCCESS',
        ip        TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # ── Bed status (current) ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS beds(
        bed        INTEGER PRIMARY KEY,
        status     TEXT     DEFAULT 'VACANT',
        patient_id TEXT,
        admitted   DATETIME,
        discharged DATETIME
    )
    ''')

    # ── Staff accounts ──
    c.execute('''
    CREATE TABLE IF NOT EXISTS staff(
        id       INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role     TEXT NOT NULL,
        fullname TEXT DEFAULT '',
        created  DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # ── Discharge history — permanent record of every patient ever ──
    # Every time a patient is discharged, a full record is saved here.
    # This table NEVER gets cleared — it is the permanent hospital registry.
    c.execute('''
    CREATE TABLE IF NOT EXISTS discharge_history(
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        bed            INTEGER,
        patient_id     TEXT,
        admitted       DATETIME,
        discharged     DATETIME DEFAULT CURRENT_TIMESTAMP,
        avg_temp       REAL,
        avg_bp         REAL,
        avg_hr         REAL,
        avg_spo2       REAL,
        max_risk_score INTEGER,
        total_readings INTEGER,
        discharged_by  TEXT DEFAULT 'nurse'
    )
    ''')

    # Default accounts
    c.execute("INSERT OR IGNORE INTO staff (username,password,role,fullname) VALUES (?,?,?,?)",
              ("nurse1",    "1234",        "nurse",     "Default Nurse"))
    c.execute("INSERT OR IGNORE INTO staff (username,password,role,fullname) VALUES (?,?,?,?)",
              ("doctor1",   "1234",        "doctor",    "Default Doctor"))
    c.execute("INSERT OR IGNORE INTO staff (username,password,role,fullname) VALUES (?,?,?,?)",
              ("admin",     "admin123",    "admin",     "System Administrator"))
    c.execute("INSERT OR IGNORE INTO staff (username,password,role,fullname) VALUES (?,?,?,?)",
              ("analytics", "analytics1",  "analytics", "Analytics Viewer"))

    # Migrations
    try:
        c.execute("ALTER TABLE patients ADD COLUMN patient_id TEXT")
        print("[Migration] Added patient_id to patients.")
    except sqlite3.OperationalError:
        pass

    try:
        c.execute("ALTER TABLE login_logs ADD COLUMN status TEXT DEFAULT 'SUCCESS'")
        c.execute("ALTER TABLE login_logs ADD COLUMN ip TEXT")
        print("[Migration] Updated login_logs.")
    except sqlite3.OperationalError:
        pass

    # Pre-populate 100 beds
    for bed in range(1, 101):
        c.execute("INSERT OR IGNORE INTO beds (bed, status) VALUES (?, 'VACANT')", (bed,))

    conn.commit()
    conn.close()

init_db()

# ================= BED HELPERS =================
def get_or_create_patient_id(bed):
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT status, patient_id FROM beds WHERE bed=?", (bed,))
    row = c.fetchone()
    if row and row[0] == "OCCUPIED" and row[1]:
        conn.close()
        return row[1]
    pid = str(uuid.uuid4())[:8].upper()
    c.execute("""
        UPDATE beds SET status='OCCUPIED', patient_id=?,
        admitted=CURRENT_TIMESTAMP, discharged=NULL WHERE bed=?
    """, (pid, bed))
    conn.commit()
    conn.close()
    return pid

def get_bed_status(bed):
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT status, patient_id FROM beds WHERE bed=?", (bed,))
    row = c.fetchone()
    conn.close()
    return {"status": row[0] if row else "UNKNOWN",
            "patient_id": row[1] if row else None}

# ================= AI RISK SCORE =================
def calculate_risk(data):
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT temp,bp,spo2 FROM thresholds WHERE id=1")
    temp_th, bp_th, spo2_th = c.fetchone()
    conn.close()
    risk = 0
    if data["temperature"] >= 38:      risk += 1
    if data["temperature"] >= temp_th: risk += 2
    if data["bp"]          >  bp_th:   risk += 2
    if data["spo2"]        <  spo2_th: risk += 3
    if data["heart_rate"]  >  110:     risk += 1
    return risk

# ================= EMERGENCY BROADCAST =================
def check_emergency(data):
    socketio.emit("nurse_update", data)
    if data["risk_score"] >= 5:
        socketio.emit("doctor_alert", data)

# ================= AUTH HELPER =================
def log_login(username, role, status, ip):
    conn = get_db()
    c    = conn.cursor()
    c.execute(
        "INSERT INTO login_logs (username, role, status, ip) VALUES (?,?,?,?)",
        (username, role, status, ip)
    )
    conn.commit()
    conn.close()

# ================= HOME =================
@app.route("/")
def home():
    return render_template("login.html")

# ================= LOGIN =================
@app.route("/login", methods=["POST"])
def do_login():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    role     = request.form.get("role", "").strip()
    ip       = request.remote_addr

    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT id, role FROM staff WHERE username=? AND password=?",
              (username, password))
    row = c.fetchone()
    conn.close()

    if not row:
        log_login(username, role, "FAILED", ip)
        return redirect("/")

    actual_role = row[1]

    if role and role != actual_role:
        log_login(username, actual_role, "WRONG_ROLE", ip)
        return redirect("/")

    session["username"] = username
    session["role"]     = actual_role
    log_login(username, actual_role, "SUCCESS", ip)

    routes = {
        "nurse":     "/nurse",
        "doctor":    "/doctor",
        "admin":     "/admin",
        "analytics": "/analytics"
    }
    return redirect(routes.get(actual_role, "/"))

# ================= LOGOUT =================
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ================= DASHBOARDS =================
@app.route("/nurse")
def nurse():
    if session.get("role") not in ("nurse", "admin"):
        return redirect("/")
    return render_template("nurse.html")

@app.route("/doctor")
def doctor():
    if session.get("role") not in ("doctor", "admin"):
        return redirect("/")
    return render_template("doctor.html")

@app.route("/analytics")
def analytics():
    if session.get("role") not in ("analytics", "admin", "doctor"):
        return redirect("/")
    return render_template("analytics.html")

@app.route("/admin")
def admin_panel():
    if session.get("role") != "admin":
        return redirect("/")
    return render_template("admin.html")

# ── Discharge history page ──
@app.route("/history")
def history_page():
    if session.get("role") not in ("nurse", "doctor", "admin"):
        return redirect("/")
    return render_template("discharge_history.html")

# ================= ADMIN — STAFF MANAGEMENT =================
@app.route("/admin/staff")
def list_staff():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT username, role, fullname, created FROM staff ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return jsonify({"staff": [
        {"username": r[0], "role": r[1], "fullname": r[2] or "", "created": r[3]}
        for r in rows
    ]})

@app.route("/admin/add_staff", methods=["POST"])
def add_staff():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    data     = request.json
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role     = (data.get("role")     or "").strip()
    fullname = (data.get("fullname") or "").strip()

    if not username or not password or role not in ("nurse", "doctor", "analytics"):
        return jsonify({"success": False, "error": "All fields are required."})
    if len(password) < 4:
        return jsonify({"success": False, "error": "Password must be at least 4 characters."})

    conn = get_db()
    c    = conn.cursor()
    try:
        c.execute("INSERT INTO staff (username,password,role,fullname) VALUES (?,?,?,?)",
                  (username, password, role, fullname))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({"success": False, "error": "Username already exists."})

@app.route("/admin/delete_staff", methods=["POST"])
def delete_staff():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    username = (request.json.get("username") or "").strip()
    if username == "admin":
        return jsonify({"success": False, "error": "Cannot delete the admin account."})
    conn = get_db()
    c    = conn.cursor()
    c.execute("DELETE FROM staff WHERE username=?", (username,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/admin/change_password", methods=["POST"])
def change_password():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    data     = request.json
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"success": False, "error": "Username and password required."})

    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT id FROM staff WHERE username=?", (username,))
    if not c.fetchone():
        conn.close()
        return jsonify({"success": False, "error": "User not found."})

    c.execute("UPDATE staff SET password=? WHERE username=?", (password, username))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

# ================= LOGIN LOGS =================
@app.route("/login_logs")
def login_logs():
    if session.get("role") != "admin":
        return jsonify({"error": "Unauthorized"}), 403
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT id, username, role, status, ip, timestamp
        FROM login_logs ORDER BY id DESC LIMIT 200
    """)
    rows = c.fetchall()
    conn.close()
    return jsonify({"logs": [
        {"id": r[0], "username": r[1], "role": r[2],
         "status": r[3], "ip": r[4], "timestamp": r[5]}
        for r in rows
    ]})

# ================= BED ADMIT / DISCHARGE =================
@app.route("/admit/<int:bed>", methods=["POST"])
def admit_patient(bed):
    pid  = str(uuid.uuid4())[:8].upper()
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        UPDATE beds SET status='OCCUPIED', patient_id=?,
        admitted=CURRENT_TIMESTAMP, discharged=NULL WHERE bed=?
    """, (pid, bed))
    conn.commit()
    conn.close()
    return jsonify({"bed": bed, "status": "OCCUPIED", "patient_id": pid})

@app.route("/discharge/<int:bed>", methods=["POST"])
def discharge_patient(bed):
    """
    Discharge a patient:
    1. Calculate their average vitals and max risk from all their readings
    2. Save a permanent record to discharge_history
    3. Mark the bed as VACANT in the beds table
    """
    conn = get_db()
    c    = conn.cursor()

    # Get current patient info
    c.execute("SELECT patient_id, admitted FROM beds WHERE bed=?", (bed,))
    row = c.fetchone()

    if row and row[0]:
        patient_id = row[0]
        admitted   = row[1]

        # Calculate summary stats from all their readings
        c.execute("""
            SELECT
                AVG(temperature),
                AVG(bp),
                AVG(heart_rate),
                AVG(spo2),
                MAX(risk_score),
                COUNT(*)
            FROM patients
            WHERE bed=? AND patient_id=?
        """, (bed, patient_id))
        stats = c.fetchone()

        discharged_by = session.get("username", "system")

        # Save permanent discharge record
        c.execute("""
            INSERT INTO discharge_history
                (bed, patient_id, admitted, discharged,
                 avg_temp, avg_bp, avg_hr, avg_spo2,
                 max_risk_score, total_readings, discharged_by)
            VALUES (?,?,?,CURRENT_TIMESTAMP,?,?,?,?,?,?,?)
        """, (
            bed,
            patient_id,
            admitted,
            round(stats[0] or 0, 1),
            round(stats[1] or 0, 1),
            round(stats[2] or 0, 1),
            round(stats[3] or 0, 1),
            stats[4] or 0,
            stats[5] or 0,
            discharged_by
        ))

    # Mark bed as vacant
    c.execute("""
        UPDATE beds SET status='VACANT', patient_id=NULL,
        discharged=CURRENT_TIMESTAMP WHERE bed=?
    """, (bed,))

    conn.commit()
    conn.close()
    return jsonify({"bed": bed, "status": "VACANT"})

@app.route("/bed_status")
def all_bed_status():
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT bed, status, patient_id, admitted FROM beds ORDER BY bed")
    rows = c.fetchall()
    conn.close()
    return jsonify([
        {"bed": r[0], "status": r[1], "patient_id": r[2], "admitted": r[3]}
        for r in rows
    ])

# ================= DISCHARGE HISTORY API =================
@app.route("/discharge_history")
def get_discharge_history():
    """Return all discharged patients — full permanent registry."""
    conn = get_db()
    c    = conn.cursor()

    # Optional filters from query params
    search_pid = request.args.get("patient_id", "").strip().upper()
    search_bed = request.args.get("bed", "").strip()
    date_from  = request.args.get("from", "").strip()
    date_to    = request.args.get("to", "").strip()

    query  = """
        SELECT id, bed, patient_id, admitted, discharged,
               avg_temp, avg_bp, avg_hr, avg_spo2,
               max_risk_score, total_readings, discharged_by
        FROM discharge_history
        WHERE 1=1
    """
    params = []

    if search_pid:
        query += " AND patient_id LIKE ?"
        params.append(f"%{search_pid}%")
    if search_bed:
        query += " AND bed = ?"
        params.append(int(search_bed))
    if date_from:
        query += " AND discharged >= ?"
        params.append(date_from)
    if date_to:
        query += " AND discharged <= ?"
        params.append(date_to + " 23:59:59")

    query += " ORDER BY id DESC LIMIT 500"

    c.execute(query, params)
    rows = c.fetchall()
    conn.close()

    return jsonify({"history": [
        {
            "id":             r[0],
            "bed":            r[1],
            "patient_id":     r[2],
            "admitted":       r[3],
            "discharged":     r[4],
            "avg_temp":       r[5],
            "avg_bp":         r[6],
            "avg_hr":         r[7],
            "avg_spo2":       r[8],
            "max_risk_score": r[9],
            "total_readings": r[10],
            "discharged_by":  r[11]
        }
        for r in rows
    ]})

@app.route("/discharge_history/stats")
def discharge_stats():
    """Summary statistics for the history dashboard."""
    conn = get_db()
    c    = conn.cursor()

    c.execute("SELECT COUNT(*) FROM discharge_history")
    total = c.fetchone()[0]

    c.execute("SELECT COUNT(DISTINCT patient_id) FROM discharge_history")
    unique = c.fetchone()[0]

    c.execute("SELECT COUNT(*) FROM discharge_history WHERE max_risk_score >= 5")
    high_risk = c.fetchone()[0]

    c.execute("SELECT AVG(avg_temp), AVG(avg_bp), AVG(avg_hr), AVG(avg_spo2) FROM discharge_history")
    avgs = c.fetchone()

    c.execute("""
        SELECT bed, COUNT(*) as cnt
        FROM discharge_history
        GROUP BY bed ORDER BY cnt DESC LIMIT 5
    """)
    busiest = c.fetchall()

    conn.close()
    return jsonify({
        "total_discharged": total,
        "unique_patients":  unique,
        "high_risk_cases":  high_risk,
        "overall_avg_temp": round(avgs[0] or 0, 1),
        "overall_avg_bp":   round(avgs[1] or 0, 1),
        "overall_avg_hr":   round(avgs[2] or 0, 1),
        "overall_avg_spo2": round(avgs[3] or 0, 1),
        "busiest_beds": [{"bed": r[0], "count": r[1]} for r in busiest]
    })

@app.route("/discharge_history/patient/<patient_id>")
def patient_full_history(patient_id):
    """Return all raw vitals ever recorded for a specific patient ID."""
    conn = get_db()
    c    = conn.cursor()

    # Discharge summary
    c.execute("""
        SELECT bed, admitted, discharged, avg_temp, avg_bp,
               avg_hr, avg_spo2, max_risk_score, total_readings, discharged_by
        FROM discharge_history WHERE patient_id=?
        ORDER BY id DESC
    """, (patient_id,))
    summary = c.fetchone()

    # All raw readings
    c.execute("""
        SELECT temperature, bp, heart_rate, spo2, risk_score, timestamp
        FROM patients WHERE patient_id=?
        ORDER BY id ASC
    """, (patient_id,))
    readings = c.fetchall()
    conn.close()

    if not summary:
        return jsonify({"error": "Patient not found"}), 404

    return jsonify({
        "patient_id":     patient_id,
        "bed":            summary[0],
        "admitted":       summary[1],
        "discharged":     summary[2],
        "avg_temp":       summary[3],
        "avg_bp":         summary[4],
        "avg_hr":         summary[5],
        "avg_spo2":       summary[6],
        "max_risk_score": summary[7],
        "total_readings": summary[8],
        "discharged_by":  summary[9],
        "readings": [
            {
                "temperature": r[0],
                "bp":          r[1],
                "heart_rate":  r[2],
                "spo2":        r[3],
                "risk_score":  r[4],
                "timestamp":   r[5]
            }
            for r in readings
        ]
    })

# ================= CHAT =================
@socketio.on("join_room")
def join(data):
    from flask_socketio import join_room
    join_room(data["room"])

@socketio.on("chat_message")
def handle_chat(data):
    room     = data["room"]
    username = data["username"]
    role     = data["role"]
    message  = data["msg"]
    conn = get_db()
    c    = conn.cursor()
    c.execute("INSERT INTO chat_messages (room,username,role,message) VALUES (?,?,?,?)",
              (room, username, role, message))
    conn.commit()
    conn.close()
    socketio.emit("chat_message", data, room=room)

@app.route("/chat_history/<room>")
def chat_history(room):
    conn = get_db()
    c    = conn.cursor()
    c.execute("""
        SELECT username, role, message, timestamp
        FROM chat_messages WHERE room=?
        ORDER BY id ASC LIMIT 100
    """, (room,))
    rows = c.fetchall()
    conn.close()
    return jsonify({"messages": rows})

# ================= SYSTEM LOGS =================
@app.route("/system_logs")
def system_logs():
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT * FROM patients ORDER BY id DESC LIMIT 50")
    rows = c.fetchall()
    conn.close()
    return jsonify({"logs": rows})

# ================= BED HISTORY =================
@app.route("/bed_history/<int:bed_id>")
def bed_history(bed_id):
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT status, patient_id FROM beds WHERE bed=?", (bed_id,))
    bed_row    = c.fetchone()
    status     = bed_row[0] if bed_row else "UNKNOWN"
    patient_id = bed_row[1] if bed_row else None

    if patient_id:
        c.execute("""
            SELECT temperature, bp, heart_rate, spo2, timestamp
            FROM patients WHERE bed=? AND patient_id=?
            ORDER BY id DESC LIMIT 20
        """, (bed_id, patient_id))
    else:
        conn.close()
        return jsonify({
            "temperature": [], "bp": [], "hr": [], "spo2": [], "time": [],
            "bed_status": status, "patient_id": None
        })

    rows = c.fetchall()
    conn.close()
    return jsonify({
        "temperature": [r[0] for r in rows][::-1],
        "bp":          [r[1] for r in rows][::-1],
        "hr":          [r[2] for r in rows][::-1],
        "spo2":        [r[3] for r in rows][::-1],
        "time":        [r[4] for r in rows][::-1],
        "bed_status":  status,
        "patient_id":  patient_id
    })

# ================= ANALYTICS DATA =================
@app.route("/analytics_data")
def analytics_data():
    conn = get_db()
    c    = conn.cursor()
    c.execute("SELECT AVG(temperature), AVG(bp), AVG(heart_rate), AVG(spo2) FROM patients")
    row = c.fetchone()
    c.execute("SELECT COUNT(DISTINCT bed) FROM patients WHERE risk_score >= 5")
    crit = c.fetchone()[0]
    conn.close()
    return jsonify({
        "avg_temp":       round(row[0] or 0, 1),
        "avg_bp":         round(row[1] or 0, 1),
        "avg_hr":         round(row[2] or 0, 1),
        "avg_spo2":       round(row[3] or 0, 1),
        "critical_count": crit
    })

# ================= CoAP SERVER =================
class PatientResource(resource.Resource):
    async def render_post(self, request):
        payload = request.payload.decode()
        parts   = payload.split(",")
        data = {
            "bed":         int(parts[0].split("=")[1]),
            "temperature": float(parts[1].split("=")[1]),
            "bp":          int(parts[2].split("=")[1]),
            "heart_rate":  int(parts[3].split("=")[1]),
            "spo2":        int(parts[4].split("=")[1]),
            "saline":      int(parts[5].split("=")[1])
        }
        risk               = calculate_risk(data)
        data["risk_score"] = risk
        data["timestamp"]  = datetime.datetime.now().strftime("%H:%M:%S")
        patient_id         = get_or_create_patient_id(data["bed"])
        data["patient_id"] = patient_id
        bed_info           = get_bed_status(data["bed"])
        data["bed_status"] = bed_info["status"]

        conn = get_db()
        c    = conn.cursor()
        c.execute("SELECT active FROM isolation WHERE bed=?", (data["bed"],))
        row = c.fetchone()
        data["isolation"] = row[0] if row else 0

        c.execute("""
            INSERT INTO patients
                (bed, patient_id, temperature, bp, heart_rate, spo2, saline, risk_score)
            VALUES (?,?,?,?,?,?,?,?)
        """, (data["bed"], patient_id, data["temperature"], data["bp"],
              data["heart_rate"], data["spo2"], data["saline"], data["risk_score"]))
        conn.commit()
        conn.close()

        check_emergency(data)
        return Message(code=CHANGED, payload=b"OK")

# ================= START CoAP =================
async def coap_server():
    root = resource.Site()
    root.add_resource(['patient'], PatientResource())
    await Context.create_server_context(root, bind=("0.0.0.0", 5683))
    print("CoAP Server Running on port 5683")
    await asyncio.get_running_loop().create_future()

def start_coap():
    asyncio.run(coap_server())

# ================= MAIN =================
if __name__ == "__main__":
    threading.Thread(target=start_coap).start()
    socketio.run(app, host="0.0.0.0", port=5000)