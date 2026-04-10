import os
import secrets

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    DEBUG = True
    DB_NAME = "smart_attendance.db"

    PERMANENT_SESSION_LIFETIME = 3600
    SESSION_COOKIE_HTTPONLY    = True
    SESSION_COOKIE_SECURE      = False

    BASE_DIR        = os.path.dirname(os.path.abspath(__file__))
    QR_CODE_DIR     = os.path.join(BASE_DIR, "static", "qrcodes")
    FACE_IMAGES_DIR = os.path.join(BASE_DIR, "static", "face_images")
    ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024

    # Attendance phase windows (minutes)
    ENTRY_WINDOW_BEFORE    = 10
    ENTRY_WINDOW_AFTER     = 10
    EXIT_WINDOW_PERCENTAGE = 25

    # Occupancy / heartbeat
    OCCUPANCY_THRESHOLD = 75
    HEARTBEAT_INTERVAL  = 15

    # Geofencing
    DEFAULT_GEOFENCE_RADIUS = 100
    ACCURACY_BUFFER_FACTOR  = 0.4
    GPS_SPOOF_SPEED_LIMIT   = 50

    # Attendance session scoring
    SCORE_ENTRY_POINTS  = 20
    SCORE_EXIT_POINTS   = 20
    SCORE_OCCUPANCY_MAX = 60
    SCORE_GEO_PENALTY   = 10

    # ── Attendance Status Rules ──────────────────────────────
    # A session gets status PRESENT, LATE, ABSENT, or INCOMPLETE
    # PRESENT  : on-time entry + valid exit + occupancy >= threshold
    # LATE     : late entry OR early exit (but not both missing)
    # ABSENT   : no entry at all, or geo-absent-triggered, or score < ABSENT_SCORE_THRESHOLD
    # INCOMPLETE: entry marked but no exit yet (class still running)
    ABSENT_SCORE_THRESHOLD = 30   # score below this → ABSENT regardless

    # ── Geo-absence auto-trigger ─────────────────────────────
    # If a student leaves the geofence for longer than this, mark ABSENT automatically
    GEO_ABSENCE_MINUTES = 5

    # ── Aggregate / semester scoring ─────────────────────────
    # Defaults (can be overridden per-course)
    DEFAULT_PRESENT_WEIGHT   = 1.0
    DEFAULT_LATE_WEIGHT      = 0.5
    DEFAULT_ABSENT_WEIGHT    = 0.0
    DEFAULT_EXAM_THRESHOLD   = 75.0   # % needed to sit exam