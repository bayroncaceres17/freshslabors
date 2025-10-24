# init_db.py
# Uso:
#   python init_db.py          # crea tablas y siembra superadmin + una tarea demo
#   python init_db.py --reset  # (opcional) borra tablas y las recrea

from flask import Flask
from flask_bcrypt import Bcrypt
from datetime import datetime
import os
import sys

from config import Config
from app import app as flask_app  # reusa la app y modelos ya definidos en app.py
from app import db, Usuario, Tarea  # importa modelos desde app.py

bcrypt = Bcrypt(flask_app)

def create_all():
    with flask_app.app_context():
        db.create_all()

def drop_all():
    with flask_app.app_context():
        db.drop_all()

def seed():
    with flask_app.app_context():
        # Datos por defecto (puedes sobreescribir con .env)
        email = os.getenv("SUPERADMIN_EMAIL", "owner@freshslabors.local")
        nombre = os.getenv("SUPERADMIN_NAME", "Superadmin")
        telefono = os.getenv("SUPERADMIN_PHONE", None)
        default_pass = os.getenv("SUPERADMIN_PASSWORD", "administradorsuper123")

        # Superadmin
        sa = Usuario.query.filter_by(email=email).first()
        if not sa:
            hashed = bcrypt.generate_password_hash(default_pass).decode("utf-8")
            sa = Usuario(
                nombre=nombre,
                email=email,
                telefono=telefono,
                password=hashed,       # ← encriptado
                rol="superadmin",
                activo=True
            )
            db.session.add(sa)
            db.session.commit()
            print(f"✅ Superadmin creado: {email} (pass: {default_pass})")
        else:
            print(f"ℹ️ Superadmin ya existía: {email}")

        # Tarea demo (activa) — ajusta si no la quieres
        if not Tarea.query.first():
            t = Tarea(
                nombre="Operación Mañana",
                descripcion="Tarea demo para pruebas",
                ubicacion="Planta A",
                prioridad="comun",
                activa=True,
                cantidad_reportes=3,
                hora_inicio="08:00",
                hora_lunch="12:00",
                hora_salida="17:00",
                tolerancia_min=30
            )
            db.session.add(t)
            db.session.commit()
            print(f"✅ Tarea de ejemplo creada: {t.nombre}")

if __name__ == "__main__":
    if "--reset" in sys.argv:
        confirm = input("Esto borrará TODAS las tablas. ¿Continuar? (yes/no): ").strip().lower()
        if confirm == "yes":
            drop_all()
            print("🗑️  Tablas eliminadas.")
        else:
            print("Cancelado.")
            sys.exit(0)

    create_all()
    seed()
    print("✅ Base de datos lista.")
