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
    DEFAULT_GEOFENCE_RADIUS  = 50     # metres — tight by default
    GPS_ACCURACY_THRESHOLD   = 50     # reject GPS readings worse than this (metres)
    GPS_SPOOF_SPEED_LIMIT    = 10     # m/s — flag as spoofed if moved faster than this

    # Attendance scoring
    SCORE_ENTRY_POINTS  = 20
    SCORE_EXIT_POINTS   = 20
    SCORE_OCCUPANCY_MAX = 60
    SCORE_GEO_PENALTY   = 10