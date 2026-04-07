import os
import secrets

class Config:
    """Application Configuration"""

    # Flask
    SECRET_KEY = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
    DEBUG = True

    # Database
    DB_NAME = "smart_attendance.db"

    # Session
    PERMANENT_SESSION_LIFETIME = 3600
    SESSION_COOKIE_HTTPONLY    = True
    SESSION_COOKIE_SECURE      = False

    # Directories
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
    # ─────────────────────────────────────────────────────────────────
    # Indoor GPS on phones routinely reads ±100–300m even when you are
    # standing in the right spot.  We NEVER hard-reject based on accuracy
    # alone — instead we use the accuracy reading as a smart buffer added
    # to the geofence radius so edge-of-building students aren't wrongly
    # blocked.  Only GPS with accuracy > ACCURACY_HARD_LIMIT (very bad
    # readings like ±800m+) gets a soft warning, never a hard reject.
    # ─────────────────────────────────────────────────────────────────
    DEFAULT_GEOFENCE_RADIUS = 100    # metres
    ACCURACY_HARD_LIMIT     = 500    # above this: warn but still allow
    ACCURACY_BUFFER_FACTOR  = 0.4    # add 40 % of accuracy as radius buffer
    GPS_SPOOF_SPEED_LIMIT   = 50     # m/s — only flag obvious teleportation

    # Attendance scoring
    SCORE_ENTRY_POINTS  = 20
    SCORE_EXIT_POINTS   = 20
    SCORE_OCCUPANCY_MAX = 60
    SCORE_GEO_PENALTY   = 10