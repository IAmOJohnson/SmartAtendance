import os
import hashlib
import secrets
import math
import csv
import io
import tempfile
from datetime import datetime, timedelta

from flask import (Flask, render_template, request, redirect,
                   session, jsonify, make_response, flash, url_for)
from werkzeug.utils import secure_filename

from config import Config
from database import get_connection, create_tables, migrate_existing_db

app = Flask(__name__)
app.config.from_object(Config)


# ═══════════════════════════════════════════════════════════
#  PASSWORDS
# ═══════════════════════════════════════════════════════════
def hash_password(plain):
    """PBKDF2-SHA256 → 'salt$hash'. Backward-compatible with legacy SHA-256."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', plain.encode(), salt.encode(), 260_000)
    return f"{salt}${h.hex()}"

def check_password(plain, stored):
    if '$' in stored:                        # new PBKDF2
        try:
            salt, h = stored.split('$', 1)
            c = hashlib.pbkdf2_hmac('sha256', plain.encode(), salt.encode(), 260_000)
            return secrets.compare_digest(h, c.hex())
        except Exception:
            return False
    else:                                    # legacy SHA-256
        return secrets.compare_digest(stored,
               hashlib.sha256(plain.encode()).hexdigest())


# ═══════════════════════════════════════════════════════════
#  GEOFENCING
# ═══════════════════════════════════════════════════════════
def haversine(lat1, lon1, lat2, lon2):
    """Exact distance in metres between two GPS coordinates."""
    R  = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a  = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def geofence_check(slat, slon, accuracy, clat, clon, radius):
    """
    Returns (ok: bool, reason: str, distance: float)
    - Rejects if GPS accuracy is worse than GPS_ACCURACY_THRESHOLD
    - Rejects if student is outside (radius + accuracy buffer)
    """
    if clat is None or clon is None:
        return True, "no geofence", 0.0

    if accuracy is not None and accuracy > Config.GPS_ACCURACY_THRESHOLD:
        return False, f"GPS too inaccurate (±{accuracy:.0f}m — move to open area)", 0.0

    dist = haversine(slat, slon, clat, clon)
    # Allow accuracy as a buffer so edge-of-fence students aren't wrongly rejected
    effective_radius = radius + (accuracy or 0) * 0.5
    if dist <= effective_radius:
        return True, f"{dist:.0f}m from class", dist
    return False, f"Too far: {dist:.0f}m from class (limit {radius:.0f}m)", dist

def detect_spoof(slat, slon, prev_lat, prev_lon, elapsed_sec):
    """True if movement speed suggests GPS spoofing."""
    if prev_lat is None or elapsed_sec <= 0:
        return False
    speed = haversine(slat, slon, prev_lat, prev_lon) / elapsed_sec
    return speed > Config.GPS_SPOOF_SPEED_LIMIT


# ═══════════════════════════════════════════════════════════
#  PHASE LOGIC
# ═══════════════════════════════════════════════════════════
def compute_phase(start_time, duration_minutes):
    start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    end   = start + timedelta(minutes=duration_minutes)
    now   = datetime.now()
    e_s   = start - timedelta(minutes=Config.ENTRY_WINDOW_BEFORE)
    e_e   = start + timedelta(minutes=Config.ENTRY_WINDOW_AFTER)
    x_s   = end   - timedelta(minutes=duration_minutes * Config.EXIT_WINDOW_PERCENTAGE / 100)
    if   e_s <= now <= e_e:  return "ENTRY"
    elif e_e <  now <  x_s:  return "MONITORING"
    elif x_s <= now <= end:  return "EXIT"
    return "CLOSED"

def is_entry_valid(entry_time, start_time):
    start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    et    = datetime.strptime(entry_time,  "%Y-%m-%d %H:%M:%S")
    return (start - timedelta(minutes=Config.ENTRY_WINDOW_BEFORE)
            <= et <=
            start + timedelta(minutes=Config.ENTRY_WINDOW_AFTER))

def is_exit_valid(exit_time, start_time, duration):
    start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
    end   = start + timedelta(minutes=duration)
    xt    = datetime.strptime(exit_time,  "%Y-%m-%d %H:%M:%S")
    ws    = end - timedelta(minutes=duration * Config.EXIT_WINDOW_PERCENTAGE / 100)
    return ws <= xt <= end

def update_all_phases():
    conn = get_connection()
    rows = conn.execute(
        "SELECT id, start_time, duration_minutes FROM class_sessions "
        "WHERE status IN ('ENTRY','MONITORING','EXIT')"
    ).fetchall()
    for r in rows:
        new = compute_phase(r['start_time'], r['duration_minutes'])
        conn.execute("UPDATE class_sessions SET status=? WHERE id=?", (new, r['id']))
    conn.commit()
    conn.close()


# ═══════════════════════════════════════════════════════════
#  SCORING
# ═══════════════════════════════════════════════════════════
def calc_occupancy(ping_count, duration_minutes):
    expected = max(1, duration_minutes / Config.HEARTBEAT_INTERVAL)
    return min(100.0, ping_count / expected * 100.0)

def calc_score(entry_ok, exit_ok, occupancy, geo_ok):
    s  = Config.SCORE_ENTRY_POINTS if entry_ok else 0
    s += Config.SCORE_EXIT_POINTS  if exit_ok  else 0
    s += (occupancy / 100.0) * Config.SCORE_OCCUPANCY_MAX
    if not geo_ok:
        s = max(0, s - Config.SCORE_GEO_PENALTY)
    return round(s, 2)


# ═══════════════════════════════════════════════════════════
#  FACE  (OpenCV — no external face_recognition lib)
# ═══════════════════════════════════════════════════════════
def allowed_file(filename):
    return ('.' in filename and
            filename.rsplit('.', 1)[1].lower() in Config.ALLOWED_EXTENSIONS)

def save_face_image(file, student_id):
    if not (file and allowed_file(file.filename)):
        return None, None
    os.makedirs(Config.FACE_IMAGES_DIR, exist_ok=True)
    filename = secure_filename(f"student_{student_id}.jpg")
    filepath = os.path.join(Config.FACE_IMAGES_DIR, filename)
    file.save(filepath)
    try:
        import cv2
        img  = cv2.imread(filepath)
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cc   = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces = cc.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        if len(faces):
            x, y, w, h = max(faces, key=lambda f: f[2]*f[3])
            roi = cv2.resize(gray[y:y+h, x:x+w], (64, 64))
            return filename, ','.join(map(str, roi.flatten().tolist()))
    except Exception as e:
        print(f"[face encode] {e}")
    return filename, None

def verify_face(student_id, face_file):
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, "OpenCV not available on this server"
    conn = get_connection()
    row  = conn.execute("SELECT face_encoding FROM students WHERE id=?",
                        (student_id,)).fetchone()
    conn.close()
    if not row or not row['face_encoding']:
        return False, "No registered face found for this student"
    stored = np.array([float(x) for x in row['face_encoding'].split(',')],
                      dtype=np.float32)
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tmp:
            face_file.save(tmp.name)
            img = cv2.imread(tmp.name)
            os.unlink(tmp.name)
        gray  = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        cc    = cv2.CascadeClassifier(
            cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        faces = cc.detectMultiScale(gray, 1.1, 5, minSize=(80, 80))
        if not len(faces):
            return False, "No face detected in captured image"
        x, y, w, h = max(faces, key=lambda f: f[2]*f[3])
        cap  = cv2.resize(gray[y:y+h, x:x+w], (64, 64)).flatten().astype(np.float32)
        dot  = float(np.dot(stored, cap))
        norm = float(np.linalg.norm(stored) * np.linalg.norm(cap))
        sim  = dot / norm if norm > 0 else 0.0
        if sim > 0.88:
            return True, "✅ Face verified"
        return False, f"Face does not match (similarity {sim:.2f})"
    except Exception as e:
        return False, f"Error: {e}"


# ═══════════════════════════════════════════════════════════
#  QR CODE  (static per student — generated server-side)
# ═══════════════════════════════════════════════════════════
def ensure_student_qr(student_id):
    """Generate and cache a static QR image for a student."""
    os.makedirs(Config.QR_CODE_DIR, exist_ok=True)
    path = os.path.join(Config.QR_CODE_DIR, f"student_{student_id}.png")
    if not os.path.exists(path):
        try:
            import qrcode
            qr = qrcode.QRCode(version=1, box_size=10, border=4)
            qr.add_data(f"STUDENT:{student_id}")
            qr.make(fit=True)
            qr.make_image(fill_color="black", back_color="white").save(path)
        except ImportError:
            # qrcode not installed — JS will generate it client-side instead
            pass
    return path


# ═══════════════════════════════════════════════════════════
#  ACTIVE CLASS HELPER
# ═══════════════════════════════════════════════════════════
def get_active_class(lecturer_id):
    update_all_phases()
    conn = get_connection()
    row  = conn.execute("""
        SELECT cs.id, cs.course_id, cs.start_time, cs.duration_minutes,
               cs.status, cs.verification_method,
               cs.latitude, cs.longitude, cs.geofence_radius,
               c.course_code, c.course_title
        FROM class_sessions cs
        JOIN courses c ON c.id = cs.course_id
        WHERE cs.lecturer_id=? AND cs.status IN ('ENTRY','MONITORING','EXIT')
        ORDER BY cs.id DESC LIMIT 1
    """, (lecturer_id,)).fetchone()
    conn.close()
    return row


# ═══════════════════════════════════════════════════════════
#  HOME
# ═══════════════════════════════════════════════════════════
@app.route("/")
def home():
    return render_template("home.html")


# ═══════════════════════════════════════════════════════════
#  ADMIN
# ═══════════════════════════════════════════════════════════
@app.route("/admin")
def admin_dashboard():
    return render_template("admin/dashboard.html")

@app.route("/admin/faculties", methods=["GET","POST"])
def admin_faculties():
    conn = get_connection()
    if request.method == "POST":
        name = request.form.get("name","").strip()
        if name:
            try:
                conn.execute("INSERT INTO faculties (name) VALUES (?)", (name,))
                conn.commit(); flash("✅ Faculty added!", "success")
            except Exception as e: flash(f"❌ {e}", "error")
    rows = conn.execute("SELECT * FROM faculties ORDER BY name").fetchall()
    conn.close()
    return render_template("admin/faculties.html", faculties=rows)

@app.route("/admin/faculties/<int:fid>/edit", methods=["GET","POST"])
def admin_edit_faculty(fid):
    conn = get_connection()
    if request.method == "POST":
        name = request.form.get("name","").strip()
        if name:
            conn.execute("UPDATE faculties SET name=? WHERE id=?", (name, fid))
            conn.commit(); conn.close()
            flash("✅ Updated!", "success"); return redirect(url_for("admin_faculties"))
    row = conn.execute("SELECT * FROM faculties WHERE id=?", (fid,)).fetchone()
    conn.close()
    if not row: flash("❌ Not found","error"); return redirect(url_for("admin_faculties"))
    return render_template("admin/edit_faculty.html", faculty=row)

@app.route("/admin/faculties/<int:fid>/delete", methods=["POST"])
def admin_delete_faculty(fid):
    conn = get_connection()
    try: conn.execute("DELETE FROM faculties WHERE id=?", (fid,)); conn.commit(); flash("✅ Deleted!","success")
    except Exception as e: flash(f"❌ {e}","error")
    conn.close(); return redirect(url_for("admin_faculties"))

@app.route("/admin/departments", methods=["GET","POST"])
def admin_departments():
    conn = get_connection()
    if request.method == "POST":
        name = request.form.get("name","").strip(); fid = request.form.get("faculty_id")
        if name and fid:
            try: conn.execute("INSERT INTO departments (name,faculty_id) VALUES (?,?)",(name,fid)); conn.commit(); flash("✅ Added!","success")
            except Exception as e: flash(f"❌ {e}","error")
    faculties   = conn.execute("SELECT * FROM faculties ORDER BY name").fetchall()
    departments = conn.execute("""SELECT d.id, d.name, f.name as faculty_name FROM departments d
        JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name""").fetchall()
    conn.close()
    return render_template("admin/departments.html", faculties=faculties, departments=departments)

@app.route("/admin/departments/<int:did>/edit", methods=["GET","POST"])
def admin_edit_department(did):
    conn = get_connection()
    if request.method == "POST":
        name = request.form.get("name","").strip(); fid = request.form.get("faculty_id")
        if name and fid:
            conn.execute("UPDATE departments SET name=?,faculty_id=? WHERE id=?",(name,fid,did))
            conn.commit(); conn.close(); flash("✅ Updated!","success"); return redirect(url_for("admin_departments"))
    faculties  = conn.execute("SELECT * FROM faculties ORDER BY name").fetchall()
    department = conn.execute("SELECT * FROM departments WHERE id=?",(did,)).fetchone()
    conn.close()
    if not department: flash("❌ Not found","error"); return redirect(url_for("admin_departments"))
    return render_template("admin/edit_department.html", department=department, faculties=faculties)

@app.route("/admin/departments/<int:did>/delete", methods=["POST"])
def admin_delete_department(did):
    conn = get_connection()
    try: conn.execute("DELETE FROM departments WHERE id=?",(did,)); conn.commit(); flash("✅ Deleted!","success")
    except Exception as e: flash(f"❌ {e}","error")
    conn.close(); return redirect(url_for("admin_departments"))

@app.route("/admin/courses", methods=["GET","POST"])
def admin_courses():
    conn = get_connection()
    if request.method == "POST":
        code = request.form.get("course_code","").strip()
        title = request.form.get("course_title","").strip()
        did  = request.form.get("department_id")
        if code and title and did:
            try: conn.execute("INSERT INTO courses (course_code,course_title,department_id) VALUES (?,?,?)",(code,title,did)); conn.commit(); flash("✅ Added!","success")
            except Exception as e: flash(f"❌ {e}","error")
    depts   = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    courses = conn.execute("SELECT c.id, c.course_code, c.course_title, d.name as dept_name FROM courses c JOIN departments d ON d.id=c.department_id ORDER BY c.course_code").fetchall()
    conn.close()
    return render_template("admin/courses.html", departments=depts, courses=courses)

@app.route("/admin/courses/<int:cid>/edit", methods=["GET","POST"])
def admin_edit_course(cid):
    conn = get_connection()
    if request.method == "POST":
        code = request.form.get("course_code","").strip(); title = request.form.get("course_title","").strip(); did = request.form.get("department_id")
        if code and title and did:
            conn.execute("UPDATE courses SET course_code=?,course_title=?,department_id=? WHERE id=?",(code,title,did,cid))
            conn.commit(); conn.close(); flash("✅ Updated!","success"); return redirect(url_for("admin_courses"))
    depts  = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    course = conn.execute("SELECT * FROM courses WHERE id=?",(cid,)).fetchone()
    conn.close()
    if not course: flash("❌ Not found","error"); return redirect(url_for("admin_courses"))
    return render_template("admin/edit_course.html", course=course, departments=depts)

@app.route("/admin/courses/<int:cid>/delete", methods=["POST"])
def admin_delete_course(cid):
    conn = get_connection()
    try: conn.execute("DELETE FROM courses WHERE id=?",(cid,)); conn.commit(); flash("✅ Deleted!","success")
    except Exception as e: flash(f"❌ {e}","error")
    conn.close(); return redirect(url_for("admin_courses"))

@app.route("/admin/register_lecturer", methods=["GET","POST"])
def admin_register_lecturer():
    conn = get_connection()
    if request.method == "POST":
        name=request.form.get("name","").strip(); sid=request.form.get("staff_id","").strip()
        email=request.form.get("email","").strip(); pwd=request.form.get("password",""); did=request.form.get("department_id")
        if all([name,sid,email,pwd,did]):
            try:
                conn.execute("INSERT INTO lecturers (name,staff_id,email,password,department_id) VALUES (?,?,?,?,?)",(name,sid,email,hash_password(pwd),did))
                conn.commit(); conn.close(); flash("✅ Registered!","success"); return redirect(url_for("admin_lecturers"))
            except Exception as e: flash(f"❌ {e}","error")
    depts = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    conn.close()
    return render_template("admin/register_lecturer.html", departments=depts)

@app.route("/admin/lecturers")
def admin_lecturers():
    conn = get_connection()
    rows = conn.execute("SELECT l.id, l.name, l.staff_id, l.email, d.name as dept_name FROM lecturers l JOIN departments d ON d.id=l.department_id ORDER BY l.name").fetchall()
    conn.close()
    return render_template("admin/lecturers.html", lecturers=rows)

@app.route("/admin/lecturers/<int:lid>/edit", methods=["GET","POST"])
def admin_edit_lecturer(lid):
    conn = get_connection()
    if request.method == "POST":
        name=request.form.get("name","").strip(); sid=request.form.get("staff_id","").strip()
        email=request.form.get("email","").strip(); pwd=request.form.get("password",""); did=request.form.get("department_id")
        if name and sid and email and did:
            if pwd: conn.execute("UPDATE lecturers SET name=?,staff_id=?,email=?,password=?,department_id=? WHERE id=?",(name,sid,email,hash_password(pwd),did,lid))
            else:   conn.execute("UPDATE lecturers SET name=?,staff_id=?,email=?,department_id=? WHERE id=?",(name,sid,email,did,lid))
            conn.commit(); conn.close(); flash("✅ Updated!","success"); return redirect(url_for("admin_lecturers"))
    depts    = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    lecturer = conn.execute("SELECT * FROM lecturers WHERE id=?",(lid,)).fetchone()
    conn.close()
    if not lecturer: flash("❌ Not found","error"); return redirect(url_for("admin_lecturers"))
    return render_template("admin/edit_lecturer.html", lecturer=lecturer, departments=depts)

@app.route("/admin/lecturers/<int:lid>/delete", methods=["POST"])
def admin_delete_lecturer(lid):
    conn = get_connection()
    try: conn.execute("DELETE FROM lecturers WHERE id=?",(lid,)); conn.commit(); flash("✅ Deleted!","success")
    except Exception as e: flash(f"❌ {e}","error")
    conn.close(); return redirect(url_for("admin_lecturers"))

@app.route("/admin/register_student", methods=["GET","POST"])
def admin_register_student():
    conn = get_connection()
    if request.method == "POST":
        name=request.form.get("name","").strip(); matric=request.form.get("matric","").strip()
        pwd=request.form.get("password",""); did=request.form.get("department_id"); level=request.form.get("level","").strip()
        face=request.files.get("face_image")
        if all([name,matric,pwd,did,level]):
            try:
                conn.execute("INSERT INTO students (name,matric,password,department_id,level) VALUES (?,?,?,?,?)",(name,matric,hash_password(pwd),did,level))
                sid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                if face:
                    fname, enc = save_face_image(face, sid)
                    if fname: conn.execute("UPDATE students SET face_image=?,face_encoding=? WHERE id=?",(fname,enc,sid))
                ensure_student_qr(sid)
                conn.commit(); conn.close(); flash("✅ Registered!","success"); return redirect(url_for("admin_students"))
            except Exception as e: flash(f"❌ {e}","error")
    depts = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    conn.close()
    return render_template("admin/register_student.html", departments=depts)

@app.route("/admin/students")
def admin_students():
    conn = get_connection()
    rows = conn.execute("SELECT s.id, s.name, s.matric, s.level, d.name as dept_name FROM students s JOIN departments d ON d.id=s.department_id ORDER BY s.name").fetchall()
    conn.close()
    return render_template("admin/students.html", students=rows)

@app.route("/admin/students/<int:sid>/edit", methods=["GET","POST"])
def admin_edit_student(sid):
    conn = get_connection()
    if request.method == "POST":
        name=request.form.get("name","").strip(); matric=request.form.get("matric","").strip()
        pwd=request.form.get("password",""); did=request.form.get("department_id"); level=request.form.get("level","").strip()
        if name and matric and did and level:
            if pwd: conn.execute("UPDATE students SET name=?,matric=?,password=?,department_id=?,level=? WHERE id=?",(name,matric,hash_password(pwd),did,level,sid))
            else:   conn.execute("UPDATE students SET name=?,matric=?,department_id=?,level=? WHERE id=?",(name,matric,did,level,sid))
            conn.commit(); conn.close(); flash("✅ Updated!","success"); return redirect(url_for("admin_students"))
    depts   = conn.execute("SELECT d.id, d.name, f.name as faculty_name FROM departments d JOIN faculties f ON f.id=d.faculty_id ORDER BY d.name").fetchall()
    student = conn.execute("SELECT * FROM students WHERE id=?",(sid,)).fetchone()
    conn.close()
    if not student: flash("❌ Not found","error"); return redirect(url_for("admin_students"))
    return render_template("admin/edit_student.html", student=student, departments=depts)

@app.route("/admin/students/<int:sid>/delete", methods=["POST"])
def admin_delete_student(sid):
    conn = get_connection()
    try: conn.execute("DELETE FROM students WHERE id=?",(sid,)); conn.commit(); flash("✅ Deleted!","success")
    except Exception as e: flash(f"❌ {e}","error")
    conn.close(); return redirect(url_for("admin_students"))


# ═══════════════════════════════════════════════════════════
#  LECTURER
# ═══════════════════════════════════════════════════════════
@app.route("/lecturer/login", methods=["GET","POST"])
def lecturer_login():
    if request.method == "POST":
        staff_id = request.form.get("staff_id","").strip()
        pwd      = request.form.get("password","")
        conn     = get_connection()
        lect     = conn.execute(
            "SELECT id,name,password,department_id FROM lecturers WHERE staff_id=?",
            (staff_id,)).fetchone()
        conn.close()
        if lect and check_password(pwd, lect['password']):
            session.clear()
            session['lecturer_id']   = lect['id']
            session['lecturer_name'] = lect['name']
            session['dept_id']       = lect['department_id']
            flash("✅ Login successful!", "success")
            return redirect(url_for("lecturer_dashboard"))
        flash("❌ Invalid Staff ID or Password", "error")
    return render_template("lecturer/login.html")

@app.route("/lecturer/logout")
def lecturer_logout():
    session.clear(); flash("✅ Logged out","success"); return redirect(url_for("home"))

@app.route("/lecturer/dashboard")
def lecturer_dashboard():
    if 'lecturer_id' not in session:
        flash("❌ Please login first","error"); return redirect(url_for("lecturer_login"))
    update_all_phases()
    conn    = get_connection()
    courses = conn.execute(
        "SELECT id,course_code,course_title FROM courses WHERE department_id=? ORDER BY course_code",
        (session['dept_id'],)).fetchall()
    conn.close()
    active = get_active_class(session['lecturer_id'])
    return render_template("lecturer/dashboard.html",
                           name=session['lecturer_name'],
                           courses=courses, active_class=active)

@app.route("/lecturer/create_class", methods=["POST"])
def lecturer_create_class():
    if 'lecturer_id' not in session: return redirect(url_for("lecturer_login"))
    lid      = session['lecturer_id']
    course_id = request.form.get("course_id")
    duration  = request.form.get("duration")
    method    = request.form.get("verification_method","QR")
    lat       = request.form.get("latitude",  type=float)
    lng       = request.form.get("longitude", type=float)
    radius    = request.form.get("geofence_radius", type=float) or Config.DEFAULT_GEOFENCE_RADIUS
    if not course_id or not duration:
        flash("❌ Please fill all fields","error"); return redirect(url_for("lecturer_dashboard"))
    conn   = get_connection()
    result = conn.execute("SELECT department_id FROM courses WHERE id=?",(course_id,)).fetchone()
    if not result:
        conn.close(); flash("❌ Invalid course","error"); return redirect(url_for("lecturer_dashboard"))
    conn.execute("""INSERT INTO class_sessions
        (course_id,lecturer_id,department_id,start_time,duration_minutes,
         verification_method,status,latitude,longitude,geofence_radius)
        VALUES (?,?,?,?,?,?,'ENTRY',?,?,?)""",
        (course_id, lid, result['department_id'],
         datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         duration, method, lat, lng, radius))
    conn.commit(); conn.close()
    flash("✅ Class started!","success"); return redirect(url_for("lecturer_scan"))

@app.route("/lecturer/scan")
def lecturer_scan():
    if 'lecturer_id' not in session: return redirect(url_for("lecturer_login"))
    active = get_active_class(session['lecturer_id'])
    if not active:
        flash("❌ No active class session","error"); return redirect(url_for("lecturer_dashboard"))
    return render_template("lecturer/scan.html", active_class=active)

@app.route("/lecturer/live/<int:class_id>")
def lecturer_live(class_id):
    if 'lecturer_id' not in session: return jsonify({"error":"Unauthorized"}), 401
    update_all_phases()
    conn = get_connection()
    sess = conn.execute("SELECT cs.*, c.course_code FROM class_sessions cs JOIN courses c ON c.id=cs.course_id WHERE cs.id=?",(class_id,)).fetchone()
    if not sess: conn.close(); return jsonify({"active":False})
    rows = conn.execute("""
        SELECT s.name, s.matric, d.name as dept_name,
               a.entry_time, a.exit_time, a.entry_valid, a.exit_valid,
               a.occupancy_percentage, a.attendance_score,
               a.face_verified, a.geofence_ok, a.geofence_violations
        FROM attendance a
        JOIN students s ON s.id=a.student_id
        JOIN departments d ON d.id=s.department_id
        WHERE a.class_session_id=? ORDER BY a.entry_time DESC""",(class_id,)).fetchall()
    conn.close()
    students = [{
        "name": r['name'], "matric": r['matric'], "dept": r['dept_name'],
        "entry_time": r['entry_time'], "exit_time": r['exit_time'],
        "entry_valid": bool(r['entry_valid']), "exit_valid": bool(r['exit_valid']),
        "occupancy":  round(r['occupancy_percentage'] or 0, 1),
        "score":      round(r['attendance_score'] or 0, 1),
        "face_ok":    bool(r['face_verified']),
        "geo_ok":     bool(r['geofence_ok']),
        "geo_violations": r['geofence_violations'],
    } for r in rows]
    return jsonify({"active":True,"course":sess['course_code'],
                    "status":sess['status'],"start":sess['start_time'],
                    "duration":sess['duration_minutes'],"students":students})

@app.route("/class/<int:class_id>/end", methods=["POST"])
def end_class(class_id):
    if 'lecturer_id' not in session: return jsonify({"error":"Unauthorized"}), 401
    conn = get_connection()
    conn.execute("UPDATE class_sessions SET status='CLOSED' WHERE id=? AND lecturer_id=?",
                 (class_id, session['lecturer_id']))
    conn.commit(); conn.close(); return "", 204

@app.route("/class/<int:class_id>/adjust_duration", methods=["POST"])
def adjust_duration(class_id):
    if 'lecturer_id' not in session: return jsonify({"error":"Unauthorized"}), 401
    dur = request.form.get("duration")
    if not dur: return jsonify({"error":"Duration required"}), 400
    conn = get_connection()
    conn.execute("UPDATE class_sessions SET duration_minutes=? WHERE id=? AND lecturer_id=?",(dur,class_id,session['lecturer_id']))
    conn.commit(); conn.close(); return jsonify({"status":"ok"})

@app.route("/class/<int:class_id>/force_phase", methods=["POST"])
def force_phase(class_id):
    if 'lecturer_id' not in session: return jsonify({"error":"Unauthorized"}), 401
    phase = request.form.get("phase","")
    if phase not in ('ENTRY','MONITORING','EXIT','CLOSED'): return jsonify({"error":"Invalid phase"}), 400
    conn = get_connection()
    conn.execute("UPDATE class_sessions SET status=? WHERE id=? AND lecturer_id=?",(phase,class_id,session['lecturer_id']))
    conn.commit(); conn.close(); return jsonify({"status":"ok"})

@app.route("/class/<int:class_id>/export/csv")
def export_csv(class_id):
    conn = get_connection()
    rows = conn.execute("""
        SELECT s.name, s.matric, d.name as dept_name,
               a.entry_time, a.exit_time, a.entry_valid, a.exit_valid,
               a.occupancy_percentage, a.attendance_score,
               a.face_verified, a.geofence_ok, a.geofence_violations
        FROM attendance a
        JOIN students s ON s.id=a.student_id
        JOIN departments d ON d.id=s.department_id
        WHERE a.class_session_id=? ORDER BY a.entry_time""",(class_id,)).fetchall()
    conn.close()
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(["Name","Matric","Department","Entry Time","Exit Time",
                "Entry Valid","Exit Valid","Occupancy %","Score /100",
                "Face Verified","Geofence OK","Geo Violations"])
    for r in rows:
        w.writerow([r['name'],r['matric'],r['dept_name'],
                    r['entry_time'] or '',r['exit_time'] or '',
                    'Yes' if r['entry_valid'] else 'No',
                    'Yes' if r['exit_valid']  else 'No',
                    f"{r['occupancy_percentage']:.1f}" if r['occupancy_percentage'] else '0',
                    f"{r['attendance_score']:.1f}"     if r['attendance_score']     else '0',
                    'Yes' if r['face_verified'] else 'No',
                    'Yes' if r['geofence_ok']   else 'No',
                    r['geofence_violations']])
    resp = make_response(si.getvalue())
    resp.headers["Content-Disposition"] = f"attachment; filename=attendance_{class_id}.csv"
    resp.headers["Content-Type"] = "text/csv"
    return resp


# ═══════════════════════════════════════════════════════════
#  STUDENT
# ═══════════════════════════════════════════════════════════
@app.route("/student/login", methods=["GET","POST"])
def student_login():
    if request.method == "POST":
        matric    = request.form.get("matric","").strip()
        pwd       = request.form.get("password","")
        device_id = request.form.get("device_id","").strip()
        conn      = get_connection()
        student   = conn.execute(
            "SELECT id,name,password,device_id FROM students WHERE matric=?",
            (matric,)).fetchone()
        if student and check_password(pwd, student['password']):
            session.clear()
            session['student_id']   = student['id']
            session['student_name'] = student['name']
            if device_id and not student['device_id']:
                conn.execute("UPDATE students SET device_id=? WHERE id=?",
                             (device_id, student['id']))
                conn.commit()
            conn.close()
            flash("✅ Login successful!","success")
            return redirect(url_for("student_dashboard"))
        conn.close(); flash("❌ Invalid Matric Number or Password","error")
    return render_template("student/login.html")

@app.route("/student/logout")
def student_logout():
    session.clear(); flash("✅ Logged out","success"); return redirect(url_for("home"))

@app.route("/student/dashboard")
def student_dashboard():
    if 'student_id' not in session:
        flash("❌ Please login first","error"); return redirect(url_for("student_login"))
    return render_template("student/dashboard.html", name=session['student_name'])

@app.route("/student/qr")
def student_qr():
    if 'student_id' not in session: return redirect(url_for("student_login"))
    sid  = session['student_id']
    ensure_student_qr(sid)
    path = f"static/qrcodes/student_{sid}.png"
    has_image = os.path.exists(os.path.join(Config.BASE_DIR, path))
    return render_template("student/qr.html",
                           student_id=sid,
                           qr_path=f"/{path}" if has_image else None)

@app.route("/student/attend_class")
def student_attend_class():
    if 'student_id' not in session: return redirect(url_for("student_login"))
    update_all_phases()
    sid  = session['student_id']
    conn = get_connection()
    info = conn.execute("""SELECT d.faculty_id FROM students s
        JOIN departments d ON d.id=s.department_id WHERE s.id=?""",(sid,)).fetchone()
    if not info: conn.close(); flash("❌ Student info not found","error"); return redirect(url_for("student_dashboard"))
    classes = conn.execute("""
        SELECT cs.id, cs.start_time, cs.duration_minutes, cs.status,
               cs.verification_method, cs.latitude, cs.longitude, cs.geofence_radius,
               c.course_code, c.course_title, d.name as dept_name, l.name as lecturer_name
        FROM class_sessions cs
        JOIN courses c     ON c.id  = cs.course_id
        JOIN departments d ON d.id  = cs.department_id
        JOIN lecturers l   ON l.id  = cs.lecturer_id
        WHERE cs.status IN ('ENTRY','MONITORING','EXIT') AND d.faculty_id=?
        ORDER BY cs.start_time DESC""",(info['faculty_id'],)).fetchall()
    conn.close()
    return render_template("student/attend_class.html", active_classes=classes)

@app.route("/student/verify_attendance/<int:class_id>")
def student_verify_attendance(class_id):
    if 'student_id' not in session: return redirect(url_for("student_login"))
    sid  = session['student_id']
    conn = get_connection()
    ci   = conn.execute("""SELECT cs.*, c.course_code, c.course_title FROM class_sessions cs
        JOIN courses c ON c.id=cs.course_id WHERE cs.id=?""",(class_id,)).fetchone()
    conn.close()
    if not ci: flash("❌ Class not found","error"); return redirect(url_for("student_attend_class"))
    ensure_student_qr(sid)
    path      = f"static/qrcodes/student_{sid}.png"
    has_image = os.path.exists(os.path.join(Config.BASE_DIR, path))
    v_text    = {'QR':'🔵 QR Code Only','QR_FACE':'🟢 QR + Face Recognition',
                 'QR_FINGERPRINT':'🔴 QR + Fingerprint'}.get(ci['verification_method'],'QR')
    return render_template("student/verify_attendance.html",
                           class_info=ci, student_id=sid,
                           verification_text=v_text,
                           qr_path=f"/{path}" if has_image else None)

@app.route("/student/check_qr_status/<int:class_id>")
def check_qr_status(class_id):
    if 'student_id' not in session: return jsonify({"error":"Not logged in"}), 401
    sid  = session['student_id']
    conn = get_connection()
    rec  = conn.execute("""SELECT qr_verified,face_verified,exit_time,
        attendance_score,geofence_ok,geofence_violations
        FROM attendance WHERE student_id=? AND class_session_id=?""",(sid,class_id)).fetchone()
    sess = conn.execute("SELECT status FROM class_sessions WHERE id=?",(class_id,)).fetchone()
    conn.close()
    phase = sess['status'] if sess else 'CLOSED'
    if rec:
        return jsonify({"current_phase":phase,
                        "qr_verified":bool(rec['qr_verified']),
                        "face_verified":bool(rec['face_verified']),
                        "exit_marked":rec['exit_time'] is not None,
                        "score":rec['attendance_score'] or 0,
                        "geo_ok":bool(rec['geofence_ok']),
                        "geo_violations":rec['geofence_violations']})
    return jsonify({"current_phase":phase,"qr_verified":False,
                    "face_verified":False,"exit_marked":False,
                    "score":0,"geo_ok":True,"geo_violations":0})

@app.route("/student/verify_face", methods=["POST"])
def student_verify_face():
    if 'student_id' not in session: return jsonify({"status":"error","message":"Not logged in"}), 401
    sid      = session['student_id']
    class_id = request.form.get("class_id", type=int)
    face     = request.files.get("face_image")
    if not face or not class_id: return jsonify({"status":"error","message":"Missing data"})
    matched, msg = verify_face(sid, face)
    if matched:
        conn = get_connection()
        conn.execute("UPDATE attendance SET face_verified=1 WHERE student_id=? AND class_session_id=?",(sid,class_id))
        conn.commit(); conn.close()
        return jsonify({"status":"success","message":msg})
    return jsonify({"status":"error","message":msg})


# ═══════════════════════════════════════════════════════════
#  QR PROCESSING  (static QR + geofence enforcement)
# ═══════════════════════════════════════════════════════════
@app.route("/process_qr", methods=["POST"])
def process_qr():
    update_all_phases()
    qr_data   = request.form.get("qr_data","")
    class_id  = request.form.get("class_id", type=int)
    slat      = request.form.get("latitude",   type=float)
    slng      = request.form.get("longitude",  type=float)
    accuracy  = request.form.get("accuracy",   type=float)   # GPS accuracy in metres
    device_id = request.form.get("device_id","").strip()

    if not class_id:
        return jsonify({"status":"error","message":"Missing class_id"})

    # Parse QR:  STUDENT:<id>
    parts = qr_data.strip().split(":")
    if len(parts) != 2 or parts[0] != "STUDENT":
        return jsonify({"status":"error","message":"Invalid QR code format"})
    try:
        student_id = int(parts[1])
    except ValueError:
        return jsonify({"status":"error","message":"Corrupted QR code"})

    conn = get_connection()
    sess = conn.execute("""SELECT cs.*, c.course_code FROM class_sessions cs
        JOIN courses c ON c.id=cs.course_id WHERE cs.id=?""",(class_id,)).fetchone()
    if not sess or sess['status'] not in ('ENTRY','MONITORING','EXIT'):
        conn.close(); return jsonify({"status":"error","message":"No active class session"})

    student = conn.execute(
        "SELECT id,name,matric,department_id,device_id FROM students WHERE id=?",
        (student_id,)).fetchone()
    if not student:
        conn.close(); return jsonify({"status":"error","message":"Student not found"})

    # Device binding
    if device_id and student['device_id'] and student['device_id'] != device_id:
        conn.close()
        return jsonify({"status":"error","message":"⛔ Unregistered device — attendance rejected"})

    # Geofence — required when class has coordinates
    geo_ok   = True
    geo_msg  = ""
    geo_dist = 0.0
    if sess['latitude'] is not None:
        if slat is None or slng is None:
            conn.close()
            return jsonify({"status":"error",
                            "message":"📍 Location required — enable GPS and try again"})
        geo_ok, geo_msg, geo_dist = geofence_check(
            slat, slng, accuracy,
            sess['latitude'], sess['longitude'], sess['geofence_radius'])
        if not geo_ok:
            conn.close()
            return jsonify({"status":"error","message":f"⛔ {geo_msg}"})

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status  = sess['status']
    info    = {"id":student['id'],"name":student['name'],"matric":student['matric'],
               "distance":f"{geo_dist:.0f}m" if geo_dist else "N/A"}

    # ── ENTRY ─────────────────────────────────────────────
    if status == "ENTRY":
        rec = conn.execute("""SELECT id,entry_time FROM attendance
            WHERE student_id=? AND class_session_id=?""",(student_id,class_id)).fetchone()
        if rec and rec['entry_time']:
            conn.close(); return jsonify({"status":"error","message":"Entry already marked"})
        ev = is_entry_valid(now_str, sess['start_time'])
        if rec:
            conn.execute("""UPDATE attendance SET entry_time=?,entry_valid=?,
                qr_verified=1,geofence_ok=? WHERE id=?""",
                (now_str, int(ev), int(geo_ok), rec['id']))
            att_id = rec['id']
        else:
            conn.execute("""INSERT INTO attendance
                (student_id,class_session_id,entry_time,entry_valid,qr_verified,geofence_ok)
                VALUES (?,?,?,?,1,?)""",(student_id,class_id,now_str,int(ev),int(geo_ok)))
            att_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("""INSERT INTO attendance_logs
            (attendance_id,logged_at,latitude,longitude,geofence_ok,log_type)
            VALUES (?,?,?,?,?,'ENTRY')""",(att_id,now_str,slat,slng,int(geo_ok)))
        conn.commit(); conn.close()
        loc_note = f" ({geo_dist:.0f}m from class)" if geo_dist else ""
        msg = f"✅ Entry marked!{loc_note}" if ev else f"⚠️ Entry marked but LATE{loc_note}"
        return jsonify({"status":"ok" if ev else "warning",
                        "phase":"ENTRY","message":msg,"student":info})

    # ── MONITORING ────────────────────────────────────────
    elif status == "MONITORING":
        rec = conn.execute("""SELECT id,entry_valid,geofence_ok FROM attendance
            WHERE student_id=? AND class_session_id=?""",(student_id,class_id)).fetchone()
        if not rec:
            conn.close(); return jsonify({"status":"error","message":"Entry not recorded yet"})
        if not geo_ok:
            conn.execute("""UPDATE attendance SET geofence_violations=geofence_violations+1,
                geofence_ok=0 WHERE id=?""",(rec['id'],))
            conn.commit(); conn.close()
            return jsonify({"status":"warning",
                            "message":f"⚠️ {geo_msg} — violation recorded","student":info})
        conn.execute("""INSERT INTO attendance_logs
            (attendance_id,logged_at,latitude,longitude,geofence_ok,log_type)
            VALUES (?,?,?,?,?,'HEARTBEAT')""",(rec['id'],now_str,slat,slng,int(geo_ok)))
        pings = conn.execute("""SELECT COUNT(*) as cnt FROM attendance_logs
            WHERE attendance_id=? AND log_type='HEARTBEAT'""",(rec['id'],)).fetchone()['cnt']
        occ   = calc_occupancy(pings, sess['duration_minutes'])
        occ_v = int(occ >= Config.OCCUPANCY_THRESHOLD)
        score = calc_score(bool(rec['entry_valid']), False, occ, bool(rec['geofence_ok']))
        conn.execute("""UPDATE attendance SET occupancy_percentage=?,occupancy_valid=?,
            attendance_score=? WHERE id=?""",(occ,occ_v,score,rec['id']))
        conn.commit(); conn.close()
        return jsonify({"status":"ok","phase":"MONITORING",
                        "message":f"✅ Presence recorded ({geo_dist:.0f}m) — Occupancy: {occ:.1f}%",
                        "occupancy":occ,"student":info})

    # ── EXIT ──────────────────────────────────────────────
    elif status == "EXIT":
        rec = conn.execute("""SELECT id,entry_valid,exit_time,geofence_ok FROM attendance
            WHERE student_id=? AND class_session_id=?""",(student_id,class_id)).fetchone()
        if not rec:
            conn.close(); return jsonify({"status":"error","message":"Entry not recorded"})
        if rec['exit_time']:
            conn.close(); return jsonify({"status":"error","message":"Exit already marked"})
        xv    = is_exit_valid(now_str, sess['start_time'], sess['duration_minutes'])
        pings = conn.execute("""SELECT COUNT(*) as cnt FROM attendance_logs
            WHERE attendance_id=? AND log_type='HEARTBEAT'""",(rec['id'],)).fetchone()['cnt']
        occ   = calc_occupancy(pings, sess['duration_minutes'])
        occ_v = int(occ >= Config.OCCUPANCY_THRESHOLD)
        score = calc_score(bool(rec['entry_valid']), xv, occ, bool(rec['geofence_ok']))
        conn.execute("""UPDATE attendance SET exit_time=?,exit_valid=?,
            occupancy_percentage=?,occupancy_valid=?,attendance_score=? WHERE id=?""",
            (now_str,int(xv),occ,occ_v,score,rec['id']))
        conn.execute("""INSERT INTO attendance_logs
            (attendance_id,logged_at,latitude,longitude,geofence_ok,log_type)
            VALUES (?,?,?,?,?,'EXIT')""",(rec['id'],now_str,slat,slng,int(geo_ok)))
        conn.commit(); conn.close()
        if score >= 80:
            return jsonify({"status":"ok","phase":"EXIT",
                            "message":f"✅ COMPLETE! Score: {score:.0f}/100","student":info})
        reasons = []
        if not rec['entry_valid']:           reasons.append("late entry")
        if not xv:                           reasons.append("early exit")
        if occ < Config.OCCUPANCY_THRESHOLD: reasons.append(f"low occupancy ({occ:.0f}%)")
        return jsonify({"status":"warning","phase":"EXIT",
                        "message":f"⚠️ Exit marked. Score:{score:.0f}/100 ({', '.join(reasons)})",
                        "student":info})

    conn.close()
    return jsonify({"status":"error","message":"Unknown phase"})


# ═══════════════════════════════════════════════════════════
#  ATTENDANCE REPORT
# ═══════════════════════════════════════════════════════════
@app.route("/attendance_report")
def attendance_report():
    dept_f   = request.args.get("department")
    course_f = request.args.get("course")
    conn     = get_connection()
    q = """SELECT s.name, s.matric, c.course_code, c.id as course_id,
               a.entry_time, a.exit_time, a.entry_valid, a.exit_valid,
               a.occupancy_valid, a.occupancy_percentage, a.attendance_score,
               cs.start_time, d.name as dept_name, d.id as dept_id
           FROM attendance a
           JOIN students s        ON s.id  = a.student_id
           JOIN class_sessions cs ON cs.id = a.class_session_id
           JOIN courses c         ON c.id  = cs.course_id
           JOIN departments d     ON d.id  = s.department_id
           WHERE 1=1"""
    params = []
    if dept_f:   q += " AND d.id=?";  params.append(dept_f)
    if course_f: q += " AND c.id=?";  params.append(course_f)
    q += " ORDER BY cs.start_time DESC, a.entry_time DESC"
    records     = conn.execute(q, params).fetchall()
    departments = conn.execute("SELECT id,name FROM departments ORDER BY name").fetchall()
    courses     = conn.execute("SELECT id,course_code,course_title FROM courses ORDER BY course_code").fetchall()
    conn.close()
    return render_template("attendance_report.html", records=records,
                           departments=departments, courses=courses,
                           selected_dept=dept_f, selected_course=course_f)


# ═══════════════════════════════════════════════════════════
#  RUN
# ═══════════════════════════════════════════════════════════
if __name__ == "__main__":
    os.makedirs(Config.QR_CODE_DIR,     exist_ok=True)
    os.makedirs(Config.FACE_IMAGES_DIR, exist_ok=True)
    create_tables()
    migrate_existing_db()
    app.run(debug=True, host='0.0.0.0', port=5000)