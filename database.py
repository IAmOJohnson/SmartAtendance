import sqlite3
from config import Config


def get_connection():
    conn = sqlite3.connect(Config.DB_NAME, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_tables():
    conn = get_connection()
    c = conn.cursor()

    # FACULTIES
    c.execute("""
    CREATE TABLE IF NOT EXISTS faculties (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT UNIQUE NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")

    # DEPARTMENTS
    c.execute("""
    CREATE TABLE IF NOT EXISTS departments (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        faculty_id INTEGER NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(name, faculty_id),
        FOREIGN KEY (faculty_id) REFERENCES faculties(id) ON DELETE CASCADE
    )""")

    # LECTURERS
    c.execute("""
    CREATE TABLE IF NOT EXISTS lecturers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        staff_id      TEXT UNIQUE NOT NULL,
        name          TEXT NOT NULL,
        email         TEXT UNIQUE NOT NULL,
        password      TEXT NOT NULL,
        department_id INTEGER NOT NULL,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
    )""")

    # STUDENTS  — added device_id for device-binding
    c.execute("""
    CREATE TABLE IF NOT EXISTS students (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT NOT NULL,
        matric        TEXT UNIQUE NOT NULL,
        password      TEXT NOT NULL,
        department_id INTEGER NOT NULL,
        level         TEXT NOT NULL,
        face_image    TEXT,
        face_encoding TEXT,
        device_id     TEXT,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
    )""")

    # COURSES
    c.execute("""
    CREATE TABLE IF NOT EXISTS courses (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        course_code   TEXT UNIQUE NOT NULL,
        course_title  TEXT NOT NULL,
        department_id INTEGER NOT NULL,
        created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
    )""")

    # CLASS SESSIONS  — added lat/lng/radius for geofencing
    c.execute("""
    CREATE TABLE IF NOT EXISTS class_sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        course_id           INTEGER NOT NULL,
        lecturer_id         INTEGER NOT NULL,
        department_id       INTEGER NOT NULL,
        start_time          TEXT NOT NULL,
        duration_minutes    INTEGER NOT NULL,
        verification_method TEXT CHECK(verification_method IN ('QR','QR_FACE','QR_FINGERPRINT')) DEFAULT 'QR',
        status              TEXT CHECK(status IN ('ENTRY','MONITORING','EXIT','CLOSED')) DEFAULT 'ENTRY',
        latitude            REAL,
        longitude           REAL,
        geofence_radius     REAL DEFAULT 100,
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (course_id)     REFERENCES courses(id)     ON DELETE CASCADE,
        FOREIGN KEY (lecturer_id)   REFERENCES lecturers(id)   ON DELETE CASCADE,
        FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
    )""")

    # ATTENDANCE  — added geofence tracking + scoring
    c.execute("""
    CREATE TABLE IF NOT EXISTS attendance (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        student_id           INTEGER NOT NULL,
        class_session_id     INTEGER NOT NULL,
        entry_time           TEXT,
        exit_time            TEXT,
        entry_valid          INTEGER DEFAULT 0,
        exit_valid           INTEGER DEFAULT 0,
        occupancy_valid      INTEGER DEFAULT 0,
        occupancy_percentage REAL    DEFAULT 0.0,
        presence_pings       TEXT,
        qr_verified          INTEGER DEFAULT 0,
        face_verified        INTEGER DEFAULT 0,
        fingerprint_verified INTEGER DEFAULT 0,
        geofence_ok          INTEGER DEFAULT 1,
        geofence_violations  INTEGER DEFAULT 0,
        attendance_score     REAL    DEFAULT 0.0,
        created_at           TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(student_id, class_session_id),
        FOREIGN KEY (student_id)       REFERENCES students(id)       ON DELETE CASCADE,
        FOREIGN KEY (class_session_id) REFERENCES class_sessions(id) ON DELETE CASCADE
    )""")

    # ATTENDANCE LOGS  — normalized heartbeat pings (replaces presence_pings CSV)
    c.execute("""
    CREATE TABLE IF NOT EXISTS attendance_logs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        attendance_id INTEGER NOT NULL,
        logged_at     TEXT NOT NULL,
        latitude      REAL,
        longitude     REAL,
        geofence_ok   INTEGER DEFAULT 1,
        log_type      TEXT CHECK(log_type IN ('ENTRY','HEARTBEAT','EXIT')) DEFAULT 'HEARTBEAT',
        FOREIGN KEY (attendance_id) REFERENCES attendance(id) ON DELETE CASCADE
    )""")

    # INDEXES
    for sql in [
        "CREATE INDEX IF NOT EXISTS idx_students_matric    ON students(matric)",
        "CREATE INDEX IF NOT EXISTS idx_students_dept      ON students(department_id)",
        "CREATE INDEX IF NOT EXISTS idx_lecturers_staff    ON lecturers(staff_id)",
        "CREATE INDEX IF NOT EXISTS idx_class_status       ON class_sessions(status)",
        "CREATE INDEX IF NOT EXISTS idx_class_lecturer     ON class_sessions(lecturer_id)",
        "CREATE INDEX IF NOT EXISTS idx_attendance_session ON attendance(class_session_id)",
        "CREATE INDEX IF NOT EXISTS idx_attendance_student ON attendance(student_id)",
        "CREATE INDEX IF NOT EXISTS idx_logs_attendance    ON attendance_logs(attendance_id)",
    ]:
        c.execute(sql)

    conn.commit()
    conn.close()
    print("✅ Database tables ready")


if __name__ == "__main__":
    create_tables()


def migrate_existing_db():
    """Safely add new columns to an existing database without losing data."""
    conn = get_connection()
    c    = conn.cursor()

    def add_col(table, col, definition):
        existing = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            print(f"  + {table}.{col}")

    add_col("students",       "device_id",           "TEXT")
    add_col("class_sessions", "latitude",             "REAL")
    add_col("class_sessions", "longitude",            "REAL")
    add_col("class_sessions", "geofence_radius",      "REAL DEFAULT 100")
    add_col("attendance",     "geofence_ok",          "INTEGER DEFAULT 1")
    add_col("attendance",     "geofence_violations",  "INTEGER DEFAULT 0")
    add_col("attendance",     "attendance_score",     "REAL DEFAULT 0.0")

    # attendance_logs table
    c.execute("""
    CREATE TABLE IF NOT EXISTS attendance_logs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        attendance_id INTEGER NOT NULL,
        logged_at     TEXT NOT NULL,
        latitude      REAL,
        longitude     REAL,
        geofence_ok   INTEGER DEFAULT 1,
        log_type      TEXT CHECK(log_type IN ('ENTRY','HEARTBEAT','EXIT')) DEFAULT 'HEARTBEAT',
        FOREIGN KEY (attendance_id) REFERENCES attendance(id) ON DELETE CASCADE
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_logs_attendance ON attendance_logs(attendance_id)")

    conn.commit()
    conn.close()

def migrate_v2():
    """Add new columns for v2 features: attendance_status, geo_absent_at, scoring weights."""
    conn = get_connection()
    c    = conn.cursor()

    def add_col(table, col, definition):
        existing = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if col not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            print(f"  + {table}.{col}")

    # attendance_status: PRESENT | LATE | ABSENT | INCOMPLETE
    add_col("attendance", "attendance_status", "TEXT DEFAULT 'INCOMPLETE'")
    # When a geo-absence was auto-triggered (student left geofence >5min)
    add_col("attendance", "geo_absent_at",     "TEXT")
    # Weighted points for aggregate calculation
    add_col("attendance", "weighted_points",   "REAL DEFAULT 0.0")

    # Course-level aggregate settings
    add_col("courses", "exam_threshold",   "REAL DEFAULT 75.0")  # % needed to sit exam
    add_col("courses", "present_weight",   "REAL DEFAULT 1.0")
    add_col("courses", "late_weight",      "REAL DEFAULT 0.5")

    conn.commit()
    conn.close()