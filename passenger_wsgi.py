# passenger_wsgi.py – Hostinger/Passenger entrypoint
import sys, os
from pathlib import Path

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path:
    sys.path.insert(0, str(BASE))

# Variables de entorno mínimas (puedes gestionarlas también desde hPanel)
os.environ.setdefault("FLASK_ENV", "production")
os.environ.setdefault("APP_TIMEZONE", "America/Bogota")

# Importa la app desde app.py
from app import app as application  # Passenger espera 'application'
