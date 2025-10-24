# config.py
import os
from pathlib import Path

BASEDIR = Path(__file__).resolve().parent
DATA_DIR = BASEDIR / "data"
DATA_DIR.mkdir(exist_ok=True)

DEFAULT_SQLITE = f"sqlite:///{(DATA_DIR / 'database.db').as_posix()}"

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-prod")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", DEFAULT_SQLITE)
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Bogota")

    # CSRF
    WTF_CSRF_ENABLED = True

    # OTP
    OTP_MODE = os.getenv("OTP_MODE", "mock")  # mock|twilio
    TWILIO_SID = os.getenv("TWILIO_SID", "")
    TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
    TWILIO_FROM = os.getenv("TWILIO_FROM", "")
