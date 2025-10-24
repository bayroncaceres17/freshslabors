from collections import defaultdict
from collections import defaultdict, Counter
import csv
from datetime import datetime, timedelta, time
from datetime import datetime
from datetime import datetime, date, time, timedelta
from functools import wraps
import io
import io as _io
import json
import os
from pathlib import Path
import random
import secrets
from zoneinfo import ZoneInfo
from flask import (
    Flask,
    render_template,
    redirect,
    url_for,
    request,
    flash,
    session,
    send_file,
    abort,
    jsonify
)
from flask import send_file
from flask_bcrypt import Bcrypt
from flask_login import (
    LoginManager,
    login_user,
    logout_user,
    current_user,
    login_required,
    UserMixin
)
from flask_sqlalchemy import SQLAlchemy
from services.payments_service import week_window_for, unpaid_list_for_period
from sqlalchemy import func, text, and_, exists, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import TypeDecorator, Text
from datetime import datetime, timedelta, date
from flask import send_file, request, flash, redirect, url_for, current_app

from services.horas import compute_range
from services.respaldo import (
    export_empleados_activos_csv_bytes,
    export_empleados_activos_xlsx_bytes,
    export_colillas_zip_bytes,
    export_historial_empleados_xlsx_bytes,
)
from services.telefono import normalize_e164, only_digits as norm_phone
from sqlalchemy.orm import backref

from services.empleado_helpers import (
    can_confirm_assistance, asistencia_window, aware_start_dt,
    tz_of_task, now_in_tz, calc_lateness, local_day
)

from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy import func, and_




class JSONText(TypeDecorator):
    """Almacena dict/list como TEXT en SQLite, y devuelve dict/list al leer."""
    impl = Text
    cache_ok = True
    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False)
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    def process_result_value(self, value, dialect):
        if value is None:
            return None
        try:
            return json.loads(value)
        except Exception:
            return value

def _parse_date(arg: str):
    s = (arg or "").strip()
    if not s:
        return datetime.utcnow().date()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            pass
    return datetime.utcnow().date()

def _like(term: str):
    return f"%{(term or '').strip().lower()}%"


# ==============================================================
# 🔧 FUNCIÓN DEFINITIVA _build_informes_data (ajustada al nuevo modelo)
# ==============================================================
import math
from datetime import datetime, date

def _build_informes_data(
    q: str,
    the_day,                 # date | (start_date, end_date) | None
    view: str,
    estado: str = "activa",  # 'activa'|'inactiva'|'todas'
    tipo: str   = "todas",   # 'todas'|'comun'|'turnos'|'emergencia'
    page: int = 1,
    per_page: int = 10,
):
    """
    Genera los datos para la vista de informes (tareas o empleados),
    usando las tablas actuales:
      - Tarea, Asignacion, Usuario, Reporte, Turno, ReportVentana
    Compatible con SQLite y SQLAlchemy 2.x/3.x.
    """
    TR, ASG, USR, RV, RP, TU = Tarea, Asignacion, Usuario, ReportVentana, Reporte, Turno

    # -----------------------------------------------------------------
    # 🔹 Subqueries auxiliares
    esperados_tarea_sq = (
        db.session.query(RV.tarea_id.label("tid"), func.count(RV.id).label("esperados"))
        .group_by(RV.tarea_id)
        .subquery()
    )
    has_turnos_sq = (
        db.session.query(TU.tarea_id.label("tid"), func.count(TU.id).label("cnt"))
        .group_by(TU.tarea_id)
        .subquery()
    )

    # -----------------------------------------------------------------
    # 🔹 Base de asignaciones (activas)
    assign_q = (
        db.session.query(
            ASG.id.label("asg_id"),
            TR.id.label("tarea_id"),
            TR.nombre.label("tarea_nombre"),
            TR.ubicacion.label("tarea_ubicacion"),
            TR.prioridad.label("tarea_prioridad"),
            TR.fecha_inicio.label("fecha_inicio"),
            TR.fecha_fin.label("fecha_fin"),
            TR.hora_inicio.label("hora_inicio"),
            TR.activa.label("tarea_activa"),
            ASG.estado.label("asg_estado"),
            USR.id.label("emp_id"),
            USR.nombre.label("emp_nombre"),
            func.coalesce(esperados_tarea_sq.c.esperados, TR.cantidad_reportes, 0).label("esperados"),
            func.coalesce(has_turnos_sq.c.cnt, 0).label("turnos_cnt"),
        )
        .select_from(ASG)
        .join(TR, TR.id == ASG.tarea_id)
        .join(USR, USR.id == ASG.usuario_id)
        .outerjoin(esperados_tarea_sq, esperados_tarea_sq.c.tid == TR.id)
        .outerjoin(has_turnos_sq, has_turnos_sq.c.tid == TR.id)
        .filter(USR.activo == True)
        .filter(ASG.estado == "activa")  # ✅ sólo asignaciones activas
    )

    # -----------------------------------------------------------------
    # 🔹 Filtros por estado y tipo de tarea
    e = (estado or "activa").lower()
    if e in ("activa", "inactiva"):
        assign_q = assign_q.filter(TR.activa == (e == "activa"))

    t = (tipo or "todas").lower()
    if t == "comun":
        assign_q = assign_q.filter(func.lower(func.coalesce(TR.prioridad, "comun")) == "comun")
    elif t == "emergencia":
        assign_q = assign_q.filter(func.lower(TR.prioridad) == "emergencia")
    elif t == "turnos":
        assign_q = assign_q.filter(func.coalesce(has_turnos_sq.c.cnt, 0) > 0)

    # -----------------------------------------------------------------
    # 🔹 Búsqueda texto libre
    if q:
        term = f"%{q.lower()}%"
        assign_q = assign_q.filter(or_(
            func.lower(TR.nombre).like(term),
            func.lower(USR.nombre).like(term),
            func.lower(TR.ubicacion).like(term),
        ))

    # -----------------------------------------------------------------
    # 🔹 Ventana de fechas para contar reportes completados
    start_dt = end_dt = None
    if isinstance(the_day, tuple) and any(the_day):
        start_d, end_d = the_day
        if start_d and end_d:
            start_dt = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0)
            end_dt   = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59)
        elif start_d:
            start_dt = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0)
            end_dt   = datetime.utcnow()
        elif end_d:
            start_dt = datetime.min
            end_dt   = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59)
    elif isinstance(the_day, date):
        start_dt = datetime(the_day.year, the_day.month, the_day.day, 0, 0, 0)
        end_dt   = datetime(the_day.year, the_day.month, the_day.day, 23, 59, 59)

    done_sq = (
        db.session.query(
            RP.usuario_id.label("u"),
            RP.tarea_id.label("t"),
            func.count(RP.id).label("done")
        )
        .filter(
            *([RP.timestamp >= start_dt] if start_dt else []),
            *([RP.timestamp <= end_dt] if end_dt else [])
        )
        .group_by(RP.usuario_id, RP.tarea_id)  # ✅ columnas reales
        .subquery()
    )

    # -----------------------------------------------------------------
    # 🔹 Unimos la base con los conteos de reportes
    bs = assign_q.subquery("b")
    q_with_done = (
        db.session.query(
            bs.c.asg_id,
            bs.c.tarea_id, bs.c.tarea_nombre, bs.c.tarea_ubicacion,
            bs.c.tarea_prioridad, bs.c.fecha_inicio, bs.c.fecha_fin,
            bs.c.hora_inicio, bs.c.tarea_activa, bs.c.asg_estado,
            bs.c.emp_id, bs.c.emp_nombre, bs.c.esperados,
            func.coalesce(done_sq.c.done, 0).label("completados"),
        )
        .select_from(bs)
        .outerjoin(done_sq, and_(done_sq.c.u == bs.c.emp_id, done_sq.c.t == bs.c.tarea_id))
    )

    # -----------------------------------------------------------------
    # 🔹 Helper de paginación
    def _paginate(lst, page, per):
        total = len(lst)
        pages = max(1, math.ceil(total / max(1, per)))
        page  = max(1, min(page, pages))
        start = (page - 1) * per
        return lst[start:start+per], dict(page=page, per_page=per, total=total, pages=pages)

    # -----------------------------------------------------------------
    # 🔹 Vista EMPLEADOS
    if view == "empleados":
        rows = q_with_done.order_by(
            func.lower(bs.c.emp_nombre),
            bs.c.tarea_id.desc(),
            bs.c.fecha_inicio.desc()
        ).all()

        DATE_MIN = date(1970, 1, 1)  # ✅ comparaciones seguras date vs date
        emp_idx = {}

        for r in rows:
            tarea_estado = "activa" if bool(r.tarea_activa) else "inactiva"
            eexp = int(r.esperados or 0)
            edone = int(r.completados or 0)

            if r.emp_id not in emp_idx:
                emp_idx[r.emp_id] = dict(
                    emp_id=r.emp_id, emp_nombre=r.emp_nombre,
                    tarea_id=r.tarea_id, tarea_nombre=r.tarea_nombre,
                    prioridad=r.tarea_prioridad or "comun",
                    fecha_inicio=r.fecha_inicio, hora_inicio=r.hora_inicio,
                    esperados=eexp, completados=edone,
                    tarea_estado=tarea_estado,
                    tareas=[{
                        "tarea_id": r.tarea_id,
                        "tarea_nombre": r.tarea_nombre,
                        "estado": tarea_estado,
                        "fecha_ini": r.fecha_inicio.strftime("%Y-%m-%d") if r.fecha_inicio else None,
                        "fecha_fin": r.fecha_fin.strftime("%Y-%m-%d") if r.fecha_fin else None
                    }]
                )
            else:
                emp = emp_idx[r.emp_id]
                if (r.fecha_inicio or DATE_MIN) > (emp["fecha_inicio"] or DATE_MIN):
                    emp.update(dict(
                        tarea_id=r.tarea_id, tarea_nombre=r.tarea_nombre,
                        prioridad=r.tarea_prioridad or "comun",
                        fecha_inicio=r.fecha_inicio, hora_inicio=r.hora_inicio,
                        tarea_estado=tarea_estado
                    ))
                emp["esperados"] += eexp
                emp["completados"] += edone
                emp["tareas"].append({
                    "tarea_id": r.tarea_id, "tarea_nombre": r.tarea_nombre,
                    "estado": tarea_estado,
                    "fecha_ini": r.fecha_inicio.strftime("%Y-%m-%d") if r.fecha_inicio else None,
                    "fecha_fin": r.fecha_fin.strftime("%Y-%m-%d") if r.fecha_fin else None
                })

        empleados = list(emp_idx.values())
        empleados.sort(key=lambda x: x["emp_nombre"].lower())
        page_items, pager = _paginate(empleados, page, per_page)
        return [], page_items, pager

    # -----------------------------------------------------------------
    # 🔹 Vista TAREAS
    rows = q_with_done.order_by(bs.c.tarea_id, func.lower(bs.c.emp_nombre)).all()
    tareas_idx = {}

    for r in rows:
        if r.tarea_id not in tareas_idx:
            tareas_idx[r.tarea_id] = dict(
                tarea_id=r.tarea_id,
                nombre=r.tarea_nombre,
                ubicacion=r.tarea_ubicacion,
                prioridad=(r.tarea_prioridad or "comun"),
                fecha_inicio=r.fecha_inicio,
                fecha_fin=r.fecha_fin,
                hora_inicio=r.hora_inicio,
                estado="activa" if bool(r.tarea_activa) else "inactiva",
                esperados=0, completados=0,
                empleados_cnt=0, empleados=[], turnos=[]
            )
        pack = tareas_idx[r.tarea_id]
        pack["empleados_cnt"] += 1
        eexp = int(r.esperados or 0)
        edone = int(r.completados or 0)
        pack["esperados"] += eexp
        pack["completados"] += edone
        pack["empleados"].append(dict(
            id=r.emp_id,
            nombre=r.emp_nombre,
            esperados=eexp,
            completados=edone,
            asg_estado=r.asg_estado or "activa"
        ))

    if not tareas_idx:
        return [], [], dict(page=1, per_page=per_page, total=0, pages=1)

    # -----------------------------------------------------------------
    # 🔹 Turnos y conteos por turno_num
    tids = list(tareas_idx.keys())
    turnos = (
        db.session.query(TU.tarea_id, TU.numero, TU.hora_inicio, TU.hora_fin)
        .filter(TU.tarea_id.in_(tids))
        .order_by(TU.tarea_id, TU.numero)
        .all()
    )
    turn_counts = (
        db.session.query(ASG.tarea_id, ASG.turno_num, func.count(ASG.id))
        .filter(ASG.estado == "activa", ASG.tarea_id.in_(tids))
        .group_by(ASG.tarea_id, ASG.turno_num)
        .all()
    )
    counts_map = {(tid, (tn or 0)): c for tid, tn, c in turn_counts}

    for tid, num, h1, h2 in turnos:
        tareas_idx[tid]["turnos"].append(dict(
            numero=num,
            hora_inicio=h1,
            hora_fin=h2,
            empleados=counts_map.get((tid, num), 0)
        ))

    tareas = list(tareas_idx.values())
    tareas.sort(key=lambda x: (x["nombre"] or "").lower())
    page_items, pager = _paginate(tareas, page, per_page)

    return page_items, [], pager




# ===========================================

from config import Config

# === CSRF (Flask-WTF) ===
from flask_wtf import CSRFProtect  # type: ignore
from flask_wtf.csrf import CSRFError, generate_csrf  # type: ignore
from jinja2 import TemplateNotFound

csrf = CSRFProtect()

app = Flask(__name__)
app.config.from_object(Config)

# CSRF una sola vez
csrf.init_app(app)
app.jinja_env.globals["csrf_token"] = generate_csrf

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = "index"

# Subidas
from werkzeug.utils import secure_filename
ALLOWED_EXT = {"jpg", "jpeg", "png", "webp"}
MAX_UPLOAD_MB = 8

# ---------------------- MODELOS ----------------------
# models.py
class AppConfig(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    brand_name   = db.Column(db.String(120))
    brand_logo   = db.Column(db.String(255))  # puede ser "logos/x.png" o "https://..."
    brand_footer = db.Column(db.String(255))

    wa_sender = db.Column(db.String(50))
    wa_prefix = db.Column(db.String(120))
    mail_from = db.Column(db.String(120))

    otp_enabled = db.Column(db.Boolean, default=False)
    otp_expiry_min = db.Column(db.Integer, default=5)
    otp_resend_sec = db.Column(db.Integer, default=30)
    otp_max_attempts = db.Column(db.Integer, default=5)

    max_upload_mb = db.Column(db.Integer, default=8)
    max_colilla_mb = db.Column(db.Integer, default=8)
    dir_colillas = db.Column(db.String(255))
    dir_pagos = db.Column(db.String(255))
    table_page_size = db.Column(db.Integer, default=20)
    theme_default = db.Column(db.String(16), default="auto")

    @classmethod
    def get_singleton(cls):
        inst = cls.query.first()
        if not inst:
            inst = cls(brand_name="Fresh Labors")
            db.session.add(inst); db.session.commit()
        return inst


class Usuario(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    telefono = db.Column(db.String(30), unique=True, nullable=True)  # acepta cualquier país
    email = db.Column(db.String(150), unique=True, nullable=True)
    zelle_nombre = db.Column(db.String(120))
    zelle_cuenta = db.Column(db.String(120))
    pago_ciclo   = db.Column(db.String(40), default="semana_en_canje")
    password = db.Column(db.String(200), nullable=True)  # admins/supervisores/superadmin
    rol = db.Column(db.String(30), default="empleado")   # empleado, supervisor, admin, superadmin
    activo = db.Column(db.Boolean, default=True)
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)
    bloqueado = db.Column(db.Boolean, default=False)
    baneado   = db.Column(db.Boolean, default=False)
    motivo_bloqueo = db.Column(db.Text, nullable=True)
    motivo_baneo   = db.Column(db.Text, nullable=True)
    nombre_legal_firma = db.Column(db.String(150))
    onboarding_completo = db.Column(db.Boolean, default=False)
    # 🆕 Campo nuevo
    last_login = db.Column(db.DateTime, nullable=True)

class Tarea(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nombre = db.Column(db.String(150), nullable=False)
    descripcion = db.Column(db.Text, nullable=True)
    ubicacion = db.Column(db.String(200), nullable=True)
    prioridad = db.Column(db.String(30), default="comun")
    activa = db.Column(db.Boolean, default=True)

    tz_name = db.Column(db.String(64), default="America/New_York")  # default USA
    tolerancia_min = db.Column(db.Integer, default=15)

    # IMPORTANTE
    fecha_inicio = db.Column(db.Date, nullable=True)
    fecha_fin    = db.Column(db.Date)  
    cantidad_reportes = db.Column(db.Integer, default=3)
    hora_inicio = db.Column(db.String(5), default="08:00")
        # <- NUEVO: debe estar también en el modelo

    exigencias_text = db.Column(db.Text, nullable=True)     
    requiere_asistencia = db.Column(db.Boolean, default=True)  # deprecated
    requiere_lunch = db.Column(db.Boolean, default=True)       # deprecated


    creado_en = db.Column(db.DateTime, default=datetime.utcnow)

    turnos = db.relationship(
        "Turno", backref="tarea",
        cascade="all, delete-orphan",
        order_by="Turno.numero"
    )

class ReportVentana(db.Model):
    __tablename__ = "report_ventana"
    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"), nullable=False)
    orden = db.Column(db.Integer, default=1)
    nombre = db.Column(db.String(40), nullable=False)
    con_horario = db.Column(db.Boolean, default=True)
    hora_ini = db.Column(db.String(5), nullable=True)
    hora_fin = db.Column(db.String(5), nullable=True)

    tarea = db.relationship(
        "Tarea",
        backref=db.backref("ventanas", cascade="all, delete-orphan", order_by="ReportVentana.orden"),
    )
    


class Turno(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"), nullable=False)
    numero = db.Column(db.Integer, nullable=False)        # 1..N
    hora_inicio = db.Column(db.String(5), nullable=False) # 'HH:MM'
    hora_fin = db.Column(db.String(5), nullable=False)    # 'HH:MM'

class Asignacion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"))

    tarifa_hora = db.Column(db.Float, default=0.0)
    estado = db.Column(db.String(20), default="activa")

    hora_inicio_override = db.Column(db.String(5), nullable=True)
    tolerancia_override = db.Column(db.Integer, nullable=True)

    turno_num = db.Column(db.Integer, nullable=True)
    doble_turno = db.Column(db.Boolean, default=False)
    codigo_asignacion = db.Column(
        db.String(24),
        unique=True,
        index=True,
        default=lambda: secrets.token_hex(6)
    )

    # 🟢 NUEVOS CAMPOS
    confirmado = db.Column(db.Boolean, default=False, nullable=True)
    confirmado_at = db.Column(db.DateTime(timezone=True), nullable=True)

    usuario = db.relationship("Usuario", backref="asignaciones")
    tarea = db.relationship("Tarea", backref="asignaciones")
    
class Reporte(db.Model):
    __tablename__ = "reporte"

    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"))
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))

    # tipos: asistencia, lunch, salida, otro, asistencia_ligera(...)
    tipo = db.Column(db.String(30), nullable=False)

    # columnas “visibles” para que te sea fácil reportar
    foto_path = db.Column(db.String(300), nullable=True)   # path relativo a /static si existe
    gps       = db.Column(db.String(100), nullable=True)   # "lat,lng" si hubo, si no = NULL
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    valido    = db.Column(db.Boolean, default=True, nullable=False)
    tipo_especial = db.Column(db.String(20), nullable=True)  # ej: "confirmacion"
    nota      = db.Column(db.Text, nullable=True)            # texto libre si lo necesitas

    # llaves auxiliares
    turno_num = db.Column(db.SmallInteger, nullable=True)
    report_ventana_id = db.Column(db.Integer, db.ForeignKey("report_ventana.id"), nullable=True)

    # 🔹 Nuevo: payload JSON (mutable para poder hacer .update y que detecte cambios)
    payload = db.Column(MutableDict.as_mutable(db.JSON), default=dict)

    tarea   = db.relationship("Tarea")
    usuario = db.relationship("Usuario")


class OTP(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    telefono = db.Column(db.String(30))
    codigo = db.Column(db.String(10))
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)
    expiracion = db.Column(db.DateTime)

class Sancion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"))
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"), nullable=True)
    tipo = db.Column(db.String(30))  # 'asistencia', 'lunch', 'boicot', 'otro'
    mensaje = db.Column(db.Text)
    creada_en = db.Column(db.DateTime, default=datetime.utcnow)
    usuario = db.relationship("Usuario")
    tarea = db.relationship("Tarea")
    nivel = db.Column(db.String(12), default="warn")
    resuelta = db.Column(db.Boolean, default=False, index=True)



class PagoSemana(db.Model):
    __tablename__ = "pago_semana"
    id = db.Column(db.Integer, primary_key=True)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=False)
    semana_fin = db.Column(db.Date, nullable=False)   # jueves de la semana
    horas = db.Column(db.Float, default=0.0)
    subtotal = db.Column(db.Float, default=0.0)
    recibo_path = db.Column(db.String(255))
    verificado_empleado = db.Column(db.Boolean, default=False)
    creado_en = db.Column(db.DateTime, default=datetime.utcnow)

    usuario = db.relationship("Usuario", backref="pagos_semana")

# ==== NUEVO: Historial de eventos ====
class HistEvento(db.Model):
    __tablename__ = "hist_evento"
    id          = db.Column(db.Integer, primary_key=True)
    tarea_id    = db.Column(db.Integer, db.ForeignKey("tarea.id"), nullable=True)
    empleado_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)
    usuario_id  = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=True)  # quién hizo la acción
    tipo        = db.Column(db.String(40), nullable=False)  # 'tarea_edit','tarea_estado','empleado_add','empleado_remove','empleado_sancion','asign_override','asign_update'
    detalle     = db.Column(JSONText, nullable=True)
    creado_en   = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    
    
class Jornada(db.Model):
    __tablename__ = "jornada"

    id = db.Column(db.Integer, primary_key=True)
    tarea_id = db.Column(db.Integer, db.ForeignKey("tarea.id"), nullable=False)
    usuario_id = db.Column(db.Integer, db.ForeignKey("usuario.id"), nullable=False)
    fecha = db.Column(db.Date, nullable=False)
    turno_num = db.Column(db.SmallInteger, default=1)
    hora_inicio = db.Column(db.DateTime)
    hora_salida = db.Column(db.DateTime)
    duracion_horas = db.Column(db.Float)
    fuente = db.Column(db.String(20), default="reporte")  # 'reporte', 'manual', 'auto'
    nota = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("tarea_id", "usuario_id", "fecha", "turno_num", name="uq_jornada"),
    )

    def calcular_duracion(self):
        """Actualiza duracion_horas si ambas horas están definidas."""
        if self.hora_inicio and self.hora_salida:
            delta = self.hora_salida - self.hora_inicio
            self.duracion_horas = round(delta.total_seconds() / 3600, 2)

ALLOWED_LOGO_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "svg"}
LOGOS_DIR = os.path.join(app.root_path, "static", "logos")
os.makedirs(LOGOS_DIR, exist_ok=True)

app.config["DB"] = db
app.config.setdefault("MODELS", {})
app.config["MODELS"].update({
    "Usuario": Usuario,
    "Reporte": Reporte, 
    "Sancion": Sancion, 
    "Turno": Turno, 
    "ReportVentana": ReportVentana,
    "Tarea": Tarea,
    "Asignacion": Asignacion,
    "Sancion": Sancion,
    "PagoSemana": PagoSemana,
    "HistEvento": HistEvento,
    # 🆕 NUEVO:
    "Jornada": Jornada,
})

from sqlalchemy import text
def ensure_schema():
    try:
        cols = [c[1] for c in db.session.execute(text("PRAGMA table_info(reporte)")).fetchall()]
        if "turno_num" not in cols:
            db.session.execute(text("ALTER TABLE reporte ADD COLUMN turno_num INTEGER"))
            db.session.commit()
    except Exception as e:
        print("ensure_schema() warning:", e)

with app.app_context():
    db.create_all()
    ensure_schema()

from sqlalchemy import event
from services.asign_sync import sync_asignaciones_entrada, sync_asignacion_entrada

_sync_task_ids = set()
_sync_asign_ids = set()

# Cambios en ventanas -> recalc por tarea
@event.listens_for(ReportVentana, "after_insert")
@event.listens_for(ReportVentana, "after_update")
@event.listens_for(ReportVentana, "after_delete")
def _rv_changed(mapper, connection, target):
    if getattr(target, "tarea_id", None):
        _sync_task_ids.add(int(target.tarea_id))

# Cambios en turnos -> recalc por tarea
@event.listens_for(Turno, "after_insert")
@event.listens_for(Turno, "after_update")
@event.listens_for(Turno, "after_delete")
def _turno_changed(mapper, connection, target):
    if getattr(target, "tarea_id", None):
        _sync_task_ids.add(int(target.tarea_id))

# Cambio de turno en una asignación -> recalc solo esa asignación
@event.listens_for(Asignacion.turno_num, "set")
def _asign_turno_changed(target, value, oldvalue, initiator):
    if value != oldvalue and getattr(target, "id", None):
        _sync_asign_ids.add(int(target.id))

# Cambio de hora_inicio de la tarea -> recalc por tarea
@event.listens_for(Tarea.hora_inicio, "set")
def _tarea_horainicio_changed(target, value, oldvalue, initiator):
    if value != oldvalue and getattr(target, "id", None):
        _sync_task_ids.add(int(target.id))

# Al COMMIT, ejecutar sincronizaciones encoladas (fuera de la tx)
@event.listens_for(db.session.__class__, "after_commit")
def _run_sync_after_commit(session):
    if not (_sync_task_ids or _sync_asign_ids):
        return
    task_ids  = set(_sync_task_ids);  _sync_task_ids.clear()
    asign_ids = set(_sync_asign_ids); _sync_asign_ids.clear()
    from flask import current_app
    with current_app.app_context():
        for tid in task_ids:
            try:
                sync_asignaciones_entrada(db, Tarea, ReportVentana, Asignacion, Turno, tarea_id=tid, force=False)
            except Exception:
                current_app.logger.exception("sync_asignaciones_entrada failed (tid=%s)", tid)
        for aid in asign_ids:
            try:
                sync_asignacion_entrada(db, Tarea, ReportVentana, Asignacion, Turno, asign_id=aid, force=False)
            except Exception:
                current_app.logger.exception("sync_asignacion_entrada failed (aid=%s)", aid)

# ---------------------- HELPERS ----------------------
def _hhmm_to_dt(base_dt, hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return base_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)

def counts_admin_warnings():
    total = db.session.query(func.count(Sancion.id)).filter(Sancion.resuelta == False).scalar() or 0
    rows = (db.session.query(Sancion.nivel, func.count(Sancion.id))
            .filter(Sancion.resuelta == False)
            .group_by(Sancion.nivel).all())
    by_level = {(lvl or "warn"): cnt for (lvl, cnt) in rows}
    return total, by_level

@app.errorhandler(404)
def not_found(e): return render_template("errors/404.html"), 404

@app.errorhandler(500)
def server_error(e): return render_template("errors/500.html"), 500

@app.context_processor
def inject_globals():
    total, by_level = counts_admin_warnings()
    return {"total_admin_warnings": total, "admin_warnings_by_level": by_level}

def _dentro_ventana(tarea, ahora):
    tol = timedelta(minutes=tarea.tolerancia_min or 0)
    if tarea.hora_inicio and tarea.hora_salida:
        h1 = datetime.combine(ahora.date(), datetime.strptime(tarea.hora_inicio,"%H:%M").time()) - tol
        h2 = datetime.combine(ahora.date(), datetime.strptime(tarea.hora_salida,"%H:%M").time()) + tol
        return h1 <= ahora <= h2
    return True

def _necesita_confirmacion(usuario_id, tarea_id, fecha):
    return not db.session.query(
        db.session.query(Reporte.id)
        .filter(Reporte.usuario_id==usuario_id,
                Reporte.tarea_id==tarea_id,
                Reporte.tipo_especial=="confirmacion",
                func.date(Reporte.timestamp)==fecha.date()).exists()
    ).scalar()


def _week_window_vi_to_th(today: date|None=None):
    today = today or datetime.utcnow().date()
    wd = today.weekday()  # 0=Mon ... 4=Fri
    offset_since_friday = (wd - 4) % 7
    friday = today - timedelta(days=offset_since_friday)
    thursday = friday + timedelta(days=6)
    return friday, thursday

def _minutes_between(asis: datetime|None, salida: datetime|None, start_oficial: datetime|None=None):
    if not asis or not salida: return 0
    start_real = max(asis, start_oficial) if start_oficial else asis
    return max(0, int((salida - start_real).total_seconds() // 60))

@app.context_processor
def inject_no_confirmados_count():
    hoy = datetime.utcnow().date()
    start_dt = datetime(hoy.year, hoy.month, hoy.day, 0, 0, 0)
    end_dt   = datetime(hoy.year, hoy.month, hoy.day, 23, 59, 59)

    confirm_subq = (
        db.session.query(Reporte.usuario_id, Reporte.tarea_id)
        .filter(
            Reporte.tipo_especial == "confirmacion",
            Reporte.timestamp >= start_dt,
            Reporte.timestamp <= end_dt
        )
        .group_by(Reporte.usuario_id, Reporte.tarea_id)
        .subquery()
    )

    no_confirmados = (
        db.session.query(Asignacion.id)
        .join(Tarea, Asignacion.tarea_id == Tarea.id)
        .join(Usuario, Asignacion.usuario_id == Usuario.id)
        .outerjoin(confirm_subq, and_(
            confirm_subq.c.usuario_id == Asignacion.usuario_id,
            confirm_subq.c.tarea_id   == Asignacion.tarea_id
        ))
        .filter(
            Asignacion.estado == "activa",
            Tarea.activa == True,
            Usuario.activo == True,
            confirm_subq.c.usuario_id.is_(None)
        )
        .count()
    )

    return dict(no_confirmados_count=no_confirmados)




def ahora_local(tarea: Tarea) -> datetime:
    return datetime.now(ZoneInfo(tarea.tz_name or "UTC"))

def dentro_ventana_reporte(tarea: Tarea, ventana: "ReportVentana", ahora_loc: datetime) -> bool:
    if not ventana or not ventana.con_horario: return True
    if not (ventana.hora_ini and ventana.hora_fin): return True
    tol = timedelta(minutes=tarea.tolerancia_min or 0)
    h1, m1 = map(int, ventana.hora_ini.split(":"))
    h2, m2 = map(int, ventana.hora_fin.split(":"))
    ini_loc = ahora_loc.replace(hour=h1, minute=m1, second=0, microsecond=0) - tol
    fin_loc = ahora_loc.replace(hour=h2, minute=m2, second=0, microsecond=0) + tol
    return ini_loc <= ahora_loc <= fin_loc


def horas_totales_semana():
    hoy = datetime.utcnow().date()
    inicio = hoy - timedelta(days=6)
    q = (Reporte.query
         .filter(Reporte.timestamp >= datetime.combine(inicio, time.min))
         .order_by(Reporte.usuario_id, Reporte.tarea_id, Reporte.timestamp.asc()))
    grupos = defaultdict(list)
    for r in q:
        clave = (r.usuario_id, r.tarea_id, r.timestamp.date())
        grupos[clave].append(r)
    horas = 0.0
    for (uid, tid, fecha), arr in grupos.items():
        arr.sort(key=lambda x: x.timestamp)
        asis = next((x for x in arr if x.tipo=="asistencia"), None)
        sal  = next((x for x in arr if x.tipo=="salida"), None)
        if not asis or not sal:
            continue
        t = Tarea.query.get(tid)
        base_date = datetime.combine(fecha, time.min)
        h_inicio = datetime.strptime((t.hora_inicio or "08:00"), "%H:%M").time()
        start_oficial = base_date.replace(hour=h_inicio.hour, minute=h_inicio.minute)
        start_real = max(asis.timestamp, start_oficial)
        horas += max(0.0, (sal.timestamp - start_real).total_seconds() / 3600.0)
    return round(horas, 1)

# subir fotos a /uploads
UPLOAD_DIR = Path(__file__).resolve().parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

def guardar_foto(file_storage):
    if not file_storage or (getattr(file_storage, "filename", "") or "").strip() == "":
        return None
    filename = file_storage.filename
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    if ext not in ALLOWED_EXT:
        return None
    file_storage.stream.seek(0, 2)  # EOF
    size_bytes = file_storage.stream.tell()
    file_storage.stream.seek(0)
    if size_bytes > MAX_UPLOAD_MB * 1024 * 1024:
        return None
    safe = secure_filename(filename)
    base, dot, extension = safe.partition(".")
    if not extension:
        extension = ext
    fname = datetime.utcnow().strftime("%Y%m%d%H%M%S") + "_" + secrets.token_hex(4) + "." + extension
    dest = UPLOAD_DIR / fname
    i = 1
    while dest.exists():
        fname = datetime.utcnow().strftime("%Y%m%d%H%M%S") + f"_{secrets.token_hex(4)}_{i}." + extension
        dest = UPLOAD_DIR / fname
        i += 1
    file_storage.save(dest)
    return f"uploads/{fname}"

#temas de horas0
from datetime import datetime, time, timedelta, timezone

def _as_time(val) -> time | None:
    """Convierte val a time si viene como 'HH:MM' o 'HH:MM:SS' (str) o ya es time/datetime."""
    if val is None:
        return None
    if isinstance(val, time):
        return val
    if isinstance(val, datetime):
        return val.time()
    if isinstance(val, str):
        s = val.strip()
        try:
            if len(s) == 5:   # HH:MM
                return datetime.strptime(s, "%H:%M").time()
            if len(s) == 8:   # HH:MM:SS
                return datetime.strptime(s, "%H:%M:%S").time()
        except Exception:
            return None
    return None

def _pick_attr(obj, names: list[str]):
    """Devuelve el primer atributo existente y no-None de 'obj' según una lista de nombres candidatos."""
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return None

def _rv_times(rv) -> tuple[time | None, time | None, str]:
    """
    Intenta extraer (hora_ini, hora_fin, tipo_nombre) de un ReportVentana flexible.
    Soporta campos: hora_ini/hora_fin, hora_inicio/hora_fin, ini/fin, desde/hasta, start/end, etc.
    """
    ini_raw = _pick_attr(rv, ["hora_ini", "hora_inicio", "ini", "desde", "start", "ini_hora"])
    fin_raw = _pick_attr(rv, ["hora_fin", "fin", "hasta", "end", "fin_hora"])
    tipo    = _pick_attr(rv, ["tipo", "nombre", "name", "label"]) or "informe"

    ini_t = _as_time(ini_raw)
    fin_t = _as_time(fin_raw)
    return ini_t, fin_t, str(tipo)

# ==== NUEVO: helper para registrar eventos ====
# ==== NUEVO: helper para registrar eventos ====
def log_event(tipo, tarea_id=None, empleado_id=None, detalle=None, usuario_id=None):
    """
    Normaliza el payload para que siempre tengamos algún UID de empleado dentro de 'detalle',
    además de persistir empleado_id en la columna. Esto evita nombres 'None' en el historial.
    """
    # Actor que ejecuta
    try:
        uid_actor = usuario_id or (current_user.id if current_user.is_authenticated else None)
    except Exception:
        uid_actor = usuario_id

    # Mergueamos detalle y, si tenemos empleado_id y NO vino en detalle, lo agregamos como 'uid'
    det = dict(detalle or {})
    if empleado_id and ("uid" not in det and "empleado_id" not in det):
        try:
            det["uid"] = int(empleado_id)
        except Exception:
            det["uid"] = empleado_id  # fallback

    ev = HistEvento(
        tipo=tipo,
        tarea_id=tarea_id,
        empleado_id=empleado_id,
        usuario_id=uid_actor,
        detalle=det
    )
    db.session.add(ev)
    db.session.commit()

# ---------------------- LOGIN ----------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(Usuario, int(user_id))

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("index"))
            if current_user.rol not in roles:
                flash("No tienes permisos.", "danger")
                return redirect(url_for("index"))
            return f(*args, **kwargs)
        return decorated
    return decorator



# === API OTP: ENVIAR ===
@app.post("/api/otp/send")
def api_otp_send():
    data = request.get_json(silent=True) or {}
    telefono = (data.get("telefono") or "").strip()

    empleado = Usuario.query.filter_by(telefono=telefono, rol="empleado", activo=True).first()
    if not empleado:
        return jsonify(ok=False, error="No existe un empleado activo con ese número."), 404

    asignado_activo = (db.session.query(Asignacion)
        .join(Tarea, Asignacion.tarea_id==Tarea.id)
        .filter(Asignacion.usuario_id==empleado.id, Tarea.activa==True).first())
    if not asignado_activo:
        return jsonify(ok=False, error="Solo pueden ingresar empleados asignados a tareas activas."), 403

    ok, msg = _can_send_otp()
    if not ok:
        return jsonify(ok=False, error=msg), 429

    codigo = "%06d" % random.randint(0, 999999)
    otp = OTP(telefono=telefono, codigo=codigo, expiracion=datetime.utcnow()+timedelta(minutes=5))
    db.session.add(otp); db.session.commit()
    print("OTP para", telefono, "=>", codigo)  # en dev

    session["otp_tel"] = telefono
    _register_send_ok()
    return jsonify(ok=True)

# === API OTP: VERIFICAR ===
@app.post("/api/otp/verify")
def api_otp_verify():
    data = request.get_json(silent=True) or {}

    # 1) Tomamos teléfono del body o de la sesión (si lo dejaste guardado en el modal)
    telefono_in = (data.get("telefono") or session.get("otp_tel") or "").strip()
    # Normalización básica (quita espacios/guiones). Acepta dígitos y '+' inicial.
    telefono = telefono_in.replace(" ", "").replace("-", "")

    codigo = (data.get("codigo") or "").strip()

    # 2) ¿Está bloqueado por intentos?
    if _otp_locked():
        return jsonify(ok=False, error="Muchos intentos fallidos. Intenta más tarde."), 429

    # 3) OTP válido y vigente
    otp = (OTP.query.filter_by(telefono=telefono, codigo=codigo)
           .order_by(OTP.creado_en.desc()).first())
    if not otp or otp.expiracion <= datetime.utcnow():
        _register_bad_attempt()
        # No cerramos modal (esto lo maneja tu frontend). Enviamos error plano.
        return jsonify(ok=False, error="Código inválido o expirado."), 400

    # 4) Usuario debe existir, ser empleado y estar activo
    user = (Usuario.query
            .filter_by(telefono=telefono, rol="empleado", activo=True)
            .first())
    if not user:
        return jsonify(ok=False, error="No tienes cuenta activa como empleado."), 404

    # 5) Debe tener al menos una asignación a tarea activa
    asignado_activo = (db.session.query(Asignacion)
        .join(Tarea, Asignacion.tarea_id == Tarea.id)
        .filter(
            Asignacion.usuario_id == user.id,
            Tarea.activa == True
        )
        .first())
    if not asignado_activo:
        return jsonify(ok=False, error="Solo pueden ingresar empleados asignados a tareas activas."), 403

    # 6) Login y limpieza de contadores de OTP
    login_user(user)
    session.pop("otp_attempts", None)
    session.pop("otp_locked_until", None)
    # OJO: Si tu frontend reabre el modal con 'otp_tel', puedes limpiar aquí sin problema.
    # Si prefieres que el modal siga sabiendo el número (p.ej., para autocompletar), comenta la siguiente línea.
    session.pop("otp_tel", None)

    # 7) Primer ingreso: ¿faltan datos básicos?
    if _necesita_datos_basicos(user):
        # Puedes enviar texto extra si tu UI lo muestra; si no, basta con 'next'
        default_exig = "Por favor completa tus datos (correo y Zelle) para continuar."
        return jsonify(ok=True, next="onboarding", exigencias=default_exig), 200

    # 8) Todo listo → al panel de empleado
    return jsonify(ok=True, next="panel"), 200

# ---------------------- SEED ----------------------
def seed_if_empty():
    db.create_all()
    email = os.getenv("SUPERADMIN_EMAIL", "owner@freshslabors.local")
    nombre = os.getenv("SUPERADMIN_NAME", "Superadmin")
    default_pass = os.getenv("SUPERADMIN_PASSWORD", "administradorsuper123")
    sa = Usuario.query.filter_by(email=email).first()
    if not sa:
        hashed = bcrypt.generate_password_hash(default_pass).decode("utf-8")
        sa = Usuario(nombre=nombre, email=email, password=hashed, rol="superadmin", activo=True)
        db.session.add(sa)
        db.session.commit()
    if db.session.query(Tarea.id).limit(1).first() is None:
        t = Tarea(nombre="Operación Mañana", descripcion="Demo", ubicacion="Planta A")
        db.session.add(t)
        db.session.commit()

#-----SECURITY TO ADMIN SUPERVISOR
@app.before_request
def supervisor_readonly_guard():
    from flask import request, abort
    if not current_user.is_authenticated: 
        return
    if current_user.rol == "supervisor":
        # Bloquea mutaciones en /admin/* y en /supervisor/* (dejamos solo GET)
        if (request.path.startswith("/admin") or request.path.startswith("/supervisor")) and request.method != "GET":
            abort(403)


# ---------------------- OTP Rate Limit (sesión) ----------------------
OTP_RESEND_COOLDOWN = 30
OTP_LOCK_MINUTES = 15
OTP_MAX_ATTEMPTS = 5

def _otp_locked():
    lock_until = session.get("otp_locked_until")
    if not lock_until:
        return False
    try:
        lu = datetime.fromisoformat(lock_until)
        return datetime.utcnow() < lu
    except Exception:
        return False

def _can_send_otp():
    if _otp_locked():
        return False, "Has sido bloqueado temporalmente. Intenta más tarde."
    last = session.get("otp_last_sent")
    if last:
        try:
            dt = datetime.fromisoformat(last)
            if (datetime.utcnow() - dt).total_seconds() < OTP_RESEND_COOLDOWN:
                return False, "Debes esperar un minuto antes de reenviar el código."
        except Exception:
            pass
    return True, None

def _register_bad_attempt():
    att = int(session.get("otp_attempts", 0)) + 1
    if att >= OTP_MAX_ATTEMPTS:
        session["otp_attempts"] = 0
        session["otp_locked_until"] = (datetime.utcnow() + timedelta(minutes=OTP_LOCK_MINUTES)).isoformat()
    else:
        session["otp_attempts"] = att

def _register_send_ok():
    session["otp_last_sent"] = datetime.utcnow().isoformat()
    session["otp_attempts"] = 0
    session.pop("otp_locked_until", None)

# ---------------------- RUTAS PÚBLICAS ----------------------
from flask import g, abort, request, render_template, redirect, url_for, flash, session
from jinja2 import TemplateNotFound
from sqlalchemy import func

@app.route("/")
def index():
    if current_user.is_authenticated:
        if current_user.rol == "empleado":
            return redirect(url_for("empleado_panel"))
        if current_user.rol == "supervisor":
            return redirect(url_for("supervisor_dashboard"))   # 👈
        if current_user.rol in ("admin","superadmin"):
            return redirect(url_for("admin_dashboard"))
    open_once = bool(session.pop("otp_open_once", False))
    try:
        return render_template("index.html", otp_telefono=session.get("otp_tel"), otp_open=open_once)
    except TemplateNotFound:
        return render_template("auth/login_admin.html")

# --- Login Admin (user/pass)
from flask import g, abort, request, render_template, redirect, url_for, flash, session
from jinja2 import TemplateNotFound
from sqlalchemy import func

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import or_, func

@app.route("/login", methods=["GET","POST"])
def login_admin():
    if request.method == "POST":
        ident_raw = (request.form.get("username") or "").strip()
        password  = (request.form.get("password") or "")
        ident_norm = ident_raw.casefold()  # robusto contra mayúsculas/minúsculas

        # Busca por email o nombre (si tienes 'username', añade otra OR)
        user = (Usuario.query
                .filter(or_(
                    func.lower(Usuario.email)  == ident_norm,
                    func.lower(Usuario.nombre) == ident_norm
                    # func.lower(Usuario.username) == ident_norm  # <-- si existe el campo
                ))
                .first())

        if not user or not user.password or not bcrypt.check_password_hash(user.password, password):
            flash("Usuario o contraseña incorrectos.", "danger")
            session["open_admin_modal_once"] = True
            return redirect(url_for("index"))

        login_user(user)
        if user.rol == "supervisor":
            return redirect(url_for("supervisor_dashboard"))
        if user.rol in ("admin","superadmin"):
            return redirect(url_for("admin_dashboard"))
        if user.rol == "empleado":
            return redirect(url_for("empleado_panel"))
        return redirect(url_for("index"))

    # GET: abre modal admin en el index
    session["open_admin_modal_once"] = True
    return redirect(url_for("index"))


@app.before_request
def supervisor_readonly_guard():
    if not current_user.is_authenticated:
        return
    if current_user.rol == "supervisor":
        if (request.path.startswith("/admin") or request.path.startswith("/supervisor")) and request.method != "GET":
            return abort(403)
def _kpis_supervisor():
    hoy = func.date(func.current_timestamp())
    return {
        "tareas_activas": db.session.query(Tarea.id).filter(Tarea.activa == True).count(),
        "informes_hoy": db.session.query(Reporte.id).filter(func.date(Reporte.timestamp) == hoy).count(),
        "personal_activo": (
            db.session.query(Asignacion.usuario_id)
                .join(Tarea, Asignacion.tarea_id == Tarea.id)
                .filter(Tarea.activa == True, Asignacion.estado == "activa")
                .distinct().count()
        ),
        "horas_semanales": 0.0,
    }

@app.route("/supervisor")
@login_required
@role_required("supervisor")
def supervisor_index():
    return redirect(url_for("supervisor_dashboard"))


from sqlalchemy import func

@app.route("/supervisor/dashboard")
@login_required
@role_required("supervisor")

def supervisor_dashboard():
    # KPIs
    empleados_activos   = db.session.query(Usuario.id).filter(Usuario.rol == "empleado", Usuario.activo == True).count()
    empleados_inactivos = db.session.query(Usuario.id).filter(Usuario.rol == "empleado", Usuario.activo == False).count()
    tareas_activas      = db.session.query(Tarea.id).filter(Tarea.activa == True).count()

    # Para la grilla de tarjetas (tus tareas activas resumidas)
    tareas = (Tarea.query
              .with_entities(Tarea.id, Tarea.nombre, Tarea.ubicacion)
              .filter(Tarea.activa == True)
              .order_by(Tarea.id.desc())
              .all())

    return render_template(
        "supervisor/dashboard.html",
        tareas=tareas,
        kpis={
            "empleados_activos": empleados_activos,
            "empleados_inactivos": empleados_inactivos,
            "tareas_activas": tareas_activas,
        }
    )

@app.route("/supervisor/tareas")
@login_required
@role_required("supervisor")

def supervisor_tareas():
    estado = (request.args.get("estado") or "activa").lower()
    solo_activas = (estado != "inactiva")

    # Traemos objetos completos de Tarea (no with_entities)
    q = Tarea.query
    q = q.filter(Tarea.activa == True) if solo_activas else q.filter(Tarea.activa == False)
    tareas = q.order_by(Tarea.id.desc()).all()

    # Mapeo tarea_id -> lista de empleados (nombre, telefono) con asignación activa
    tarea_ids = [t.id for t in tareas]
    empleados_por_tarea = {tid: [] for tid in tarea_ids}
    if tarea_ids:
        rows = (db.session.query(Asignacion.tarea_id, Usuario.nombre, Usuario.telefono)
                .join(Usuario, Usuario.id == Asignacion.usuario_id)
                .filter(
                    Asignacion.tarea_id.in_(tarea_ids),
                    Asignacion.estado == "activa",
                    Usuario.activo == True,
                    Usuario.rol == "empleado"
                )
                .order_by(Usuario.nombre.asc())
                .all())
        for tid, nom, tel in rows:
            empleados_por_tarea[tid].append({"nombre": nom, "telefono": tel})

    return render_template(
        "supervisor/tareas.html",
        tareas=tareas,
        empleados_por_tarea=empleados_por_tarea,
        estado=("activa" if solo_activas else "inactiva")
    )


@app.route("/supervisor/empleados")
@login_required
@role_required("supervisor")

def supervisor_empleados():
    qterm = (request.args.get("q") or "").strip().lower()
    base = Usuario.query.filter(Usuario.rol == "empleado", Usuario.activo == True)

    if qterm:
        base = base.filter(
            func.lower(Usuario.nombre).contains(qterm) |
            func.lower(func.coalesce(Usuario.telefono, "")).contains(qterm)
        )

    empleados = (base.with_entities(Usuario.id, Usuario.nombre, Usuario.telefono)
                      .order_by(Usuario.nombre.asc())
                      .all())
    return render_template("supervisor/empleados.html", empleados=empleados, q=qterm)


# --- helpers (ponlos en utils.py o arriba de tus rutas) ---

def gen_auto_email() -> str:
    # dominio reservado (RFC 2606) y 128 bits aleatorios
    token = secrets.token_hex(16)
    ymd = datetime.utcnow().strftime('%Y%m%d')
    return f"emp-{ymd}-{token}@autogen.invalid"

def unique_auto_email(UserModel, exclude_id=None) -> str:
    for _ in range(5):
        e = gen_auto_email()
        q = UserModel.query.filter_by(email=e)
        if exclude_id:
            q = q.filter(UserModel.id != exclude_id)
        if not q.first():
            return e
    # fallback extremo (ya sería mala suerte)
    return f"emp-{uuid.uuid4().hex}@autogen.invalid"


# --- Login Empleado (OTP WhatsApp)
# === LOGIN EMPLEADO: solicitar código OTP (sin registro) ===
@app.route("/login-employee", methods=["GET", "POST"])
def login_employee():
    if request.method == "POST":
        telefono = (request.form.get("telefono") or "").strip()
        tel_norm = normalize_e164(telefono)
        # 1) Validación E.164: '+' y 8–15 dígitos
        if not (tel_norm.startswith("+") and tel_norm[1:].isdigit() and 8 <= len(tel_norm[1:]) <= 15):
            session["otp_open_once"] = True
            session["otp_tel"] = telefono
            
            flash("Formato inválido. Incluye '+' y solo dígitos (ej.: +1 7865551234).", "danger")
            return redirect(url_for("index"))

        # 2) Empleado activo
        empleado = Usuario.query.filter_by(telefono=tel_norm, rol="empleado", activo=True).first()
        if not empleado:
            session["otp_open_once"] = True
            session["otp_tel"] = tel_norm
            flash("No existe un empleado activo con ese número.", "danger")
            return redirect(url_for("index"))

        # 3) Debe tener asignación en tarea activa
        asignado_activo = (
            db.session.query(Asignacion)
            .join(Tarea, Asignacion.tarea_id == Tarea.id)
            .filter(Asignacion.usuario_id == empleado.id, Tarea.activa == True)
            .first()
        )
        if not asignado_activo:
            session["otp_open_once"] = True
            session["otp_tel"] = tel_norm
            flash("Solo pueden ingresar empleados asignados a tareas activas.", "warning")
            return redirect(url_for("index"))

        # 4) Rate limit
        ok, msg = _can_send_otp()
        if not ok:
            session["otp_open_once"] = True
            session["otp_tel"] = tel_norm
            flash(msg or "Espera antes de solicitar otro código.", "warning")
            return redirect(url_for("index"))

        # 5) Generar OTP (5 min)
        codigo = "%06d" % random.randint(0, 999999)
        otp = OTP(telefono=tel_norm, codigo=codigo, expiracion=datetime.utcnow() + timedelta(minutes=5))
        db.session.add(otp); db.session.commit()

        print("OTP para", tel_norm, "=>", codigo)  # modo dev

        # Éxito → abrir modal OTP
        flash("Te enviamos un código OTP por WhatsApp (modo dev: consola del servidor).", "info")
        session["otp_tel"] = tel_norm
        session["otp_show_otp"] = True   # abre modal OTP
        session.pop("otp_open_once", None)  # no abrir el de teléfono
        _register_send_ok()
        return redirect(url_for("index"))

    # GET: todo se hace vía modal en index
    return redirect(url_for("index"))


# === VERIFICAR OTP (misma página: index con modal) ===
@app.route("/verify-otp", methods=["GET","POST"])
def verify_otp():
    telefono = session.get("otp_tel")
    if not telefono:
        # sin teléfono en sesión, vuelve al modal de teléfono
        session["otp_open_once"] = True
        flash("Vuelve a ingresar tu teléfono.", "warning")
        return redirect(url_for("index"))

    if request.method == "POST":
        if _otp_locked():
            flash("Muchos intentos fallidos. Intenta más tarde.", "danger")
            session.pop("otp_tel", None)
            return redirect(url_for("index"))

        codigo = (request.form.get("codigo") or "").strip()
        otp = (OTP.query.filter_by(telefono=telefono, codigo=codigo)
               .order_by(OTP.creado_en.desc()).first())

        if otp and otp.expiracion > datetime.utcnow():
            user = Usuario.query.filter_by(telefono=telefono, rol="empleado", activo=True).first()
            if not user:
                # No hay registro ni creación de cuenta: solo bloquear
                flash("No existe un empleado activo con ese número.", "danger")
                session.pop("otp_tel", None)
                return redirect(url_for("index"))

            asignado_activo = (
                db.session.query(Asignacion)
                .join(Tarea, Asignacion.tarea_id==Tarea.id)
                .filter(Asignacion.usuario_id==user.id, Tarea.activa==True)
                .first()
            )
            if not asignado_activo:
                flash("Solo pueden ingresar empleados asignados a tareas activas.", "warning")
                session.pop("otp_tel", None)
                return redirect(url_for("index"))

            login_user(user)
            session.pop("otp_tel", None)
            session.pop("otp_attempts", None)
            session.pop("otp_locked_until", None)

            if _necesita_datos_basicos(user):
                return redirect(url_for("empleado_onboarding"))
            return redirect(url_for("empleado_panel"))

        # Código incorrecto → reabrir modal OTP (no el de teléfono)
        _register_bad_attempt()
        session["otp_show_otp"] = True
        flash("Código inválido o expirado. Te quedan intentos disponibles.", "danger")
        return redirect(url_for("index"))

    # GET: no renderizamos otra vista; volvemos al index
    session["otp_show_otp"] = True
    return redirect(url_for("index"))


@app.route("/empleado/onboarding", methods=["GET","POST"])
@login_required
@role_required("empleado")
def empleado_onboarding():
    # Si ya completó onboarding, no permitir re-editar (solo email en otra vista)
    if getattr(current_user, "onboarding_completo", False):
        flash(
            "Tu información ya fue confirmada. Si necesitas cambiar algo, contacta a Administración. "
            "(Puedes editar tu correo en Mi perfil).",
            "info"
        )
        return redirect(url_for("empleado_panel"))

    # Buscar exigencias de alguna tarea activa del empleado (para mostrar)
    asig = (
        db.session.query(Asignacion)
        .join(Tarea, Asignacion.tarea_id == Tarea.id)
        .filter(
            Asignacion.usuario_id == current_user.id,
            Asignacion.estado == "activa",
            Tarea.activa == True
        )
        .first()
    )
    if asig and asig.tarea and getattr(asig.tarea, "exigencias_text", None):
        exig = asig.tarea.exigencias_text
    else:
        exig = (
            "Debes usar implementos de seguridad (gafas, guantes, botas, chaleco y los indicados por la tarea). "
            "La modalidad de pago es semanal: se trabaja de viernes a jueves y el pago corresponde a esa semana."
        )

    if request.method == "POST":
        # --- tomar datos
        nombre_legal = (request.form.get("nombre_legal_firma") or current_user.nombre or "").strip()
        telefono_in  = (request.form.get("telefono") or "").strip()
        zelle_nom    = (request.form.get("zelle_nombre") or "").strip()
        zelle_cta    = (request.form.get("zelle_cuenta") or "").strip()
        email_in     = (request.form.get("email") or "").strip().lower()  # a minúsculas

        # --- validar requeridos
        if not nombre_legal:
            flash("El nombre legal es requerido.", "danger")
            return redirect(url_for("empleado_onboarding"))

        # Teléfono en E.164 y DEBE coincidir con el verificado
        tel_norm = normalize_e164(telefono_in)
        if not (tel_norm.startswith("+") and tel_norm[1:].isdigit() and 8 <= len(tel_norm[1:]) <= 15):
            flash("Teléfono inválido. Usa formato con prefijo (E.164), por ejemplo: +1 7865551234.", "danger")
            return redirect(url_for("empleado_onboarding"))
        if current_user.telefono and tel_norm != normalize_e164(current_user.telefono):
            flash("El teléfono no coincide con el número verificado por OTP.", "danger")
            return redirect(url_for("empleado_onboarding"))

        # Zelle obligatorios
        if not zelle_nom or not zelle_cta:
            flash("Completa nombre e identificador de Zelle.", "danger")
            return redirect(url_for("empleado_onboarding"))

        # --- email: si está vacío, generar uno único; si no, validar formato y unicidad
        if not email_in:
            email_in = unique_auto_email(Usuario, exclude_id=current_user.id)
        else:
            # (opcional) formato simple
            if "@" not in email_in or email_in.count("@") != 1:
                flash("Correo inválido.", "danger")
                return redirect(url_for("empleado_onboarding"))
            # unicidad
            exists = (
                Usuario.query
                .filter(Usuario.email == email_in, Usuario.id != current_user.id)
                .first()
            )
            if exists:
                flash("Ese correo ya está en uso.", "danger")
                return redirect(url_for("empleado_onboarding"))

        # --- guardar
        current_user.nombre_legal_firma = nombre_legal
        current_user.telefono = current_user.telefono or tel_norm  # reafirma verificado
        current_user.zelle_nombre = zelle_nom
        current_user.zelle_cuenta = zelle_cta
        current_user.email = email_in
        current_user.onboarding_completo = True
        db.session.commit()

        flash("Datos guardados. ¡Bienvenido!", "success")
        return redirect(url_for("empleado_panel"))

    # GET
    return render_template("empleado/first_time.html", exigencias=exig)



@app.route("/registro", methods=["GET","POST"])
def registro_empleado():
    pre_tel = session.get("pre_tel")
    if request.method == "POST":
        nombre = (request.form.get("nombre") or "").strip()
        telefono = normalize_e164(request.form.get("telefono") or "")
        if not (telefono.startswith('+') and telefono[1:].isdigit() and 8 <= len(telefono[1:]) <= 15):
            flash("Teléfono inválido. Usa formato + y 8–15 dígitos.", "warning")
            return redirect(url_for("login_employee"))
        if Usuario.query.filter_by(telefono=telefono).first():
            flash("Ese teléfono ya está registrado.", "warning")
            return redirect(url_for("login_employee"))

        u = Usuario(nombre=nombre, telefono=telefono, rol="empleado", activo=True)
        db.session.add(u); db.session.commit()
        flash("Registro completo. Solicita OTP para ingresar.", "success")
        return redirect(url_for("login_employee"))
    return render_template("auth/registro_empleado.html", telefono_prefill=pre_tel)

@app.post("/api/empleado/first-time")
@login_required
def api_empleado_first_time():
    if current_user.rol != "empleado":
        return jsonify(ok=False, error="No autorizado"), 403
    if getattr(current_user, "onboarding_completo", False):
        return jsonify(ok=False, error="Ya completaste tu información. Solo el correo puede editarse luego."), 400

    data = request.get_json(silent=True) or {}
    nombre_legal = (data.get("nombre_legal_firma") or current_user.nombre or "").strip()
    telefono_in  = (data.get("telefono") or "").strip()
    zelle_nom    = (data.get("zelle_nombre") or "").strip()
    zelle_cta    = (data.get("zelle_cuenta") or "").strip()
    email_in     = (data.get("email") or "").strip()

    if not nombre_legal:
        return jsonify(ok=False, error="El nombre legal es requerido."), 400

    tel_norm = normalize_e164(telefono_in)
    if not (tel_norm.startswith("+") and tel_norm[1:].isdigit() and 8 <= len(tel_norm[1:]) <= 15):
        return jsonify(ok=False, error="Teléfono inválido. Usa formato + y 8–15 dígitos (E.164)."), 400
    if current_user.telefono and tel_norm != normalize_e164(current_user.telefono):
        return jsonify(ok=False, error="El teléfono no coincide con el verificado por OTP."), 400

    if not zelle_nom or not zelle_cta:
        return jsonify(ok=False, error="Completa nombre e identificador de Zelle."), 400

    # ✓ email
    if not email_in:
        email_in = unique_auto_email(Usuario, exclude_id=current_user.id)
    else:
        exists = (Usuario.query
                  .filter(Usuario.email == email_in, Usuario.id != current_user.id)
                  .first())
        if exists:
            return jsonify(ok=False, error="Ese correo ya está en uso."), 400

    current_user.nombre_legal_firma = nombre_legal
    current_user.telefono = current_user.telefono or tel_norm
    current_user.zelle_nombre = zelle_nom
    current_user.zelle_cuenta = zelle_cta
    current_user.email = email_in
    current_user.onboarding_completo = True
    db.session.commit()
    return jsonify(ok=True)



@app.route("/empleado/perfil", methods=["GET", "POST"])
@login_required
@role_required("empleado")
def empleado_perfil():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip() or None
        # si quieren borrar el email, permitimos None
        try:
            current_user.email = email
            db.session.commit()
            flash("Correo actualizado.", "success")
        except IntegrityError:
            db.session.rollback()
            flash("Ese correo ya está en uso por otro usuario.", "danger")
        return redirect(url_for("empleado_perfil"))

    return render_template("empleado/perfil.html")



def empleado_tarea(id):
    t = Tarea.query.get_or_404(id)
    asign = (Asignacion.query
             .filter_by(tarea_id=t.id, usuario_id=current_user.id, estado="activa")
             .first())

    # SOLO mostrar botón de emergencia si la tarea es común o de turno
    tipo = getattr(t, "tipo", None)  # 'comun' | 'turno' | 'emergencia'
    show_emerg_btn = (tipo in ("comun", "turno"))

    # emergencias activas asignadas al empleado
    # Emergencias activas asignadas al empleado
    emerg_tareas = (Tarea.query
                    .join(Asignacion, Asignacion.tarea_id == Tarea.id)
                    .filter(Asignacion.usuario_id == current_user.id,
                            Asignacion.estado == "activa",
                            Tarea.activa == True,
                            Tarea.tipo == "emergencia")
                    .order_by(Tarea.nombre.asc())
                    .all())


    # … calcula lo demás (asistencia, lunch, ventanas, enviados_hoy, etc.)
    return render_template(
        "empleado/reportes_asistencia.html",
        t=t,
        asign=asign,
        show_emerg_btn=show_emerg_btn,
        emerg_tareas=emerg_tareas,
        # ... el resto de tu contexto ...
    )
# ===================== util: detectar si el cliente espera JSON =====================
from flask import request, redirect, url_for, flash, jsonify
def wants_json() -> bool:
    # Devuelve JSON si es fetch/XHR o Accept JSON explícito
    return request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest" \
        or (request.accept_mimetypes.accept_json and not request.accept_mimetypes.accept_html)


# app.py (o el módulo donde están tus @app.post)
from flask import request, redirect, url_for, flash, jsonify

# --- helpers de respuesta/redirect ---
def _flash_redirect(message: str, tarea_id: int | None, category: str = "success"):
    try:
        flash(message, category)
    except Exception:
        pass
    if tarea_id:
        # ojo: tu endpoint usa tid, no id
        return redirect(url_for("empleado_tarea", tid=tarea_id), code=303)
    return redirect(url_for("empleado_dashboard"), code=303)

def _wants_html() -> bool:
    # si es formulario (multipart) o el navegador espera html => redirigimos
    ct = (request.content_type or "").lower()
    if ct.startswith("multipart/"):
        return True
    accept = (request.headers.get("Accept") or "").lower()
    return "text/html" in accept

# ===================== Confirmar asistencia (botón) =====================
from services.jornada_service import (
    asistencia_window_3h,
    compute_entrada_regla,
    upsert_jornada_entrada,
    build_reporte_confirmacion,
    build_reporte_asistencia,
    cerrar_jornada_con_salida,
    ensure_entrada_en_start_si_falta,
    marcar_pendientes_emergencia_hoy,
    hoy_tiene_asistencia,                 # 👈 lo usas en ventana
    get_emergencias_del_empleado,         # 👈 para el endpoint de lista
)

from services.jornada_service import (
    resolve_times_for_report,
    asistencia_window_3h,
    compute_entrada_regla,
    upsert_jornada_entrada,
    build_reporte_asistencia,
    build_reporte_lunch,
    build_reporte_ventana,
    hoy_tiene_asistencia,
)

from services.empleado_helpers import (
    tz_of_task, aware_start_dt, now_in_tz, local_day, calc_lateness,
    ahora_local, dentro_ventana_reporte,
)

from services.empleado_helpers import (
    tz_of_task, aware_start_dt, now_in_tz, local_day, calc_lateness,
    ahora_local, dentro_ventana_reporte,
)

import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from werkzeug.utils import secure_filename

def _ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def _rel_from_static(path_abs: str) -> str:
    p = (path_abs or "").replace("\\", "/")
    return p.split("static/", 1)[1] if "static/" in p else p

ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
MAX_MB = 8
def _safe_ext(filename: str):
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTS else ""


# ---------------------- EMPLEADO ----------------------
@app.route("/empleado")
@login_required
@role_required("empleado")
def empleado_panel():
    def formatHM(minutes):
        if not isinstance(minutes, int) or minutes < 0:
            return '0 min'
        hours = minutes // 60
        mins = minutes % 60
        if hours:
            return f"{hours} h {mins} min" if mins else f"{hours} h"
        return f"{mins} min"

    def _parse_hhmm(s: str, fallback="08:00"):
        try:
            h, m = map(int, (s or fallback).split(":"))
            return h, m
        except Exception:
            return 8, 0
    
    def _bounds_local_utc(tz_name: str, aware_now: datetime):
        day_start_loc = aware_now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_loc   = aware_now.replace(hour=23, minute=59, second=59, microsecond=0)
        start_utc = day_start_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        end_utc   = day_end_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        return day_start_loc, day_end_loc, start_utc, end_utc

    # Verifica si la hora de inicio es válida antes de calcular el tiempo restante
    def _next_start_minutes(t, aware_now: datetime, a=None):
        h_in_str = getattr(a, "hora_inicio_override", None) or getattr(t, "hora_inicio", "08:00") or "08:00"
        try:
            hh, mm = map(int, h_in_str.split(":"))
        except Exception:
            hh, mm = 8, 0
        next_loc = aware_now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if aware_now >= next_loc:
            next_loc = next_loc + timedelta(days=1)
        return max(0, int((next_loc - aware_now).total_seconds() // 60))


    asigns = (
        Asignacion.query
        .filter(and_(Asignacion.usuario_id==current_user.id, Asignacion.estado=="activa"))
        .join(Tarea, Asignacion.tarea_id == Tarea.id)
        .filter(Tarea.activa.is_(True))
        .all()
    )

    items = []
    for a in asigns:
        t = a.tarea
        tz_name = getattr(t, "tz_name", None) or "America/New_York"

        try:
            now_loc = ahora_local(t)  # aware en tz de la tarea
        except Exception:
            now_loc = datetime.now(ZoneInfo(tz_name))

        day_start_loc, day_end_loc, start_utc, end_utc = _bounds_local_utc(tz_name, now_loc)

        # Jornada del día local (se deshabilita solo si tiene INICIO y SALIDA)
        tn = getattr(a, "turno_num", None) or 1
        jhoy = (Jornada.query
                .filter_by(tarea_id=t.id, usuario_id=a.usuario_id,
                           fecha=day_start_loc.date(), turno_num=tn)
                .first())
        disabled_today = bool(jhoy and jhoy.hora_inicio is not None and jhoy.hora_salida is not None)

        # Conteos programados hoy
        esperados_prog = ReportVentana.query.filter_by(tarea_id=t.id).count()
        completados_prog = (Reporte.query
            .filter(Reporte.tarea_id==t.id,
                    Reporte.usuario_id==a.usuario_id,
                    Reporte.report_ventana_id.isnot(None),
                    Reporte.timestamp>=start_utc, Reporte.timestamp<=end_utc)
            .count())

        # Obligatorios hoy (confirmación + asistencia) -> máx 2
        obligatorios_done = (Reporte.query
            .filter(Reporte.tarea_id==t.id,
                    Reporte.usuario_id==a.usuario_id,
                    Reporte.tipo.in_(("confirmacion","asistencia")),
                    Reporte.timestamp>=start_utc, Reporte.timestamp<=end_utc)
            .count())
        obligatorios_done = max(0, min(2, obligatorios_done))

        # Totales que verá el empleado (programados + 2 obligatorios)
        total = int(esperados_prog) + 2
        hechos = int(completados_prog) + int(obligatorios_done)

        # ¿ya confirmó hoy?  (acepta flag de asignación O reporte asistencia_ligera/confirmacion)
        ya_confirmada = bool(getattr(a, "confirmado", False)) or db.session.query(
            db.session.query(Reporte.id).filter(
                Reporte.tarea_id == t.id,
                Reporte.usuario_id == a.usuario_id,
                Reporte.tipo.in_(("asistencia_ligera", "confirmacion")),
                Reporte.timestamp >= start_utc, Reporte.timestamp <= end_utc
            ).exists()
        ).scalar()

        # ventana de confirmación: desde 3h antes del inicio hasta FIN DEL DÍA local
        h_in_str = getattr(a, "hora_inicio_override", None) or getattr(t, "hora_inicio", "08:00") or "08:00"
        hh, mm = _parse_hhmm(h_in_str)
        start_loc = now_loc.replace(hour=hh, minute=mm, second=0, microsecond=0)
        window_start = start_loc - timedelta(hours=3)
        window_end   = day_end_loc  # << antes era start_loc + 3h; ahora se puede confirmar aunque esté tarde

        can_confirm = (window_start <= now_loc <= window_end) and (not ya_confirmada) and (not disabled_today)

        # “tarde” solo si hoy aplica y no está deshabilitada
        minutes_late = 0
        if not disabled_today and now_loc > start_loc:
            minutes_late = int((now_loc - start_loc).total_seconds() // 60)

        # Si terminó la jornada/día: no mostrar tarde; mostrar countdown al siguiente inicio
        next_start_in_min = 0
        if disabled_today or now_loc >= day_end_loc:
            minutes_late = 0
            next_start_in_min = _next_start_minutes(t, now_loc, a)

        # última actividad de HOY para ordenar
        last_rep = (Reporte.query
                    .filter(Reporte.tarea_id==t.id,
                            Reporte.usuario_id==a.usuario_id,
                            Reporte.timestamp>=start_utc, Reporte.timestamp<=end_utc)
                    .order_by(Reporte.timestamp.desc())
                    .first())
        last_activity = None
        if last_rep and getattr(last_rep, "timestamp", None):
            last_activity = last_rep.timestamp.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo(tz_name))

        # Empaquetado (incluye alias para compatibilidad con el template)
        items.append(dict(
            tarea=t, asign=a,

            # NUEVO + alias para el template viejo
            total=total, hechos=hechos,
            esperados=total,          # alias para el template
            completados=hechos,       # alias para el template

            esperados_prog=esperados_prog,
            completados_prog=completados_prog,
            obligatorios_done=obligatorios_done,

            ya_confirmada=bool(ya_confirmada),
            can_confirm=bool(can_confirm),
            minutes_late=max(0, minutes_late),

            disabled_today=disabled_today,
            next_start_in_min=next_start_in_min,

            last_activity=last_activity,
        ))

    # orden server-side por actividad reciente
    def _order_key(it):
        la = it.get("last_activity")
        return la.timestamp() if la else 0.0

    items.sort(key=_order_key, reverse=True)

    current_date_us = datetime.utcnow().strftime("%m/%d/%Y")
    return render_template("empleado/empleado.html",
                           items=items,
                           current_date_us=current_date_us, formatHM=formatHM)
    
# ====== CONFIRMAR TAREA (Empleado) ======
@app.post("/api/empleado/confirmar-tarea")
@login_required
@role_required("empleado")
def api_empleado_confirmar_tarea():
    # Imports locales
    from services.jornada_service import (
        asistencia_window_3h,
        compute_entrada_regla,
        upsert_jornada_entrada,
        build_reporte_confirmacion,
        resolve_times_for_report,   # ← punto único de verdad de tiempos
    )
    try:
        # JSON o form
        if request.is_json:
            data = request.get_json(silent=True) or {}
            tarea_id = int(data.get("tarea_id") or 0)
        else:
            tarea_id = request.form.get("tarea_id", type=int) or 0

        if not tarea_id:
            return jsonify(ok=False, error="ID de tarea faltante."), 400

        t = Tarea.query.get(tarea_id)
        if not t or not getattr(t, "activa", True):
            return jsonify(ok=False, error="Tarea no encontrada o inactiva."), 404

        a = (Asignacion.query
             .filter(Asignacion.tarea_id == t.id,
                     Asignacion.usuario_id == current_user.id,
                     Asignacion.estado == "activa")
             .first())
        if not a:
            return jsonify(ok=False, error="No tienes asignación activa en esta tarea."), 403

        # Ventana (nos da start_local de referencia)
        ini_win, fin_win, tz_name_win, start_loc_win = asistencia_window_3h(t)

        # === Punto único de verdad para tiempos ===
        # device_ts no aplica aquí, así que None
        ts_db, now_loc, tz_name, meta = resolve_times_for_report(t, device_ts=None)

        # Elegimos el start de referencia (si no hay, caemos a now_loc)
        start_ref = start_loc_win or now_loc

        # Marcar confirmación en asignación (si existen los campos)
        if hasattr(a, "confirmado"):
            a.confirmado = True
        if hasattr(a, "confirmado_at"):
            a.confirmado_at = now_loc

        # Reglas de entrada y upsert de jornada
        entrada_loc, status, minutes_late = compute_entrada_regla(now_loc, start_ref)
        upsert_jornada_entrada(db, Jornada, t, a, entrada_loc, start_ref)

        # Reporte ligero de confirmación (OJO: kwargs)
        rep = build_reporte_confirmacion(
            Reporte, current_user.id, t,
            ts_db=ts_db,
            now_loc=now_loc,
            start_loc=start_ref,
            tz_name=tz_name,
            meta=meta
        )
        db.session.add(rep)
        db.session.commit()

        # Mensaje de retorno
        msg = f"Asistencia confirmada a las {now_loc.strftime('%H:%M')} "
        if status == "on_time":
            msg += "(a tiempo)"
        elif status == "late":
            msg += f"({minutes_late} min tarde)"
        else:
            msg += "(temprano)"

        return jsonify(
            ok=True,
            msg=msg,
            late=(status == "late"),
            minutes_late=minutes_late
        )

    except Exception as e:
        db.session.rollback()
        try:
            current_app.logger.exception("Error en /api/empleado/confirmar-tarea")
        except Exception:
            pass
        # Si quieres ver el detalle durante debugging, descomenta:
        # return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500
        return jsonify(ok=False, error="Error interno al confirmar asistencia."), 500


# ===================== API: Lunch =====================
@app.post("/api/empleado/reportes/lunch")
@login_required
@role_required("empleado")
def api_empleado_reporte_lunch():
    from services.jornada_service import resolve_times_for_report, build_reporte_lunch
    from sqlalchemy import or_
    # ↑ no importo hoy_tiene_asistencia: no lo necesitamos

    tarea_id = int(request.form.get("tarea_id") or 0)
    opcion = (request.form.get("opcion") or "").strip().lower()  # "si" | "no"
    if not tarea_id or opcion not in ("si", "no"):
        return (_flash_redirect("Parámetros inválidos.", None, "danger")
                if _wants_html() else (jsonify(ok=False, error="Parámetros inválidos."), 400))

    a = (Asignacion.query
         .filter(and_(Asignacion.tarea_id == tarea_id,
                      Asignacion.usuario_id == current_user.id,
                      Asignacion.estado == "activa"))
         .first())
    if not a:
        return (_flash_redirect("No estás asignado a esta tarea.", tarea_id, "danger")
                if _wants_html() else (jsonify(ok=False, error="No estás asignado a esta tarea."), 403))

    t = a.tarea

    device_ts = (request.form.get("device_ts") or "").strip() or None

    # Punto único de tiempos (NO cambio cómo guardas nada)
    ts_db, now_loc, tz_name, meta = resolve_times_for_report(t, device_ts)

    # ----- SOLO verificar existencia de asistencia HOY (local) -----
    # Como tus Reporte.timestamp están en LOCAL-NAIVE, armamos el rango local-NAIVE:
    start_db = now_loc.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    end_db   = now_loc.replace(hour=23, minute=59, second=59, microsecond=0).replace(tzinfo=None)

    # 'valido' puede ser True/1/NULL en tu BD:
    valido_ok = or_(Reporte.valido.is_(True), Reporte.valido == 1, Reporte.valido.is_(None))

    existe_asistencia_hoy = db.session.query(
        db.session.query(Reporte.id).filter(
            Reporte.usuario_id == current_user.id,
            Reporte.tarea_id == tarea_id,
            Reporte.tipo == "asistencia",   # ← SOLO este tipo
            valido_ok,
            Reporte.timestamp >= start_db,
            Reporte.timestamp <= end_db
        ).exists()
    ).scalar()

    if not existe_asistencia_hoy:
        return (_flash_redirect("Debes registrar tu asistencia antes del lunch.", tarea_id, "danger")
                if _wants_html() else (jsonify(ok=False, error="Asistencia requerida antes del lunch."), 400))
    # ---------------------------------------------------------------

    # GPS (sin cambios)
    lat = (request.form.get("lat") or "").strip()
    lng = (request.form.get("lng") or "").strip()
    gps_str = f"{lat},{lng}" if lat and lng else None
    gps_status = "ok" if gps_str else "not_activated"

    # Foto si "sí" (sin cambios)
    path_rel = None
    if opcion == "si":
        foto = request.files.get("foto")
        if not foto:
            return (_flash_redirect("Se requiere foto para 'Sí'.", tarea_id, "danger")
                    if _wants_html() else (jsonify(ok=False, error="Se requiere foto para 'Sí'."), 400))
        ext = _safe_ext(foto.filename)
        if not ext:
            return (_flash_redirect("Formato no permitido.", tarea_id, "danger")
                    if _wants_html() else (jsonify(ok=False, error="Formato no permitido."), 400))
        foto.seek(0, os.SEEK_END); size_mb = foto.tell()/(1024*1024); foto.seek(0)
        MAX_MB = 8
        if size_mb > MAX_MB:
            return (_flash_redirect(f"Archivo > {MAX_MB}MB", tarea_id, "danger")
                    if _wants_html() else (jsonify(ok=False, error=f"Archivo > {MAX_MB}MB"), 400))

        day = now_loc.date().isoformat()
        base_dir = os.path.join("static", "uploads", "informes", day, str(t.id), "lunch")
        _ensure_dir(base_dir)
        fname = f"{datetime.utcnow().strftime('%H%M%S')}_{current_user.id}{ext}"
        path_abs = os.path.join(base_dir, secure_filename(fname))
        foto.save(path_abs)
        path_rel = _rel_from_static(path_abs)

    # Nota si no hay GPS (sin cambios)
    nota_final = None
    if gps_status != "ok":
        nota_final = "Ubicación no activada; el informe lo tendrá en cuenta."

    # Construir y guardar (sin cambios)
    rep = build_reporte_lunch(
        Reporte, current_user.id, t,
        ts_db=ts_db,
        now_loc=now_loc,
        tz_name=tz_name,
        opcion=opcion,
        foto_rel=path_rel,
        gps_str=gps_str,
        turno_num=getattr(a, "turno_num", None),
        lat=lat or None,
        lng=lng or None,
        device_ts=device_ts,
        meta=meta
    )
    rep.nota = nota_final

    db.session.add(rep); db.session.commit()

    if _wants_html():
        return _flash_redirect("Lunch registrado.", tarea_id, "success")
    return jsonify(ok=True, msg="Lunch registrado", opcion=opcion)



# ===================== Reporte de Asistencia (pantallazo obligatorio, GPS opcional) =====================
@app.post("/api/empleado/reportes/asistencia")
@login_required
@role_required("empleado")
def api_empleado_reporte_asistencia():
    from datetime import datetime
    from services.jornada_service import (
        resolve_times_for_report,
        asistencia_window_3h,
        compute_entrada_regla,
        upsert_jornada_entrada,
        build_reporte_asistencia,
        hoy_tiene_asistencia,
    )

    # Si es <form enctype="multipart/form-data"> → redirigimos (HTML); si no, JSON.
    wants_html = bool(request.content_type and request.content_type.startswith("multipart/"))

    tarea_id = int(request.form.get("tarea_id") or 0)
    if not tarea_id:
        return (_flash_redirect("tarea_id requerido", None, "danger")
                if wants_html else (jsonify(ok=False, error="tarea_id requerido"), 400))

    a = (Asignacion.query
         .filter(and_(Asignacion.tarea_id == tarea_id,
                      Asignacion.usuario_id == current_user.id,
                      Asignacion.estado == "activa"))
         .first())
    if not a:
        return (_flash_redirect("No estás asignado a esta tarea.", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error="No estás asignado a esta tarea."), 403))

    t = a.tarea
    if not t or not getattr(t, "activa", True):
        return (_flash_redirect("Tarea inactiva.", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error="Tarea inactiva."), 400))

    # Debe existir confirmación previa (asistencia ligera)
    if not a.confirmado:
        msg = "Primero debes confirmar la tarea antes de enviar el reporte de asistencia."
        return (_flash_redirect(msg, tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error=msg), 400))

    # Foto obligatoria
    foto = request.files.get("foto")
    if not foto:
        return (_flash_redirect("La foto es obligatoria.", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error="La foto es obligatoria."), 400))

    # Validación extensión/tamaño
    ext = _safe_ext(foto.filename)
    if not ext:
        return (_flash_redirect("Formato no permitido.", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error="Formato no permitido."), 400))
    foto.seek(0, os.SEEK_END); size_mb = foto.tell()/(1024*1024); foto.seek(0)
    if size_mb > MAX_MB:
        return (_flash_redirect(f"Archivo > {MAX_MB}MB", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error=f"Archivo > {MAX_MB}MB"), 400))

    # Extras
    lat        = (request.form.get("lat") or "").strip()
    lng        = (request.form.get("lng") or "").strip()
    gps_status = (request.form.get("gps_status") or "unknown").strip()   # ok|denied|unsupported|unknown|not_activated
    pantallazo = (request.form.get("es_pantallazo_gps") == "on")
    device_ts  = (request.form.get("device_ts") or "").strip() or None
    gps_str    = f"{lat},{lng}" if lat and lng else None
    if not gps_str and gps_status == "unknown":
        gps_status = "not_activated"

    # Ventana / inicio local de la jornada (para reglas)
    ini, fin, tz_name_win, start_loc = asistencia_window_3h(t)

    # Punto único de verdad para tiempos
    ts_db, now_loc, tz_name, meta = resolve_times_for_report(t, device_ts)

    # Guardar foto
    day = local_day(tz_name, start_loc or now_loc).isoformat()
    base_dir = os.path.join("static", "uploads", "informes", day, str(t.id), "asistencia")
    _ensure_dir(base_dir)
    fname = f"{datetime.utcnow().strftime('%H%M%S')}_{current_user.id}{ext}"
    path_abs = os.path.join(base_dir, secure_filename(fname))
    foto.save(path_abs)
    foto_rel = _rel_from_static(path_abs)

    # Reglas + jornada
    entrada_loc, status, minutes_late = compute_entrada_regla(now_loc, start_loc)
    upsert_jornada_entrada(db, Jornada, t, a, entrada_loc, start_loc)

    # Builder
    rep = build_reporte_asistencia(
        Reporte, current_user.id, t,
        ts_db=ts_db,
        now_loc=now_loc,
        start_loc=start_loc,
        tz_name=tz_name,
        in_window=bool(ini and fin and (ini <= now_loc <= fin)),
        nota=None,
        lat=lat or None,
        lng=lng or None,
        device_ts=device_ts,
        path_abs=None,   # seteamos foto abajo
        meta=meta
    )

    # Asegurar metadata que las vistas consultan para "done"
    rep.tipo = "asistencia"                  # <- clave para queries de "done"
    if hasattr(rep, "tipo_especial"):
        rep.tipo_especial = "asistencia_entrada"   # <- compat con vistas que miran tipo_especial
    rep.report_ventana_id = None
    rep.foto_path = foto_rel
    rep.gps = gps_str
    rep.turno_num = getattr(a, "turno_num", None)
    rep.valido = True

    if not gps_str:
        rep.nota = (rep.nota or "Ubicación no activada; el informe lo tendrá en cuenta.")

    # Payload extra visible
    if hasattr(rep, "payload") and isinstance(rep.payload, dict):
        rep.payload.update({
            "gps_status": gps_status,
            "pantallazo_gps": bool(pantallazo),
            "path": rep.foto_path,
        })

    db.session.add(rep)
    db.session.commit()

    if wants_html:
        # Redirige para que la vista recalcule y pinte “✅ Ya registraste tu asistencia.”
        return _flash_redirect("Asistencia registrado.", tarea_id, "success")

    return jsonify(
        ok=True,
        msg="Asistencia registrada",
        late=(status == "late"),
        minutes_late=minutes_late,
        in_window=bool(ini and fin and (ini <= now_loc <= fin)),
        file=rep.foto_path
    )

# ===================== Reporte de Ventana programada =====================
@app.post("/api/empleado/reporte-ventana")
@login_required
@role_required("empleado")
def api_empleado_reporte_ventana():
    from services.jornada_service import resolve_times_for_report, build_reporte_ventana
    from sqlalchemy import or_

    # ======== LECTURA DE DATOS ========
    if request.content_type and request.content_type.startswith("multipart/"):
        tarea_id   = request.form.get("tarea_id", type=int)
        ventana_id = request.form.get("ventana_id", type=int)
        orden      = request.form.get("orden", type=int)
        nota_in    = (request.form.get("nota") or "").strip() or None
        gps        = (request.form.get("gps") or "").strip() or None
        lat        = (request.form.get("lat") or "").strip()
        lng        = (request.form.get("lng") or "").strip()
        if (not gps) and lat and lng:
            gps = f"{lat},{lng}"
        gps_status = (request.form.get("gps_status") or "").strip().lower() or "unknown"
        device_ts  = (request.form.get("device_ts") or "").strip() or None
        foto       = request.files.get("foto")
        wants_html = True
    else:
        data       = request.get_json(silent=True) or {}
        tarea_id   = data.get("tarea_id")
        ventana_id = data.get("ventana_id")
        orden      = data.get("orden")
        nota_in    = (data.get("nota") or "").strip() or None
        gps        = (data.get("gps") or "").strip() or None
        lat        = (data.get("lat") or "").strip() if data.get("lat") else ""
        lng        = (data.get("lng") or "").strip() if data.get("lng") else ""
        if (not gps) and lat and lng:
            gps = f"{lat},{lng}"
        gps_status = (data.get("gps_status") or "").strip().lower() or "unknown"
        device_ts  = (data.get("device_ts") or "").strip() or None
        foto       = None
        wants_html = _wants_html()

    if not tarea_id:
        return (_flash_redirect("Tarea inválida.", None, "danger")
                if wants_html else (jsonify(ok=False, error="Tarea inválida"), 400))

    # ======== VALIDACIÓN DE ASIGNACIÓN ========
    asign = Asignacion.query.filter_by(
        tarea_id=tarea_id, usuario_id=current_user.id, estado="activa"
    ).first()
    if not asign:
        return (_flash_redirect("No estás asignado a esa tarea.", tarea_id, "danger")
                if wants_html else (jsonify(ok=False, error="No estás asignado a esa tarea."), 403))

    tarea = asign.tarea
    ts_db, now_loc, tz_name, meta = resolve_times_for_report(tarea, device_ts)

    # ======== VERIFICAR ASISTENCIA HOY (POR DÍA, NO POR HORA) ========
    start_db = now_loc.replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    end_db   = now_loc.replace(hour=23, minute=59, second=59, microsecond=0).replace(tzinfo=None)
    valido_ok = or_(Reporte.valido.is_(True), Reporte.valido == 1, Reporte.valido.is_(None))

    existe_asistencia_hoy = db.session.query(
        db.session.query(Reporte.id).filter(
            Reporte.usuario_id == current_user.id,
            Reporte.tarea_id == tarea.id,
            Reporte.tipo == "asistencia",
            valido_ok,
            Reporte.timestamp >= start_db,
            Reporte.timestamp <= end_db
        ).exists()
    ).scalar()

    if not existe_asistencia_hoy:
        return (_flash_redirect("Debes registrar tu asistencia antes de este reporte.", tarea.id, "danger")
                if wants_html else (jsonify(ok=False, error="Asistencia requerida antes de reportes."), 400))

    # ======== RESOLVER VENTANA (mantiene compatibilidad con tu flujo) ========
    if ventana_id:
        v = ReportVentana.query.filter_by(id=ventana_id, tarea_id=tarea.id).first()
    elif orden is not None:
        v = ReportVentana.query.filter_by(tarea_id=tarea.id, orden=int(orden)).first()
    else:
        v = None
    if not v:
        return (_flash_redirect("Ventana inválida.", tarea.id, "danger")
                if wants_html else (jsonify(ok=False, error="Ventana inválida"), 400))

    # ======== NORMALIZAR GPS ========
    coords = None
    if gps:
        try:
            lat_str, lng_str = gps.split(",", 1)
            coords = {"lat": float(lat_str.strip()), "lng": float(lng_str.strip())}
            gps_status = "ok"
        except Exception:
            gps = None
            gps_status = "not_activated"
    else:
        gps_status = "not_activated"

    # ======== GUARDAR FOTO ========
    day = local_day(tz_name, now_loc).isoformat()
    foto_rel = None
    if foto:
        ALLOWED_EXTS = {".jpg", ".jpeg", ".png"}
        MAX_MB = 8
        ext = os.path.splitext(foto.filename or "")[1].lower()
        if ext not in ALLOWED_EXTS:
            return (_flash_redirect("Formato de imagen no permitido.", tarea.id, "danger")
                    if wants_html else (jsonify(ok=False, error="Formato de imagen no permitido."), 400))
        foto.seek(0, os.SEEK_END); size_mb = foto.tell()/(1024*1024); foto.seek(0)
        if size_mb > MAX_MB:
            return (_flash_redirect(f"Archivo > {MAX_MB}MB", tarea.id, "danger")
                    if wants_html else (jsonify(ok=False, error=f"Archivo > {MAX_MB}MB"), 400))
        base_dir = os.path.join("static", "uploads", "informes", day, str(tarea.id), "ventana")
        _ensure_dir(base_dir)
        fname = f"{datetime.utcnow().strftime('%H%M%S')}_{current_user.id}{ext}"
        foto_path_abs = os.path.join(base_dir, secure_filename(fname))
        foto.save(foto_path_abs)
        foto_rel = _rel_from_static(foto_path_abs)

    # ======== NOTA FINAL ========
    nota_final = nota_in or ""
    if gps_status != "ok":
        aviso = "Ubicación no activada; el informe lo tendrá en cuenta."
        if aviso not in nota_final:
            nota_final = (nota_final + " | " + aviso).strip(" |")

    # ======== CREAR REPORTE ========
    r = build_reporte_ventana(
        Reporte, current_user.id, tarea,
        ts_db=ts_db,
        now_loc=now_loc,
        tz_name=tz_name,
        ventana_id=v.id,
        turno_num=getattr(asign, "turno_num", None),
        foto_rel=foto_rel,
        gps_str=gps,
        device_ts=device_ts,
        nota=nota_final,
        meta={
            **meta,
            "gps_status": gps_status,
            "coords": coords,
            "ventana": {"id": v.id, "nombre": v.nombre},
        }
    )

    r.gps = gps or None
    if hasattr(r, "payload") and isinstance(r.payload, dict):
        if coords:
            r.payload["gps"] = {"lat": coords["lat"], "lng": coords["lng"]}
        else:
            r.payload["gps"] = None

    db.session.add(r)
    db.session.commit()

    if wants_html:
        return _flash_redirect("Reporte enviado.", tarea.id, "success")
    return jsonify(ok=True, msg="Reporte enviado", file=foto_rel)


# ===================== EMPLEADO: SALIDA EMERGENCIA =======================
@app.post("/api/empleado/salida-emergencia")
@login_required
@role_required("empleado")
def api_empleado_salida_emergencia():
    try:
        tarea_id = int(request.form.get("tarea_id") or 0)
        if not tarea_id:
            return jsonify(ok=False, error="tarea_id requerido"), 400

        asign = (Asignacion.query
                 .filter(and_(Asignacion.tarea_id == tarea_id,
                              Asignacion.usuario_id == current_user.id,
                              Asignacion.estado == "activa"))
                 .first())
        if not asign:
            return jsonify(ok=False, error="No estás asignado a esta tarea."), 403

        t = asign.tarea

        # ⛔ No permitir salida-emergencia desde una tarea de prioridad 'emergencia'
        prioridad_norm = (getattr(t, "prioridad", "") or "").strip().lower()
        if prioridad_norm == "emergencia":
            return jsonify(ok=False, error="Esta tarea es de emergencia; no aplica salida de emergencia."), 400

        gps       = (request.form.get("gps") or "").strip() or None
        device_ts = (request.form.get("device_ts") or "").strip() or None

        tz_name = tz_of_task(t)
        now_aw  = now_in_tz(tz_name)  # aware (zona de la tarea)

        # Asegurar entrada si no existía
        ensure_entrada_en_start_si_falta(db, Jornada, t, asign, now_aw)

        # Cerrar jornada
        cerrar_jornada_con_salida(db, Jornada, t, asign, now_aw)

        # Crear reporte de salida
        ts_naive_utc = now_aw.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        rep_salida = Reporte(
            usuario_id=current_user.id,
            tarea_id=t.id,
            tipo="salida_emergencia",  # Cambié a tipo 'salida_emergencia'
            timestamp=ts_naive_utc,
            valido=True,
            gps=gps,
            turno_num=getattr(asign, "turno_num", None),
        )
        if hasattr(Reporte, "payload"):
            rep_salida.payload = {
                "emergencia": True,
                "device_ts": device_ts,
                "fecha_dia": local_day(tz_name, now_aw).isoformat(),
                "tz_name": tz_name,
                "gps": gps,
                "nota": "Salida de emergencia registrada"  # Nota explicativa
            }
        db.session.add(rep_salida)

        # Marcar reportes faltantes del día como emergencia
        creados = marcar_pendientes_emergencia_hoy(
            db=db,
            Reporte=Reporte,
            ReportVentana=ReportVentana,
            tarea=t,
            usuario_id=current_user.id,
            tz_name=tz_name,
            now_aw=now_aw,
            gps=gps,
            device_ts=device_ts,
            turno_num=getattr(asign, "turno_num", None),
        )

        db.session.commit()

        flash(
            f"Salida de emergencia registrada a las {now_aw.strftime('%H:%M')} — {creados} reportes completados automáticamente.",
            "success",
        )
        return redirect(url_for("empleado_tarea", tid=t.id))  # <- usa 'tid'

    except Exception as e:
        db.session.rollback()
        print("❌ Error salida emergencia:", e)
        flash("Error interno al registrar salida de emergencia.", "danger")
        return redirect(url_for("empleado_tarea", tid=tarea_id))


# ===================== Listar emergencias activas del empleado =====================
@app.get("/api/empleado/emergencias")
@login_required
@role_required("empleado")
def api_empleado_listar_emergencias():
    exclude_tid = request.args.get("exclude_tid", type=int)
    try:
        lista = Tarea.query.filter(
            Tarea.tipo == 'emergencia',  # Solo tareas de tipo 'emergencia'
            Tarea.activa == 1,  # Solo las tareas activas
            Tarea.id != exclude_tid  # Excluir la tarea actual si es necesario
        ).all()
        # Devolver la lista de emergencias activas
        return jsonify(ok=True, emergencias=[t.nombre for t in lista])
    except Exception as e:
        print("❌ listar emergencias:", e)
        return jsonify(ok=False, error="No fue posible listar emergencias"), 500



# ===================== ADMIN: Job cerrar-ventanas (marcar 'missed') =====================
@app.post("/admin/jobs/cerrar-ventanas")
@login_required
@role_required("admin","superadmin")
def admin_job_cerrar_ventanas():
    """
    Recorre ventanas del DÍA ANTERIOR (fecha local de cada tarea).
    Si no hubo reporte en la franja, registra Sancion nivel 'warn' y log_event.
    Idempotente: revisa si ya existe sanción para esa tarea/usuario/fecha/ventana.
    """
    hoy_utc = datetime.utcnow().date()
    # Nos enfocamos en ayer (UTC) y resolvemos por tz de cada tarea internamente:
    ayer_utc = hoy_utc - timedelta(days=1)

    # Candidatos: asignaciones activas ayer
    asigns = (Asignacion.query
              .join(Tarea, Asignacion.tarea_id==Tarea.id)
              .filter(Asignacion.estado=="activa", Tarea.activa==True)
              .all())

    cerrados = 0
    for a in asigns:
        t = a.tarea
        tz = ZoneInfo(getattr(t, "tz_name", None) or "UTC")
        # Fecha local de 'ayer' para esa tarea:
        base = datetime(ayer_utc.year, ayer_utc.month, ayer_utc.day, 12, 0, 0, tzinfo=ZoneInfo("UTC")).astimezone(tz)
        fecha_local = base.date()

        ventanas = ReportVentana.query.filter_by(tarea_id=t.id).all()
        tol = timedelta(minutes=t.tolerancia_min or 0)
        for v in ventanas:
            if not (v.con_horario and v.hora_ini and v.hora_fin):
                continue
            try:
                h1, m1 = map(int, v.hora_ini.split(":"))
                h2, m2 = map(int, v.hora_fin.split(":"))
            except Exception:
                continue

            ini_loc = datetime(fecha_local.year, fecha_local.month, fecha_local.day, h1, m1, 0, tzinfo=tz) - tol
            fin_loc = datetime(fecha_local.year, fecha_local.month, fecha_local.day, h2, m2, 0, tzinfo=tz) + tol
            ini_utc = ini_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
            fin_utc = fin_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

            hubo = db.session.query(
                db.session.query(Reporte.id).filter(
                    Reporte.tarea_id==t.id, Reporte.usuario_id==a.usuario_id,
                    Reporte.report_ventana_id==v.id,
                    Reporte.timestamp>=ini_utc, Reporte.timestamp<=fin_utc
                ).exists()
            ).scalar()

            if not hubo:
                # Evitar duplicar sanción: buscamos una sanción de “missed ventana” para ese día
                ya = (Sancion.query.filter(
                        Sancion.usuario_id==a.usuario_id,
                        Sancion.tarea_id==t.id,
                        Sancion.tipo=="ventana_missed",
                        Sancion.mensaje.ilike(f"%orden={v.orden}%{fecha_local.isoformat()}%")
                     ).first())
                if ya:
                    continue
                db.session.add(Sancion(
                    usuario_id=a.usuario_id,
                    tarea_id=t.id,
                    tipo="ventana_missed",
                    mensaje=f"Reporte de ventana faltante (orden={v.orden}) el {fecha_local.isoformat()}",
                    nivel="warn",
                    resuelta=False
                ))
                # log opcional si tienes helper
                try:
                    log_event("ventana_missed", tarea_id=t.id, empleado_id=a.usuario_id,
                              detalle={"orden": v.orden, "fecha_local": fecha_local.isoformat()})
                except Exception:
                    pass
                cerrados += 1

    db.session.commit()
    return jsonify(ok=True, missed_registrados=cerrados)



    
@app.route("/a/<codigo>")
@login_required
def empleado_via_codigo(codigo):
    asig = Asignacion.query.filter_by(codigo_asignacion=codigo).first_or_404()
    if current_user.rol == "empleado" and current_user.id != asig.usuario_id:
        flash("No estás autorizado para esta asignación.", "danger")
        return redirect(url_for("empleado_panel"))
    return redirect(url_for("empleado_tarea", tid=asig.tarea_id))

#======== ENDPOINT PARA LISTAR TAREA ============
@app.route("/empleado/tarea/<int:tid>")
@login_required
@role_required("empleado")
def empleado_tarea(tid):
    from datetime import timedelta
    from zoneinfo import ZoneInfo

    # 1) Trae la tarea SOLO si está activa
    t = (Tarea.query
         .filter(Tarea.id == tid,
                 or_(Tarea.activa == True, Tarea.activa == 1))  # activa=1/True
         .first_or_404())

    # 2) Verifica asignación ACTIVA del usuario para esa tarea
    asign = (Asignacion.query
             .filter_by(usuario_id=current_user.id, tarea_id=t.id, estado="activa")
             .first())
    if not asign:
        # Mejor 404 para no revelar si la tarea existe
        from flask import abort
        abort(404)

    # Hora local aware de la TAREA
    now_loc = ahora_local(t)  # aware con tz correcto

    # ===== RANGO DEL DÍA LOCAL (AWARE) -> PARA DB USAMOS NAIVE LOCAL =====
    start_loc = now_loc.replace(hour=0, minute=0, second=0, microsecond=0)
    end_loc   = now_loc.replace(hour=23, minute=59, second=59, microsecond=0)
    start_db  = start_loc.replace(tzinfo=None)
    end_db    = end_loc.replace(tzinfo=None)

    # Helper 'valido' (True / 1 / NULL)
    valido_ok = or_(Reporte.valido.is_(True), Reporte.valido == 1, Reporte.valido.is_(None))

    # ¿ya hay asistencia HOY?
    asist_hoy = db.session.query(
        db.session.query(Reporte.id).filter(
            Reporte.usuario_id == current_user.id,
            Reporte.tarea_id == t.id,
            valido_ok,
            Reporte.tipo == "asistencia",
            Reporte.timestamp >= start_db,
            Reporte.timestamp <= end_db
        ).exists()
    ).scalar()

    # ¿ya hay lunch HOY?
    lunch_hoy = db.session.query(
        db.session.query(Reporte.id).filter(
            Reporte.usuario_id == current_user.id,
            Reporte.tarea_id == t.id,
            valido_ok,
            Reporte.tipo == "lunch",
            Reporte.timestamp >= start_db,
            Reporte.timestamp <= end_db
        ).exists()
    ).scalar()

    # === Ventana de asistencia (solo visual) ===
    h_in_str = asign.hora_inicio_override or getattr(t, "hora_inicio", None) or "08:00"
    try:
        h_in, m_in = map(int, h_in_str.split(":"))
    except Exception:
        h_in, m_in = 8, 0

    asis_tol_min = (
        asign.tolerancia_override
        if getattr(asign, "tolerancia_override", None) is not None
        else (getattr(t, "tolerancia_min", None) or 10)
    )
    asis_ini = now_loc.replace(hour=h_in, minute=m_in, second=0, microsecond=0) - timedelta(minutes=asis_tol_min)
    asis_fin = now_loc.replace(hour=h_in, minute=m_in, second=0, microsecond=0) + timedelta(minutes=asis_tol_min)

    asistencia = dict(
        done=bool(asist_hoy),
        enabled=(asis_ini <= now_loc <= asis_fin) and not asist_hoy,
        too_early=(now_loc < asis_ini) and not asist_hoy,
        tarde=(now_loc > asis_fin) and not asist_hoy,
        hora_inicio_eff=h_in_str,
        tol_eff=asis_tol_min
    )

    # ===== Ventanas de reportes =====
    tol = timedelta(minutes=getattr(t, "tolerancia_min", 0) or 0)
    ventanas = []
    qv = ReportVentana.query.filter_by(tarea_id=t.id).order_by(ReportVentana.orden.asc())
    for v in qv.all():
        # ¿ya hay un reporte válido HOY para esta ventana?
        reporte = (Reporte.query.filter(
            Reporte.tarea_id == t.id,
            Reporte.usuario_id == current_user.id,
            Reporte.report_ventana_id == v.id,
            valido_ok,
            Reporte.timestamp >= start_db,
            Reporte.timestamp <= end_db
        ).first())

        hi_raw = (v.hora_ini or "").strip()
        hf_raw = (v.hora_fin or "").strip()
        has_schedule = bool(hi_raw and hf_raw)

        ini_str = hi_raw if has_schedule else None
        fin_str = hf_raw if has_schedule else None

        too_early = tarde = enabled = False
        if has_schedule:
            try:
                hh1, mm1 = map(int, hi_raw.split(":"))
                hh2, mm2 = map(int, hf_raw.split(":"))
            except Exception:
                hh1, mm1, hh2, mm2 = 0, 0, 23, 59

            ini_loc = now_loc.replace(hour=hh1, minute=mm1, second=0, microsecond=0) - tol
            fin_loc = now_loc.replace(hour=hh2, minute=mm2, second=0, microsecond=0) + tol

            enabled   = (ini_loc <= now_loc <= fin_loc) and not bool(reporte)
            too_early = (now_loc < ini_loc) and not bool(reporte)
            tarde     = (now_loc > fin_loc) and not bool(reporte)
        else:
            enabled = not bool(reporte)

        ventanas.append({
            "id": v.id,
            "orden": v.orden,
            "nombre": v.nombre,
            "ini": ini_str,
            "fin": fin_str,
            "done": bool(reporte),
            "enabled": bool(enabled) and bool(asist_hoy),
            "too_early": bool(too_early) and bool(asist_hoy),
            "tarde": bool(tarde) and bool(asist_hoy),
        })

    # Lunch (habilitado sólo si hay asistencia y aún no se registró)
    lunch = dict(done=bool(lunch_hoy), enabled=bool(asist_hoy) and not bool(lunch_hoy))

    enviados_hoy = (Reporte.query
        .filter(Reporte.tarea_id == t.id,
                Reporte.usuario_id == current_user.id,
                Reporte.timestamp >= start_db,
                Reporte.timestamp <= end_db).count())

    # Emergencias: la función ya filtra a 'activas' y de tipo/prioridad emergencia
    emerg_tareas = get_emergencias_del_empleado(Tarea, Asignacion, current_user.id, exclude_tid=t.id)
    prioridad_norm = ((getattr(t, "prioridad", "") or "").strip().lower())
    show_emerg_btn = (prioridad_norm in {"comun", "común", "turno", "turnos"} and bool(emerg_tareas))

    return render_template(
        "empleado/empleado_tarea.html",
        t=t, asign=asign,
        asistencia=asistencia,
        lunch=lunch,
        ventanas=ventanas,
        enviados_hoy=enviados_hoy,
        now_loc=now_loc,
        show_emerg_btn=show_emerg_btn,
        emerg_tareas=emerg_tareas,
        prioridad_norm=prioridad_norm,
    )



#======= HELPERS TZ =========
def _tzaware_now_for_task(tarea: Tarea):
    """Devuelve ahora aware en la TZ de la tarea; además de UTC naive para DB."""
    tz = tarea.tz_name or "America/New_York"
    aware = datetime.now(ZoneInfo(tz))
    return aware

def _local_date_for_task(tarea: Tarea, aware_dt: datetime):
    """Fecha local (date) usando TZ de la tarea."""
    return aware_dt.date()

def _to_utc_naive(aware_dt: datetime) -> datetime:
    """Convierte un aware a UTC naive (tu DB usa naive UTC en Reporte/Jornada)."""
    return aware_dt.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

def _jornada_get_or_create(tid: int, uid: int, fecha_local: date, turno_num: int | None):
    """Busca o crea jornada única por tarea/usuario/fecha/turno_num."""
    j = (Jornada.query
         .filter_by(tarea_id=tid, usuario_id=uid, fecha=fecha_local, turno_num=turno_num or 1)
         .first())
    if not j:
        j = Jornada(
            tarea_id=tid,
            usuario_id=uid,
            fecha=fecha_local,
            turno_num=(turno_num or 1),
            fuente="reporte"
        )
        db.session.add(j)
        db.session.flush()
    return j

@app.route("/empleado/reporte/nuevo/<int:tid>", methods=["POST"])
@login_required
@role_required("empleado")
def crear_reporte(tid):
    asign = (Asignacion.query
             .filter_by(tarea_id=tid, usuario_id=current_user.id, estado="activa")
             .first_or_404())
    tarea = asign.tarea

    # tiempos
    ahora_loc = _tzaware_now_for_task(tarea)       # aware en TZ tarea
    ahora_utc_naive = _to_utc_naive(ahora_loc)     # naive UTC para DB
    hoy_local = _local_date_for_task(tarea, ahora_loc)

    # validar código de asignación
    cod_asig_form = (request.form.get("cod_asig") or "").strip().lower()
    if cod_asig_form != (asign.codigo_asignacion or "").lower():
        flash("El ID de asignación no coincide con tu tarea.", "danger")
        return redirect(url_for("empleado_tarea", tid=tid))

    # turno para el reporte (si no hay, null). Si quisieras permitir override por form, léelo:
    turno_num = asign.turno_num

    # ¿ya hay asistencia hoy?
    start_loc = datetime.combine(hoy_local, time(0,0,0), tzinfo=ZoneInfo(tarea.tz_name or "America/New_York"))
    end_loc   = datetime.combine(hoy_local, time(23,59,59), tzinfo=ZoneInfo(tarea.tz_name or "America/New_York"))
    start_utc = _to_utc_naive(start_loc)
    end_utc   = _to_utc_naive(end_loc)

    asist_hoy = db.session.query(
        db.session.query(Reporte.id).filter(
            Reporte.usuario_id==current_user.id,
            Reporte.tarea_id==tid,
            Reporte.tipo=="asistencia",
            Reporte.timestamp>=start_utc,
            Reporte.timestamp<=end_utc
        ).exists()
    ).scalar()

    tipo = (request.form.get("tipo") or "").strip()  # 'asistencia' | 'lunch' | 'ventana'

    # Siempre exigir asistencia primero
    if not asist_hoy and tipo != "asistencia":
        flash("Primero debes registrar tu asistencia.", "warning")
        return redirect(url_for("empleado_tarea", tid=tid))

    # ---------- ASISTENCIA ----------
    if tipo == "asistencia":
        # Ventana tolerada alrededor de la hora_inicio efectiva
        h_in_str = asign.hora_inicio_override or tarea.hora_inicio or "08:00"
        try:
            h_in, m_in = map(int, h_in_str.split(":"))
        except Exception:
            h_in, m_in = 8, 0

        tol_min = asign.tolerancia_override if asign.tolerancia_override is not None else (tarea.tolerancia_min or 10)
        base = ahora_loc.replace(hour=h_in, minute=m_in, second=0, microsecond=0)
        asis_ini = base - timedelta(minutes=tol_min)
        asis_fin = base + timedelta(minutes=tol_min)
        if not (asis_ini <= ahora_loc <= asis_fin):
            flash(f"La asistencia solo puede registrarse alrededor de {h_in_str} (±{tol_min} min).", "warning")
            return redirect(url_for("empleado_tarea", tid=tid))

        # evitar doble asistencia hoy (día local)
        ya_asis = db.session.query(
            db.session.query(Reporte.id).filter(
                Reporte.usuario_id==current_user.id, Reporte.tarea_id==tid,
                Reporte.tipo=="asistencia",
                Reporte.timestamp>=start_utc, Reporte.timestamp<=end_utc
            ).exists()
        ).scalar()
        if ya_asis:
            flash("Tu asistencia de hoy ya está registrada.", "info")
            return redirect(url_for("empleado_tarea", tid=tid))

        gps  = request.form.get("gps") or None
        foto = guardar_foto(request.files.get("foto"))  # opcional para asistencia
        nota = request.form.get("nota") or None

        tipo_especial = None
        if _necesita_confirmacion(current_user.id, tid, ahora_utc_naive) and gps:
            tipo_especial = "confirmacion"

        # Guarda el reporte (con turno)
        r = Reporte(
            tarea_id=tid, usuario_id=current_user.id, tipo="asistencia",
            turno_num=turno_num,
            gps=gps, foto_path=foto,
            timestamp=ahora_utc_naive, valido=True,
            tipo_especial=tipo_especial, nota=nota
        )
        db.session.add(r)

        # Upsert de JORNADA → fija hora_inicio
        j = _jornada_get_or_create(tid, current_user.id, hoy_local, turno_num)
        if not j.hora_inicio:
            j.hora_inicio = ahora_utc_naive  # guardamos UTC naive (consistente con Reporte)
            j.fuente = j.fuente or "reporte"

        db.session.commit()
        flash("Asistencia registrada.", "success")
        return redirect(url_for("empleado_tarea", tid=tid))

    # ---------- LUNCH (obligatorio sí/no; si es sí exige foto) ----------
    if tipo == "lunch":
        lunch_choice = (request.form.get("lunch_choice") or "").strip().lower()  # "si" | "no"
        nota = request.form.get("nota") or None

        if lunch_choice not in ("si", "no"):
            flash("Debes indicar si hubo lunch (Sí/No).", "warning")
            return redirect(url_for("empleado_tarea", tid=tid))

        foto_path = None
        if lunch_choice == "si":
            foto_path = guardar_foto(request.files.get("foto"))
            if not foto_path:
                flash("Sube una foto del lunch (requerida si marcaste Sí).", "warning")
                return redirect(url_for("empleado_tarea", tid=tid))

        db.session.add(Reporte(
            tarea_id=tid, usuario_id=current_user.id, tipo="lunch",
            turno_num=turno_num,
            foto_path=foto_path, timestamp=ahora_utc_naive, valido=True,
            nota=(nota or f"lunch_{lunch_choice}")
        ))

        # (Opcional) Marcar en jornada una nota; el descuento 0.5h lo haces al calcular nómina.
        j = _jornada_get_or_create(tid, current_user.id, hoy_local, turno_num)
        if lunch_choice == "si":
            # Solo notas; el descuento real se aplica en cálculo de horas/pago
            j.nota = (j.nota or "")
            if "lunch_si" not in (j.nota or ""):
                j.nota = ((j.nota + " ").strip() + "lunch_si").strip()

        db.session.commit()
        flash("Lunch registrado.", "success")
        return redirect(url_for("empleado_tarea", tid=tid))

    # ---------- REPORTES DE VENTANA ----------
    if tipo == "ventana":
        orden = int(request.form.get("report_orden") or 0)
        v = ReportVentana.query.filter_by(tarea_id=tid, orden=orden).first()
        if not v:
            flash("Ventana inválida.", "danger")
            return redirect(url_for("empleado_tarea", tid=tid))

        # respeta ventana + tolerancia de la tarea
        if not dentro_ventana_reporte(tarea, v, ahora_loc):
            flash("Fuera de la ventana permitida para este reporte.", "warning")
            return redirect(url_for("empleado_tarea", tid=tid))

        # evita duplicado en franja (± tolerancia)
        tol = timedelta(minutes=tarea.tolerancia_min or 0)
        try:
            h1, m1 = map(int, (v.hora_ini or "00:00").split(":"))
            h2, m2 = map(int, (v.hora_fin or "23:59").split(":"))
        except Exception:
            h1, m1, h2, m2 = 0, 0, 23, 59

        ini_loc = ahora_loc.replace(hour=h1, minute=m1, second=0, microsecond=0) - tol
        fin_loc = ahora_loc.replace(hour=h2, minute=m2, second=0, microsecond=0) + tol
        ini_utc = _to_utc_naive(ini_loc)
        fin_utc = _to_utc_naive(fin_loc)

        ya = db.session.query(
            db.session.query(Reporte.id).filter(
                Reporte.tarea_id==tid, Reporte.usuario_id==current_user.id,
                Reporte.timestamp>=ini_utc, Reporte.timestamp<=fin_utc
            ).exists()
        ).scalar()
        if ya:
            flash("Ya enviaste el reporte de esta ventana.", "info")
            return redirect(url_for("empleado_tarea", tid=tid))

        gps  = request.form.get("gps") or None
        foto = guardar_foto(request.files.get("foto"))  # opcional
        nota = request.form.get("nota") or None

        db.session.add(Reporte(
            tarea_id=tid, usuario_id=current_user.id, tipo="otro",
            turno_num=turno_num,
            report_ventana_id=v.id,
            gps=gps, foto_path=foto, timestamp=ahora_utc_naive, valido=True, nota=nota
        ))

        db.session.commit()
        flash("Reporte enviado.", "success")
        return redirect(url_for("empleado_tarea", tid=tid))

    flash("Tipo de reporte inválido.", "danger")
    return redirect(url_for("empleado_tarea", tid=tid))

# ---------------------- ADMIN ----------------------


@app.route("/admin", endpoint="admin_system")
@login_required
@role_required("admin","superadmin")
def admin_system():
    # Solo staff en la tabla de "Usuarios del sistema"
    usuarios = (Usuario.query
                .filter(Usuario.rol.in_(["admin","superadmin","supervisor"]))
                .order_by(Usuario.nombre.asc())
                .all())

    # Empleados para la tabla de soporte (mismo admin.html)
    empleados = (Usuario.query
                 .filter(Usuario.rol == "empleado")
                 .order_by(Usuario.nombre.asc())
                 .all())

    # NO pases appcfg aquí; ya se inyecta con @app.context_processor
    return render_template("admin/admin.html", usuarios=usuarios, empleados=empleados)

# --- ADMIN USUARIOS / ROLES ---
# GET /admin/usuarios
@app.get("/admin/usuarios")
@login_required
@role_required("admin","superadmin")
def admin_usuarios():
    usuarios = (Usuario.query
        .filter(Usuario.rol.in_(["admin","superadmin","supervisor"]))  # 👈 filtro
        .order_by(Usuario.nombre.asc())
        .all())
    return render_template("admin/usuarios.html", usuarios=usuarios)

# POST /admin/usuarios/<uid>/credenciales
@app.post("/admin/usuarios/<int:uid>/credenciales")
@login_required
@role_required("superadmin")
def admin_usuarios_actualizar_credenciales(uid):
    u = Usuario.query.get_or_404(uid)

    email = (request.form.get("email") or "").strip() or None
    telefono = (request.form.get("telefono") or "").replace(" ", "").replace("-", "") or None
    password = request.form.get("password") or ""

    # Validación teléfono si viene
    if telefono:
        import re
        if not re.match(r"^\+(1|56|57)\d{7,14}$", telefono):
            flash("Teléfono inválido. Usa +1, +56 o +57 y solo dígitos.", "error")
            return redirect(url_for("admin_usuarios"))

    # Actualizaciones
    u.email = email
    u.telefono = telefono
    if password.strip():
        u.set_password(password.strip())   # asumiendo método set_password en tu modelo

    db.session.commit()
    flash("Acceso actualizado correctamente.", "success")
    return redirect(url_for("admin_usuarios"))


@app.post("/admin/usuarios/<int:uid>/editar-basico", endpoint="admin_usuarios_editar_basico")
@login_required
@role_required("admin","superadmin")
def admin_usuarios_editar_basico(uid):
    u = Usuario.query.get_or_404(uid)

    # Un admin NO puede editar a un superadmin
    if current_user.rol != "superadmin" and u.rol == "superadmin":
        flash("No puedes editar a un superadmin.", "danger")
        return redirect(url_for("admin_system"))

    # ---- Datos
    email_in = (request.form.get("email") or "").strip().lower() or None
    tel_raw  = (request.form.get("telefono") or "").strip() or None
    pw1      = request.form.get("password") or ""
    pw2      = request.form.get("password2") or ""

    # ---- Teléfono: normaliza + valida prefijo
    tel_norm = None
    if tel_raw:
        tel_norm = tel_raw.replace(" ", "").replace("-", "")
        if not tel_norm.startswith(("+1","+56","+57")) or not tel_norm[1:].isdigit():
            flash("Teléfono inválido. Usa prefijo +1, +56 o +57 y solo dígitos.", "warning")
            return redirect(url_for("admin_system"))

    # ---- Duplicados (excluyendo al mismo usuario)
    if email_in and Usuario.query.filter(Usuario.email == email_in, Usuario.id != u.id).first():
        flash("Ese correo ya está en uso por otro usuario.", "danger")
        return redirect(url_for("admin_system"))

    if tel_norm and Usuario.query.filter(Usuario.telefono == tel_norm, Usuario.id != u.id).first():
        flash("Ese teléfono ya está en uso por otro usuario.", "danger")
        return redirect(url_for("admin_system"))

    # ---- Aplicar cambios
    u.email    = email_in
    u.telefono = tel_norm

    # ---- Password (opcional)
    if pw1 or pw2:
        if pw1 != pw2:
            flash("Las contraseñas no coinciden.", "warning")
            return redirect(url_for("admin_system"))
        if len(pw1) < 6:
            flash("La contraseña debe tener al menos 6 caracteres.", "warning")
            return redirect(url_for("admin_system"))
        u.password = bcrypt.generate_password_hash(pw1).decode("utf-8")

    db.session.commit()
    flash("Usuario actualizado.", "success")
    return redirect(url_for("admin_system"))


# ====== EMPLEADOS (ADMIN) ======
from math import ceil
from sqlalchemy import or_, func

@app.get("/admin/empleados")
@login_required
@role_required("admin","superadmin")
def admin_empleados():
    """
    Lista de empleados (rol='empleado') con búsqueda y paginación.
    Parámetros opcionales:
      - q: texto libre (nombre/teléfono/email)
      - estado: all | activos | inactivos
      - page: 1..N
      - per_page: por defecto 12
    """
    q = (request.args.get("q") or "").strip().lower()
    estado = (request.args.get("estado") or "activos").lower()
    page = max(1, request.args.get("page", 1, type=int))
    per_page = request.args.get("per_page", 12, type=int)

    base = (Usuario.query
            .filter(Usuario.rol=="empleado")
            .order_by(func.lower(Usuario.nombre)))
    if q:
        like = f"%{q}%"
        base = base.filter(or_(
            func.lower(Usuario.nombre).like(like),
            func.lower(func.coalesce(Usuario.telefono,"")).like(like),
            func.lower(func.coalesce(Usuario.email,"")).like(like),
            func.lower(func.coalesce(Usuario.zelle_nombre,"")).like(like),
            func.lower(func.coalesce(Usuario.zelle_cuenta,"")).like(like),
        ))
    if estado == "activos":
        base = base.filter(Usuario.activo==True, Usuario.bloqueado==False, Usuario.baneado==False)
    elif estado == "inactivos":
        base = base.filter(or_(Usuario.activo==False, Usuario.bloqueado==True, Usuario.baneado==True))

    total = base.count()
    total_pages = max(1, ceil(total / per_page))
    if page > total_pages: page = total_pages
    rows = base.offset((page-1)*per_page).limit(per_page).all()

    return render_template(
        "admin/empleados.html",
        empleados=rows,
        q=q, estado=estado,
        page=page, per_page=per_page, total=total, total_pages=total_pages
    )

@app.get("/admin/empleados/<int:uid>/json")
@login_required
@role_required("admin","superadmin")
def admin_empleados_json(uid):
    """
    Devuelve los datos del empleado en JSON para precargar un modal.
    """
    u = Usuario.query.get_or_404(uid)
    if u.rol != "empleado":
        return jsonify(ok=False, error="No es un empleado"), 400
    data = dict(
        id=u.id,
        nombre=u.nombre or "",
        telefono=u.telefono or "",
        email=u.email or "",
        zelle_nombre=u.zelle_nombre or "",
        zelle_cuenta=u.zelle_cuenta or "",
        nombre_legal_firma=u.nombre_legal_firma or "",
        activo=bool(u.activo),
        bloqueado=bool(u.bloqueado),
        baneado=bool(u.baneado),
        motivo_bloqueo=u.motivo_bloqueo or "",
        motivo_baneo=u.motivo_baneo or ""
    )
    return jsonify(ok=True, empleado=data)

@app.post("/admin/empleados/<int:uid>/editar", endpoint="admin_empleados_editar")
@login_required
@role_required("admin","superadmin")
def admin_empleados_editar(uid):
    e = Usuario.query.get_or_404(uid)
    if e.rol != "empleado":
        flash("Solo se pueden editar usuarios con rol 'empleado'.", "warning")
        return redirect(url_for("admin_system") + "#empleadosSection")

    e.nombre   = (request.form.get("nombre") or "").strip()

    # Teléfono con prefijo obligatorio
    tel_raw = (request.form.get("telefono") or "").strip()
    if tel_raw:
        tel_norm = tel_raw.replace(" ", "").replace("-", "")
        if not tel_norm.startswith(("+1","+56","+57")) or not tel_norm[1:].replace("+","",1).isdigit():
            flash("Teléfono inválido. Usa prefijo +1, +56 o +57 y solo dígitos.", "warning")
            return redirect(url_for("admin_system") + "#empleadosSection")
        e.telefono = tel_norm
    else:
        e.telefono = None

    e.email    = (request.form.get("email") or "").strip() or None

    z_nom = (request.form.get("zelle_nombre") or "").strip() or None
    z_cta = (request.form.get("zelle_cuenta") or "").strip() or None
    if hasattr(e, "zelle_nombre"):  e.zelle_nombre  = z_nom
    if hasattr(e, "zelle_titular"): e.zelle_titular = z_nom
    if hasattr(e, "zelle_cuenta"):  e.zelle_cuenta  = z_cta

    confirmado = (request.form.get("nombre_confirmado") == "on")
    if hasattr(e, "nombre_confirmado"):   e.nombre_confirmado = bool(confirmado)
    if hasattr(e, "nombre_legal_firma"):  e.nombre_legal_firma = "CONFIRMADO" if confirmado else None

    db.session.commit()
    flash("Empleado actualizado.", "success")
    return redirect(url_for("admin_system") + "#empleadosSection")

# (Opcional) Crear empleado desde soporte (por si lo necesitas)
@app.post("/admin/empleados/crear")
@login_required
@role_required("admin","superadmin")
def admin_empleados_crear():
    nombre  = (request.form.get("nombre") or "").strip()
    tel     = (request.form.get("telefono") or "").strip() or None
    email   = (request.form.get("email") or "").strip() or None
    znombre = (request.form.get("zelle_nombre") or "").strip() or None
    zcuenta = (request.form.get("zelle_cuenta") or "").strip() or None
    firma   = (request.form.get("nombre_legal_firma") or "").strip() or None
    if not nombre:
        flash("El nombre es obligatorio.", "warning")
        return redirect(url_for("admin_empleados"))
    if tel and Usuario.query.filter_by(telefono=tel).first():
        flash("Ese teléfono ya está registrado.", "danger");  return redirect(url_for("admin_empleados"))
    if email and Usuario.query.filter_by(email=email).first():
        flash("Ese email ya está registrado.", "danger");     return redirect(url_for("admin_empleados"))

    u = Usuario(
        nombre=nombre, telefono=tel, email=email,
        zelle_nombre=znombre, zelle_cuenta=zcuenta,
        nombre_legal_firma=firma,
        rol="empleado", activo=True
    )
    db.session.add(u); db.session.commit()
    try:
        log_event("empleado_add", empleado_id=u.id, detalle={"via":"admin"})
    except Exception:
        pass
    flash("Empleado creado.", "success")
    return redirect(url_for("admin_empleados"))


from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

@app.post("/admin/usuarios/crear")
@login_required
@role_required("superadmin")
def admin_usuarios_crear():
    nombre   = (request.form.get("nombre") or "").strip()
    email    = (request.form.get("email") or "").strip().lower() or None
    telefono = (request.form.get("telefono") or "").strip() or None
    rol      = (request.form.get("rol") or "").strip().lower()
    raw_pw   = request.form.get("password") or ""

    if not nombre or not rol:
        flash("Nombre y rol son obligatorios.", "warning")
        return redirect(url_for("admin_usuarios"))

    # Normaliza y valida teléfono (prefijo obligatorio)
    if not telefono:
        flash("El teléfono es obligatorio.", "warning")
        return redirect(url_for("admin_usuarios"))
    tel_norm = telefono.replace(" ", "").replace("-", "")
    if not tel_norm.startswith(("+1","+56","+57")) or not tel_norm[1:].isdigit():
        flash("Teléfono inválido. Usa prefijo +1, +56 o +57 y solo dígitos.", "warning")
        return redirect(url_for("admin_usuarios"))
    telefono = tel_norm

    ROLES = {"admin","superadmin","supervisor","empleado"}
    if rol not in ROLES:
        flash("Rol inválido.", "danger")
        return redirect(url_for("admin_usuarios"))

    # Staff requiere contraseña; empleado NO (usa OTP)
    pw_hash = None
    if rol in {"admin","superadmin","supervisor"}:
        if len(raw_pw) < 6:
            flash("Para staff, la contraseña es obligatoria (mín. 6).", "warning")
            return redirect(url_for("admin_usuarios"))
        pw_hash = bcrypt.generate_password_hash(raw_pw).decode("utf-8")

    # Chequeo de duplicados por email y/o teléfono
    if email or telefono:
        dup = Usuario.query.filter(
            or_(
                Usuario.email == email if email else False,
                Usuario.telefono == telefono if telefono else False
            )
        ).first()
        if dup:
            if email and dup.email == email and telefono and dup.telefono == telefono:
                flash("Usuario existente con ese correo y ese número.", "danger")
            elif email and dup.email == email:
                flash("Usuario existente con ese correo.", "danger")
            elif telefono and dup.telefono == telefono:
                flash("Usuario existente con ese número.", "danger")
            else:
                flash("Usuario duplicado.", "danger")
            return redirect(url_for("admin_usuarios"))

    try:
        u = Usuario(
            nombre=nombre,
            email=email,            # None si vacío
            telefono=telefono,      # normalizado con prefijo
            rol=rol,
            password=pw_hash,       # tu modelo usa 'password'
            activo=True,
        )
        db.session.add(u)
        db.session.commit()
        flash(f"Usuario {nombre} ({rol}) creado.", "success")
    except IntegrityError:
        db.session.rollback()
        flash("Usuario existente con ese correo o ese número.", "danger")
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Fallo creando usuario")
        flash(f"Ocurrió un error al crear el usuario: {e}", "danger")

    return redirect(url_for("admin_usuarios"))



@app.post("/admin/usuarios/<int:uid>/cambiar-rol")
@login_required
@role_required("superadmin")  # 👈 solo superadmin puede tocar roles
def admin_usuarios_cambiar_rol(uid):
    u = Usuario.query.get_or_404(uid)
    nuevo = (request.form.get("rol") or "").strip().lower()
    if nuevo not in ("admin","superadmin","supervisor","empleado"):
        flash("Rol inválido.", "danger")
        return redirect(url_for("admin_usuarios"))
    u.rol = nuevo
    db.session.commit()
    flash("Rol actualizado.", "success")
    return redirect(url_for("admin_usuarios"))

@app.post("/admin/usuarios/<int:uid>/toggle-activo")
@login_required
@role_required("admin","superadmin")  # admin puede activar/desactivar, pero no cambiar rol
def admin_usuarios_toggle_activo(uid):
    u = Usuario.query.get_or_404(uid)
    # evita que un admin desactive a un superadmin
    if u.rol == "superadmin" and current_user.rol != "superadmin":
        flash("No puedes desactivar a un superadmin.", "danger")
        return redirect(url_for("admin_usuarios"))
    u.activo = not u.activo
    db.session.commit()
    flash("Estado actualizado.", "success")
    return redirect(url_for("admin_usuarios"))

@app.post("/admin/usuarios/<int:uid>/eliminar")
@login_required
@role_required("superadmin")  # 👈 solo superadmin elimina
def admin_usuarios_eliminar(uid):
    u = Usuario.query.get_or_404(uid)
    if u.id == current_user.id:
        flash("No puedes eliminarte a ti mismo.", "warning")
        return redirect(url_for("admin_usuarios"))
    if u.rol == "superadmin":
        flash("No puedes eliminar a otro superadmin.", "danger")
        return redirect(url_for("admin_usuarios"))
    db.session.delete(u)
    db.session.commit()
    flash("Usuario eliminado.", "warning")
    return redirect(url_for("admin_usuarios"))


# --- Configuración General ---
from services.config_service import read_config, write_config

@app.context_processor
def inject_app_config():
    try:
        cfg = read_config()
    except Exception:
        cfg = {}
    return dict(appcfg=cfg)


@app.get("/admin/config")
@login_required
@role_required("admin","superadmin")
def admin_config_get():
    cfg = read_config()
    return render_template("admin/config.html", cfg=cfg)  # opcional si quieres una página aparte

@app.post("/admin/config")
@login_required
@role_required("admin","superadmin")
def admin_config_post():
    cfg = read_config()

    # ===== Campos simples =====
    cfg["brand_name"]      = (request.form.get("brand_name") or "").strip() or cfg.get("brand_name") or "Fresh Labors"
    cfg["brand_footer"]    = (request.form.get("brand_footer") or "").strip()
    cfg["wa_sender"]       = (request.form.get("wa_sender") or "").strip()
    cfg["wa_prefix"]       = (request.form.get("wa_prefix") or "").strip()
    cfg["mail_from"]       = (request.form.get("mail_from") or "").strip()
    cfg["otp_enabled"]     = bool(request.form.get("otp_enabled"))
    cfg["otp_expiry_min"]  = int(request.form.get("otp_expiry_min") or cfg.get("otp_expiry_min") or 10)
    cfg["otp_resend_sec"]  = int(request.form.get("otp_resend_sec") or cfg.get("otp_resend_sec") or 30)
    cfg["otp_max_attempts"]= int(request.form.get("otp_max_attempts") or cfg.get("otp_max_attempts") or 5)
    cfg["max_upload_mb"]   = int(request.form.get("max_upload_mb") or cfg.get("max_upload_mb") or 8)
    cfg["max_colilla_mb"]  = int(request.form.get("max_colilla_mb") or cfg.get("max_colilla_mb") or 8)
    cfg["dir_colillas"]    = (request.form.get("dir_colillas") or cfg.get("dir_colillas") or "static/uploads/colillas").strip()
    cfg["dir_pagos"]       = (request.form.get("dir_pagos") or cfg.get("dir_pagos") or "static/uploads/pagos").strip()
    cfg["table_page_size"] = int(request.form.get("table_page_size") or cfg.get("table_page_size") or 12)
    cfg["theme_default"]   = (request.form.get("theme_default") or cfg.get("theme_default") or "auto")

    # ===== URL de logo (opcional) — solo se aplica si NO subes archivo y NO pides eliminar =====
    brand_logo_text = (request.form.get("brand_logo") or "").strip()

    # ===== Eliminar logo actual (opcional) =====
    if request.form.get("remove_logo"):
        old_logo = cfg.get("brand_logo")
        if old_logo and old_logo.startswith("logos/"):
            try:
                os.remove(os.path.join(app.root_path, "static", old_logo))
            except Exception:
                pass
        cfg["brand_logo"] = ""
        # Si también se cargó un archivo, a continuación lo sobreescribirá; si no, queda vacío.

    # ===== Logo archivo (opcional; TIENE PRIORIDAD) =====
    file = request.files.get("brand_logo_file")
    if file and file.filename:
        filename = secure_filename(file.filename)
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if ext in ALLOWED_LOGO_EXTS:
            stamp = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
            base  = secure_filename((cfg["brand_name"] or "app").lower().replace(" ", "_"))
            final = f"{base}_{stamp}.{ext}"
            path_abs = os.path.join(LOGOS_DIR, final)
            file.save(path_abs)
            cfg["brand_logo"] = f"logos/{final}"  # ruta relativa bajo /static
        else:
            flash("Formato de logo inválido. Usa png, jpg, jpeg, gif, webp o svg.", "warning")
            # Si el archivo no es válido y NO se marcó remove_logo, aplica la URL si existe:
            if not request.form.get("remove_logo") and brand_logo_text:
                cfg["brand_logo"] = brand_logo_text
    else:
        # Si NO subiste archivo y NO eliminaste, aplica lo que venga por URL (si viene algo)
        if not request.form.get("remove_logo") and brand_logo_text:
            cfg["brand_logo"] = brand_logo_text

    write_config(cfg)
    flash("Configuración guardada.", "success")
    return redirect(url_for("admin_system"))

@app.get("/admin/config.json")
@login_required
@role_required("admin","superadmin")
def admin_config_json():
    return jsonify(read_config())


# app.py (imports)
from datetime import datetime
from flask import send_file, request, flash, redirect, url_for, current_app
from services.respaldo import export_empleados_activos_csv_bytes, export_colillas_zip_bytes


@app.get("/admin/export/historial.csv")
@login_required
@role_required("admin","superadmin")
def export_historial_csv():
    rows = (db.session.query(HistEvento)
            .order_by(HistEvento.creado_en.desc()).limit(2000).all())
    buf = _io.StringIO(newline="")
    w = csv.writer(buf)
    w.writerow(["id","tipo","tarea_id","empleado_id","usuario_id","detalle","creado_en"])
    for e in rows:
        w.writerow([e.id, e.tipo, e.tarea_id or "", e.empleado_id or "", e.usuario_id or "",
                    json.dumps(e.detalle, ensure_ascii=False) if e.detalle else "", 
                    e.creado_en.isoformat()])
    mem = _io.BytesIO(buf.getvalue().encode("utf-8-sig")); mem.seek(0)
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name="historial.csv")

@app.get("/admin/estadisticas")
@login_required
@role_required("admin","superadmin")
def admin_estadisticas():
    hoy = datetime.utcnow().date()
    inicio_semana = hoy - timedelta(days=hoy.weekday())  # lunes
    fin_semana = inicio_semana + timedelta(days=6)

    # tareas semana
    tareas_semana = (Tarea.query
                     .filter(Tarea.creado_en >= datetime.combine(inicio_semana, time.min))
                     .count())

    # horas hechas (ya tienes helper horas_totales_semana)
    horas_semana = horas_totales_semana()

    # baneos/bloqueos actuales
    baneos = Usuario.query.filter_by(baneado=True).count()
    bloqueos = Usuario.query.filter_by(bloqueado=True).count()

    # faltas de confirmación hoy (ya inyectas no_confirmados_count)
    faltas_conf_hoy = inject_no_confirmados_count()["no_confirmados_count"]

    # sanciones (pendientes vs resueltas)
    sanciones_pend = Sancion.query.filter_by(resuelta=False).count()
    sanciones_semana = (Sancion.query
                        .filter(Sancion.creada_en >= datetime.combine(inicio_semana, time.min))
                        .count())

    return render_template("admin/estadisticas.html",
        rango=(inicio_semana, fin_semana),
        tarjetas=dict(
            tareas_semana=tareas_semana,
            horas_semana=horas_semana,
            baneos=baneos,
            bloqueos=bloqueos,
            faltas_conf_hoy=faltas_conf_hoy,
            sanciones_pend=sanciones_pend,
            sanciones_semana=sanciones_semana
        )
    )


def _pct(a, b): 
    b = int(b or 0)
    return 0 if b<=0 else round((int(a or 0)/b)*100, 1)

@app.get("/admin/reportes-usuario")
@login_required
@role_required("admin","superadmin","supervisor")
def admin_reportes_usuario():
    hoy = datetime.utcnow().date()
    start = hoy - timedelta(days=6)
    start_dt = datetime.combine(start, time.min)
    end_dt   = datetime.combine(hoy, time.max)

    total_reportes = Reporte.query.filter(Reporte.timestamp>=start_dt, Reporte.timestamp<=end_dt).count()
    total_usuarios = Usuario.query.filter_by(rol="empleado", activo=True).count()
    total_sanc = Sancion.query.filter(Sancion.creada_en>=start_dt, Sancion.creada_en<=end_dt).count()
    total_sanc_pend = Sancion.query.filter_by(resuelta=False).count()

    # faltas de confirmación hoy reusando helper
    faltas_conf_hoy = inject_no_confirmados_count()["no_confirmados_count"]

    # cuadros por tipo/nivel
    por_tipo = db.session.query(Sancion.tipo, func.count(Sancion.id)).group_by(Sancion.tipo).all()
    por_nivel = db.session.query(Sancion.nivel, func.count(Sancion.id)).group_by(Sancion.nivel).all()

    datos = dict(
        total_reportes=total_reportes,
        total_usuarios=total_usuarios,
        sanciones_semana=total_sanc,
        sanciones_pendientes=total_sanc_pend,
        faltas_conf_hoy=faltas_conf_hoy,
        pct_users_sancionados=_pct(total_sanc, total_usuarios),
        por_tipo={t or "otro": c for t,c in por_tipo},
        por_nivel={n or "warn": c for n,c in por_nivel},
    )
    return render_template("admin/reportes_usuario.html", datos=datos)

# ===================== ADMIN: Editar Reporte =====================
@app.get("/admin/reportes/<int:rid>/editar", endpoint="admin_reporte_editar")
@login_required
@role_required("admin","superadmin")
def admin_reporte_editar_get(rid):
    r = Reporte.query.get_or_404(rid)
    return render_template("admin/reporte_editar.html", r=r)

@app.post("/admin/reportes/<int:rid>/editar", endpoint="admin_reporte_editar_post")
@login_required
@role_required("admin","superadmin")
def admin_reporte_editar_post(rid):
    r = Reporte.query.get_or_404(rid)

    # Acepta multipart
    new_gps  = request.form.get("gps") or None
    new_nota = request.form.get("nota") or None
    new_tipo_especial = request.form.get("tipo_especial") or None
    new_tipo = request.form.get("tipo") or None
    new_ts   = request.form.get("timestamp") or None  # "YYYY-MM-DD HH:MM"

    foto = request.files.get("foto")
    if foto:
        new_foto = guardar_foto(foto)
        if new_foto:
            r.foto_path = new_foto

    if new_gps is not None: r.gps = new_gps
    if new_nota is not None: r.nota = new_nota
    if new_tipo_especial is not None: r.tipo_especial = (new_tipo_especial or None)
    if new_tipo:
        r.tipo = new_tipo

    if new_ts:
        try:
            # Se guarda como UTC naive, consistente con tu modelo
            dt = datetime.fromisoformat(new_ts)
            # si crees que lo pasan como hora local de la tarea podríamos convertir; por ahora lo tomamos literal
            r.timestamp = dt
        except Exception:
            flash("Fecha/hora inválida. Usa formato YYYY-MM-DD HH:MM", "warning")

    db.session.commit()
    flash("Reporte actualizado.", "success")
    return redirect(url_for("admin_reporte_editar", rid=rid))


@app.route("/admin/dashboard")
@login_required
@role_required("admin","superadmin")
def admin_dashboard():
    hoy = datetime.utcnow().date()
    tarjetas = dict(
        tareas_activas = Tarea.query.filter_by(activa=True).count(),
        informes_hoy   = Reporte.query.filter(func.date(Reporte.timestamp) == hoy).count(),
        personal_activo = (
            db.session.query(Asignacion.usuario_id)
                .join(Tarea, Asignacion.tarea_id == Tarea.id)
                .filter(Tarea.activa == True, Asignacion.estado == "activa")
                .distinct().count()
        ),
        horas_semanales = horas_totales_semana()
    )
    tareas = Tarea.query.filter_by(activa=True).order_by(Tarea.id.desc()).all()
    return render_template("admin/admin_dashboard.html", tarjetas=tarjetas, tareas=tareas)

# /admin/tareas  -> acepta ?estado=activa|inactiva&page=N
@app.route("/admin/tareas")
@login_required
@role_required("admin","superadmin")
def admin_tareas():
    estado = request.args.get("estado", "activa")
    page   = request.args.get("page", 1, type=int)
    per_page = 10  # <<--- AQUÍ fuerzas 10 por página

    q = Tarea.query
    if estado == "inactiva":
        q = q.filter_by(activa=False)
    else:
        estado = "activa"
        q = q.filter_by(activa=True)

    q = q.order_by(Tarea.id.desc())

    # Si usas Flask-SQLAlchemy >=3
    pagination = q.paginate(page=page, per_page=per_page, error_out=False)

    # Renderiza pasando SOLO los items de esta página,
    # y el objeto pagination para los enlaces.
    return render_template(
        "admin/tareas.html",
        tareas=pagination.items,
        pagination=pagination,
        estado=estado,
    )



@app.route("/admin/tarea/nueva")
@login_required
@role_required("admin","superadmin")
def admin_tarea_nueva():
    return render_template("admin/crear_tarea.html")

# --- Crear tarea (POST) ---
@app.route("/admin/tarea/crear", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def crear_tarea():
    from datetime import date
    import secrets

    # Campos base
    nombre_o_supervisor = (request.form.get("nombre") or "").strip()
    ubicacion = (request.form.get("ubicacion") or "").strip() or None
    prioridad = (request.form.get("prioridad") or "comun").lower()
    desc_form = (request.form.get("descripcion") or None)

    # Fecha/Hora de inicio
    fecha_inicio_s = request.form.get("fecha_inicio")
    hora_inicio = (request.form.get("hora_inicio") or "08:00").strip()
    if not fecha_inicio_s:
        flash("Debes establecer la fecha de inicio.", "warning")
        return redirect(url_for("admin_tarea_nueva"))
    try:
        fi = date.fromisoformat(fecha_inicio_s)
    except ValueError:
        flash("Fecha de inicio inválida.", "warning")
        return redirect(url_for("admin_tarea_nueva"))

    # Tolerancia
    try:
        tolerancia_min = int(request.form.get("tolerancia_min", 15))
    except Exception:
        tolerancia_min = 15

    # TZ
    tz_name = (request.form.get("tz_name") or "").strip() or "America/New_York"

    # Cantidad de reportes (requerido)
    try:
        cant_rep = int(request.form.get("cantidad_reportes") or 0)
    except Exception:
        cant_rep = 0
    if cant_rep <= 0:
        flash("Debes indicar la cantidad de reportes que tendrá la tarea.", "warning")
        return redirect(url_for("admin_tarea_nueva"))

    # Nombre autogenerado si vacío en EMERGENCIA
    if prioridad == "emergencia" and not nombre_o_supervisor:
        nombre_o_supervisor = f"ID{secrets.token_hex(3).upper()}"

    # Crear Tarea
    t = Tarea(
        nombre=nombre_o_supervisor,
        descripcion=desc_form,
        exigencias_text=desc_form,
        ubicacion=ubicacion,
        prioridad=prioridad,
        activa=True,
        fecha_inicio=fi,
        cantidad_reportes=cant_rep,
        hora_inicio=hora_inicio,
        tolerancia_min=tolerancia_min,
        tz_name=tz_name,
        requiere_asistencia=True,
        requiere_lunch=True
    )
    db.session.add(t)
    db.session.flush()  # necesito el id

    # Ventanas de reportes
    for i in range(1, cant_rep + 1):
        nombre_rep = (request.form.get(f"rep{i}_nombre") or f"Reporte {i}").strip()
        h_ini = (request.form.get(f"rep{i}_ini") or None)
        h_fin = (request.form.get(f"rep{i}_fin") or None)
        db.session.add(ReportVentana(
            tarea_id=t.id,
            orden=i,
            nombre=nombre_rep,
            con_horario=True if (h_ini or h_fin) else False,
            hora_ini=h_ini,
            hora_fin=h_fin
        ))

    # ----------------- TURNO: SOLO si corresponde -----------------
    # Necesitamos que el front nos mande "emerg_es_turnos=1" cuando el switch esté activo
    emerg_flag = (request.form.get("emerg_es_turnos") or "").strip().lower() in ("1","true","on","si","sí")
    allow_turnos = (prioridad == "turnos") or (prioridad == "emergencia" and emerg_flag)

    if allow_turnos:
        n_turnos_raw = request.form.get("num_turnos")
        n = 0
        if n_turnos_raw not in (None, ""):
            try:
                n = max(1, min(int(n_turnos_raw), 24))
            except Exception:
                n = 0

        if n > 0:
            # Tomo horas manuales si vienen; si no, reparto uniforme
            has_manual = any((request.form.get(f"turno_{i}_ini") or request.form.get(f"turno_{i}_fin")) for i in range(1, n+1))
            if has_manual:
                for i in range(1, n + 1):
                    ini = request.form.get(f"turno_{i}_ini") or None
                    fin = request.form.get(f"turno_{i}_fin") or None
                    db.session.add(Turno(tarea_id=t.id, numero=i, hora_inicio=ini, hora_fin=fin))
            else:
                # reparto uniforme en 24h empezando en hora_inicio base
                try:
                    h0, m0 = map(int, (hora_inicio or "08:00").split(":"))
                    start_min = h0*60 + m0
                except Exception:
                    start_min = 8*60
                dur = max(60, (24*60)//n)
                for i in range(n):
                    ini_min = start_min + i*dur
                    fin_min = start_min + (i+1)*dur
                    ini = f"{(ini_min//60)%24:02d}:{ini_min%60:02d}"
                    fin = f"{(fin_min//60)%24:02d}:{(fin_min%60):02d}"
                    db.session.add(Turno(tarea_id=t.id, numero=i+1, hora_inicio=ini, hora_fin=fin))
    # ----------------------------------------------------------------

    db.session.commit()
    
    try:
        log_event("tarea_create", tarea_id=t.id, detalle={
            "prioridad": prioridad,
            "cantidad_reportes": cant_rep,
            "hora_inicio": hora_inicio,
            "tolerancia_min": tolerancia_min,
            "tz_name": tz_name,
            "fecha_inicio": fi.isoformat(),
            "requiere_asistencia": True,
            "requiere_lunch": True,
            "turnos_creados": len(getattr(t, "turnos", []) or [])
        })
    except Exception:
        pass

    # ---- SYNC overrides después de crear tarea/turnos/reportes ----
    try:
        from services.asign_sync import sync_asignaciones_entrada
        sync_asignaciones_entrada(db, Tarea, ReportVentana, Asignacion, Turno,
                                tarea_id=t.id, force=False)
    except Exception:
        current_app.logger.exception("sync_asignaciones_entrada post-crear")

    flash("Tarea creada correctamente.", "success")
    return redirect(url_for("admin_tareas"))


# Detalle + edición rápida
# Detalle + edición rápida
@app.route("/admin/tarea/<int:tid>", methods=["GET","POST"])
@login_required
@role_required("admin","superadmin")
def tarea_detalle(tid):
    t = Tarea.query.get_or_404(tid)

    # --- GET JSON para llenar el modal de editar (AJAX) ---
    if request.method == 'GET' and request.headers.get('X-Requested-With') == 'XMLHttpRequest' \
       and request.query_string and request.args.get('format') == 'json':
        data = dict(
            id=t.id,
            nombre=t.nombre,
            descripcion=t.descripcion,
            ubicacion=t.ubicacion,
            prioridad=t.prioridad,
            fecha_inicio=(t.fecha_inicio.isoformat() if t.fecha_inicio else None),
            hora_inicio=t.hora_inicio,
            tolerancia_min=t.tolerancia_min,
            tz_name=t.tz_name,
            turnos=[{"ini": x.hora_inicio, "fin": x.hora_fin} for x in (t.turnos or [])],
            reportes=[{"nombre": v.nombre, "ini": v.hora_ini, "fin": v.hora_fin} for v in (t.ventanas or [])],
            # opcionales
            hora_lunch=getattr(t, "hora_lunch", None),
            hora_salida=getattr(t, "hora_salida", None),
            requiere_asistencia=getattr(t, "requiere_asistencia", None),
            requiere_lunch=getattr(t, "requiere_lunch", None),
            requiere_salida=getattr(t, "requiere_salida", None),
            exigencias_text=getattr(t, "exigencias_text", None),
        )
        return jsonify(ok=True, data=data)

    # --- POST: editar y registrar historial ---
    if request.method == "POST":
        # 1) Prev state
        prev = dict(
            nombre=t.nombre,
            descripcion=t.descripcion,
            ubicacion=t.ubicacion,
            prioridad=t.prioridad,
            fecha_inicio=(t.fecha_inicio.isoformat() if t.fecha_inicio else None),
            hora_inicio=t.hora_inicio,
            tolerancia_min=t.tolerancia_min,
            tz_name=t.tz_name,
            hora_lunch=getattr(t, "hora_lunch", None),
            hora_salida=getattr(t, "hora_salida", None),
            requiere_asistencia=getattr(t, "requiere_asistencia", None),
            requiere_lunch=getattr(t, "requiere_lunch", None),
            requiere_salida=getattr(t, "requiere_salida", None),
            exigencias_text=getattr(t, "exigencias_text", None),
            turnos=len(t.turnos or []),
            reportes=len(t.ventanas or []),
        )

        # 2) Cambios base
        t.nombre = (request.form.get("nombre") or t.nombre or "").strip()
        t.descripcion = (request.form.get("descripcion") or None)
        t.ubicacion = (request.form.get("ubicacion") or "").strip() or None
        t.prioridad = (request.form.get("prioridad") or t.prioridad or "comun").lower()

        fi_raw = request.form.get("fecha_inicio")
        t.fecha_inicio = date.fromisoformat(fi_raw) if fi_raw else t.fecha_inicio

        t.hora_inicio = request.form.get("hora_inicio") or t.hora_inicio
        if request.form.get("tolerancia_min") not in (None, ""):
            try:
                t.tolerancia_min = int(request.form.get("tolerancia_min"))
            except Exception:
                pass

        tz_in = (request.form.get("tz_name") or "").strip()
        t.tz_name = tz_in or t.tz_name

        # 3) Turnos (opcional)
        if request.form.get("num_turnos") not in (None, ""):
            try:
                n = max(1, min(int(request.form.get("num_turnos")), 24))
            except Exception:
                n = 0
            if n > 0:
                Turno.query.filter_by(tarea_id=t.id).delete()
                for i in range(1, n + 1):
                    ini = request.form.get(f"turno_{i}_ini") or None
                    fin = request.form.get(f"turno_{i}_fin") or None
                    db.session.add(Turno(tarea_id=t.id, numero=i, hora_inicio=ini, hora_fin=fin))

        # 4) Ventanas de reportes (opcional)
        cantidad_reportes = request.form.get("cantidad_reportes")
        if cantidad_reportes not in (None, ""):
            ReportVentana.query.filter_by(tarea_id=t.id).delete()
            try:
                cnt = int(cantidad_reportes or 0)
            except Exception:
                cnt = 0
            cnt = max(0, min(cnt, 10))
            for i in range(1, cnt + 1):
                nombre_rep = (request.form.get(f"rep{i}_nombre") or f"Reporte {i}").strip()
                h_ini = request.form.get(f"rep{i}_ini") or None
                h_fin = request.form.get(f"rep{i}_fin") or None
                db.session.add(ReportVentana(
                    tarea_id=t.id, orden=i, nombre=nombre_rep,
                    con_horario=True if (h_ini or h_fin) else False,
                    hora_ini=h_ini, hora_fin=h_fin
                ))

        db.session.commit()

        # >>>>>>>>>>>> BLOQUE NUEVO: SYNC overrides tras editar <<<<<<<<<<<<
        try:
            from services.asign_sync import sync_asignaciones_entrada
            # force=False para no pisar manuales; cambia a True si quieres sobreescribir siempre
            sync_asignaciones_entrada(db, Tarea, ReportVentana, Asignacion, Turno,
                                      tarea_id=t.id, force=False)
        except Exception:
            current_app.logger.exception("sync_asignaciones_entrada post-editar")
        # >>>>>>>>>>>> FIN BLOQUE NUEVO <<<<<<<<<<<<

        # 5) now_state + diff
        now_state = dict(
            nombre=t.nombre,
            descripcion=t.descripcion,
            ubicacion=t.ubicacion,
            prioridad=t.prioridad,
            fecha_inicio=(t.fecha_inicio.isoformat() if t.fecha_inicio else None),
            hora_inicio=t.hora_inicio,
            hora_lunch=getattr(t, "hora_lunch", None),
            hora_salida=getattr(t, "hora_salida", None),
            tolerancia_min=t.tolerancia_min,
            tz_name=t.tz_name,
            requiere_asistencia=getattr(t, "requiere_asistencia", None),
            requiere_lunch=getattr(t, "requiere_lunch", None),
            requiere_salida=getattr(t, "requiere_salida", None),
            exigencias_text=getattr(t, "exigencias_text", None),
            turnos=len(t.turnos or []),
            reportes=len(t.ventanas or []),
        )

        cambios = {}
        for k, v_prev in prev.items():
            v_new = now_state.get(k)
            if v_prev != v_new:
                cambios[k] = {"prev": v_prev, "new": v_new}
        if cambios:
            try:
                log_event("tarea_edit", tarea_id=t.id, detalle={"fields": cambios})
            except Exception:
                pass

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=True)

        flash("Cambios guardados.", "success")
        return redirect(url_for("tarea_detalle", tid=tid))

    # GET normal
    return render_template("admin/tarea_detalle.html", t=t)


@app.route("/admin/tarea/<int:tid>/eliminar", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def eliminar_tarea(tid):
    t = Tarea.query.get_or_404(tid)
    nombre = t.nombre
    try:
        # 🔒 Defensa: eliminar hijos explícitamente (por si el cascade se pierde)
        Turno.query.filter_by(tarea_id=t.id).delete(synchronize_session=False)
        ReportVentana.query.filter_by(tarea_id=t.id).delete(synchronize_session=False)
        Asignacion.query.filter_by(tarea_id=t.id).delete(synchronize_session=False)

        db.session.delete(t)   # con el cascade en el backref también funciona sin las 3 líneas de arriba
        db.session.commit()

        try:
            log_event("tarea_eliminada", tarea_id=tid, detalle={"nombre": nombre})
        except Exception:
            pass

        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=True)
        flash("Tarea eliminada.", "success")

    except Exception as e:
        db.session.rollback()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify(ok=False, error=str(e)), 500
        flash("No se pudo eliminar la tarea.", "danger")

    return redirect(url_for('admin_tareas'))


# ---- Establecer "hora de salida" (crea Reporte tipo 'salida') ----
@app.route("/admin/tarea/<int:tid>/salida/persona/<int:uid>", methods=["POST"])
@login_required
@role_required("admin","superadmin","supervisor")
def admin_set_salida_persona(tid, uid):
    """
    Body JSON: { "fecha": "YYYY-MM-DD", "hora": "HH:MM" }
    Crea (o reemplaza) un reporte 'salida' para ese usuario/tarea en la fecha dada.
    """
    data = request.get_json(silent=True) or {}
    fecha_s = (data.get("fecha") or "").strip()
    hora_s  = (data.get("hora") or "").strip()
    try:
        y,m,d = map(int, fecha_s.split("-"))
        hh,mm = map(int, (hora_s or "18:00").split(":"))
        ts = datetime(y, m, d, hh, mm, 0)
    except Exception:
        return jsonify(ok=False, error="Fecha/hora inválidas"), 400

    # Opcional: borra salida anterior del mismo día para ese usuario/tarea
    start = datetime(ts.year, ts.month, ts.day, 0, 0, 0)
    end   = datetime(ts.year, ts.month, ts.day, 23, 59, 59)
    Reporte.query.filter(
        Reporte.tarea_id==tid, Reporte.usuario_id==uid,
        Reporte.tipo=="salida",
        Reporte.timestamp>=start, Reporte.timestamp<=end
    ).delete()

    r = Reporte(tarea_id=tid, usuario_id=uid, tipo="salida", timestamp=ts, valido=True)
    db.session.add(r); db.session.commit()
    return jsonify(ok=True)

@app.route("/admin/tarea/<int:tid>/salida/todos", methods=["POST"])
@login_required
@role_required("admin","superadmin","supervisor")
def admin_set_salida_todos(tid):
    """
    Body JSON: { "fecha": "YYYY-MM-DD", "hora": "HH:MM" }
    Crea 'salida' para TODOS los empleados con asignación activa a la tarea.
    """
    data = request.get_json(silent=True) or {}
    fecha_s = (data.get("fecha") or "").strip()
    hora_s  = (data.get("hora") or "").strip()
    try:
        y,m,d = map(int, fecha_s.split("-"))
        hh,mm = map(int, (hora_s or "18:00").split(":"))
        ts = datetime(y, m, d, hh, mm, 0)
    except Exception:
        return jsonify(ok=False, error="Fecha/hora inválidas"), 400

    asigns = (Asignacion.query
              .join(Usuario, Usuario.id == Asignacion.usuario_id)
              .filter(Asignacion.tarea_id==tid, Asignacion.estado=="activa", Usuario.activo==True)
              .all())

    start = datetime(ts.year, ts.month, ts.day, 0, 0, 0)
    end   = datetime(ts.year, ts.month, ts.day, 23, 59, 59)

    for a in asigns:
        Reporte.query.filter(
            Reporte.tarea_id==tid, Reporte.usuario_id==a.usuario_id,
            Reporte.tipo=="salida",
            Reporte.timestamp>=start, Reporte.timestamp<=end
        ).delete()
        db.session.add(Reporte(tarea_id=tid, usuario_id=a.usuario_id, tipo="salida", timestamp=ts, valido=True))
    db.session.commit()
    return jsonify(ok=True)

@app.route("/admin/tarea/<int:tid>/override/<int:aid>", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def override_horas(tid, aid):
    a = Asignacion.query.get_or_404(aid)
    prev = dict(
        hora_inicio=a.hora_inicio_override,
        tolerancia=a.tolerancia_override,
    )
    a.hora_inicio_override = request.form.get("hora_inicio") or None
    tol = request.form.get("tolerancia")
    a.tolerancia_override  = int(tol) if tol else None
    db.session.commit()
    try:
        log_event("asign_override", tarea_id=tid, empleado_id=a.usuario_id, detalle={
            "prev": prev,
            "new": dict(
                hora_inicio=a.hora_inicio_override,
                tolerancia=a.tolerancia_override
            )
        })
    except Exception:
        pass
    flash("Override guardado.", "success")
    return redirect(url_for("tarea_detalle", tid=tid))


@app.route("/admin/tarea/<int:tid>/desasignar/<int:aid>", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def desasignar_empleado(tid, aid):
    a = Asignacion.query.get_or_404(aid)
    uid = a.usuario_id
    db.session.delete(a)
    db.session.commit()
    try:
        log_event("empleado_remove", tarea_id=tid, empleado_id=uid)
    except Exception:
        pass
    flash("Empleado desasignado.", "info")
    return redirect(url_for("tarea_detalle", tid=tid))

@app.route("/admin/tarea/<int:tid>/toggle", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def toggle_tarea(tid):
    t = Tarea.query.get_or_404(tid)
    t.activa = not t.activa
    db.session.commit()
    try:
        log_event("tarea_estado", tarea_id=tid, detalle={"activa": bool(t.activa)})
    except Exception:
        pass
    flash("Estado de tarea actualizado.", "success")
    return redirect(url_for("tarea_detalle", tid=tid))

@app.route("/admin/reports")
@login_required
@role_required("admin","superadmin","supervisor")
def admin_reports():
    f_fecha = request.args.get("fecha")   # YYYY-MM-DD
    f_tarea = request.args.get("tarea_id")
    f_user  = request.args.get("usuario_id")
    q = Reporte.query
    if f_fecha:
        y,m,d = map(int, f_fecha.split("-"))
        start = datetime(y,m,d,0,0,0)
        end   = datetime(y,m,d,23,59,59)
        q = q.filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
    if f_tarea:
        q = q.filter_by(tarea_id=int(f_tarea))
    if f_user:
        q = q.filter_by(usuario_id=int(f_user))
    reportes = q.order_by(Reporte.timestamp.desc()).limit(200).all()
    tareas = Tarea.query.order_by(Tarea.nombre.asc()).all()
    usuarios = Usuario.query.filter_by(rol="empleado").order_by(Usuario.nombre.asc()).all()
    return render_template("admin/reports.html", reportes=reportes, tareas=tareas, usuarios=usuarios)

from math import ceil
from datetime import datetime, date, timedelta
from flask import render_template, request
from flask_login import login_required
from services.horas import compute_range, compute_employee_detail, week_bounds_fri_thu

@app.route("/admin/horas")
@login_required
@role_required("admin","superadmin")
def admin_horas():
    vista = (request.args.get("vista") or "resumen").strip().lower()
    if vista not in ("resumen", "tabla"):
        vista = "resumen"

    usuario_id = request.args.get("usuario_id", type=int)
    tarea_id   = request.args.get("tarea_id", type=int)

    q       = (request.args.get("q") or "").strip().lower()
    estado  = (request.args.get("estado") or "").strip().lower()
    fecha_s = (request.args.get("fecha") or "").strip()

    if fecha_s:
        try:
            any_day = datetime.fromisoformat(fecha_s).date()
        except Exception:
            any_day = date.today()
    else:
        any_day = date.today()

    d_from, d_to = week_bounds_fri_thu(any_day)

    kpis, cards_all, rows_all, grupos_all = compute_range(d_from, d_to, usuario_id, tarea_id)

    per_page = request.args.get("per_page", type=int) or 10
    page = max(1, request.args.get("page", 1, type=int))
    total_items = total_pages = shown_count = 0
    cards_table, rows_by_emp, tasks_by_emp = [], {}, {}

    if vista == "tabla":
        def emp_match_q(c):
            hay = f"{c.get('nombre','')} {c.get('identificacion','')} {(c.get('telefono') or '')}".lower()
            return (not q) or (q in hay)
        def emp_match_estado(c):
            if not estado: return True
            pct = int(c.get("reportes_pct") or 0)
            if estado == "completado": return pct == 100
            if estado == "incompleto": return 0 < pct < 100
            if estado == "sin":        return pct == 0
            return True

        filtered_cards = [c for c in cards_all if emp_match_q(c) and emp_match_estado(c)]
        filtered_cards.sort(key=lambda c: (-float(c.get("horas", 0) or 0.0), c.get("nombre","").lower()))

        total_items = len(filtered_cards)
        total_pages = max(1, ceil(total_items / per_page))
        if page > total_pages: page = total_pages
        start = (page - 1) * per_page
        end   = start + per_page
        cards_table = filtered_cards[start:end]
        shown_count = len(cards_table)

        page_emp_ids = {c["usuario_id"] for c in cards_table}
        rows_page = [r for r in rows_all if r["usuario_id"] in page_emp_ids]
        rows_page.sort(key=lambda r: (r["usuario"].lower(), r["tarea"].lower(), r["fecha"]))

        for r in rows_page:
            uid = r["usuario_id"]
            rows_by_emp.setdefault(uid, []).append(r)
            tkey = (r["tarea_id"], r["tarea"])
            tmap = tasks_by_emp.setdefault(uid, {})
            node = tmap.setdefault(tkey, {"tarea_id": r["tarea_id"], "tarea": r["tarea"], "horas": 0.0, "total": 0.0})
            node["horas"] += float(r.get("horas") or 0.0)
            node["total"] += float(r.get("total") or 0.0)
        for uid, tmap in tasks_by_emp.items():
            tasks_by_emp[uid] = sorted(tmap.values(), key=lambda x: x["tarea"].lower())

    return render_template(
        "admin/horas.html",
        vista=vista,
        d_from=d_from, d_to=d_to,
        kpis=kpis,
        cards=cards_all,
        grupos=grupos_all,
        cards_table=cards_table,
        rows_by_emp=rows_by_emp,
        tasks_by_emp=tasks_by_emp,
        per_page=per_page, page=page, total_items=total_items, total_pages=total_pages, shown_count=shown_count,
        q=q, estado=estado, fecha=fecha_s,
        usuario_id=usuario_id, tarea_id=tarea_id
    )

@app.route("/admin/horas/detalle/<int:uid>")
@login_required
@role_required("admin","superadmin")
def admin_horas_detalle(uid):
    fecha_str = (request.args.get("fecha") or "").strip()
    hoy = date.today()
    try:
        d = datetime.strptime(fecha_str, "%Y-%m-%d").date() if fecha_str else hoy
    except ValueError:
        d = hoy
    data = compute_employee_detail(uid, d, d)
    if not data: abort(404)
    return render_template("admin/_horas_detalle_modal.html", **data, d_from=d, d_to=d)

# Cuando se cree Reporte(tipo='asistencia'|'salida'), puedes llamar:
from services.horas_turno import registrar_inicio, registrar_salida, _get_task_tz

def _on_reporte_creado(reporte):
    M = app.config["MODELS"]; Tarea = M["Tarea"]
    t = Tarea.query.get(reporte.tarea_id)
    if not t:
        return
    ts = reporte.timestamp               # correcto
    fecha_local = ts.date()              # si _get_task_tz maneja tz, puedes convertir aquí
    hhmm = ts.strftime("%H:%M")

    tpo = (reporte.tipo or "").lower()
    if tpo == "asistencia":
        registrar_inicio(t, reporte.usuario_id, fecha_local, hhmm, fuente="reporte")
    elif tpo == "salida":
        registrar_salida(t, reporte.usuario_id, fecha_local, hhmm, fuente="reporte")

    db.session.commit()


# === ENDPOINT: snapshot para el modal de Jornada por tarea/fecha =============
from datetime import date, datetime
from flask import request, jsonify, abort
from services.horas_turno import collect_task_day_snapshot, registrar_inicio, registrar_salida, _models, _get_task_tz

@app.get("/admin/tarea/<int:tid>/jornada/modal-data", endpoint="admin_tarea_jornada_modal_data")
def admin_tarea_jornada_modal_data(tid: int):
    """Devuelve JSON para poblar el modal 'HORA SALIDA' de una tarea y fecha dada."""
    q_fecha = (request.args.get("fecha") or "").strip()
    try:
        fecha = date.fromisoformat(q_fecha) if q_fecha else date.today()
    except Exception:
        return jsonify(ok=False, error="Fecha inválida"), 400

    try:
        data = collect_task_day_snapshot(tid, fecha)
        return jsonify(ok=True, data=data)
    except Exception as e:
        app.logger.exception("Error modal-data jornada")
        return jsonify(ok=False, error=str(e)), 500


# === ENDPOINT: registrar/actualizar jornada (individual) =====================
@app.post("/admin/jornada/registrar", endpoint="admin_jornada_registrar")
def admin_jornada_registrar():
    """
    Body JSON:
    {
      "tarea_id": 3,
      "usuario_id": 7,
      "fecha": "2025-10-15",
      "turno_num": 1,                # opcional (default 1)
      "hora_inicio": "07:30",        # opcional
      "hora_salida": "17:00",        # opcional
      "fuente": "manual",            # opcional
      "nota": "Ajuste por admin"     # opcional
    }
    """
    M = app.config["MODELS"]
    Tarea = M["Tarea"]

    js = request.get_json(silent=True) or {}
    try:
        tarea_id   = int(js.get("tarea_id"))
        usuario_id = int(js.get("usuario_id"))
    except Exception:
        return jsonify(ok=False, error="tarea_id/usuario_id inválidos"), 400

    try:
        fecha = date.fromisoformat((js.get("fecha") or "").strip() or date.today().isoformat())
    except Exception:
        return jsonify(ok=False, error="Fecha inválida"), 400

    # 🔹 Si el front no envía turno explícito, usa el turno real de la asignación
    asign = app.config["MODELS"]["Asignacion"].query.filter_by(
        tarea_id=tarea_id, usuario_id=usuario_id, estado="activa"
    ).first()
    turno_num = int(js.get("turno_num") or getattr(asign, "turno_num", 1) or 1)

    fuente    = (js.get("fuente") or "manual").strip()[:20]
    nota      = (js.get("nota") or "").strip()[:255]
    h_ini     = (js.get("hora_inicio") or "").strip()   # "HH:MM"
    h_sal     = (js.get("hora_salida") or "").strip()

    t = Tarea.query.get_or_404(tarea_id)

    try:
        j = None
        if h_ini:
            j = registrar_inicio(t, usuario_id, fecha, h_ini, fuente=fuente, turno_num=turno_num)
        if h_sal:
            j = registrar_salida(t, usuario_id, fecha, h_sal, fuente=fuente, turno_num=turno_num)
        if not (h_ini or h_sal):
            return jsonify(ok=False, error="Debe enviar hora_inicio y/o hora_salida"), 400

        if j is not None and nota:
            j.nota = nota

        db.session.commit()
        return jsonify(ok=True)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error registrar jornada")
        return jsonify(ok=False, error=str(e)), 500


# === ENDPOINT: registrar en masa (todos los asignados activos de la tarea) ===
@app.post("/admin/jornada/registrar-todos", endpoint="admin_jornada_registrar_todos")
def admin_jornada_registrar_todos():
    """
    Body JSON:
    {
      "tarea_id": 3,
      "fecha": "2025-10-15",
      "turno_num": 1,             # opcional
      "hora_inicio": null,        # opcional; si viene se aplica a todos
      "hora_salida": "17:00",     # opcional; si viene se aplica a todos
      "fuente": "manual",         # opcional
      "nota": "Salida general"    # opcional
    }
    """
    js = request.get_json(silent=True) or {}
    M = app.config["MODELS"]
    Tarea, Asignacion = M["Tarea"], M["Asignacion"]

    try:
        tarea_id = int(js.get("tarea_id"))
    except Exception:
        return jsonify(ok=False, error="tarea_id inválido"), 400

    try:
        fecha = date.fromisoformat((js.get("fecha") or "").strip() or date.today().isoformat())
    except Exception:
        return jsonify(ok=False, error="Fecha inválida"), 400

    turno_num = int(js.get("turno_num") or 1)
    fuente    = (js.get("fuente") or "manual").strip()[:20]
    nota      = (js.get("nota") or "").strip()[:255]
    h_ini     = (js.get("hora_inicio") or "").strip() or None
    h_sal     = (js.get("hora_salida") or "").strip() or None

    if not (h_ini or h_sal):
        return jsonify(ok=False, error="Debe enviar hora_inicio y/o hora_salida"), 400

    t = Tarea.query.get_or_404(tarea_id)

    try:
        asigns = (Asignacion.query
          .filter(Asignacion.tarea_id==tarea_id)
          .all())

        count = 0
        for a in asigns:
            # 🔹 Usar el turno real del empleado (de la tabla Asignacion)
            turno_real = getattr(a, "turno_num", None) or turno_num or 1

            j = None
            if h_ini:
                j = registrar_inicio(t, a.usuario_id, fecha, h_ini, fuente=fuente, turno_num=turno_real)
            if h_sal:
                j = registrar_salida(t, a.usuario_id, fecha, h_sal, fuente=fuente, turno_num=turno_real)
            if j and nota:
                j.nota = nota
            count += 1


        db.session.commit()
        return jsonify(ok=True, afectados=count)
    except Exception as e:
        db.session.rollback()
        app.logger.exception("Error registrar-todos jornada")
        return jsonify(ok=False, error=str(e)), 500



from services.export_us import export_period_us

# --- EXPORT HORAS (US) ---
from flask import send_file, request, jsonify
from datetime import date, timedelta
import os

def _snap_to_friday(d: date) -> date:
    # 0=Mon..6=Sun; viernes es weekday=4
    delta = (d.weekday() - 4) % 7
    return d - timedelta(days=delta)

@app.route("/admin/horas/export_us")
@login_required
@role_required("admin","superadmin")
def admin_export_us():
    """
    Exporta horas por semana (viernes→jueves) o por mes.
    Query params:
      - period: 'week' | 'month'
      - start:  YYYY-MM-DD (para week se ancla al viernes en servidor)
      - otm:    multiplicador overtime (float, opcional)
      - inc_tasks_inactive=1 / inc_people_inactive=1
    """
    period = (request.args.get("period") or "week").lower().strip()
    if period not in ("week", "month"):
        return jsonify(ok=False, error="period inválido: usa 'week' o 'month'"), 400

    start_arg = (request.args.get("start") or "").strip()
    if start_arg:
        try:
            any_date = date.fromisoformat(start_arg)
        except ValueError:
            return jsonify(ok=False, error="start inválido (usa YYYY-MM-DD)"), 400
    else:
        # si no vino start, usa hoy (comportamiento legacy)
        any_date = date.today()

    # Seguridad: el server también ancla los viernes
    if period == "week":
        any_date = _snap_to_friday(any_date)

    otm = request.args.get("otm", type=float) or 1.0
    include_inactive_tasks  = request.args.get("inc_tasks_inactive") == "1"
    include_inactive_people = request.args.get("inc_people_inactive") == "1"

    from services.export_us import export_period_us
    out, start, end = export_period_us(
        app, any_date,
        period=period,
        ot_multiplier=otm,
        include_inactive_tasks=include_inactive_tasks,
        include_inactive_people=include_inactive_people
    )
    return send_file(out, as_attachment=True, download_name=os.path.basename(out))

#--- EDITAR Y ARREGLAR TEMAS DE HORAS CON LOS EMPLEADOS---
# ====== EDITOR DE HORAS POR DÍA (admin) ======
from datetime import datetime, date, time, timedelta
from flask import Response

@app.route("/admin/horas/editar-dia", methods=["GET"])
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_dia():
    """
    Muestra, por empleado y fecha (y opcional tarea), los reportes del día
    para poder editar la hora de 'asistencia' y 'salida'.
    Parámetros:
      - uid (int) obligatorio
      - fecha (YYYY-MM-DD) obligatorio
      - tarea_id (int) opcional (si quieres filtrar sólo una tarea)
    """
    uid = request.args.get("uid", type=int)
    fecha_s = request.args.get("fecha")
    tarea_id = request.args.get("tarea_id", type=int)

    if not uid or not fecha_s:
        flash("Faltan parámetros (uid y fecha).", "warning")
        return redirect(url_for("admin_horas"))

    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except Exception:
        flash("Fecha inválida.", "danger")
        return redirect(url_for("admin_horas"))

    # Traer reportes de ese día (todas las tareas o una)
    start = datetime.combine(f, time.min)
    end   = datetime.combine(f, time.max)
    q = (Reporte.query
         .filter(Reporte.usuario_id==uid,
                 Reporte.timestamp>=start, Reporte.timestamp<=end))
    if tarea_id:
        q = q.filter(Reporte.tarea_id==tarea_id)
    reps = q.order_by(Reporte.tarea_id.asc(), Reporte.timestamp.asc()).all()

    # Agrupar por tarea para mostrar limpito
    from collections import defaultdict
    by_task = defaultdict(list)
    for r in reps:
        by_task[r.tarea_id].append(r)

    u = Usuario.query.get(uid)
    tareas = {t.id: t for t in Tarea.query.filter(Tarea.id.in_(by_task.keys())).all()} if by_task else {}

    return render_template("admin/horas_editar_dia.html",
                           empleado=u, fecha=f, by_task=by_task, tareas=tareas)
    

@app.route("/admin/horas/editar-dia", methods=["POST"], endpoint="admin_horas_editar_dia_post")
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_dia_post():
    """
    Espera JSON:
      {
        "uid": 123,
        "fecha": "YYYY-MM-DD",
        "items": [
          {
            "tarea_id": 77,
            "asistencia_id": 111, "asistencia_ts": "YYYY-MM-DD HH:MM",
            "salida_id": 222,     "salida_ts":     "YYYY-MM-DD HH:MM",
            "crear_salida_si_falta": true
          },
          { "tarea_id": null, "lunch_minutos": 30 }  # opcional, lunch general del día
        ]
      }
    Crea/actualiza asistencia/salida en pasado o futuro. Controla lunch.
    """
    data = request.get_json(silent=True) or {}
    uid = data.get("uid")
    fecha_s = data.get("fecha")
    items = data.get("items") or []

    if not uid or not fecha_s:
        return jsonify({"ok": False, "error": "uid y fecha son obligatorios"}), 400

    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except Exception:
        return jsonify({"ok": False, "error": "Fecha inválida"}), 400

    start = datetime.combine(f, time.min)
    end   = datetime.combine(f, time.max)

    for it in items:
        tarea_id = it.get("tarea_id")
        asis_id  = it.get("asistencia_id")
        asis_ts  = it.get("asistencia_ts")
        sal_id   = it.get("salida_id")
        sal_ts   = it.get("salida_ts")
        crear_si = bool(it.get("crear_salida_si_falta"))

        def _parse_dt(s):
            if not s: return None
            try: return datetime.strptime(s, "%Y-%m-%d %H:%M")
            except: return None

        asis_dt = _parse_dt(asis_ts)
        sal_dt  = _parse_dt(sal_ts)

        # Obtener/crear registros
        asis = Reporte.query.get(asis_id) if asis_id else None
        if not asis and (tarea_id is not None):
            asis = (Reporte.query
                    .filter_by(usuario_id=uid, tarea_id=tarea_id, tipo="asistencia")
                    .filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
                    .order_by(Reporte.timestamp.asc()).first())

        sal = Reporte.query.get(sal_id) if sal_id else None
        if not sal and (tarea_id is not None):
            sal = (Reporte.query
                   .filter_by(usuario_id=uid, tarea_id=tarea_id, tipo="salida")
                   .filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
                   .order_by(Reporte.timestamp.desc()).first())

        # Crear si faltan
        if (not asis) and asis_dt and (tarea_id is not None):
            asis = Reporte(tarea_id=tarea_id, usuario_id=uid, tipo="asistencia",
                           timestamp=asis_dt, valido=True)
            db.session.add(asis)
        if (not sal) and sal_dt and crear_si and (tarea_id is not None):
            sal = Reporte(tarea_id=tarea_id, usuario_id=uid, tipo="salida",
                          timestamp=sal_dt, valido=True)
            db.session.add(sal)

        # Actualizar
        if asis and asis_dt: asis.timestamp = asis_dt
        if sal and  sal_dt:  sal.timestamp  = sal_dt

        # Validación orden
        if asis and sal and asis.timestamp and sal.timestamp and sal.timestamp <= asis.timestamp:
            db.session.rollback()
            return jsonify({"ok": False, "error": "La salida debe ser posterior a la asistencia."}), 400

        # Lunch por ítem (cuando tarea_id es None usamos lunch 'general' del día)
        lunch_min = it.get("lunch_minutos")
        if lunch_min is not None:
            tid = tarea_id  # puede ser None (lunch general)
            lunch_row = (Reporte.query
                         .filter_by(usuario_id=uid, tipo="lunch", tarea_id=tid)
                         .filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
                         .first())
            if int(lunch_min) <= 0:
                if lunch_row: db.session.delete(lunch_row)
            else:
                if not lunch_row:
                    lunch_row = Reporte(usuario_id=uid, tarea_id=tid, tipo="lunch",
                                        timestamp=start.replace(hour=12, minute=0),
                                        valido=True, nota="Lunch administrativamente marcado")
                    db.session.add(lunch_row)
                # Nota: el descuento en horas se hace con a.lunch_min en tu cálculo estricto

    db.session.commit()
    return jsonify({"ok": True})

@app.get("/admin/empleado/<int:uid>/modal-editar")
@login_required
@role_required("admin","superadmin")
def admin_empleado_modal_editar(uid):
    u = Usuario.query.get_or_404(uid)
    # tareas activas del empleado
    asigs = (Asignacion.query
             .join(Tarea, Tarea.id==Asignacion.tarea_id)
             .filter(Asignacion.usuario_id==uid, Asignacion.estado=="activa", Tarea.activa==True)
             .order_by(Tarea.nombre.asc()).all())
    tareas = [{"id": a.tarea.id, "nombre": a.tarea.nombre, "ubicacion": a.tarea.ubicacion, "tarifa": a.tarifa_hora} for a in asigs]
    return render_template("admin/_editar_empleado_modal.html", emp=u, tareas=tareas)

# === EDITOR POR TAREA Y DÍA (usa tu template horas_editar.html) ===
@app.get("/admin/horas/editar-tarea", endpoint="admin_horas_editar_tarea")
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_tarea():
    tid = request.args.get("tarea_id", type=int)
    fecha_s = request.args.get("fecha")
    if not tid or not fecha_s:
        flash("Faltan parámetros (tarea_id y fecha).", "warning")
        return redirect(url_for("admin_horas"))
    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except Exception:
        flash("Fecha inválida.", "danger")
        return redirect(url_for("admin_horas"))

    start = datetime.combine(f, time.min)
    end   = datetime.combine(f, time.max)

    # empleados asignados a esa tarea (cualquier estado), ordenados por nombre
    asigs = (Asignacion.query
             .filter(Asignacion.tarea_id==tid)
             .join(Usuario, Usuario.id==Asignacion.usuario_id)
             .order_by(Usuario.nombre.asc()).all())

    # reportes del día por empleado
    by_emp = {}
    for a in asigs:
        reps = (Reporte.query
                .filter_by(tarea_id=tid, usuario_id=a.usuario_id)
                .filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
                .order_by(Reporte.timestamp.asc()).all())
        by_emp[a.usuario_id] = reps

    tarea = Tarea.query.get(tid)
    if not tarea:
        flash("Tarea no encontrada.", "warning")
        return redirect(url_for("admin_horas"))

    return render_template("admin/horas_editar.html",
                           tarea=tarea, fecha=f, by_emp=by_emp, asigs=asigs)


#--- por tarea

# === HISTORIAL: fechas trabajadas por empleado (y opcional tarea) ===
@app.get("/admin/horas/historial-fechas")
@login_required
@role_required("admin","superadmin")
def admin_horas_historial_fechas():
    uid      = request.args.get("uid", type=int)
    tarea_id = request.args.get("tarea_id", type=int)
    days     = request.args.get("days", type=int) or 60  # últimos 60 días por defecto
    if not uid:
        return jsonify(ok=False, error="Falta uid"), 400

    hoy   = datetime.utcnow().date()
    desde = hoy - timedelta(days=max(1, min(days, 365))-1)

    start_dt = datetime.combine(desde, time.min)
    end_dt   = datetime.combine(hoy,   time.max)

    q = (Reporte.query
         .filter(Reporte.usuario_id==uid,
                 Reporte.timestamp>=start_dt, Reporte.timestamp<=end_dt))
    if tarea_id:
        q = q.filter(Reporte.tarea_id==tarea_id)

    # agrupamos por día y tarea
    by_day_task = defaultdict(lambda: defaultdict(list))
    for r in q.order_by(Reporte.timestamp.asc()).all():
        d = r.timestamp.date().isoformat()
        by_day_task[d][r.tarea_id].append(r)

    # compactamos: por fecha -> lista de tareas con flags asistencia/salida
    fechas = []
    for d in sorted(by_day_task.keys(), reverse=True):
        tareas = []
        for tid, arr in by_day_task[d].items():
            tipos = { (r.tipo or "").lower() for r in arr }
            tareas.append({
                "tarea_id": tid,
                "tiene_asistencia": "asistencia" in tipos,
                "tiene_salida": "salida" in tipos,
            })
        fechas.append({"fecha": d, "tareas": tareas})
    return jsonify(ok=True, fechas=fechas)

@app.post("/admin/horas/editar-bulk")
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_bulk():
    """
    Asigna horas netas (HH:MM) y lunch a muchas fechas de un empleado en una tarea.
    Body JSON: { uid, tarea_id, items: [ {fecha:'YYYY-MM-DD', hhmm:'HH:MM', lunch_on:bool}, ... ] }
    """
    data = request.get_json(silent=True) or {}
    uid      = data.get("uid")
    tarea_id = data.get("tarea_id")
    items    = data.get("items") or []
    if not uid or not tarea_id:
        return jsonify(ok=False, error="Faltan uid/tarea_id"), 400
    if not isinstance(items, list) or not items:
        return jsonify(ok=False, error="Sin items"), 400

    # ---- helpers locales ----
    
    def _parse_hhmm_to_minutes(s: str):
        s = (s or "").strip()
        if not s: return None
        try:
            hh, mm = map(int, s.split(":"))
            if hh < 0 or mm < 0 or mm > 59: return None
            return hh*60 + mm
        except Exception:
            return None

    def _ensure_reports_for_day(uid: int, tarea_id: int, d: date, minutes: int, lunch_on: bool):
        """
        Crea/ajusta reportes 'asistencia' y 'salida' en la fecha d para que
        el neto quede = minutes. Si lunch_on=True, agrega un marcador 'lunch'.
        """
        start = datetime.combine(d, time.min)
        end   = datetime.combine(d, time.max)
        reps = (Reporte.query
                .filter(Reporte.usuario_id==uid, Reporte.tarea_id==tarea_id)
                .filter(Reporte.timestamp>=start, Reporte.timestamp<=end)
                .order_by(Reporte.timestamp.asc()).all())

        # Busca o crea asistencia/salida
        asis = next((r for r in reps if (r.tipo or "").lower()=="asistencia"), None)
        sal  = next((r for r in reps if (r.tipo or "").lower()=="salida"), None)

        base = datetime.combine(d, time(hour=8))  # hora base 08:00 si no tienes hora oficial
        if not asis:
            asis = Reporte(usuario_id=uid, tarea_id=tarea_id, tipo="asistencia", timestamp=base)
            db.session.add(asis)
        else:
            asis.timestamp = base

        # salida = asistencia + minutes
        sal_dt = asis.timestamp + timedelta(minutes=max(0, minutes))
        if not sal:
            sal = Reporte(usuario_id=uid, tarea_id=tarea_id, tipo="salida", timestamp=sal_dt)
            db.session.add(sal)
        else:
            sal.timestamp = sal_dt

        # Lunch “administrativo” como nota (opcional)
        if lunch_on:
            lunch_row = Reporte(
                usuario_id=uid, tarea_id=tarea_id, tipo="nota",
                timestamp=asis.timestamp + timedelta(minutes=1),
                nota="Lunch administrativamente marcado"
            )
            db.session.add(lunch_row)

    # ---- procesa items ----
    ok_count, bad = 0, []
    for it in items:
        f = (it.get("fecha") or "").strip()
        hhmm = (it.get("hhmm") or "").strip()
        lunch_on = bool(it.get("lunch_on"))
        try:
            d = datetime.strptime(f, "%Y-%m-%d").date()
        except Exception:
            bad.append({"fecha": f, "error": "Fecha inválida"});  continue
        mins = _parse_hhmm_to_minutes(hhmm)
        if mins is None:
            bad.append({"fecha": f, "error": "HH:MM inválido"});  continue

        _ensure_reports_for_day(uid, tarea_id, d, mins, lunch_on)
        ok_count += 1

    db.session.commit()
    # (Opcional) registrar evento
    try:
        log_event("tarea_edit", tarea_id=tarea_id, empleado_id=uid,
                  detalle={"ok": ok_count, "bad": bad})
    except Exception:
        pass

    return jsonify(ok=True, ok_count=ok_count, bad=bad)


# === EDITOR SIMPLE POR TAREA Y DÍA ===
@app.get("/admin/horas/editar-simple", endpoint="admin_horas_editar_simple")
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_simple():
    from sqlalchemy import func
    tarea_id   = request.args.get("tarea_id", type=int)
    tarea_name = request.args.get("tarea_name", type=str)
    fecha_s    = request.args.get("fecha")

    if not fecha_s:
        flash("Falta 'fecha'.", "warning")
        return redirect(url_for("admin_horas"))

    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except Exception:
        flash("Fecha inválida.", "danger")
        return redirect(url_for("admin_horas"))

    M          = current_app.config["MODELS"]
    Usuario    = M["Usuario"]
    Tarea      = M["Tarea"]
    Asignacion = M["Asignacion"]
    Reporte    = M["Reporte"]

    # Resolver tarea
    tarea = None
    if tarea_id:
        tarea = Tarea.query.get(tarea_id)
    elif tarea_name:
        tarea = (Tarea.query
                 .filter(func.lower(Tarea.nombre) == (tarea_name or "").strip().lower())
                 .first()) \
             or (Tarea.query.filter(Tarea.nombre.ilike(f"%{tarea_name}%")).first())

    if not tarea:
        flash("Tarea no encontrada.", "warning")
        return redirect(url_for("admin_horas"))

    start = datetime.combine(f, time.min)
    end   = datetime.combine(f, time.max)

    asigs = (Asignacion.query
             .filter(Asignacion.tarea_id == tarea.id)
             .join(Usuario, Usuario.id == Asignacion.usuario_id)
             .order_by(Usuario.nombre.asc())
             .all())

    rows = []
    for a in asigs:
        reps = (Reporte.query
                .filter_by(tarea_id=tarea.id, usuario_id=a.usuario_id)
                .filter(Reporte.timestamp >= start, Reporte.timestamp <= end)
                .order_by(Reporte.timestamp.asc()).all())
        asist = next((r for r in reps if (r.tipo or "").lower() == "asistencia"), None)
        sal   = next((r for r in reps if (r.tipo or "").lower() == "salida"), None)
        lunch = next((r for r in reps if (r.tipo or "").lower() == "lunch"), None)

        # Prefill HH:MM neto
        hhmm_prefill = ""
        if asist and sal:
            base_ini = a.hora_inicio_override or tarea.hora_inicio
            dt_ini_of = None
            if base_ini:
                try:
                    h, m = map(int, base_ini.split(":"))
                    dt_ini_of = datetime(f.year, f.month, f.day, h, m, 0)
                except Exception:
                    dt_ini_of = None
            start_real = max(asist.timestamp, dt_ini_of) if dt_ini_of else asist.timestamp
            minutos = max(0, int((sal.timestamp - start_real).total_seconds() // 60))
            if lunch and (a.lunch_min or 0) > 0:
                minutos = max(0, minutos - int(a.lunch_min or 0))
            hh, mm = divmod(minutos, 60)
            hhmm_prefill = f"{hh:02d}:{mm:02d}"

        rows.append({
            "uid": a.usuario_id,
            "nombre": a.usuario.nombre,
            "tel": getattr(a.usuario, "telefono", "") or "",
            "hhmm": hhmm_prefill,
            "lunch_on": bool(lunch),
            "lunch_min": int(a.lunch_min or 0),
        })

    if request.args.get("modal") == "1" or request.headers.get("X-Requested-With") == "fetch":
        return render_template("admin/_editar_simple_modal.html",
                               tarea=tarea, fecha=f, rows=rows, asigs=asigs)

    return render_template("admin/horas_editar_simple.html",
                           tarea=tarea, fecha=f, rows=rows, asigs=asigs)

from datetime import datetime, date, time, timedelta
from sqlalchemy import func

# === POST: Guardar cambios del editor SIMPLE (por tarea/día) ===
@app.post("/admin/horas/editar-simple", endpoint="admin_horas_editar_simple_post")
@login_required
@role_required("admin","superadmin")
def admin_horas_editar_simple_post():
    """Guarda cambios de HH:MM y Lunch por empleados, para una tarea y fecha."""
    M          = current_app.config["MODELS"]
    Usuario    = M["Usuario"]
    Tarea      = M["Tarea"]
    Asignacion = M["Asignacion"]
    Reporte    = M["Reporte"]

    tarea_id   = request.form.get("tarea_id", type=int)
    tarea_name = request.form.get("tarea_name", type=str)
    fecha_s    = request.form.get("fecha")

    # Resolver fecha
    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except Exception:
        msg = "Fecha inválida"
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=False, error=msg), 400
        flash(msg, "danger")
        return redirect(url_for("admin_horas"))

    # Resolver tarea
    tarea = None
    if tarea_id:
        tarea = Tarea.query.get(tarea_id)
    elif tarea_name:
        tarea = (Tarea.query
                 .filter(func.lower(Tarea.nombre) == (tarea_name or "").strip().lower())
                 .first()) \
             or (Tarea.query.filter(Tarea.nombre.ilike(f"%{tarea_name}%")).first())

    if not tarea:
        msg = "Tarea no encontrada"
        if request.headers.get("X-Requested-With") == "fetch":
            return jsonify(ok=False, error=msg), 404
        flash(msg, "warning")
        return redirect(url_for("admin_horas"))

    session = Usuario.query.session

    # Asignaciones de esta tarea (cualquier estado) para conocer overrides y lunch_min
    asigs = (Asignacion.query
             .filter(Asignacion.tarea_id == tarea.id)
             .join(Usuario, Usuario.id == Asignacion.usuario_id)
             .order_by(Usuario.nombre.asc())
             .all())

    updated = 0
    for a in asigs:
        uid = a.usuario_id
        # Campos del formulario (por fila)
        hhmm  = request.form.get(f"hhmm_{uid}", "").strip()
        lunch_on = bool(request.form.get(f"lunch_{uid}"))

        # Si el usuario dejó HH:MM vacío → NO tocamos esa persona
        minutes = _parse_hhmm_to_minutes(hhmm)
        if minutes is None:
            continue

        # lunch_min y hora oficial (override > tarea)
        lunch_min = int(a.lunch_min or 0)
        start_of  = a.hora_inicio_override or tarea.hora_inicio

        _ensure_reports_for_day(
            session=session, Reporte=Reporte,
            uid=uid, tid=tarea.id, f=f,
            net_minutes=minutes, lunch_on=lunch_on,
            lunch_min=lunch_min, official_start_hhmm=start_of
        )
        updated += 1

    session.commit()

    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify(ok=True, updated=updated)

    flash(f"Cambios guardados ({updated} filas).", "success")
    # volver a horas con los mismos filtros (si venías de modal, te sirve igual)
    return redirect(url_for("admin_horas", vista=request.args.get("vista","tabla")))


@app.post("/admin/horas/toggle-lunch", endpoint="admin_toggle_lunch")
@login_required
@role_required("admin","superadmin")
def admin_toggle_lunch():
    """
    Marca / desmarca un lunch (tipo='lunch') para uid/tarea_id/fecha.
    Body JSON: { "uid": 1, "tarea_id": 77, "fecha": "YYYY-MM-DD", "on": true }
    """
    data = request.get_json(silent=True) or {}
    uid = data.get("uid"); tid = data.get("tarea_id")
    fecha_s = data.get("fecha"); on = bool(data.get("on"))
    if uid is None or fecha_s is None:
        return jsonify(ok=False, error="Parámetros incompletos"), 400
    try:
        f = datetime.strptime(fecha_s, "%Y-%m-%d").date()
    except:
        return jsonify(ok=False, error="Fecha inválida"), 400

    start = datetime.combine(f, time.min); end = datetime.combine(f, time.max)
    q = (Reporte.query.filter_by(usuario_id=uid, tipo="lunch")
         .filter(Reporte.timestamp>=start, Reporte.timestamp<=end))
    if tid is None:
        q = q.filter(Reporte.tarea_id.is_(None))
    else:
        q = q.filter(Reporte.tarea_id==tid)
    lunch = q.first()

    if on and not lunch:
        db.session.add(Reporte(
            usuario_id=uid, tarea_id=tid, tipo="lunch",
            timestamp=start.replace(hour=12, minute=0), valido=True,
            nota="Lunch administrativamente marcado"
        ))
    if (not on) and lunch:
        db.session.delete(lunch)

    db.session.commit()
    return jsonify(ok=True)


@app.route("/admin/warnings", methods=["GET","POST"])
@login_required
@role_required("admin","superadmin")
def admin_warnings():
    today = datetime.utcnow().date()
    start_today = datetime(today.year, today.month, today.day)
    tareas = Tarea.query.filter_by(activa=True).all()
    pendientes = []
    for t in tareas:
        for a in t.asignaciones:
            tiene = (Reporte.query
                     .filter_by(usuario_id=a.usuario_id, tarea_id=t.id, tipo="asistencia")
                     .filter(Reporte.timestamp>=start_today).first())
            if not tiene and a.usuario.activo:
                pendientes.append(dict(usuario=a.usuario, tarea=t))

    if request.method == "POST":
        ids = request.form.getlist("seleccion")  # "uid-tid"
        msg = request.form.get("mensaje", "Incumplimiento de confirmación de asistencia.")
        nivel = request.form.get("nivel", "warn")
        for packed in ids:
            uid, tid = map(int, packed.split("-"))
            db.session.add(Sancion(usuario_id=uid, tarea_id=tid, tipo="asistencia", mensaje=msg, nivel=nivel))
        db.session.commit()
        flash("Amonestaciones registradas.", "success")

    sanciones_recientes = Sancion.query.order_by(Sancion.creada_en.desc()).limit(50).all()
    return render_template("admin/warnings.html", pendientes=pendientes, sanciones=sanciones_recientes)

@app.route("/admin/warnings/<int:sid>/resolver", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def resolver_warning(sid):
    s = Sancion.query.get_or_404(sid)
    s.resuelta = True
    db.session.commit()
    flash("Amonestación marcada como resuelta.", "success")
    return redirect(url_for("admin/warnings"))

# WhatsApp Setup
@app.route("/admin/setup-whatsapp", methods=["GET","POST"])
@login_required
@role_required("admin","superadmin")
def setup_whatsapp():
    to = (request.args.get("to") or "").strip()

    tareas = Tarea.query.filter_by(activa=True).all()
    bloques = []
    for t in tareas:
        empleados = [a.usuario.nombre for a in t.asignaciones
                     if a.estado == "activa" and a.usuario.activo]
        bloque = (
            f"NOMBRE DE TAREA O SUPERVISOR: {t.nombre}\n"
            "LISTA DE EMPLEADOS ASIGNADOS:\n" +
            ("\n".join(f" - {n}" for n in empleados) if empleados else " - (sin asignados)")
        )
        bloques.append(bloque)
    texto = "\n\n".join(bloques)

    if to:
        from urllib.parse import quote
        payload = quote(texto)
        link = f"https://wa.me/{to.lstrip('+').replace(' ', '')}?text={payload}"
        return redirect(link)

    return render_template("setup_result.html", texto=texto, to=None)


#--- TEMA DE PAGOS DE ADMINISTRADOR DONDE VAN LAS COLILLAS

import os
UPLOAD_RECIBOS_DIR = os.path.join("uploads", "recibos")
os.makedirs(UPLOAD_RECIBOS_DIR, exist_ok=True)

@app.route("/admin/pagos", methods=["GET"])
@login_required
@role_required("admin","superadmin")
def admin_pagos():
    # Resolve jueves de la semana a consultar
    arg_week_end = (request.args.get("week_end") or "").strip()
    if arg_week_end:
        try:
            end = datetime.strptime(arg_week_end, "%Y-%m-%d").date()
        except Exception:
            end = week_window_for(datetime.utcnow().date())[1]
    else:
        end = week_window_for(datetime.utcnow().date())[1]
    start = end - timedelta(days=6)

    # ¿Incluir también empleados con 0h?
    today = datetime.utcnow().date()
    _, this_thu = week_window_for(today)
    is_thursday_today = (datetime.utcnow().weekday() == 3)   # jueves
    is_current_week = (end == this_thu)
    force_all = (request.args.get("show") == "all")
    include_zero = force_all or (is_thursday_today and is_current_week)

    # PENDIENTES (con nombre y teléfono)
    pendientes = unpaid_list_for_period(
        db, Usuario, Reporte, PagoSemana, Tarea, Asignacion,
        start=start, end=end, include_zero=include_zero
    )

    # PAGADOS de esa semana (para el bloque inferior)
    pagados = (PagoSemana.query
               .filter(PagoSemana.semana_fin == end)
               .order_by(PagoSemana.id.desc())
               .all())

    prev_end = end - timedelta(days=7)
    next_end = end + timedelta(days=7)

    return render_template(
        "admin/pagos.html",
        semana_inicio=start, semana_fin=end,
        prev_end=prev_end, next_end=next_end,
        include_zero=include_zero,
        pendientes=pendientes, pagados=pagados
    )



# --- imports que podrías necesitar arriba de tu app.py ---
import os
import uuid
from datetime import datetime, date, timedelta
from werkzeug.utils import secure_filename
from flask import current_app, request, redirect, url_for, flash
from flask_login import login_required
# asumo que ya tienes: db, Usuario, PagoSemana, role_required

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".pdf"}
MAX_COLILLA_MB = 8  # opcional: tamaño máximo sugerido

def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTS else ""

def _ensure_folder(path: str):
    os.makedirs(path, exist_ok=True)

def _parse_week_end(s: str) -> date:
    return datetime.fromisoformat(s).date()

@app.route("/admin/pagos/subir", methods=["POST"])
@login_required
@role_required("admin", "superadmin")
def admin_pagos_subir():
    try:
        usuario_id = int(request.form.get("usuario_id"))
    except Exception:
        flash("Usuario inválido.", "error")
        return redirect(url_for("admin_pagos"))

    semana_fin_raw = (request.form.get("semana_fin") or "").strip()
    try:
        semana_fin = _parse_week_end(semana_fin_raw)
    except Exception:
        flash("Fecha de semana (jueves) inválida.", "error")
        return redirect(url_for("admin_pagos"))

    # Horas y subtotal (pueden venir vacíos; tratamos como 0)
    def _parse_float(s):
        try:
            return float(s)
        except Exception:
            return 0.0
    horas = _parse_float(request.form.get("horas"))
    subtotal = _parse_float(request.form.get("subtotal"))

    file = request.files.get("recibo")
    if not file or not file.filename:
        flash("Debes adjuntar una colilla (imagen o PDF).", "error")
        return redirect(url_for("admin_pagos", week_end=semana_fin.isoformat()))

    ext = _safe_ext(file.filename)
    if not ext:
        flash("Formato no permitido. Usa JPG, PNG o PDF.", "error")
        return redirect(url_for("admin_pagos", week_end=semana_fin.isoformat()))

    # (Opcional) validación de tamaño si usas MAX_CONTENT_LENGTH a nivel app
    # current_app.config["MAX_CONTENT_LENGTH"] = MAX_COLILLA_MB * 1024 * 1024

    # Carpeta destino: static/uploads/pagos/AAAA/AAAA-MM-DD/
    year_folder = str(semana_fin.year)
    day_folder = semana_fin.isoformat()  # jueves de la semana
    rel_dir = os.path.join("uploads", "pagos", year_folder, day_folder)
    abs_dir = os.path.join(current_app.root_path, "static", rel_dir)
    _ensure_folder(abs_dir)

    # Nombre de archivo: <uid>_<uuid4><ext>
    fname = f"{usuario_id}_{uuid.uuid4().hex}{ext}"
    abs_path = os.path.join(abs_dir, secure_filename(fname))

    try:
        file.save(abs_path)
    except Exception as e:
        current_app.logger.exception("Error guardando colilla")
        flash("No pude guardar la colilla. Intenta de nuevo.", "error")
        return redirect(url_for("admin_pagos", week_end=semana_fin.isoformat()))

    # Ruta RELATIVA para BD (respecto a /static)
    recibo_rel = os.path.join(rel_dir, fname).replace("\\", "/")

    # Crear/actualizar PagoSemana único por (usuario_id, semana_fin)
    pago = (PagoSemana.query
            .filter_by(usuario_id=usuario_id, semana_fin=semana_fin)
            .first())
    if not pago:
        pago = PagoSemana(usuario_id=usuario_id, semana_fin=semana_fin)

    pago.horas = horas
    pago.subtotal = subtotal
    pago.recibo_path = recibo_rel  # <<<<<< RUTA RELATIVA A /static
    # pago.verificado_empleado se mantiene False hasta que el empleado confirme

    db.session.add(pago)
    db.session.commit()

    flash("Pago registrado correctamente. (El empleado aún debe confirmar)", "success")
    return redirect(url_for("admin_pagos", week_end=semana_fin.isoformat()))


@app.get("/admin/pagos/api/unpaid")
@login_required
@role_required("admin","superadmin")
def api_admin_unpaid():
    end_s = request.args.get("week_end")
    try:
        end = datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else week_window_for(datetime.utcnow().date())[1]
    except Exception:
        end = week_window_for(datetime.utcnow().date())[1]
    start = end - timedelta(days=6)
    include_zero = (request.args.get("show") == "all")

    rows = unpaid_list_for_period(
        db, Usuario, Reporte, PagoSemana, Tarea, Asignacion,
        start, end, include_zero
    )
    # ya vienen con nombre y telefono
    out = []
    for r in rows:
        out.append({
            "usuario_id": r["usuario_id"],
            "nombre": r["nombre"],
            "telefono": r["telefono"],
            "horas": r["horas"],
            "subtotal": r["subtotal"],
            "semana_fin": r["semana_fin"].isoformat()
        })
    return jsonify(ok=True, rows=out)



@app.route("/admin/pagos/historial/<int:uid>")
@login_required
@role_required("admin","superadmin")
def admin_pagos_historial(uid):
    """
    Historial de pagos por persona. Permite filtrar por semana de canje (?week_end=YYYY-MM-DD).
    """
    u = Usuario.query.get_or_404(uid)
    week_end_s = request.args.get("week_end")
    pagos_q = PagoSemana.query.filter_by(usuario_id=uid).order_by(PagoSemana.semana_fin.desc(), PagoSemana.creado_en.desc())
    if week_end_s:
        try:
            we = datetime.strptime(week_end_s, "%Y-%m-%d").date()
            pagos_q = pagos_q.filter(PagoSemana.semana_fin==we)
        except Exception:
            pass
    pagos = pagos_q.all()
    # opcional: también calcula estimado de esa semana
    estimado = None
    if week_end_s:
        try:
            end = datetime.strptime(week_end_s, "%Y-%m-%d").date()
            start = end - timedelta(days=6)
            mins = _minutes_week_for_user(db, Reporte, Tarea, Asignacion, uid, start, end)  # usa helper interno
            asigs = Asignacion.query.filter_by(usuario_id=uid, estado="activa").all()
            tarifa = round(sum(a.tarifa_hora or 0 for a in asigs)/max(1,len(asigs)), 2) if asigs else 0.0
            estimado = dict(
                horas=round(mins/60.0, 2),
                subtotal=round((mins/60.0)*tarifa, 2),
                semana_fin=end
            )
        except Exception:
            pass
    return render_template("admin/pagos_historial.html", emp=u, pagos=pagos, estimado=estimado)


# --- arriba del archivo asegúrate de tener estos imports ---
import os, uuid
from datetime import datetime, date, timedelta
from flask import current_app, request, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
from flask_login import login_required

ALLOWED_EXTS = {".jpg", ".jpeg", ".png", ".pdf"}

def _safe_ext(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    return ext if ext in ALLOWED_EXTS else ""

def _ensure_folder(path: str):
    os.makedirs(path, exist_ok=True)

# --- este endpoint reemplaza al anterior admin_pagos_editar ---
@app.route("/admin/pagos/editar/<int:pago_id>", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def admin_pagos_editar(pago_id):
    pago = PagoSemana.query.get_or_404(pago_id)

    # 1) Horas / Subtotal (opcionales)
    def _parse_float(s):
        try: return float(s)
        except: return None
    horas = _parse_float(request.form.get("horas"))
    subtotal = _parse_float(request.form.get("subtotal"))
    if horas is not None: pago.horas = horas
    if subtotal is not None: pago.subtotal = subtotal

    # 2) Reemplazo de colilla (opcional)
    file = request.files.get("recibo")
    if file and file.filename:
        ext = _safe_ext(file.filename)
        if not ext:
            flash("Formato no permitido. Usa JPG, PNG o PDF.", "error")
            return redirect(url_for("admin_pagos", week_end=pago.semana_fin.isoformat()))

        # borrar anterior si existía
        if pago.recibo_path:
            try:
                old_abs = os.path.join(current_app.root_path, "static", pago.recibo_path)
                if os.path.exists(old_abs): os.remove(old_abs)
            except Exception:
                current_app.logger.exception("No se pudo borrar la colilla anterior")

        # carpeta destino según la semana (jueves)
        year_folder = str(pago.semana_fin.year)
        day_folder = pago.semana_fin.isoformat()
        rel_dir = os.path.join("uploads", "pagos", year_folder, day_folder)
        abs_dir = os.path.join(current_app.root_path, "static", rel_dir)
        _ensure_folder(abs_dir)

        fname = f"{pago.usuario_id}_{uuid.uuid4().hex}{ext}"
        abs_path = os.path.join(abs_dir, secure_filename(fname))
        try:
            file.save(abs_path)
        except Exception:
            flash("No pude guardar la nueva colilla. Intenta de nuevo.", "error")
            return redirect(url_for("admin_pagos", week_end=pago.semana_fin.isoformat()))

        pago.recibo_path = os.path.join(rel_dir, fname).replace("\\", "/")

    db.session.add(pago)
    db.session.commit()
    flash("Pago actualizado.", "success")
    return redirect(url_for("admin_pagos", week_end=pago.semana_fin.isoformat()))


@app.route("/admin/pagos/eliminar/<int:pago_id>", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def admin_pagos_eliminar(pago_id):
    pago = PagoSemana.query.get_or_404(pago_id)
    # borrar archivo si existe
    if pago.recibo_path:
        try:
            abs_path = os.path.join(current_app.root_path, "static", pago.recibo_path)
            if os.path.exists(abs_path):
                os.remove(abs_path)
        except Exception as e:
            current_app.logger.exception("No se pudo borrar archivo de colilla")

    semana_fin_iso = pago.semana_fin.isoformat() if getattr(pago, "semana_fin", None) else None
    db.session.delete(pago)
    db.session.commit()
    flash("Colilla y registro eliminados. El empleado vuelve a aparecer como pendiente.", "warning")
    return redirect(url_for("admin_pagos", week_end=semana_fin_iso))

@app.route("/empleado/pagos", methods=["GET"])
@login_required
@role_required("empleado")
def empleado_pagos():
    pagos = (
        PagoSemana.query
        .filter_by(usuario_id=current_user.id)
        .order_by(PagoSemana.semana_fin.desc(), PagoSemana.id.desc())
        .limit(5)
        .all()
    )
    return render_template("empleado/pagos.html", pagos=pagos)

@app.route("/empleado/pagos/confirmar/<int:pid>", methods=["POST"])
@login_required
@role_required("empleado")
def empleado_pagos_confirmar(pid):
    p = PagoSemana.query.get_or_404(pid)
    if p.usuario_id != current_user.id: abort(403)
    p.verificado_empleado = True
    db.session.commit()
    flash("¡Gracias! Confirmación recibida.", "success")
    return redirect(url_for("empleado_pagos"))


import os
from flask import send_from_directory, abort

# Config (ajústalo a tu estructura)
UPLOAD_RECIBOS = os.path.join(app.root_path, "data", "recibos")  # carpeta privada fuera de /static

import os
from flask import send_from_directory, abort

@app.route("/empleado/pagos/<int:pid>/recibo")
@login_required
@role_required("empleado")
def empleado_pago_recibo(pid):
    p = PagoSemana.query.get_or_404(pid)
    if p.usuario_id != current_user.id:
        abort(403)

    # Campo con el nombre del archivo, por ejemplo "recibo_123.png"
    fname = getattr(p, "recibo_filename", None)
    if not fname:
        abort(404)

    carpeta = os.path.join(app.root_path, "uploads", "pagos")
    fpath = os.path.join(carpeta, fname)
    if not os.path.isfile(fpath):
        abort(404)

    return send_from_directory(carpeta, fname)


import zipfile
from io import BytesIO

@app.route("/admin/export/empleados_activos.csv")
@login_required
@role_required("admin","superadmin")
def export_empleados_activos_csv():
    """
    Empleados activos (no bloqueados/banneados) con columnas:
    id, nombre, telefono, email, zelle_nombre, zelle_cuenta, confirmado
    Nota: genera CSV con BOM UTF-8 para Excel.
    """
    data = export_empleados_activos_csv_bytes()  # <- viene del service
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"empleados_activos_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    )
    
from services.respaldo import export_empleados_activos_xlsx_bytes

@app.get("/admin/export/empleados_activos.xlsx")
@login_required
@role_required("admin","superadmin")
def export_empleados_activos_xlsx():
    data = export_empleados_activos_xlsx_bytes()
    # Detecta si devolvió fallback CSV
    if data[:4] != b'PK\x03\x04':
        # devolvió CSV; mándalo bien con nombre .csv
        return send_file(io.BytesIO(data), mimetype="text/csv", as_attachment=True,
                         download_name="empleados_activos.csv")
    return send_file(io.BytesIO(data),
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True,
                     download_name="empleados_activos.xlsx")


@app.route("/admin/export/colillas.zip")
@login_required
@role_required("admin","superadmin")
def export_colillas_zip():
    """
    Descarga colillas por rango (?start=YYYY-MM-DD&end=YYYY-MM-DD).
    El ZIP usa nombres: (Nombre)_(YYYY-MM-DD)_(Telefono).ext
    La resolución de rutas respeta tu estructura 'static/uploads/pagos/...'.
    """
    def _p(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    start = _p(request.args.get("start") or "") or (datetime.utcnow().date() - timedelta(days=30))
    end   = _p(request.args.get("end") or "")   or datetime.utcnow().date()
    if start > end:
        start, end = end, start

    # 👉 delegamos toda la lógica al service (resuelve rutas y arma nombres)
    data = export_colillas_zip_bytes(start, end)
    fname = f"colillas_{start.isoformat()}_{end.isoformat()}.zip"
    return send_file(
        io.BytesIO(data),
        mimetype="application/zip",
        as_attachment=True,
        download_name=fname
    )


# PDF tabla MEMO
@app.route("/admin/pdf", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def generar_pdf():
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    c.setFont("Helvetica-Bold", 14); c.drawString(72, 800, "Tabla MEMO - Tareas activas")
    c.setFont("Helvetica", 10); y = 780
    for t in Tarea.query.filter_by(activa=True).all():
        c.drawString(72, y, f"ID {t.id} • {t.nombre} • {t.ubicacion or ''} • {t.prioridad}")
        y -= 16
        if y < 72: c.showPage(); y = 800
    c.showPage(); c.save(); buffer.seek(0)
    return send_file(buffer, as_attachment=True, download_name="tabla_memo.pdf", mimetype="application/pdf")


@app.route("/admin/historial")
@login_required
@role_required("admin","superadmin")
def admin_historial_global():
    tipos = ["tarea_create", "tarea_edit", "tarea_estado"]

    q = (db.session.query(
            HistEvento,
            Tarea.nombre.label("tarea_nombre"),
            Usuario.nombre.label("actor_nombre"),
            Usuario.rol.label("actor_rol")
         )
         .outerjoin(Tarea, HistEvento.tarea_id == Tarea.id)
         .outerjoin(Usuario, HistEvento.usuario_id == Usuario.id)
         .filter(HistEvento.tipo.in_(tipos))
         .order_by(HistEvento.creado_en.desc())
         .limit(300))

    eventos_raw = q.all()
    eventos = []
    for e, tarea_nombre, actor_nombre, actor_rol in eventos_raw:
        tipo = (e.tipo or "").strip()
        detalle = e.detalle or {}

        if tipo == "tarea_edit" and isinstance(detalle.get("fields"), dict):
            partes = []
            for k, v in detalle["fields"].items():
                prev = v.get("prev")
                new  = v.get("new")
                partes.append(f"{k}: {prev} → {new}")
            resumen = " ; ".join(partes) if partes else "Edición de tarea"
        elif tipo == "tarea_estado":
            estado = detalle.get("activa")
            resumen = "Estado: Activa" if estado else "Estado: Finalizada"
        elif tipo == "tarea_create":
            resumen = "Creación de tarea"
        else:
            from json import dumps
            resumen = dumps(detalle, ensure_ascii=False)

        eventos.append(dict(
            fecha=e.creado_en,
            usuario=actor_nombre or (f"#{e.usuario_id}" if e.usuario_id else "-"),
            rol=(actor_rol or "").lower() if actor_rol else "",
            cambio=resumen,
            tarea=tarea_nombre or (f"#{e.tarea_id}" if e.tarea_id else "-")
        ))

    return render_template("admin/historial.html", eventos=eventos)


# === Evidencias estáticas (fotos de reportes) ===
import os
from flask import send_from_directory, abort
from werkzeug.utils import safe_join

UPLOAD_DIR = app.config.get("UPLOAD_FOLDER", os.path.join(os.getcwd(), "uploads"))

@app.route("/uploads/<path:fname>", endpoint="uploads_file")
def uploads_file(fname):
    # Sirve archivos SOLO de la carpeta UPLOAD_DIR
    full = safe_join(UPLOAD_DIR, fname)
    if not full or not os.path.isfile(full):
        abort(404)
    # No forzamos descarga; se muestra inline
    return send_from_directory(UPLOAD_DIR, fname)


# ==============================================================
# 🧩 RUTAS DE INFORMES (vista principal y recarga AJAX)

def _parse_date(s):
    s = (s or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try: return datetime.strptime(s, fmt).date()
        except: pass
    return None

# ---------- INFORMES: VISTA PRINCIPAL ----------
@app.route("/admin/informes")
@login_required
@role_required("admin", "superadmin")
def admin_informes():
    q       = (request.args.get("q") or "").strip()
    estado  = (request.args.get("estado") or "activa").lower()
    tipo    = (request.args.get("tipo") or "todas").lower()
    view    = (request.args.get("view") or "tareas").lower()
    page    = int(request.args.get("page") or 1)
    perp    = int(request.args.get("per_page") or 10)
    start_s = (request.args.get("start") or "").strip()
    end_s   = (request.args.get("end") or "").strip()

    the_day = None
    if start_s or end_s:
        try:
            start_d = datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else None
            end_d   = datetime.strptime(end_s,   "%Y-%m-%d").date() if end_s   else None
            the_day = (start_d, end_d)
        except Exception:
            the_day = None

    tareas, empleados, pager = _build_informes_data(
        q=q, the_day=the_day, view=view,
        estado=estado, tipo=tipo,
        page=page, per_page=perp
    )

    return render_template(
        "admin/informes.html",
        q=q, estado=estado, tipo=tipo, view=view,
        start=start_s, end=end_s,
        tareas=tareas, empleados=empleados, pager=pager
    )

# ---------- INFORMES: fragmento de tarjetas con paginación ----------
# --- Parcial de tarjetas ------------------------------------------------------
@app.route("/admin/informes/_cards")
def admin_informes_cards():
    q       = (request.args.get("q") or "").strip()
    estado  = (request.args.get("estado") or "activa").lower()
    tipo    = (request.args.get("tipo") or "todas").lower()
    view    = (request.args.get("view") or "tareas").lower()
    page    = int(request.args.get("page") or 1)
    per     = int(request.args.get("per_page") or 10)

    # rango opcional
    start_s = request.args.get("start")
    end_s   = request.args.get("end")
    the_day = None
    if start_s or end_s:
        s_d = datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else None
        e_d = datetime.strptime(end_s,   "%Y-%m-%d").date() if end_s   else None
        the_day = (s_d, e_d)

    tareas, empleados, pager = _build_informes_data(
        q=q, the_day=the_day, view=view, estado=estado, tipo=tipo, page=page, per_page=per
    )

    return render_template(
        "admin/_informes_cards.html",
        view=view, tareas=tareas, empleados=empleados, pager=pager
    )


# ========= RUTAS INFORMES (remplazos compatibles con tus modelos) =========
from flask import jsonify, request
from sqlalchemy import func, and_, or_
from datetime import datetime, date, timedelta

# Aliases de modelos (ajústalos si tus nombres son distintos)
TR = Tarea
ASG = Asignacion
USR = Usuario
RV = ReportVentana
RP = Reporte
TU = Turno

def _parse_iso(ymd: str):
    try:
        return datetime.fromisoformat(ymd).date()
    except Exception:
        return None


# ---------- INFORMES: DÍAS CON ACTIVIDAD/VENTANAS (por Tarea) ----------
# imports (arriba del archivo, asegúrate de tenerlos)
from datetime import datetime, date, timedelta, timezone
from zoneinfo import ZoneInfo

# ---------- INFORMES: DÍAS POR TAREA ----------
# Lista TODOS los días del rango de la tarea (no depende de que existan reportes)
@app.get("/admin/informes/api/task/<int:tid>/days")
def admin_inf_task_days(tid: int):
    tz = _safe_tz()

    # Usa el que tengas disponible; si no usas SQLAlchemy 2.x, usa Tarea.query.get
    task = Tarea.query.get(tid)  # <-- si usabas db.session.get(...), cámbialo por esto
    if not task:
        return jsonify({"days": []}), 404

    hoy_local = datetime.now(tz).date()

    # Rango visible: desde fecha_inicio hasta hoy (capado por fecha_fin si existe)
    d_ini = task.fecha_inicio or hoy_local
    d_fin = min(task.fecha_fin or hoy_local, hoy_local)

    if d_fin < d_ini:
        return jsonify({"days": []})

    days = []
    cur = d_ini
    while cur <= d_fin:
        days.append(cur.isoformat())
        cur += timedelta(days=1)

    return jsonify({"days": days})


# ---------- INFORMES: PERSONAS EN UN DÍA (por Tarea) ----------
# --- Personas de un día -------------------------------------------------------
@app.route("/admin/informes/api/task/<int:tid>/day/<date_str>/people")
def admin_inf_people_for_day(tid, date_str):
    # fecha del día (00:00:00 – 23:59:59) en UTC
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        return jsonify({"people": []})

    start_dt = datetime(d.year, d.month, d.day, 0, 0, 0)
    end_dt   = datetime(d.year, d.month, d.day, 23, 59, 59)

    # base: asignaciones activas a la tarea
    ASG, USR, RV, RP = Asignacion, Usuario, ReportVentana, Reporte

    # esperados por tarea (ventanas configuradas) – si no hay RV, se usa cantidad_reportes
    esperados_tarea_sq = (
        db.session.query(RV.tarea_id.label("tid"), func.count(RV.id).label("esperados"))
        .filter(RV.tarea_id == tid)
        .group_by(RV.tarea_id).subquery()
    )

    # completados por usuario ese día
    done_sq = (
        db.session.query(RP.usuario_id.label("uid"), func.count(RP.id).label("done"))
        .filter(RP.tarea_id == tid, RP.timestamp >= start_dt, RP.timestamp <= end_dt)
        .group_by(RP.usuario_id).subquery()
    )

    rows = (
        db.session.query(
            USR.id.label("usuario_id"),
            USR.nombre.label("nombre"),
            func.coalesce(esperados_tarea_sq.c.esperados, Tarea.cantidad_reportes, 0).label("esperados"),
            func.coalesce(done_sq.c.done, 0).label("completados")
        )
        .join(ASG, ASG.usuario_id == USR.id)
        .join(Tarea, Tarea.id == ASG.tarea_id)
        .outerjoin(esperados_tarea_sq, esperados_tarea_sq.c.tid == Tarea.id)
        .outerjoin(done_sq, done_sq.c.uid == USR.id)
        .filter(ASG.tarea_id == tid, USR.activo == True)
        .order_by(func.lower(USR.nombre))
        .all()
    )

    people = [
        dict(
            usuario_id=r.usuario_id,
            nombre=r.nombre,
            esperados=int(r.esperados or 0),
            completados=int(r.completados or 0),
        ) for r in rows
    ]
    return jsonify({"people": people})


# ---------- INFORMES: REPORTES POR PERSONA/DÍA (cumplidos / retardo / falló) ----------
# --- Informes detallados por persona -----------------------------------------
def _as_time(val) -> time | None:
    if val is None: return None
    if isinstance(val, time): return val
    if isinstance(val, datetime): return val.time()
    if isinstance(val, str):
        s = val.strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try: return datetime.strptime(s, fmt).time()
            except: pass
    return None

def _pick_attr(obj, names):
    for n in names:
        if hasattr(obj, n):
            v = getattr(obj, n)
            if v is not None:
                return v
    return None

def _rv_times(rv):
    ini_raw = _pick_attr(rv, ["hora_ini", "hora_inicio", "ini", "desde", "start", "ini_hora"])
    fin_raw = _pick_attr(rv, ["hora_fin", "fin", "hasta", "end", "fin_hora"])
    tipo    = _pick_attr(rv, ["tipo", "nombre", "name", "label"]) or "informe"
    return _as_time(ini_raw), _as_time(fin_raw), str(tipo)

def _combine_local(d, t: time|None, tz: ZoneInfo):
    t = t or time(0, 0, 0)
    local_dt = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=tz)
    return local_dt.astimezone(timezone.utc)


def _midpoint_time(t1: time, t2: time) -> time:
    """Devuelve una time a mitad entre t1 y t2 (asume mismo día; si t2<=t1, cruza medianoche)."""
    dt1 = datetime(2000,1,1, t1.hour, t1.minute, t1.second)
    dt2 = datetime(2000,1,1, t2.hour, t2.minute, t2.second)
    if dt2 <= dt1:
        dt2 += timedelta(days=1)
    mid = dt1 + (dt2 - dt1)/2
    return time(mid.hour, mid.minute, mid.second)

def _ensure_required_windows(ventanas: list[dict], tarea: "Tarea", d, tz: ZoneInfo) -> list[dict]:
    """
    Si faltan confirmacion / asistencia / lunch, crea ventanas "virtuales"
    usando hora_inicio/hora_fin de la tarea.
    - confirmacion: ventana corta en la hora de inicio (−5..+10m)
    - asistencia: dentro de la primera media hora desde inicio (0..+30m)
    - lunch: centrada en la mitad de la jornada (±30m)
    """
    have = { (v.get("tipo") or "").lower() for v in ventanas }

    def _as_time(val):
        if val is None: return None
        if isinstance(val, time): return val
        if isinstance(val, datetime): return val.time()
        if isinstance(val, str):
            s = val.strip()
            for fmt in ("%H:%M", "%H:%M:%S"):
                try: return datetime.strptime(s, fmt).time()
                except: pass
        return None

    def _utc_of(d, t):
        local_dt = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=tz)
        return local_dt.astimezone(timezone.utc)

    h_ini = _as_time(getattr(tarea, "hora_inicio", None) or getattr(tarea, "hora_ini", None)) or time(8,0,0)
    h_fin = _as_time(getattr(tarea, "hora_fin", None) or getattr(tarea, "fin_hora", None)) or time(17,0,0)

    # confirmacion
    if "confirmacion" not in have:
        i = _utc_of(d, (datetime.combine(d, h_ini) - timedelta(minutes=5)).time())
        f = _utc_of(d, (datetime.combine(d, h_ini) + timedelta(minutes=10)).time())
        if f <= i: f += timedelta(minutes=10)
        ventanas.append(dict(
            tipo="confirmacion",
            ini_utc=i, fin_utc=f,
            ini_local=i.astimezone(tz).strftime("%H:%M"),
            fin_local=f.astimezone(tz).strftime("%H:%M"),
            _virtual=True
        ))

    # asistencia
    if "asistencia" not in have:
        i = _utc_of(d, h_ini)
        f = _utc_of(d, (datetime.combine(d, h_ini) + timedelta(minutes=30)).time())
        if f <= i: f = i + timedelta(minutes=30)
        ventanas.append(dict(
            tipo="asistencia",
            ini_utc=i, fin_utc=f,
            ini_local=i.astimezone(tz).strftime("%H:%M"),
            fin_local=f.astimezone(tz).strftime("%H:%M"),
            _virtual=True
        ))

    # lunch
    if "lunch" not in have:
        mid = _midpoint_time(h_ini, h_fin)
        i = _utc_of(d, (datetime.combine(d, mid) - timedelta(minutes=30)).time())
        f = _utc_of(d, (datetime.combine(d, mid) + timedelta(minutes=30)).time())
        if f <= i: f = i + timedelta(minutes=30)
        ventanas.append(dict(
            tipo="lunch",
            ini_utc=i, fin_utc=f,
            ini_local=i.astimezone(tz).strftime("%H:%M"),
            fin_local=f.astimezone(tz).strftime("%H:%M"),
            _virtual=True
        ))

    # Ordena por inicio
    ventanas.sort(key=lambda v: v["ini_utc"])
    return ventanas

def _ventanas_del_dia(tarea: "Tarea", d, tz: ZoneInfo):
    """
    1) Si hay ReportVentana: usa sus rangos.
    2) Si no, reparte 'cantidad_reportes' entre hora_inicio/hora_fin.
    Devuelve [{tipo, ini_utc, fin_utc, ini_local, fin_local}]
    """
    ventanas = []

    # 1) Con ReportVentana
    rvs = (ReportVentana.query
           .filter_by(tarea_id=tarea.id)
           .order_by(ReportVentana.orden.asc(), ReportVentana.id.asc())
           .all())
    if rvs:
        for rv in rvs:
            ini_t, fin_t, tipo = _rv_times(rv)
            if ini_t is None and fin_t is None:
                continue
            ini_utc = _combine_local(d, (ini_t or fin_t), tz)
            fin_utc = _combine_local(d, (fin_t or ini_t), tz)
            if fin_utc <= ini_utc:
                fin_utc += timedelta(days=1)
            # también calculamos la HH:MM local para mostrar en UI
            ini_loc = ini_utc.astimezone(tz).strftime("%H:%M")
            fin_loc = fin_utc.astimezone(tz).strftime("%H:%M")
            ventanas.append(dict(tipo=tipo, ini_utc=ini_utc, fin_utc=fin_utc,
                                 ini_local=ini_loc, fin_local=fin_loc))
        return ventanas

    # 2) Sin ReportVentana → fallback por cantidad_reportes
    n = int(getattr(tarea, "cantidad_reportes", 0) or 0)
    if n <= 0:
        return ventanas

    h_ini = _as_time(_pick_attr(tarea, ["hora_inicio", "hora_ini"]))
    h_fin = _as_time(_pick_attr(tarea, ["hora_fin", "fin_hora"]))

    if h_ini and h_fin:
        ini_utc = _combine_local(d, h_ini, tz)
        fin_utc = _combine_local(d, h_fin, tz)
        if fin_utc <= ini_utc:
            fin_utc += timedelta(days=1)
        total = (fin_utc - ini_utc).total_seconds()
        slot = total / n
        for i in range(n):
            s = ini_utc + timedelta(seconds=slot * i)
            e = ini_utc + timedelta(seconds=slot * (i + 1)) - timedelta(seconds=1)
            ventanas.append(dict(
                tipo=f"informe_{i+1}",
                ini_utc=s, fin_utc=e,
                ini_local=s.astimezone(tz).strftime("%H:%M"),
                fin_local=e.astimezone(tz).strftime("%H:%M")
            ))
        return ventanas

    # Fallback mínimo: n ventanitas desde h_ini o 08:00, saltos de 15 min
    base_utc = _combine_local(d, h_ini or time(8, 0, 0), tz)
    for i in range(n):
        s = base_utc + timedelta(minutes=15 * i)
        e = s + timedelta(minutes=15) - timedelta(seconds=1)
        ventanas.append(dict(
            tipo=f"informe_{i+1}",
            ini_utc=s, fin_utc=e,
            ini_local=s.astimezone(tz).strftime("%H:%M"),
            fin_local=e.astimezone(tz).strftime("%H:%M")
        ))
    return ventanas

# ============ Helpers específicos de evidencia/obligatorios ============
def _fmt_hhmm(dt_aware, tz):
    return dt_aware.astimezone(tz).strftime("%H:%M")

def _get_foto(r):
    # acepta 'foto_url' o 'foto_path' o nada
    return getattr(r, "foto_url", None) or getattr(r, "foto_path", None)

def _get_gps_tuple(r):
    """
    Intenta extraer lat/lon; si no existen, devuelve (None, None, gps_raw)
    gps_raw puede venir en r.gps, r.ubicacion, r.location_json, etc.
    """
    lat = getattr(r, "lat", None) or getattr(r, "latitude", None)
    lon = getattr(r, "lon", None) or getattr(r, "lng", None) or getattr(r, "longitude", None)
    gps_raw = getattr(r, "gps", None) or getattr(r, "ubicacion", None) or getattr(r, "location", None)

    # intenta parsear strings tipo "lat,lon"
    if (lat is None or lon is None) and isinstance(gps_raw, str) and "," in gps_raw:
        try:
            parts = [p.strip() for p in gps_raw.split(",")]
            if len(parts) >= 2:
                lat = lat or float(parts[0])
                lon = lon or float(parts[1])
        except Exception:
            pass
    return lat, lon, gps_raw

def _truthy(x):
    s = str(x or "").strip().lower()
    return s in {"1","true","t","yes","y","si","sí","ok"}

def _get_lunch_choice(r):
    """
    Busca si el reporte dice 'lunch' sí/no.
    Soporta varios nombres de campo: lunch, almuerzo, lunch_si, choice, opcion, extra, etc.
    """
    # campos directos boolean/string
    for name in ["lunch", "almuerzo", "lunch_si", "lunch_yes", "hizo_lunch", "opcion", "choice"]:
        if hasattr(r, name):
            val = getattr(r, name)
            # si viene como 'si'/'no', 'yes'/'no'
            if isinstance(val, str):
                if val.strip().lower() in {"si","sí","yes","y","1","true"}:
                    return True
                if val.strip().lower() in {"no","n","0","false"}:
                    return False
            return _truthy(val)

    # JSON u objeto 'extra'
    extra = getattr(r, "extra", None) or getattr(r, "extra_json", None) or getattr(r, "metadata", None)
    if isinstance(extra, dict):
        for key in ["lunch","almuerzo","lunch_si","lunch_yes","hizo_lunch","choice","opcion"]:
            if key in extra:
                return _truthy(extra[key])
    return None  # desconocido

def _required_specs():
    """
    Especifica requerimientos por tipo.
    - confirmacion: sin foto obligatoria, sin gps obligatorio
    - asistencia: requiere foto + gps
    - lunch: si 'sí' => requiere foto; si 'no' no requiere foto
    """
    return {
        "confirmacion": {"req_foto": False, "req_gps": False, "is_lunch": False},
        "asistencia":   {"req_foto": True,  "req_gps": True,  "is_lunch": False},
        "lunch":        {"req_foto": "if_yes", "req_gps": False, "is_lunch": True},
    }
# ======================================================================

# ---------- INFORMES: REPORTES POR PERSONA/DÍA (con evidencia y obligatorios) ----------
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from flask import jsonify

@app.get("/admin/informes/api/task/<int:tid>/day/<string:ymd>/person/<int:uid>/reports")
def admin_informes_task_day_person_reports(tid, ymd, uid):
    """
    SIEMPRE 200.
    Devuelve:
      - ventanas con HH:MM local (ini/fin)
      - reportes asignados a ventana (on_time/late) o placeholder fail
      - 'otros' no asignados (late si hay ventanas; on_time si no hay)
      - evidencia: foto_url, gps, hora_envio_local
      - flags de obligatoriedad (confirmación/asistencia y lunch)
      - resumen y descuento por lunch (30 min si 'si')
    """
    # --- helpers cortos ---
    def _parse_ymd(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date()
        except Exception:
            return None

    def _aware(ts):
        if ts is None: return None
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)

    def _hhmm_local(ts_aware, tz):
        return ts_aware.astimezone(tz).strftime("%H:%M")

    # --- base ---
    d = _parse_ymd(ymd)
    tarea = Tarea.query.get(tid)
    if not d or not tarea:
        return jsonify(
            informes=[],
            ventanas=[],
            resumen={"on_time":0,"late":0,"fail":0,"pending":0},
            fallo_dia=False,
            descuento_lunch_min=0
        ), 200

    tz = ZoneInfo(getattr(tarea, "tz_name", None) or "America/New_York")
    tol = timedelta(minutes=int(getattr(tarea, "tolerancia_min", 0) or 0))

    # Ventanas esperadas (incluye HH:MM local para UI)
    ventanas = _ventanas_del_dia(tarea, d, tz)
    ventanas = _ensure_required_windows(ventanas, tarea, d, tz)

    # Rango del día LOCAL en UTC para consultar reportes reales
    day_start_utc = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz).astimezone(timezone.utc)
    day_end_utc   = (day_start_utc + timedelta(days=1)) - timedelta(seconds=1)

    reales = (Reporte.query
              .filter(Reporte.tarea_id==tid,
                      Reporte.usuario_id==uid,
                      Reporte.timestamp >= day_start_utc,
                      Reporte.timestamp <= day_end_utc,
                      (Reporte.valido == True) if hasattr(Reporte, "valido") else True)
              .order_by(Reporte.timestamp.asc())
              .all())

    # Normalización
    reales = [r for r in reales if _aware(r.timestamp)]
    usados = set()
    informes = []

    # Map de evidencia obligatoria por tipo (reglas que pediste):
    # - Confirmación de asistencia: OBLIGATORIA pero SIN foto
    # - Asistencia normal: OBLIGATORIA con foto y gps
    # - Lunch 'sí': OBLIGATORIO con foto (y computa descuento 30m)
    # - Lunch 'no': OBLIGATORIO sin foto
    def _info_evidencia(r: "Reporte"):
        tipo = (getattr(r, "tipo", "") or "").lower()
        is_conf = (getattr(r, "tipo_especial", "") or "").lower() == "confirmacion"
        foto = getattr(r, "foto_path", None) or getattr(r, "foto_url", None)
        gps  = getattr(r, "gps", None) or getattr(r, "ubicacion", None)
        # Lunch: inferimos "no" si es tipo 'lunch' sin foto (tu flujo guarda nota "Sin lunch")
        if tipo == "lunch":
            lunch_si = bool(foto)
            requiere_foto = lunch_si  # sí → requiere; no → no requiere
            obligatorio = True
            return obligatorio, requiere_foto, lunch_si, foto, gps, is_conf
        if tipo == "asistencia":
            if is_conf:
                return True, False, False, foto, gps, True   # confirmación sin foto obligatoria
            return True, True, False, foto, gps, False        # asistencia normal con foto+gps
        # otros/ventanas
        return False, True, False, foto, gps, False

    # 1) Asignar reportes a ventanas 0..1
    for v in ventanas:
        v_ini = v["ini_utc"]; v_fin = v["fin_utc"]
        asignado = None
        for r in reales:
            if r.id in usados: continue
            ts = _aware(r.timestamp)
            if (ts >= (v_ini - tol)) and (ts <= (v_fin + tol)):
                asignado = r
                usados.add(r.id)
                break

        if asignado is None:
            informes.append(dict(
                id=None,
                tipo=v["tipo"],
                obligatorio=False,
                is_confirmacion=False,
                estado="fail",
                ventana_ini=v["ini_local"], ventana_fin=v["fin_local"],
                hora_envio=None,
                delay_mins=None, delay_human=None,
                foto_url=None, gps=None
            ))
        else:
            ts = _aware(asignado.timestamp)
            estado = "late" if ts > (v_fin + tol) else "on_time"
            atraso = int(max(0, round((ts - (v_fin + tol)).total_seconds() / 60.0))) if estado=="late" else 0
            atraso_h = (f"{atraso}m" if atraso < 60 else f"{atraso//60}h {atraso%60}m") if atraso>0 else None
            obligatorio, requiere_foto, lunch_si, foto, gps, is_conf = _info_evidencia(asignado)

            informes.append(dict(
                id=asignado.id,
                tipo=(getattr(asignado,"tipo","") or v["tipo"]),
                obligatorio=obligatorio,
                is_confirmacion=is_conf,
                estado=estado,
                ventana_ini=v["ini_local"], ventana_fin=v["fin_local"],
                hora_envio=_hhmm_local(ts, tz),
                delay_mins=(atraso if estado=="late" else None),
                delay_human=atraso_h,
                foto_url=foto,
                gps=gps,
                requiere_foto=requiere_foto,
            ))

    # 2) Reportes no asignados: late si hay ventanas; on_time si no hay
    estado_sobrante = "late" if ventanas else "on_time"
    for r in reales:
        if r.id in usados: continue
        ts  = _aware(r.timestamp)
        obligatorio, requiere_foto, lunch_si, foto, gps, is_conf = _info_evidencia(r)
        informes.append(dict(
            id=r.id,
            tipo=(getattr(r,"tipo",None) or "otro"),
            obligatorio=obligatorio,
            is_confirmacion=is_conf,
            estado=estado_sobrante,
            ventana_ini=None, ventana_fin=None,
            hora_envio=_hhmm_local(ts, tz),
            delay_mins=None, delay_human=None,
            foto_url=foto, gps=gps,
            requiere_foto=requiere_foto
        ))

    # 3) Resumen + obligatorios + descuento lunch
    resumen = {"on_time":0, "late":0, "fail":0, "pending":0}
    for it in informes:
        resumen[it["estado"]] = resumen.get(it["estado"], 0) + 1

    # Obligatorios: confirmación/asistencia y lunch (sí o no)
    tiene_confirm = any(it.get("is_confirmacion") for it in informes if it.get("id"))
    tiene_asist   = any((it.get("tipo")=="asistencia") and it.get("id") for it in informes)
    # lunch: sí/no (sí = con foto; no = sin foto); cualquiera de los dos cumple obligatoriedad
    lunch_si = any((it.get("tipo")=="lunch" and it.get("id") and it.get("foto_url")) for it in informes)
    lunch_no = any((it.get("tipo")=="lunch" and it.get("id") and not it.get("foto_url")) for it in informes)

    fallo_dia = False
    # Si quieres que la confirmación sea requisito aparte, usa (not tiene_confirm)
    # Aquí tratamos confirmación como parte de asistencia (como me pediste):
    if not tiene_asist: fallo_dia = True
    if not (lunch_si or lunch_no): fallo_dia = True

    descuento_lunch_min = 30 if lunch_si else 0

    return jsonify(
        ventanas=[{"tipo":v["tipo"], "ini":v["ini_local"], "fin":v["fin_local"]} for v in ventanas],
        informes=informes,
        resumen=resumen,
        fallo_dia=fallo_dia,
        descuento_lunch_min=descuento_lunch_min
    ), 200



@app.route("/admin/amonestaciones", methods=["POST"])
@login_required
@role_required("admin", "superadmin")
def admin_crear_amonestacion():
    """
    Crea una sanción simple asociada a (usuario, tarea, fecha, motivo).
    Espera JSON: { uid, tid, fecha (YYYY-MM-DD), motivo }
    """
    data = request.get_json(silent=True) or {}
    uid = data.get("uid")
    tid = data.get("tid")
    fecha = data.get("fecha")  # string Y-m-d
    motivo = (data.get("motivo") or "").strip() or "Amonestación desde Informes"

    if not (uid and tid and fecha):
        return jsonify({"ok": False, "error": "Faltan parámetros"}), 400

    try:
        fdate = datetime.strptime(fecha, "%Y-%m-%d")
    except Exception:
        return jsonify({"ok": False, "error": "Fecha inválida"}), 400

    # Crear la sanción
    s = Sancion(
        usuario_id = int(uid),
        tarea_id   = int(tid),
        creada_en  = fdate,
        motivo     = motivo,
        resuelta   = False,
    )
    db.session.add(s)
    db.session.commit()
    return jsonify({"ok": True, "id": s.id})


# ---------- INFORMES: AMONESTAR (opcional) ----------
@app.route("/admin/informes/api/sancion", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def api_informes_crear_sancion():
    data = request.get_json(force=True) or {}
    uid = int(data.get("usuario_id") or 0)
    tid = int(data.get("tarea_id") or 0)
    fecha = (data.get("fecha") or "").strip()
    motivo = (data.get("motivo") or "Incumplimiento").strip()
    if not (uid and tid and fecha):
        return jsonify({"ok": False, "error": "Faltan campos"}), 400
    try:
        y,m,d = map(int, fecha.split("-"))
        cuando = datetime(y,m,d,12,0,0)
    except Exception:
        return jsonify({"ok": False, "error": "Fecha inválida"}), 400

    s = Sancion(usuario_id=uid, tarea_id=tid, motivo=motivo, creada_en=cuando)
    db.session.add(s); db.session.commit()
    return jsonify({"ok": True, "id": s.id})

# ---- 4) TAREAS DE UNA PERSONA (para el modal por persona) ----
@app.route("/admin/informes/api/person/<int:usuario_id>/tasks")
@login_required
@role_required("admin", "superadmin")
def admin_informes_api_person_tasks(usuario_id):
    """
    Devuelve todas las tareas (activas o inactivas) en las que ha estado el empleado.
    Incluye nombre, estado, prioridad, ubicación y fechas visibles para el modal.
    """

    TR, ASG, RP = Tarea, Asignacion, Reporte

    # Tareas relacionadas al usuario
    tarea_rows = (
        db.session.query(
            TR.id, TR.nombre, TR.activa, TR.fecha_inicio, TR.fecha_fin,
            TR.prioridad, TR.ubicacion
        )
        .join(ASG, ASG.tarea_id == TR.id)
        .filter(ASG.usuario_id == usuario_id)
        .distinct()
        .all()
    )

    tasks = []
    for tid, tname, tactiva, tstart, tfin, tprio, ubic in tarea_rows:
        # Primer día con informe
        first_rep = (
            db.session.query(func.min(RP.timestamp))
            .filter(RP.tarea_id == tid, RP.usuario_id == usuario_id)
            .scalar()
        )

        # Fecha de inicio visible
        if isinstance(tstart, datetime):
            fecha_ini = tstart.date().isoformat()
        elif isinstance(tstart, date):
            fecha_ini = tstart.isoformat()
        elif first_rep:
            fecha_ini = (
                first_rep.date().isoformat()
                if isinstance(first_rep, datetime)
                else str(first_rep)
            )
        else:
            fecha_ini = date.today().isoformat()

        # Fecha de fin (si existe en la tarea)
        fecha_fin = (
            tfin.isoformat() if isinstance(tfin, (datetime, date)) else None
        )

        tasks.append({
            "tarea_id": tid,
            "tarea_nombre": tname,                # ✅ nombre consistente con frontend
            "estado": "activa" if tactiva else "inactiva",
            "prioridad": tprio or "comun",
            "ubicacion": ubic or "N/A",
            "fecha_ini": fecha_ini,
            "fecha_fin": fecha_fin
        })

    # Ordena: activas primero y luego las más recientes
    tasks.sort(
        key=lambda x: (x["estado"] != "activa", x["fecha_ini"]),
        reverse=False
    )

    return jsonify({"tasks": tasks})

@app.route("/admin/informes/tarea/<int:tarea_id>")
@login_required
@role_required("admin","superadmin")
def admin_informes_detalle_tarea(tarea_id):
    t = Tarea.query.get_or_404(tarea_id)
    the_day = _parse_date(request.args.get("date") or "") or datetime.utcnow().date()

    ventanas = ReportVentana.query.filter_by(tarea_id=tarea_id).order_by(ReportVentana.orden.asc()).all()

    asigns = (Asignacion.query
              .join(Usuario, Usuario.id == Asignacion.usuario_id)
              .filter(Asignacion.tarea_id == tarea_id, Usuario.activo == True)
              .order_by(Usuario.nombre.asc())
              .all())

    filas = []
    tol = timedelta(minutes=t.tolerancia_min or 0)

    start_dt = datetime(the_day.year, the_day.month, the_day.day, 0, 0, 0)
    end_dt   = datetime(the_day.year, the_day.month, the_day.day, 23, 59, 59)

    for a in asigns:
        emp = a.usuario
        for v in ventanas:
            if v.con_horario and v.hora_ini and v.hora_fin:
                h1,m1 = map(int, v.hora_ini.split(":"))
                h2,m2 = map(int, v.hora_fin.split(":"))
                ini = datetime(the_day.year, the_day.month, the_day.day, h1, m1, 0) - tol
                fin = datetime(the_day.year, the_day.month, the_day.day, h2, m2, 0) + tol
                ok = db.session.query(
                    db.session.query(Reporte.id).filter(
                        Reporte.tarea_id==tarea_id,
                        Reporte.usuario_id==emp.id,
                        Reporte.timestamp>=ini,
                        Reporte.timestamp<=fin
                    ).exists()
                ).scalar()
            else:
                ok = db.session.query(
                    db.session.query(Reporte.id).filter(
                        Reporte.tarea_id==tarea_id,
                        Reporte.usuario_id==emp.id,
                        Reporte.timestamp>=start_dt,
                        Reporte.timestamp<=end_dt
                    ).exists()
                ).scalar()
            filas.append(dict(
                emp_nombre=emp.nombre,
                v_nombre=v.nombre,
                ini=v.hora_ini, fin=v.hora_fin,
                cumplida=bool(ok)
            ))

    filas.sort(key=lambda r: (r["emp_nombre"].lower(), r["v_nombre"].lower()))
    return render_template("admin/informes_detalle_tarea.html",
                           tarea=t, the_day=the_day, filas=filas)

# ===================== RUTAS DE INFORMES =====================

@app.route("/admin/informes/semana.csv")
@login_required
@role_required("admin","superadmin")
def admin_informes_semana_csv():
    """
    Informe semanal 'bonito' en CSV:
    - RESUMEN GLOBAL (días, tareas, empleados, totales, % con barra unicode)
    - CUMPLIMIENTO POR DÍA
    - CUMPLIMIENTO POR TAREA (peor→mejor)
    - CUMPLIMIENTO POR EMPLEADO (Top 10 / Bottom 10)
    - DETALLE por día con SUBTOTALES
    Sin dependencias adicionales. Excel-friendly (separador ';' + BOM).
    """
    # ---- Ventana semanal (hoy y 6 días atrás) ----
    end = datetime.utcnow().date()
    start = end - timedelta(days=6)
    start_dt = datetime(start.year, start.month, start.day, 0, 0, 0)
    end_dt   = datetime(end.year, end.month, end.day, 23, 59, 59)

    # ---- Modelos ----
    RP, ASG, TR, USR, RV = Reporte, Asignacion, Tarea, Usuario, ReportVentana

    # ---- Esperados por tarea (ventanas) -> fallback a cantidad_reportes ----
    esperados_tarea_sq = (
        db.session.query(RV.tarea_id.label("tid"), func.count(RV.id).label("esperados"))
        .group_by(RV.tarea_id)
        .subquery()
    )

    # Usamos func.date(...) (string 'YYYY-MM-DD' en SQLite), evita cast(Date)
    dia_col = func.date(RP.timestamp).label("dia")

    rows = (
        db.session.query(
            dia_col,                                # 'YYYY-MM-DD' (str)
            TR.id.label("tarea_id"),
            TR.nombre.label("tarea"),
            TR.prioridad.label("prioridad"),
            TR.fecha_inicio.label("fecha_ini"),
            TR.hora_inicio.label("hora_ini"),
            USR.nombre.label("empleado"),
            func.count(RP.id).label("reportes"),
            func.max(func.coalesce(esperados_tarea_sq.c.esperados, TR.cantidad_reportes, 0)).label("esperados")
        )
        .join(ASG, and_(ASG.tarea_id == RP.tarea_id, ASG.usuario_id == RP.usuario_id))
        .join(TR, TR.id == ASG.tarea_id)
        .join(USR, USR.id == ASG.usuario_id)
        .outerjoin(esperados_tarea_sq, esperados_tarea_sq.c.tid == TR.id)
        .filter(RP.timestamp >= start_dt, RP.timestamp <= end_dt)
        .group_by(dia_col, TR.id, TR.nombre, TR.prioridad, TR.fecha_inicio, TR.hora_inicio, USR.nombre)
        .order_by(dia_col.asc(), TR.nombre.asc(), USR.nombre.asc())
        .all()
    )

    # ---------- Helpers internos ----------
    
# ---- TZ helpers ----
from zoneinfo import ZoneInfo  # <--- NUEVO import

def _default_tzname() -> str:
    # usa lo que te convenga por defecto en USA:
    return app.config.get("DEFAULT_TZ", "America/New_York")

def _safe_tzname() -> str:
    tz = (request.args.get("tz") or "").strip()
    return tz if tz else _default_tzname()

def _safe_tz() -> ZoneInfo:
    try:
        return ZoneInfo(_safe_tzname())
    except Exception:
        return ZoneInfo("UTC")

def _day_range_from_ymd_and_tz(ymd: str, tz: ZoneInfo):
    """
    ymd='YYYY-MM-DD' -> (start_utc_naive, end_utc_naive)
    Interpreta ese día en la TZ del usuario y lo convierte a UTC naive
    (asumiendo Reporte.timestamp almacenado en UTC naive).
    """
    d = datetime.strptime(ymd, "%Y-%m-%d").date()
    start_local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)
    end_local   = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc   = end_local.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc

    
    def _parse_hhmm_to_minutes(hhmm: str | None) -> int | None:
        """'08:30' -> 510. Devuelve None si está vacío o es inválido."""
        if not hhmm:
            return None
        s = hhmm.strip()
        if not s:
            return None
        try:
            h, m = map(int, s.split(":"))
            if h < 0 or m < 0 or m >= 60:
                return None
            return h * 60 + m
        except Exception:
            return None

def _ensure_reports_for_day(session, Reporte, uid: int, tid: int, f: date,
                            net_minutes: int, lunch_on: bool,
                            lunch_min: int, official_start_hhmm: str | None):
    """
    Crea/ajusta/elimina reportes del día para (uid,tid):
      - Ajusta 'asistencia' y 'salida' para que el neto sea 'net_minutes'.
      - Si lunch_on=True y lunch_min>0, crea/asegura un 'lunch' (sólo presencia).
      - Si net_minutes==0 -> elimina asistencia/salida/lunch del día.
    """
    r0 = datetime(f.year, f.month, f.day, 0, 0, 0)
    r1 = datetime(f.year, f.month, f.day, 23, 59, 59)

    reps = (session.query(Reporte)
            .filter(Reporte.usuario_id == uid,
                    Reporte.tarea_id == tid,
                    Reporte.timestamp >= r0,
                    Reporte.timestamp <= r1)
            .order_by(Reporte.timestamp.asc())
            .all())

    asist = next((r for r in reps if (r.tipo or "").lower() == "asistencia"), None)
    salida = next((r for r in reps if (r.tipo or "").lower() == "salida"), None)
    lunch  = next((r for r in reps if (r.tipo or "").lower() == "lunch"), None)

    # Si neto 0 → borra todo lo del día (asistencia/salida/lunch) para esta tarea
    if net_minutes == 0:
        for r in (asist, salida, lunch):
            if r:
                session.delete(r)
        return

    # Hora oficial de inicio
    start_of = None
    if official_start_hhmm:
        try:
            hh, mm = map(int, official_start_hhmm.split(":"))
            start_of = datetime(f.year, f.month, f.day, hh, mm, 0)
        except Exception:
            start_of = None

    # Si ya había asistencia y está después del oficial, respétala; si está antes, usa la oficial.
    if asist and start_of:
        start_real = max(asist.timestamp, start_of)
    else:
        start_real = start_of or datetime(f.year, f.month, f.day, 8, 0, 0)  # fallback 08:00

    # Toma en cuenta lunch_min sólo si lunch_on
    add_minutes = net_minutes + (lunch_min if lunch_on and (lunch_min or 0) > 0 else 0)
    end_real = start_real + timedelta(minutes=add_minutes)

    # Crear/actualizar asistencia
    if not asist:
        asist = Reporte(usuario_id=uid, tarea_id=tid, tipo="asistencia", timestamp=start_real)
        session.add(asist)
    else:
        asist.timestamp = start_real

    # Crear/actualizar salida
    if not salida:
        salida = Reporte(usuario_id=uid, tarea_id=tid, tipo="salida", timestamp=end_real)
        session.add(salida)
    else:
        salida.timestamp = end_real

    # Lunch sólo presencia (la hora no importa para el cálculo actual, pero colocamos al medio)
    if lunch_on and (lunch_min or 0) > 0:
        lunch_at = start_real + timedelta(minutes=max(1, net_minutes // 2))
        if not lunch:
            lunch = Reporte(usuario_id=uid, tarea_id=tid, tipo="lunch", timestamp=lunch_at)
            session.add(lunch)
        else:
            lunch.timestamp = lunch_at
    else:
        if lunch:
            session.delete(lunch)

    def _pct(done, exp):
        exp = int(exp or 0); done = int(done or 0)
        return 0 if exp <= 0 else round(min(100, (done/exp)*100))

    def _bar(pct):
        # barra unicode en 10 pasos (para ojo en Excel)
        blocks = "▁▂▃▄▅▆▇█"
        if pct <= 0: return ""
        idx = min(len(blocks)-1, max(0, int(round(pct/100*(len(blocks)-1)))))
        return blocks[idx] * 10


def _necesita_datos_basicos(u: Usuario) -> bool:
    """
    Devuelve True si faltan datos críticos para el empleado.
    Ajusta aquí lo que consideres 'mínimo':
    - email
    - zelle_nombre
    - zelle_cuenta
    """
    return not all([
        (u.email or "").strip(),
        (u.zelle_nombre or "").strip(),
        (u.zelle_cuenta or "").strip(),
    ])

    # ---------- Agregaciones ----------
    total_done = 0
    total_exp  = 0
    dias_set, tareas_set, empleados_set = set(), set(), set()

    by_day_done = Counter(); by_day_exp = Counter()
    by_task_done = Counter(); by_task_exp = Counter()
    by_emp_done = Counter();  by_emp_exp = Counter()

    task_meta = {}                # tid -> (nombre, prioridad, fecha_ini_str, hora_ini_str)
    task_emps = defaultdict(set)  # tid -> {empleados}
    emp_tasks = defaultdict(set)  # emp -> {tids}
    day_detail = defaultdict(list)

    for r in rows:
        exp  = int(r.esperados or 0)
        done = int(r.reportes or 0)

        total_done += done
        total_exp  += exp

        dias_set.add(r.dia)
        tareas_set.add(r.tarea_id)
        empleados_set.add(r.empleado)

        by_day_done[r.dia] += done
        by_day_exp[r.dia]  += exp

        by_task_done[r.tarea_id] += done
        by_task_exp[r.tarea_id]  += exp
        if r.tarea_id not in task_meta:
            fecha_ini_str = r.fecha_ini.strftime("%Y-%m-%d") if r.fecha_ini else ""
            hora_ini_str  = (r.hora_ini.strftime("%H:%M") if hasattr(r.hora_ini, "strftime") else (r.hora_ini or ""))
            task_meta[r.tarea_id] = (r.tarea, (r.prioridad or "comun"), fecha_ini_str, hora_ini_str)
        task_emps[r.tarea_id].add(r.empleado)

        by_emp_done[r.empleado] += done
        by_emp_exp[r.empleado]  += exp
        emp_tasks[r.empleado].add(r.tarea_id)

        day_detail[r.dia].append(r)

    # ---------- Construcción del CSV 'premium' ----------
    buf = _io.StringIO(newline="")
    w = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_MINIMAL)

    # META
    w.writerow([f"INFORME SEMANAL Fresh's Labors ({start.isoformat()} — {end.isoformat()})"])
    w.writerow([f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"])
    w.writerow([])

    # 1) RESUMEN GLOBAL
    w.writerow(["# RESUMEN GLOBAL"])
    glob_pct = _pct(total_done, total_exp)
    w.writerow(["Días", len(dias_set)])
    w.writerow(["Tareas distintas", len(tareas_set)])
    w.writerow(["Empleados distintos", len(empleados_set)])
    w.writerow(["Reportes hechos", total_done])
    w.writerow(["Reportes esperados", total_exp])
    w.writerow(["Cumplimiento global", f"{glob_pct}%", _bar(glob_pct)])
    w.writerow([])

    # 2) CUMPLIMIENTO POR DÍA
    w.writerow(["# CUMPLIMIENTO POR DÍA"])
    w.writerow(["Fecha", "Hechos", "Esperados", "%", "Bar"])
    for d in sorted(dias_set):
        dp = _pct(by_day_done[d], by_day_exp[d])
        w.writerow([d, by_day_done[d], by_day_exp[d], f"{dp}%", _bar(dp)])
    w.writerow([])

    # 3) CUMPLIMIENTO POR TAREA (ordenado por peor→mejor)
    w.writerow(["# CUMPLIMIENTO POR TAREA"])
    w.writerow(["ID Tarea","Tarea","Prioridad","Inicio Fecha","Inicio Hora","Empleados","Hechos","Esperados","%","Bar"])
    task_rows = []
    for tid in tareas_set:
        tname, tprio, tfi, thi = task_meta.get(tid, ("","comun","",""))
        td, te = by_task_done[tid], by_task_exp[tid]
        tp = _pct(td, te)
        task_rows.append((tp, tid, tname, tprio, tfi, thi, len(task_emps[tid]), td, te))
    for tp, tid, tname, tprio, tfi, thi, nemp, td, te in sorted(task_rows, key=lambda x: x[0]):
        w.writerow([tid, tname, tprio, tfi, thi, nemp, td, te, f"{tp}%", _bar(tp)])
    w.writerow([])

    # 4) CUMPLIMIENTO POR EMPLEADO (Top 10 / Bottom 10)
    w.writerow(["# CUMPLIMIENTO POR EMPLEADO"])
    emp_rows = []
    for emp in empleados_set:
        ed, ee = by_emp_done[emp], by_emp_exp[emp]
        ep = _pct(ed, ee)
        emp_rows.append((ep, emp, len(emp_tasks[emp]), ed, ee))
    # Top 10
    w.writerow(["## Top 10 (mayor %)"])
    w.writerow(["Empleado","Tareas distintas","Hechos","Esperados","%","Bar"])
    for ep, emp, nt, ed, ee in sorted(emp_rows, key=lambda x: x[0], reverse=True)[:10]:
        w.writerow([emp, nt, ed, ee, f"{ep}%", _bar(ep)])
    w.writerow([])
    # Bottom 10
    w.writerow(["## Bottom 10 (menor %)"])
    w.writerow(["Empleado","Tareas distintas","Hechos","Esperados","%","Bar"])
    for ep, emp, nt, ed, ee in sorted(emp_rows, key=lambda x: x[0])[:10]:
        w.writerow([emp, nt, ed, ee, f"{ep}%", _bar(ep)])
    w.writerow([])

    # 5) DETALLE POR DÍA (con subtotales)
    w.writerow(["# DETALLE POR DÍA"])
    w.writerow([
        "Fecha","ID Tarea","Tarea","Prioridad","Inicio Fecha","Inicio Hora",
        "Empleado","Hechos (día)","Esperados (día)","% (día)","Bar"
    ])
    for d in sorted(dias_set):
        day_done = 0
        day_exp  = 0
        for r in sorted(day_detail[d], key=lambda x: (x.tarea, x.empleado)):
            exp = int(r.esperados or 0)
            done = int(r.reportes or 0)
            dp   = _pct(done, exp)
            fecha_ini_str = r.fecha_ini.strftime("%Y-%m-%d") if r.fecha_ini else ""
            hora_ini_str  = (r.hora_ini.strftime("%H:%M") if hasattr(r.hora_ini, "strftime") else (r.hora_ini or ""))
            w.writerow([
                d, r.tarea_id, r.tarea, (r.prioridad or "comun"),
                fecha_ini_str, hora_ini_str,
                r.empleado, done, exp, f"{dp}%", _bar(dp)
            ])
            day_done += done; day_exp += exp
        dpp = _pct(day_done, day_exp)
        w.writerow(["SUBTOTAL " + d, "", "", "", "", "", "", day_done, day_exp, f"{dpp}%", _bar(dpp)])
        w.writerow([])

    # 6) TOTALES
    w.writerow(["# TOTALES"])
    w.writerow(["Hechos", total_done])
    w.writerow(["Esperados", total_exp])
    w.writerow(["Cumplimiento global", f"{glob_pct}%", _bar(glob_pct)])

    # ---- Descargar (BOM + ';') ----
    mem = _io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    mem.seek(0)
    fname = f"informes_semana_{end.isoformat()}.csv"
    return send_file(mem, mimetype="text/csv", as_attachment=True, download_name=fname)





# ----- NUEVO EDNPOINT PARA EL INFORME DE SEMANA ------- 
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.formatting.rule import DataBarRule
from openpyxl.utils import get_column_letter
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from sqlalchemy import func
from collections import defaultdict, Counter
from flask import request, send_file
import io as _io

@app.route("/admin/informes/semana.xlsx", endpoint="admin_informes_semana_xlsx")
@login_required
@role_required("admin","superadmin")
def admin_informes_semana_xlsx():
    # ===== Parámetros de rango =====
    def _parse_date_any(s):
        s = (s or "").strip()
        if not s: return None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try: return datetime.strptime(s, fmt).date()
            except: pass
        return None

    end_d = _parse_date_any(request.args.get("end")) or datetime.utcnow().date()
    start_d = _parse_date_any(request.args.get("start")) or (end_d - timedelta(days=29))
    start_dt = datetime(start_d.year, start_d.month, start_d.day, 0, 0, 0, tzinfo=timezone.utc)
    end_dt   = datetime(end_d.year, end_d.month, end_d.day, 23, 59, 59, tzinfo=timezone.utc)
    dias_rango = (end_d - start_d).days + 1

    # ===== Aliases de modelos =====
    RP, TR, USR, ASG, RV, SN, TU = Reporte, Tarea, Usuario, Asignacion, ReportVentana, Sancion, Turno

    # ===== Asignaciones activas reales =====
    asign_q = (db.session.query(
                    ASG.id.label("asg_id"),
                    USR.id.label("emp_id"), USR.nombre.label("empleado"),
                    TR.id.label("tarea_id"), TR.nombre.label("tarea"),
                    TR.prioridad, TR.ubicacion, TR.fecha_inicio, TR.hora_inicio,
                    ASG.turno_num, ASG.doble_turno
                )
                .join(USR, USR.id==ASG.usuario_id)
                .join(TR,  TR.id==ASG.tarea_id)
                .filter(ASG.estado=="activa", TR.activa==True, USR.activo==True)
                .order_by(func.lower(USR.nombre), func.lower(TR.nombre))
                .all())

    # ===== Ventanas/día por tarea (o cantidad_reportes si no hay) =====
    ventanas_cnt = {}
    for t_id, cnt in db.session.query(RV.tarea_id, func.count(RV.id)).group_by(RV.tarea_id):
        ventanas_cnt[t_id] = cnt
    for t_id, cant in db.session.query(TR.id, TR.cantidad_reportes).all():
        if t_id not in ventanas_cnt:
            ventanas_cnt[t_id] = int(cant or 0)

    # ===== Reportes del rango (incluye tipo) =====
    base = (db.session.query(
                RP.id, RP.timestamp, RP.tarea_id, RP.usuario_id,
                func.coalesce(RP.tipo, "").label("tipo"),
                TR.nombre.label("tarea"), TR.prioridad, TR.tz_name, TR.tolerancia_min,
                USR.nombre.label("empleado")
            )
            .join(TR, TR.id==RP.tarea_id)
            .join(USR, USR.id==RP.usuario_id)
            .filter(RP.timestamp >= start_dt, RP.timestamp <= end_dt)
            .order_by(RP.timestamp.asc())
            .all())

    # ===== Ventanas por tarea (para detectar “tarde/temprano”) =====
    ventanas_by_tarea = defaultdict(list)
    for v in db.session.query(RV).all():
        ventanas_by_tarea[v.tarea_id].append(v)

    def fuera_de_ventana_local(tarea_id, tz_name, tol_min, ts_utc):
        vs = ventanas_by_tarea.get(tarea_id) or []
        if not vs:
            return None  # no evaluable
        try:
            tz = ZoneInfo(tz_name or "UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        loc = ts_utc.astimezone(tz)
        tol = timedelta(minutes=int(tol_min or 0))
        for v in vs:
            # nombres flexibles
            h1 = getattr(v, "hora_inicio", None) or getattr(v, "hora_ini", None) or getattr(v, "hora_ini", None)
            h2 = getattr(v, "hora_fin", None) or getattr(v, "fin_hora", None)
            if not h1 or not h2:
                # si los almacenas como 'HH:MM' en str
                h1 = getattr(v, "hora_ini", None)
                h2 = getattr(v, "hora_fin", None)
            if not h1: continue
            # parse
            def _as_hm(val):
                if hasattr(val, "hour"): return (val.hour, val.minute)
                try:
                    hh,mm = str(val).split(":")[:2]
                    return (int(hh), int(mm))
                except:
                    return (0,0)
            H1,M1 = _as_hm(h1)
            H2,M2 = _as_hm(h2 or h1)
            ini = loc.replace(hour=H1, minute=M1, second=0, microsecond=0) - tol
            fin = loc.replace(hour=H2, minute=M2, second=0, microsecond=0) + tol
            if fin <= ini:
                fin += timedelta(days=1)
            if ini <= loc <= fin:
                return False
        return True

    # ===== Sanciones en el rango y abiertas =====
    sanc_rango_map = defaultdict(int)
    sanc_nores_map = defaultdict(int)
    sanc_rango = (db.session.query(SN.usuario_id, SN.tarea_id, func.count(SN.id))
                  .filter(SN.creada_en >= start_dt, SN.creada_en <= end_dt)
                  .group_by(SN.usuario_id, SN.tarea_id).all())
    sanc_nores = (db.session.query(SN.usuario_id, SN.tarea_id, func.count(SN.id))
                  .filter(SN.resuelta==False)
                  .group_by(SN.usuario_id, SN.tarea_id).all())
    for uid, tid, n in sanc_rango: sanc_rango_map[(uid, tid)] = int(n or 0)
    for uid, tid, n in sanc_nores: sanc_nores_map[(uid, tid)] = int(n or 0)

    # ===== Agregaciones =====
    emp_total = Counter()
    emp_dia   = defaultdict(Counter)
    emp_mes   = defaultdict(Counter)
    total_mes = Counter()

    et_hechos    = Counter()               # (uid, tid) -> hechos
    et_fuera     = Counter()               # (uid, tid) -> envíos fuera de ventana
    tarea_hechos = Counter()               # tid -> hechos

    # >>> Nuevos contadores por tipo obligatorio
    et_tipo = defaultdict(Counter)         # (uid, tid)["confirmacion"/"asistencia"/"lunch"/otros] -> n

    for r in base:
        d = r.timestamp.date().isoformat()
        ym = r.timestamp.strftime("%Y-%m")
        emp_total[r.empleado] += 1
        emp_dia[r.empleado][d] += 1
        emp_mes[r.empleado][ym] += 1
        total_mes[ym] += 1

        et_hechos[(r.usuario_id, r.tarea_id)] += 1
        tarea_hechos[r.tarea_id] += 1

        tname = (r.tipo or "").strip().lower()
        if tname not in ("confirmacion","asistencia","lunch"):
            tname = "otros"
        et_tipo[(r.usuario_id, r.tarea_id)][tname] += 1

        fh = fuera_de_ventana_local(r.tarea_id, r.tz_name, r.tolerancia_min, r.timestamp)
        if fh is True:
            et_fuera[(r.usuario_id, r.tarea_id)] += 1

    # Esperados por (uid, tid) ≈ ventanas*días (si asignado a esa tarea)
    asigs_activas = (db.session.query(ASG.usuario_id, ASG.tarea_id)
                     .join(TR, TR.id==ASG.tarea_id)
                     .join(USR, USR.id==ASG.usuario_id)
                     .filter(ASG.estado=="activa", TR.activa==True, USR.activo==True)
                     .all())
    et_esperados = Counter()
    emp_act_por_tarea = defaultdict(set)
    for uid, tid in asigs_activas:
        emp_act_por_tarea[tid].add(uid)
        vd = int(ventanas_cnt.get(tid, 0))
        if vd > 0:
            et_esperados[(uid, tid)] += vd * dias_rango

    def _pct(done, exp):
        exp = int(exp or 0); done = int(done or 0)
        return 0 if exp <= 0 else round(min(100, (done/exp)*100))

    # ===== Workbook y estilos =====
    wb = Workbook()
    H = lambda: Font(bold=True, color="1B2559")
    head_fill = PatternFill("solid", fgColor="EDF2FF")
    thin = Side(style="thin", color="E6E9F4")
    border_all = Border(left=thin, right=thin, top=thin, bottom=thin)

    # ---------- Hoja: Asignaciones activas ----------
    ws = wb.active; ws.title = "Asignaciones activas"
    ws.append([f"Foto real al {end_d.isoformat()} — empleados asignados a tareas activas"])
    ws.append([])
    ws.append(["Empleado","ID Tarea","Tarea","Prioridad","Ubicación","Fecha inicio","Hora inicio","Turno #","Doble turno"])
    for c in ws[ws.max_row]:
        c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    for a in asign_q:
        ws.append([
            a.empleado, a.tarea_id, a.tarea, (a.prioridad or "comun"),
            a.ubicacion or "", a.fecha_inicio.isoformat() if a.fecha_inicio else "",
            a.hora_inicio or "", a.turno_num or "", "Sí" if a.doble_turno else "No"
        ])
    for row in ws.iter_rows(min_row=3, max_row=ws.max_row, min_col=1, max_col=9):
        for c in row: c.border = border_all
    for col in range(1,10):
        ws.column_dimensions[get_column_letter(col)].width = [26,10,28,12,22,14,12,10,12][col-1]

    # ---------- Hoja: Empleado x Tarea (rango) ----------
    ws2 = wb.create_sheet("Empleado x Tarea (rango)")
    ws2.append([f"Rango: {start_d.isoformat()} — {end_d.isoformat()} (días={dias_rango})"])
    ws2.append([])
    ws2.append([
        "Empleado","ID Tarea","Tarea",
        "Hechos","Esperados","No-cumpl.","% Cumpl.",
        "Tarde/Fuera","Sanciones (rango)","Sanciones no resueltas",
        # nuevos
        "Confirms","Asistencias","Lunch","Otros"
    ])
    for c in ws2[ws2.max_row]:
        c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")

    meta_t = {t.id: t for t in db.session.query(TR).all()}
    keys = set(et_hechos.keys()) | set(et_esperados.keys()) | {(uid, tid) for uid, tid in asigs_activas}
    start_r = ws2.max_row + 1
    for (uid, tid) in sorted(keys, key=lambda k: (meta_t.get(k[1]).nombre if meta_t.get(k[1]) else "", k[0])):
        t = meta_t.get(tid)
        tarea_name = t.nombre if t else f"Tarea {tid}"
        uobj = db.session.get(USR, uid)
        emp_name = (uobj.nombre if uobj else f"UID {uid}")

        hechos = int(et_hechos.get((uid, tid), 0))
        esper  = int(et_esperados.get((uid, tid), 0))
        noc    = max(0, esper - hechos)
        pct    = (hechos/esper) if esper>0 else 0
        tarde  = int(et_fuera.get((uid, tid), 0))
        sanc_r = int(sanc_rango_map.get((uid, tid), 0))
        sanc_nr= int(sanc_nores_map.get((uid, tid), 0))

        tstats = et_tipo.get((uid, tid), Counter())
        ws2.append([
            emp_name, tid, tarea_name,
            hechos, esper, noc, pct,
            tarde, sanc_r, sanc_nr,
            int(tstats.get("confirmacion", 0)),
            int(tstats.get("asistencia", 0)),
            int(tstats.get("lunch", 0)),
            int(tstats.get("otros", 0))
        ])
    end_r = ws2.max_row

    # formatos
    for row in ws2.iter_rows(min_row=3, max_row=end_r, min_col=1, max_col=14):
        for c in row: c.border = border_all
    for col, w in enumerate([26,10,28,12,12,14,14,14,16,20,12,14,10,10], start=1):
        ws2.column_dimensions[get_column_letter(col)].width = w
    for cell in ws2.iter_rows(min_row=start_r, max_row=end_r, min_col=7, max_col=7):
        for c in cell: c.number_format = "0%"
    if start_r <= end_r:
        ws2.conditional_formatting.add(
            f"G{start_r}:G{end_r}",
            DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1,
                        color="FF10B981", showValue=True)
        )

    # ---------- Hoja: Empleados (rango) ----------
    ws3 = wb.create_sheet("Empleados (rango)")
    ws3.append([f"Rango: {start_d.isoformat()} — {end_d.isoformat()}"])
    ws3.append([])
    ws3.append(["Empleado","Total en rango"])
    for c in ws3[ws3.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    for emp, total in sorted(emp_total.items(), key=lambda x: (-x[1], x[0].lower())):
        ws3.append([emp, total])
    for row in ws3.iter_rows(min_row=3, max_row=ws3.max_row, min_col=1, max_col=2):
        for c in row: c.border = border_all
    ws3.column_dimensions["A"].width = 30
    ws3.column_dimensions["B"].width = 16

    ws3.append([])
    ws3.append(["Empleado","Fecha","Reportes"])
    for c in ws3[ws3.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    start_r = ws3.max_row + 1
    for emp in sorted(emp_dia.keys(), key=lambda x: x.lower()):
        for dia, n in sorted(emp_dia[emp].items()):
            ws3.append([emp, dia, n])
    end_r = ws3.max_row
    for row in ws3.iter_rows(min_row=start_r, max_row=end_r, min_col=1, max_col=3):
        for c in row: c.border = border_all
    ws3.column_dimensions["C"].width = 14

    # ---------- Hoja: Mensual ----------
    ws4 = wb.create_sheet("Mensual")
    ws4.append([f"Rango: {start_d.isoformat()} — {end_d.isoformat()}"])
    ws4.append([])
    ws4.append(["Empleado","Mes (YYYY-MM)","Reportes"])
    for c in ws4[ws4.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    for emp in sorted(emp_mes.keys(), key=lambda x: x.lower()):
        for ym, n in sorted(emp_mes[emp].items()):
            ws4.append([emp, ym, n])
    ws4.append([])
    ws4.append(["Mes (YYYY-MM)","Total"])
    for c in ws4[ws4.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    for ym, n in sorted(total_mes.items()):
        ws4.append([ym, n])
    for row in ws4.iter_rows(min_row=3, max_row=ws4.max_row, min_col=1, max_col=3):
        for c in row: c.border = border_all
    for col in ("A","B","C"): ws4.column_dimensions[col].width = 22

    # ---------- Hoja: Tareas (rango) ----------
    ws5 = wb.create_sheet("Tareas (rango)")
    ws5.append([f"Rango: {start_d.isoformat()} — {end_d.isoformat()} (días={dias_rango})"])
    ws5.append([])
    ws5.append(["ID Tarea","Tarea","Prioridad","Empleados activos","Ventanas/día","Hechos","Esperados (aprox)","No cumpl.","%"])
    for c in ws5[ws5.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")

    start_r = ws5.max_row + 1
    for tid in sorted({t for _, t in asigs_activas} | set(tarea_hechos.keys())):
        t = meta_t.get(tid)
        nombre = t.nombre if t else f"Tarea {tid}"
        prior  = (t.prioridad or "comun") if t else "comun"
        hechos = int(tarea_hechos.get(tid, 0))
        vent   = int(ventanas_cnt.get(tid, 0))
        emp_ct = len(emp_act_por_tarea.get(tid, set()))
        esp    = vent * emp_ct * dias_rango if vent>0 and emp_ct>0 else 0
        noc    = max(0, esp - hechos)
        pct    = (hechos/esp) if esp>0 else 0
        ws5.append([tid, nombre, prior, emp_ct, vent, hechos, esp, noc, pct])
    end_r = ws5.max_row
    for row in ws5.iter_rows(min_row=3, max_row=end_r, min_col=1, max_col=9):
        for c in row: c.border = border_all
    for col, w in enumerate([10,32,12,16,16,14,18,14,12], start=1):
        ws5.column_dimensions[get_column_letter(col)].width = w
    for cell in ws5.iter_rows(min_row=start_r, max_row=end_r, min_col=9, max_col=9):
        for c in cell: c.number_format = "0%"
    if start_r <= end_r:
        ws5.conditional_formatting.add(
            f"I{start_r}:I{end_r}",
            DataBarRule(start_type="num", start_value=0, end_type="num", end_value=1,
                        color="FF3B82F6", showValue=True)
        )

    # ---------- Hoja: Fuera de horario ----------
    ws6 = wb.create_sheet("Fuera de horario")
    ws6.append([f"Rango: {start_d.isoformat()} — {end_d.isoformat()}"])
    ws6.append([])
    ws6.append(["Empleado","ID Tarea","Tarea","Envíos fuera de ventana"])
    for c in ws6[ws6.max_row]: c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    for (uid, tid), n in sorted(et_fuera.items(), key=lambda x: -x[1]):
        uobj = db.session.get(USR, uid)
        emp_name = (uobj.nombre if uobj else f"UID {uid}")
        t = meta_t.get(tid)
        nombre = t.nombre if t else f"Tarea {tid}"
        ws6.append([emp_name, tid, nombre, int(n or 0)])
    for row in ws6.iter_rows(min_row=3, max_row=ws6.max_row, min_col=1, max_col=4):
        for c in row: c.border = border_all
    for col, w in enumerate([26,10,30,18], start=1):
        ws6.column_dimensions[get_column_letter(col)].width = w

    # ---------- Hoja: Glosario ----------
    ws7 = wb.create_sheet("Glosario")
    ws7.append(["Campo / Métrica","Descripción"])
    for c in ws7[1]:
        c.font = H(); c.fill = head_fill; c.alignment = Alignment(horizontal="center")
    glosa = [
        ("Asignaciones activas", "Listado de empleados actualmente asignados a tareas activas."),
        ("Hechos", "Reportes recibidos en el rango seleccionado."),
        ("Esperados", "Ventanas por día × empleados activos × días del rango (aprox.)."),
        ("No-cumplimiento", "max(Esperados − Hechos, 0)."),
        ("% Cumplimiento", "Hechos / Esperados."),
        ("Fuera de horario", "Reportes fuera de cualquier ventana de la tarea (con tolerancia)."),
        ("Sanciones (rango)", "Sanciones de ese empleado en esa tarea creadas dentro del rango."),
        ("Sanciones no resueltas", "Sanciones abiertas (no resueltas) para ese empleado en esa tarea."),
        ("Confirms / Asistencias / Lunch / Otros", "Conteos por tipo de reporte obligatorio y otros dentro del rango.")
    ]
    for k,v in glosa:
        ws7.append([k,v])
    for row in ws7.iter_rows(min_row=1, max_row=ws7.max_row, min_col=1, max_col=2):
        for c in row: c.border = border_all
    ws7.column_dimensions["A"].width = 28
    ws7.column_dimensions["B"].width = 70

    # ===== Descargar =====
    mem = _io.BytesIO(); wb.save(mem); mem.seek(0)
    fname = f"informes_{start_d.isoformat()}_{end_d.isoformat()}.xlsx"
    return send_file(mem,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=fname
    )

# ===== NO CONFIRMADOS  =====
from services.no_confirmados import get_pendientes, aplicar_accion

@app.route("/admin/no-confirmados", methods=["GET", "POST"], endpoint="admin_no_confirmados")
@login_required
@role_required("admin","superadmin")
def admin_no_confirmados():
    session = Asignacion.query.session

    raw_h = request.args.get("h", default="3")
    h = "all" if str(raw_h).lower() in ("all","todo","sin","none","-1","0") else request.args.get("h", type=int, default=3)
    confirm_mode = request.args.get("confirm", default="before")
    include_past = (request.args.get("scope", "future") == "all")
    debug = (request.args.get("debug", type=int) == 1)
    qtext = (request.args.get("q", "") or "").strip()   # <--- NUEVO

    if request.method == "POST":
        action = request.form.get("action", "amonestar")
        ids    = request.form.getlist("asign")
        msg    = request.form.get("mensaje", "Incumplimiento de confirmación de asistencia.")
        nivel  = request.form.get("nivel", "warn")
        count = aplicar_accion(session, Sancion, Asignacion, action, ids, msg, nivel)
        flash((f"{count} {'amonestación' if action=='amonestar' else 'remoción'}(es) aplicadas.", "success")
              if count else ("No se aplicaron cambios.", "warning"))
        # mantener parámetros actuales, incluida la búsqueda
        return redirect(url_for("admin_no_confirmados",
                                h=raw_h, confirm=confirm_mode,
                                scope=("all" if include_past else "future"),
                                q=qtext,
                                debug=int(debug)))

    result = get_pendientes(session, Asignacion, Tarea, Usuario, Reporte,
                            horas_ventana=h, confirm_mode=confirm_mode,
                            include_past=include_past, now_tz="America/Bogota",
                            debug=debug, search_text=qtext)  # <--- pasa q

    return render_template("admin/admin_no_confirmados.html",
                           pendientes=result["items"],
                           h=h,
                           all_mode=result["all_mode"],
                           confirm_mode=result["confirm_mode"],
                           scope=("all" if include_past else "future"),
                           stats=result["stats"],
                           cont=result["cont"],
                           muestras=result.get("muestras_excluidos", []),
                           debug=debug,
                           q=qtext)  # <--- pasar a template


# -------- MODALES EMPLEADO (HTML + JSON informes) --------
@app.route("/admin/tarea/<int:tid>/empleados/modal")
@login_required
@role_required("admin", "superadmin")
def modal_empleados_tarea(tid):
    from zoneinfo import ZoneInfo
    from datetime import datetime, date, time, timedelta
    from collections import defaultdict
    from sqlalchemy import func, and_, or_

    # ====== HELPERS DE FORMATO (NAIVE TAL CUAL) ======
    def _ts_local_str(dt: datetime | None) -> str | None:
        if not dt: return None
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")

    def _ts_hhmm_24(dt: datetime | None) -> str | None:
        if not dt: return None
        return dt.strftime("%H:%M")

    def _ts_hhmm_12(dt: datetime | None) -> str | None:
        if not dt: return None
        return dt.strftime("%I:%M %p").lower()

    # --- NUEVO: helpers que aceptan str/time/datetime ---
    def _coerce_to_hhmm_str(v) -> str | None:
        """
        Devuelve 'HH:MM' desde:
          - '08:30' (str) -> '08:30'
          - time(8,30)    -> '08:30'
          - datetime(...) -> 'HH:MM' de ese dt
        """
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return None
            # soporta '08:30', '08:30:00'
            try:
                if len(s) >= 5 and s[2] == ":":
                    return s[:5]
            except Exception:
                pass
            return None
        if isinstance(v, time):
            return v.strftime("%H:%M")
        if isinstance(v, datetime):
            return v.strftime("%H:%M")
        # por si almacenan como int minutos o algo raro: ignorar
        return None

    def _to_12_from_any(v) -> str | None:
        """
        Convierte cualquiera (str/time/datetime) -> 'hh:mm am/pm'.
        """
        s = _coerce_to_hhmm_str(v)
        if not s:
            return None
        try:
            hh, mm = map(int, s.split(":"))
            dummy = datetime(2000, 1, 1, hh, mm)
            return dummy.strftime("%I:%M %p").lower()
        except Exception:
            return None

    def _parse_hhmm(hhmm: str | time | None) -> time | None:
        if not hhmm:
            return None
        if isinstance(hhmm, time):
            return hhmm
        if isinstance(hhmm, str):
            try:
                hh, mm = (hhmm or "00:00").split(":")
                return time(int(hh), int(mm))
            except Exception:
                return None
        return None

    def _task_tz(t) -> ZoneInfo:
        tzname = (t.tz_name or "America/New_York").strip()
        try:
            return ZoneInfo(tzname)
        except Exception:
            return ZoneInfo("America/New_York")

    def _local_day_bounds_utc(day: date, tz: ZoneInfo):
        ini_local = datetime.combine(day, time.min).replace(tzinfo=tz)
        fin_local = datetime.combine(day, time.max).replace(tzinfo=tz)
        ini_utc = ini_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        fin_utc = fin_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        return ini_utc, fin_utc

    def _to_epoch_utc_naive(dt_utc_naive: datetime) -> int | None:
        if not dt_utc_naive: return None
        return int(dt_utc_naive.replace(tzinfo=ZoneInfo("UTC")).timestamp())

    def _pick_photo(r: "Reporte"):
        try:
            if isinstance(r.payload, dict):
                p = r.payload.get("path") or r.payload.get("foto") or r.payload.get("photo")
                if isinstance(p, str) and p.strip():
                    return p.strip()
        except Exception:
            pass
        return (r.foto_path or "").strip() or None

    def _gps_from_reporte(r: "Reporte"):
        try:
            if isinstance(r.payload, dict):
                g = r.payload.get("coords") or r.payload.get("gps")
                if isinstance(g, dict):
                    lat = g.get("lat") or g.get("latitude")
                    lon = g.get("lon") or g.get("lng") or g.get("longitude")
                    acc = g.get("acc") or g.get("accuracy")
                    if lat is not None and lon is not None:
                        out = {"lat": float(lat), "lon": float(lon)}
                        if acc is not None:
                            out["acc"] = float(acc)
                        return out
        except Exception:
            pass
        if getattr(r, "gps", None):
            try:
                parts = [p.strip() for p in str(r.gps).split(",")]
                if len(parts) >= 2:
                    return {"lat": float(parts[0]), "lon": float(parts[1])}
            except Exception:
                pass
        return None

    def _classify_estado(ini_ts: int, fin_ts: int, tol_min: int, submitted_at: int, now_ts: int) -> str:
        tol = max(0, int(tol_min or 0))
        if submitted_at:
            if submitted_at <= fin_ts:
                return "on_time"
            elif submitted_at <= fin_ts + tol*60:
                return "late"
            else:
                return "late"
        else:
            return "pending" if now_ts <= fin_ts + tol*60 else "missed"

    view = (request.args.get("view") or "").strip().lower()

    # =================== JSON (POR DÍA, sin TZ) ===================
    if view == "informes":
        uid = request.args.get("uid", "").strip()
        if not uid.isdigit():
            return jsonify(ok=False, error="uid inválido"), 400
        uid = int(uid)

        t = Tarea.query.get_or_404(tid)

        # Día a consultar (solo fecha, independiente de zona)
        date_str = (request.args.get("date") or "").strip()
        if date_str:
            try:
                d_day = datetime.strptime(date_str, "%Y-%m-%d").date()
            except Exception:
                return jsonify(ok=False, error="date inválida (YYYY-MM-DD)"), 400
        else:
            d_day = datetime.utcnow().date()

        # valido acepta True / 1 / NULL
        valido_ok = or_(Reporte.valido.is_(True), Reporte.valido == 1, Reporte.valido.is_(None))

        asign = (Asignacion.query
                 .filter(Asignacion.tarea_id == tid,
                         Asignacion.usuario_id == uid,
                         Asignacion.estado == "activa")
                 .first())
        tolerancia_min = (asign.tolerancia_override
                          if asign and asign.tolerancia_override is not None
                          else (t.tolerancia_min or 0))

        # Reportes del DÍA (sin TZ): func.date(timestamp) == fecha
        reports = (Reporte.query
                   .filter(Reporte.tarea_id == tid,
                           Reporte.usuario_id == uid,
                           valido_ok,
                           func.date(Reporte.timestamp) == d_day)
                   .order_by(Reporte.timestamp.asc())
                   .all())

        # Confirmación única (más reciente), sin limitar por día
        conf = (Reporte.query
                .filter(Reporte.tarea_id == tid,
                        Reporte.usuario_id == uid,
                        valido_ok,
                        or_(Reporte.tipo == "asistencia_ligera",
                            Reporte.tipo_especial == "confirmacion"))
                .order_by(Reporte.timestamp.desc())
                .first())

        # Del día
        asis  = next((r for r in reports if r.tipo == "asistencia"), None)
        lunch = next((r for r in reports if r.tipo == "lunch"), None)

        # ===== Ventanas del día (SIEMPRE) + horas 12h directo desde v.hora_ini/fin =====
        ventanas = []
        for v in (t.ventanas or []):
            rep_v = next((r for r in reports if r.report_ventana_id == getattr(v, "id", None)), None)
            submitted_at = _to_epoch_utc_naive(rep_v.timestamp) if rep_v else None
            estado = "enviado" if rep_v else "pendiente"

            # Foto / GPS si hay reporte
            foto_v = _pick_photo(rep_v) if rep_v else None
            gps_v = _gps_from_reporte(rep_v) if rep_v else None

            # *** AQUÍ el cambio: soporta str/time/datetime ***
            hi_12 = _to_12_from_any(getattr(v, "hora_ini", None))
            hf_12 = _to_12_from_any(getattr(v, "hora_fin", None))
            hi_raw = _coerce_to_hhmm_str(getattr(v, "hora_ini", None))
            hf_raw = _coerce_to_hhmm_str(getattr(v, "hora_fin", None))

            ventanas.append(dict(
                report_ventana_id=getattr(v, "id", None),
                nombre=(v.nombre or f"Ventana {v.orden or ''}".strip()),
                tolerancia_min=tolerancia_min,
                estado=estado,
                submitted_at=submitted_at,
                submitted_at_local_str=_ts_local_str(rep_v.timestamp) if rep_v else None,
                submitted_at_hhmm_24=_ts_hhmm_24(rep_v.timestamp) if rep_v else None,
                submitted_at_hhmm_12=_ts_hhmm_12(rep_v.timestamp) if rep_v else None,
                foto_path=foto_v,
                payload={"gps": gps_v} if gps_v else {},
                # Siempre devolvemos las horas de la ventana en 12h
                hora_ini_12=hi_12,
                hora_fin_12=hf_12,
                # y el crudo HH:MM por si lo necesitas en la UI
                hora_ini_raw=hi_raw,
                hora_fin_raw=hf_raw,
            ))

        # Empaquetador de reportes básicos
        def _pack_basic(r: "Reporte"):
            if not r: return None
            return dict(
                ts=_to_epoch_utc_naive(r.timestamp),
                ts_local_str=_ts_local_str(r.timestamp),
                ts_hhmm_24=_ts_hhmm_24(r.timestamp),
                ts_hhmm_12=_ts_hhmm_12(r.timestamp),
                foto_path=_pick_photo(r),
                gps=_gps_from_reporte(r),
                payload=r.payload or {},
                nota=r.nota
            )

        data = dict(
            ok=True,
            tarea_id=tid,
            usuario_id=uid,
            tz=t.tz_name or "America/New_York",   # informativo
            fecha=d_day.isoformat(),
            now_ts=int(datetime.utcnow().timestamp()),
            asistencia_ligera=_pack_basic(conf),
            asistencia=_pack_basic(asis),
            lunch=_pack_basic(lunch),
            ventanas=ventanas
        )
        return jsonify(data), 200

    # =================== HTML (original) ===================
    t = Tarea.query.get_or_404(tid)

    asignaciones = (
        db.session.query(Asignacion, Usuario)
        .join(Usuario, Usuario.id == Asignacion.usuario_id)
        .filter(Asignacion.tarea_id == tid, Asignacion.estado == "activa")
        .order_by(Usuario.nombre.asc())
        .all()
    )

    hoy = datetime.utcnow().date()
    confirmas = (
        db.session.query(Reporte.usuario_id)
        .filter(
            Reporte.tarea_id == tid,
            ((Reporte.tipo_especial == "confirmacion") | (Reporte.tipo == "asistencia_ligera")),
            func.date(Reporte.timestamp) == hoy
        )
        .distinct()
        .all()
    )
    confirmados_hoy = {row[0] for row in confirmas}

    start_7d = hoy - timedelta(days=6)
    stats = {}
    for a, u in asignaciones:
        q = (Reporte.query
             .filter(Reporte.tarea_id == tid,
                     Reporte.usuario_id == u.id,
                     Reporte.timestamp >= datetime.combine(start_7d, time.min))
             .order_by(Reporte.timestamp.asc())
             .all())
        by_date = defaultdict(list)
        for r in q:
            by_date[r.timestamp.date()].append(r)

        horas_sem, horas_hoy = 0.0, 0.0
        for d, arr in by_date.items():
            arr.sort(key=lambda r: r.timestamp)
            asis = next((x for x in arr if x.tipo == "asistencia"), None)
            sal  = next((x for x in arr if x.tipo == "salida"), None)
            if not asis or not sal:
                continue
            base = datetime.combine(d, time.min)
            h_inicio = a.hora_inicio_override or t.hora_inicio
            try:
                start_oficial = _hhmm_to_dt(base, h_inicio)
            except Exception:
                start_oficial = asis.timestamp
            start_real = max(asis.timestamp, start_oficial)
            horas = max(0.0, (sal.timestamp - start_real).total_seconds() / 3600.0)
            horas_sem += horas
            if d == hoy:
                horas_hoy += horas

        tarifa = a.tarifa_hora or 0.0
        stats[a.id] = dict(
            horas_hoy=round(horas_hoy, 2),
            pago_hoy=round(horas_hoy * tarifa, 2),
            horas_7d=round(horas_sem, 2),
            pago_7d=round(horas_sem * tarifa, 2),
        )

    san_counts = {}
    uid_list = [u.id for _, u in asignaciones]
    if uid_list:
        rows = (db.session.query(Sancion.usuario_id, func.count(Sancion.id))
                .filter(and_(Sancion.usuario_id.in_(uid_list), Sancion.resuelta == False))
                .group_by(Sancion.usuario_id).all())
        san_counts = {uid: cnt for uid, cnt in rows}

    return render_template(
        "admin/_modal_empleados.html",
        t=t,
        asignaciones=asignaciones,
        confirmados_hoy=confirmados_hoy,
        stats=stats,
        san_counts=san_counts
    )
 
    
# POST /admin/empleado/<uid>/editar
import re
@app.route("/admin/empleado/<int:uid>/editar", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def empleado_editar(uid):
    from services.asignar_empleado import normalize_phone_e164
    u = Usuario.query.get_or_404(uid)
    nombre = (request.form.get("nombre") or "").strip()
    telefono = normalize_phone_e164(request.form.get("telefono") or "")

    if not nombre or not telefono:
        return jsonify(ok=False, error="Nombre y teléfono son obligatorios."), 400
    if not re.match(r"^\+[1-9]\d{7,14}$", telefono or ""):
        return jsonify(ok=False, error="Teléfono inválido. Usa formato internacional (E.164)."), 400

    clash = Usuario.query.filter(Usuario.telefono==telefono, Usuario.id!=u.id).first()
    if clash:
        return jsonify(ok=False, error="Ese teléfono ya está asignado a otro empleado."), 409

    u.nombre = nombre
    u.telefono = telefono
    db.session.commit()

    try:
        log_event("empleado_edit", empleado_id=u.id, detalle={"nombre": nombre, "telefono": telefono})
    except Exception:
        pass

    return jsonify(ok=True, toast="Empleado actualizado", level="success")


# --- JSON: horas (versión A) ---
@app.route("/admin/tarea/<int:tid>/empleado/<int:uid>/horas", methods=["GET"], endpoint="admin_horas_usuario")
@login_required
@role_required("admin","superadmin")
def admin_horas_usuario(tid, uid):
    days = int(request.args.get("days", 14))
    fin = datetime.utcnow().date()
    ini = fin - timedelta(days=days - 1)

    asign = Asignacion.query.filter_by(tarea_id=tid, usuario_id=uid, estado="activa").first()
    tarea = Tarea.query.get_or_404(tid)
    tarifa = asign.tarifa_hora if asign else 0.0

    q = (Reporte.query
         .filter(Reporte.tarea_id == tid,
                 Reporte.usuario_id == uid,
                 Reporte.timestamp >= datetime.combine(ini, time.min))
         .order_by(Reporte.timestamp.asc())
         .all())

    by_date = defaultdict(list)
    for r in q:
        by_date[r.timestamp.date()].append(r)

    rows = []
    for i in range(days):
        d = ini + timedelta(days=i)
        arr = sorted(by_date.get(d, []), key=lambda r: r.timestamp)
        asis = next((x for x in arr if x.tipo == "asistencia"), None)
        sal  = next((x for x in arr if x.tipo == "salida"), None)
        horas = 0.0
        if asis and sal:
            base = datetime.combine(d, time.min)
            h_inicio = (asign.hora_inicio_override or tarea.hora_inicio) if asign else tarea.hora_inicio
            try:
                start_oficial = _hhmm_to_dt(base, h_inicio)
            except Exception:
                start_oficial = asis.timestamp
            start_real = max(asis.timestamp, start_oficial)
            horas = max(0.0, (sal.timestamp - start_real).total_seconds() / 3600.0)
        rows.append(dict(fecha=d.isoformat(), horas=round(horas, 2), ganado=round(horas * (tarifa or 0.0), 2)))

    return jsonify(ok=True, rows=rows, tarifa=tarifa)

# --- JSON: horas por fecha (mejorado: soporta ?from_start=1) ---
# --- JSON: horas por fecha (usa JORNADAS) ---
@app.route("/admin/tarea/<int:tid>/horas-por-fecha/<int:uid>", methods=["GET"], endpoint="admin_horas_por_fecha")
@login_required
@role_required("admin", "superadmin")
def admin_horas_por_fecha(tid, uid):
    from collections import defaultdict
    from datetime import datetime, timedelta, date

    # --- Parámetros ---
    from_start = request.args.get("from_start", type=int) == 1
    days = request.args.get("days", default=14, type=int)
    if not from_start:
        days = max(1, min(int(days or 14), 90))

    tarea = Tarea.query.get_or_404(tid)

    # --- Tarifa base desde asignación activa ---
    asign = Asignacion.query.filter_by(tarea_id=tid, usuario_id=uid, estado="activa").first()
    tarifa_base = float(asign.tarifa_hora) if (asign and asign.tarifa_hora) else 0.0

    # --- Determinar rango de fechas ---
    hoy = date.today()
    if from_start:
        if getattr(tarea, "fecha_inicio", None):
            desde = tarea.fecha_inicio
        else:
            first_j = (
                Jornada.query.filter_by(usuario_id=uid, tarea_id=tid)
                .order_by(Jornada.fecha.asc())
                .first()
            )
            desde = first_j.fecha if first_j and first_j.fecha else hoy
    else:
        desde = hoy - timedelta(days=days - 1)

    start_dt = datetime(desde.year, desde.month, desde.day, 0, 0, 0)
    end_dt = datetime(hoy.year, hoy.month, hoy.day, 23, 59, 59)

    # --- Traer jornadas ---
    jornadas = (
        Jornada.query.filter(Jornada.usuario_id == uid, Jornada.tarea_id == tid)
        .filter(Jornada.hora_inicio >= start_dt, Jornada.hora_inicio <= end_dt)
        .order_by(Jornada.hora_inicio.asc())
        .all()
    )

    # --- Agrupar por día ---
    horas_por_dia = defaultdict(float)
    dinero_por_dia = defaultdict(float)

    for j in jornadas:
        # Fecha de la jornada
        dia = (
            j.fecha
            if isinstance(j.fecha, date)
            else (j.hora_inicio.date() if j.hora_inicio else hoy)
        )

        # Duración (usa duracion_horas o calcula si no está)
        if j.duracion_horas is not None:
            h = float(j.duracion_horas or 0.0)
        elif j.hora_inicio and j.hora_salida:
            h = round((j.hora_salida - j.hora_inicio).total_seconds() / 3600.0, 2)
        else:
            h = 0.0

        # Tarifa: si la jornada tiene campo tarifa_hora, úsalo; si no, tarifa_base
        tarifa_j = getattr(j, "tarifa_hora", None)
        try:
            tarifa_j = float(tarifa_j) if tarifa_j is not None else None
        except Exception:
            tarifa_j = None
        tarifa_aplicar = tarifa_j if tarifa_j is not None else tarifa_base

        horas_por_dia[dia] += h
        dinero_por_dia[dia] += round(h * tarifa_aplicar, 2)

    # --- Construir salida ---
    total_dias = (hoy - desde).days + 1
    rows = []
    for i in range(total_dias):
        dia = desde + timedelta(days=i)
        f_iso = dia.isoformat()
        h = round(horas_por_dia.get(dia, 0.0), 2)
        g = round(dinero_por_dia.get(dia, 0.0), 2)
        rows.append({"fecha": f_iso, "horas": h, "ganado": g})

    return jsonify(ok=True, rows=rows)



from datetime import datetime, timedelta
import json
from sqlalchemy import or_
from jinja2 import TemplateNotFound

# ===== Historial por tarea (modal) =====
@app.route("/admin/tarea/<int:tid>/historial/modal")
@login_required
@role_required("admin","superadmin")
def modal_historial_tarea(tid):
    t = Tarea.query.get_or_404(tid)

    # Trae eventos recientes de esta tarea
    events = (HistEvento.query
              .filter(HistEvento.tarea_id == tid)
              .order_by(HistEvento.creado_en.desc())
              .limit(200)
              .all())

    # Ids de actores y empleados referenciados en 'detalle'
    uid_actor = {e.usuario_id for e in events if e.usuario_id}
    uid_emps  = set()

    for e in events:
        # Siempre incluye el empleado_id de la fila del evento (si existe)
        if getattr(e, "empleado_id", None):
            try:
                uid_emps.add(int(e.empleado_id))
            except Exception:
                uid_emps.add(e.empleado_id)

        # Además, incluye cualquier UID que venga codificado dentro de 'detalle'
        d = (e.detalle or {}) if isinstance(e.detalle, dict) else {}
        for k in ("uid", "empleado_id", "usuario_id"):
            if d.get(k):
                try:
                    uid_emps.add(int(d[k]))
                except Exception:
                    uid_emps.add(d[k])


    all_uids = {u for u in (uid_actor | uid_emps) if u}
    users = Usuario.query.filter(Usuario.id.in_(all_uids)).all() if all_uids else []
    user_map = {u.id: u for u in users}

    def emp_name(uid):
        u = user_map.get(uid)
        return (u.nombre or f"#{uid}") if u else f"#{uid}"

    def actor_str(e):
        u = user_map.get(e.usuario_id)
        if not u:
            return "—"
        rol = (u.rol or "").lower()
        return f"{u.nombre} · {rol}"

    def money(n):
        try:
            return f"${float(n):.2f}"
        except Exception:
            return str(n)

    def turno_tag(n):
        return f"T{int(n)}" if (n is not None and str(n).strip() != "") else "Sin turno"

    def nice_prioridad(p):
        p = (p or "").lower()
        if p == "turnos":      return "Por turnos"
        if p == "emergencia":  return "Emergencia"
        if p == "comun":       return "Común"
        return p or "—"

    # Humaniza cada evento en una fila lista para la tabla
    rows = []
    for e in events:
        tipo_raw = (e.tipo or "").strip().lower()
        d        = e.detalle if isinstance(e.detalle, dict) else {}

        tipo_badge = "Otro"
        cambio_txt = None

        if tipo_raw in ("tarea_create", "tarea_creacion", "tarea_new"):
            tipo_badge = "Creación"
            cambio_txt = "Creación de tarea"

        elif tipo_raw in ("tarea_estado", "tarea_status"):
            tipo_badge = "Cambio de estado"
            estado = d.get("activa")
            cambio_txt = f"Estado: {'Activa' if estado else 'Finalizada'}"

        elif tipo_raw in ("empleado_add", "empleado_alta", "asign_add"):
            tipo_badge = "Empleado agregado"
            uid = d.get("uid") or d.get("empleado_id") or e.empleado_id
            tarifa = d.get("tarifa_hora")
            turno  = d.get("turno_num")
            parts = [f"Empleado agregado: {emp_name(uid)}"]
            if tarifa is not None: parts.append(f"({money(tarifa)}/h)")
            if turno is not None:  parts.append(f"{turno_tag(turno)}")
            cambio_txt = " ".join(parts)

        elif tipo_raw in ("empleado_remove", "asign_remove", "empleado_baja"):
            tipo_badge = "Empleado removido"
            uid = d.get("uid") or d.get("empleado_id") or e.empleado_id
            cambio_txt = f"Empleado removido: {emp_name(uid)}"

        elif tipo_raw in ("empleado_sancion", "sancion", "sanction"):
            tipo_badge = "Sanción a empleado"
            uid   = d.get("uid") or d.get("empleado_id") or e.empleado_id
            tipo  = (d.get("tipo") or "").capitalize()
            mot   = d.get("motivo") or d.get("motivo_txt") or ""
            cambio_txt = f"Sanción a {emp_name(uid)}: {tipo} — “{mot}”".strip()

        elif tipo_raw in ("asign_update", "asign_override"):
            tipo_badge = "Ajuste de asignación"
            prev = d.get("prev") or {}
            new  = d.get("new")  or {}
            diffs = []
            if "tarifa_hora" in prev or "tarifa_hora" in new:
                diffs.append(f"Tarifa: {money(prev.get('tarifa_hora'))} → {money(new.get('tarifa_hora'))}")
            if "turno_num" in prev or "turno_num" in new:
                diffs.append(f"Turno: {turno_tag(prev.get('turno_num'))} → {turno_tag(new.get('turno_num'))}")
            cambio_txt = " ; ".join(diffs) if diffs else "Actualización de asignación"

        elif tipo_raw in ("tarea_edit", "tarea_update", "tarea_editar"):
            tipo_badge = "Edición de tarea"
            fields = d.get("fields") if isinstance(d.get("fields"), dict) else {}
            pretties = []
            for k, vv in fields.items():
                _prev = vv.get("prev")
                _new  = vv.get("new")

                if k == "nombre":
                    pretties.append(f"Nombre: {_prev or '—'} → {_new or '—'}")
                elif k == "prioridad":
                    pretties.append(f"Prioridad: {nice_prioridad(_prev)} → {nice_prioridad(_new)}")
                elif k in ("hora_inicio", "hora_lunch", "hora_salida"):
                    label = {"hora_inicio":"Hora inicio","hora_lunch":"Hora descanso","hora_salida":"Hora fin"}[k]
                    pretties.append(f"{label}: {_prev or '—'} → {_new or '—'}")
                elif k == "fecha_inicio":
                    pretties.append(f"Fecha de inicio: {_prev or '—'} → {_new or '—'}")
                elif k == "tolerancia_min":
                    pretties.append(f"Tolerancia: {(_prev or 0)} min → {(_new or 0)} min")
                elif k == "tz_name":
                    pretties.append(f"Zona horaria: {(_prev or '—')} → {(_new or '—')}")
                elif k == "requiere_asistencia":
                    pretties.append(f"Check-in requerido: {bool(_prev)} → {bool(_new)}")
                elif k == "requiere_lunch":
                    pretties.append(f"Almuerzo requerido: {bool(_prev)} → {bool(_new)}")
                elif k == "requiere_salida":
                    pretties.append(f"Check-out requerido: {bool(_prev)} → {bool(_new)}")
                elif k == "turnos":
                    try:
                        _p = int(_prev or 0); _n = int(_new or 0)
                    except Exception:
                        _p, _n = _prev, _new
                    pretties.append(f"N.º de turnos: {_p} → {_n}")
                elif k == "reportes":
                    try:
                        _p = int(_prev or 0); _n = int(_new or 0)
                    except Exception:
                        _p, _n = _prev, _new
                    pretties.append(f"Reportes diarios: {_p} → {_n}")
                else:
                    pretties.append(f"{k}: {_prev} → {_new}")

            cambio_txt = " ; ".join(pretties) if pretties else "Edición de tarea"

        else:
            # fallback legible
            from json import dumps
            tipo_badge = (e.tipo or "Evento")
            cambio_txt = dumps(d, ensure_ascii=False) if d else "—"

        rows.append(dict(
            fecha=e.creado_en,
            tipo=tipo_badge,
            cambio=cambio_txt,
            actor=actor_str(e)
        ))

    return render_template("admin/_modal_historial.html", t=t, rows=rows)


# Autocomplete para empleados (por nombre o teléfono)
@app.route("/admin/empleados/buscar")
@login_required
@role_required("admin","superadmin")
def empleados_buscar():
    import re
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])

    q_low = q.lower()
    q_digits = re.sub(r"\D+", "", q)

    qry = Usuario.query.filter(Usuario.rol=="empleado")

    if q_digits:
        usuarios = (qry
            .filter(
                (Usuario.nombre.ilike(f"%{q_low}%")) |
                (Usuario.telefono.ilike(f"%{q_digits}%")) |
                (Usuario.telefono.ilike(f"%{q}%"))
            )
            .order_by(Usuario.nombre.asc()).limit(12).all())
    else:
        usuarios = (qry
            .filter(Usuario.nombre.ilike(f"%{q_low}%"))
            .order_by(Usuario.nombre.asc()).limit(12).all())

    def norm_phone(p):
        return re.sub(r"\D+", "", p or "")

    res = []
    for u in usuarios:
        res.append({
            "id": u.id,
            "name": u.nombre,
            "phone": u.telefono or "",
            "phone_digits": norm_phone(u.telefono or "")
        })
    return jsonify(res)


# app.py
from services.asignar_empleado import (
    asignar_empleado_core,
    upsert_user_by_phone,
    AssignError,
)

# /admin/tarea/<tid>/asignar-ajax  -> crea asignación (nuevo empleado o existente)
# POST /admin/tarea/<tid>/asignar-ajax
@app.post("/admin/tarea/<int:tid>/asignar-ajax")
@login_required
@role_required("admin", "superadmin")
def asignar_empleado_ajax(tid):
    from services.asignar_empleado import (
        upsert_user_by_phone, asignar_empleado_core, AssignError
    )

    try:
        nombre       = (request.form.get("nombre") or "").strip()
        telefono     = (request.form.get("telefono") or "").strip()
        tarifa_hora  = request.form.get("tarifa_hora")
        turno_num    = request.form.get("turno_num")
        doble_turno  = request.form.get("doblar_turno") in ("1", "true", "True", True)

        # 1️⃣ Crear o buscar usuario por teléfono
        u = upsert_user_by_phone(nombre, telefono, db=db, Usuario=Usuario)

        # 2️⃣ Ejecutar asignación (valida turnos, overrides, duplicados, etc.)
        payload, status = asignar_empleado_core(
            tid=tid,
            usuario_id=u.id,
            tarifa_hora=tarifa_hora,
            turno_num=turno_num,
            doble_turno=doble_turno,
            db=db,
            models={"Usuario": Usuario, "Tarea": Tarea, "Asignacion": Asignacion, "Turno": Turno},
            log_event=log_event
        )

        # 3️⃣ Mensaje para feedback (toast global o respuesta AJAX)
        if payload.get("idempotent"):
            payload["toast"] = payload.get("msg") or "Este empleado ya estaba asignado."
            payload["level"] = "info"
        else:
            payload["toast"] = payload.get("msg") or "Empleado agregado a la tarea."
            payload["level"] = "success"

        payload["ok"] = True
        return jsonify(payload), status

    
    except AssignError as e:
        return jsonify(ok=False, error=str(e)), e.status
    except Exception as ex:
        print("❌ Error en asignar_empleado_ajax:", ex)
        return jsonify(ok=False, error="Error interno asignando empleado."), 500


# ---------- Form normal (POST clásico) ----------
@app.route("/admin/asignacion/<int:aid>/editar", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def asignacion_editar(aid):
    from services.asignar_empleado import asignar_empleado_core, AssignError
    asg = Asignacion.query.get_or_404(aid)
    t = Tarea.query.get_or_404(asg.tarea_id)

    data = request.get_json(force=True, silent=True) or {}
    tarifa_hora = data.get("tarifa_hora", asg.tarifa_hora)
    turno_num   = data.get("turno_num", asg.turno_num)

    try:
        payload, status = asignar_empleado_core(
            tid=t.id,
            usuario_id=asg.usuario_id,
            tarifa_hora=tarifa_hora,
            turno_num=turno_num,
            doble_turno=bool(asg.doble_turno),
            db=db,
            models={"Usuario": Usuario, "Tarea": Tarea, "Asignacion": Asignacion, "Turno": Turno},
            log_event=log_event
        )
        # Mensaje para UI
        if payload.get("idempotent"):
            payload["toast"] = "Asignación sin cambios"
            payload["level"] = "info"
        else:
            payload["toast"] = "Asignación actualizada"
            payload["level"] = "success"
        return jsonify(payload), status
    except AssignError as e:
        return jsonify(ok=False, error=str(e)), e.status
    except Exception:
        return jsonify(ok=False, error="No se pudo guardar la asignación."), 500



from datetime import datetime, date, time as dtime, timedelta
from flask import render_template, request
from jinja2 import TemplateNotFound

@app.route("/admin/tarea/<int:tid>/reportes/modal")
@login_required
@role_required("admin","superadmin")
def modal_reportes_empleado(tid):
    uid = request.args.get("uid", type=int)
    if not uid:
        return "<div class='p-3 text-danger'>Empleado inválido</div>"

    # Objetos base
    t = Tarea.query.get_or_404(tid)
    u = Usuario.query.get_or_404(uid)
    tol = int(t.tolerancia_min or 0)

    # Ventanas configuradas (si usas otro nombre del modelo, ajusta aquí)
    ventanas = (ReportVentana.query
                .filter_by(tarea_id=tid)
                .order_by(ReportVentana.orden.asc())
                .all())

    # Todos los reportes del empleado en la tarea
    reps = (Reporte.query
            .filter_by(tarea_id=tid, usuario_id=uid)
            .order_by(Reporte.timestamp.asc())
            .all())

    # Helper de fechas
    def _combine_today(hhmm: str, base_date: date):
        h, m = [int(x) for x in hhmm.split(":")]
        return datetime(base_date.year, base_date.month, base_date.day, h, m)

    now = datetime.now()
    base_day = now.date()

    # Hora inicio/fin de la tarea si están configuradas (HH:MM)
    tarea_ini = _combine_today(t.hora_ini, base_day) if getattr(t, "hora_ini", None) else None
    tarea_fin = _combine_today(t.hora_fin, base_day) if getattr(t, "hora_fin", None) else None

    # --- Reportes especiales ---
    # 1) Confirmación: solo importa existencia.
    rep_confirm = next((r for r in reps if (r.tipo or "").lower() == "confirmacion"), None)
    if rep_confirm is None:
        # a veces lo guardan como 'confirmación' con acento
        rep_confirm = next((r for r in reps if (r.tipo or "").lower() == "confirmación"), None)

    # Estado confirmación:
    # - OK si existe
    # - Pendiente si aún no llega la hora de inicio (o no hay hora)
    # - Falló si ya pasó inicio (+ tol) y no existe
    if rep_confirm:
        estado_confirm = "ok"
        delta_confirm_min = None
    else:
        if tarea_ini:
            limite_conf = tarea_ini + timedelta(minutes=tol)
            estado_confirm = "pendiente" if now <= limite_conf else "fallo"
        else:
            estado_confirm = "pendiente"
        delta_confirm_min = None

    # 2) Asistencia / Check-in con foto: se admite 'asistencia' o, si no hay, 'inicio'
    rep_asist = next((r for r in reps if (r.tipo or "").lower() == "asistencia"), None)
    if rep_asist is None:
        rep_asist = next((r for r in reps if (r.tipo or "").lower() == "inicio"), None)  # compat

    # Ventana de asistencia: desde hora de inicio hasta inicio + 30 min (+ tol)
    # Si no hay hora de inicio, consideramos el día (más la tolerancia)
    delta_asist_min = None
    if tarea_ini:
        asist_ini = tarea_ini - timedelta(minutes=tol)  # permitimos llegar algo antes
        asist_fin = tarea_ini + timedelta(minutes=30 + tol)  # 30 min para check-in + tolerancia
    else:
        asist_ini = datetime.combine(base_day, dtime.min)
        asist_fin = datetime.combine(base_day, dtime.max)

    if rep_asist:
        # retraso contra la hora de inicio real (no contra asist_fin)
        if tarea_ini:
            delta_asist_min = int((rep_asist.timestamp - tarea_ini).total_seconds() // 60)
            if rep_asist.timestamp <= asist_fin:
                estado_asist = "ok" if delta_asist_min <= tol else "retraso"
            else:
                estado_asist = "retraso"
        else:
            estado_asist = "ok"
    else:
        if now > asist_fin:
            estado_asist = "fallo"
        else:
            estado_asist = "pendiente"

    # --- Construcción de filas (rows): especiales + ventanas ---
    rows = []

    rows.append({
        "kind": "special",
        "label": "Confirmación de asistencia",
        "horario": "Antes de iniciar",
        "reporte": rep_confirm,
        "estado": estado_confirm,
        "delta_min": delta_confirm_min,
        "espera_foto": False,
    })

    rows.append({
        "kind": "special",
        "label": "Asistencia (check-in)",
        "horario": (f"{t.hora_ini} – {t.hora_ini}+30 min" if getattr(t,"hora_ini",None) else "Primeros 30 min"),
        "reporte": rep_asist,
        "estado": estado_asist,
        "delta_min": delta_asist_min,
        "espera_foto": True,
    })

    # Ventanas de reporte configuradas
    for v in ventanas:
        # Cálculo de rango con tolerancia
        if v.hora_ini and v.hora_fin:
            vini = _combine_today(v.hora_ini, base_day) - timedelta(minutes=tol)
            vfin = _combine_today(v.hora_fin, base_day) + timedelta(minutes=tol)
        else:
            vini = datetime.combine(base_day, dtime.min)
            vfin = datetime.combine(base_day, dtime.max)

        # Encuentra PRIMER reporte del empleado dentro del rango
        r_match = None
        for r in reps:
            if vini <= r.timestamp <= vfin:
                r_match = r
                break

        # Estado ventana
        estado = "pendiente"
        delta = None
        if r_match:
            # si llega después de hora_fin "real" (sin la tol extra), es retraso
            if v.hora_fin:
                fin_real = _combine_today(v.hora_fin, base_day)
                delta = int((r_match.timestamp - fin_real).total_seconds() // 60)
                estado = "ok" if delta <= tol else "retraso"
            else:
                estado = "ok"
        else:
            if now > vfin:
                estado = "fallo"

        rows.append({
            "kind": "ventana",
            "ventana": v,
            "label": (v.nombre or v.tipo or f"Ventana {v.id}"),
            "horario": (f"{v.hora_ini} – {v.hora_fin}" if (v.hora_ini and v.hora_fin) else "Día completo"),
            "reporte": r_match,
            "estado": estado,
            "delta_min": delta,
            "espera_foto": True,  # usualmente estas ventanas piden foto
        })

    # Render
    try:
        return render_template("admin/_modal_reportes_empleado.html",
                               t=t, u=u, tolerance=tol,
                               rows=rows)
    except TemplateNotFound:
        # Fallback muy básico
        html = ["<div class='container p-2'><h6>Reportes</h6><ul class='list-group'>"]
        for row in rows:
            est = row.get("estado")
            lbl = row.get("label")
            r = row.get("reporte")
            ts = r.timestamp if r else "—"
            html.append(f"<li class='list-group-item'>{lbl} · {est} · {ts}</li>")
        html.append("</ul></div>")
        return "".join(html)

@app.route("/admin/empleado/<int:uid>/desbanear", methods=["POST"])
@login_required
@role_required("admin", "superadmin")
def empleado_desbanear(uid):
    """
    Quita flag de baneo (y bloqueo si corresponde).
    """
    try:
        u = Usuario.query.get_or_404(uid)
        u.baneado = False
        # si quieres también liberar bloqueado:
        # u.bloqueado = False
        db.session.commit()
        try:
            log_event("empleado_unban", empleado_id=u.id)
        except Exception:
            pass
        return jsonify(ok=True)
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="No se pudo desbanear al empleado."), 500

# Remover por tid/uid
# POST /admin/tarea/<tid>/empleado/<uid>/remover
@app.post("/admin/tarea/<int:tid>/empleado/<int:uid>/remover")
@login_required
@role_required("admin","superadmin")
def remover_por_tid_uid(tid, uid):
    a = Asignacion.query.filter_by(tarea_id=tid, usuario_id=uid, estado="activa").first()
    if not a:
        return jsonify(ok=False, error="Asignación no encontrada."), 404
    try:
        # si prefieres hard delete aquí:
        db.session.delete(a)
        db.session.commit()
        try:
            log_event("empleado_remove", tarea_id=tid, empleado_id=uid)
        except Exception:
            pass
        return jsonify(ok=True, toast="Empleado removido de la tarea", level="success")
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="No se pudo remover la asignación."), 500


# Remover asignación (AJAX)
# POST /admin/asignacion/<aid>/remover
@app.route("/admin/asignacion/<int:aid>/remover", methods=["POST"])
@login_required
@role_required("admin","superadmin")
def asignacion_remover(aid):
    try:
        a = Asignacion.query.get_or_404(aid)
        db.session.delete(a)
        db.session.commit()
        try:
            log_event("asign_remove", tarea_id=a.tarea_id, empleado_id=a.usuario_id, detalle={"asignacion_id": a.id})
        except Exception:
            pass
        return jsonify(ok=True, toast="Empleado removido de la tarea", level="success")
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error="No se pudo remover la asignación."), 500


# Listar sanciones de un empleado (opcionalmente filtradas por tarea)
@app.get("/admin/empleado/<int:uid>/sanciones")
@login_required
@role_required("admin", "superadmin")
def empleado_sanciones(uid):
    """Devuelve las sanciones (amonestaciones, bloqueos, baneos) del empleado."""
    try:
        tarea_id = request.args.get("tid", type=int)
        q = Sancion.query.filter(Sancion.usuario_id == uid)
        if tarea_id:
            q = q.filter(Sancion.tarea_id == tarea_id)
        q = q.order_by(Sancion.creada_en.desc())

        rows = []
        for s in q.all():
            rows.append({
                "id": s.id,
                "tipo": s.tipo,  # amonestacion / bloquear / banear
                "motivo": s.mensaje or "",
                "fecha": s.creada_en.strftime("%Y-%m-%d") if s.creada_en else None,
                "nivel": s.nivel,
                "resuelta": bool(s.resuelta),
            })
        return jsonify(ok=True, rows=rows)
    except Exception as e:
        print("❌ Error listando sanciones:", e)
        return jsonify(ok=False, rows=[]), 500


# Listar sanciones de un empleado (opcionalmente filtradas por tarea)
@app.post("/admin/empleado/<int:uid>/sancion")
@login_required
@role_required("admin", "superadmin")
def empleado_sancion(uid):
    from datetime import datetime
    from sqlalchemy.exc import IntegrityError

    tipo_raw = (request.form.get("tipo") or "").strip().lower()
    motivo = (request.form.get("motivo") or "").strip()
    tarea_id = request.form.get("tarea_id", type=int)

    try:
        u = Usuario.query.get_or_404(uid)
        if not motivo:
            return jsonify(ok=False, error="Debes indicar un motivo."), 400

        # 🔹 Normalizar tipo (acepta variaciones)
        tipo_map = {
            "amonestar": "amonestacion",
            "amonestación": "amonestacion",
            "amonestacion": "amonestacion",
            "bloquear": "bloquear",
            "banear": "banear",
        }
        tipo = tipo_map.get(tipo_raw)
        if tipo is None:
            return jsonify(ok=False, error=f"Tipo de sanción inválido: {tipo_raw}"), 400

        # 🔹 Nivel por tipo
        nivel = "warn" if tipo == "amonestacion" else "danger"

        # 🔹 Crear registro en tabla Sancion
        s = Sancion(
            usuario_id=u.id,
            tarea_id=tarea_id,
            tipo=tipo,
            mensaje=motivo,
            creada_en=datetime.utcnow(),
            nivel=nivel,
            resuelta=False,
        )
        db.session.add(s)

        # 🔹 Aplicar efectos por tipo
        if tipo == "bloquear":
            u.bloqueado = True
            for a in Asignacion.query.filter_by(usuario_id=u.id, estado="activa").all():
                a.estado = "inactiva"

        elif tipo == "banear":
            u.bloqueado = True
            u.baneado = True
            for a in Asignacion.query.filter_by(usuario_id=u.id, estado="activa").all():
                a.estado = "inactiva"

        # 🔹 Guardar cambios y registrar evento
        db.session.commit()
        log_event(
            "empleado_sancion",
            empleado_id=u.id,
            tarea_id=tarea_id,
            detalle={"tipo": tipo, "motivo": motivo},
        )

        toast_msg = (
            f"Amonestación registrada correctamente."
            if tipo == "amonestacion"
            else f"Empleado {tipo} correctamente."
        )
        return jsonify(ok=True, toast=toast_msg, level="success"), 200

    except IntegrityError:
        db.session.rollback()
        return jsonify(ok=False, error="Error guardando sanción (DB)."), 500
    except Exception as e:
        db.session.rollback()
        print("❌ Error sancionando:", e)
        return jsonify(ok=False, error="Error interno al registrar sanción."), 500


#DESBLOQUEAR EMPLEADO
@app.post("/admin/empleado/<int:uid>/desbloquear")
@login_required
@role_required("admin", "superadmin")
def empleado_desbloquear(uid):
    """
    Quita el bloqueo (no el baneo) de un empleado.
    No elimina sanciones previas; solo cambia el estado.
    """
    try:
        u = Usuario.query.get_or_404(uid)

        # No permitir desbloquear si está baneado
        if u.baneado:
            return jsonify(ok=False, error="No puedes desbloquear a un empleado baneado. Solo el administrador puede desbanearlo."), 403

        if not u.bloqueado:
            return jsonify(ok=True, toast="El empleado ya estaba desbloqueado.", level="info"), 200

        u.bloqueado = False
        db.session.commit()

        try:
            log_event("empleado_unblock", empleado_id=u.id)
        except Exception:
            pass

        return jsonify(ok=True, toast="Empleado desbloqueado correctamente.", level="success"), 200

    except Exception as e:
        db.session.rollback()
        print("❌ Error desbloqueando empleado:", e)
        return jsonify(ok=False, error="Error interno al desbloquear empleado."), 500


# Finalizar / activar tarea
# Finalizar / activar tarea (con fecha_fin)
@app.route("/admin/tarea/<int:tid>/finalizar", methods=["POST"])
@login_required
@role_required("admin","superadmin")

def finalizar_tarea(tid):
    t = Tarea.query.get_or_404(tid)
    t.activa = False
    # Guarda fecha_fin = hoy en UTC (solo fecha). Si prefieres tz de la tarea, ajustamos.
    t.fecha_fin = datetime.utcnow().date()
    db.session.commit()
    try:
        log_event("tarea_estado", tarea_id=tid, detalle={"activa": False, "fecha_fin": t.fecha_fin.isoformat()})
    except Exception:
        pass

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True)

    flash("Tarea finalizada.", "success")
    return redirect(url_for('admin_tareas'))

@app.route("/admin/tarea/<int:tid>/activar", methods=["POST"])
@login_required
@role_required("admin","superadmin")

def activar_tarea(tid):
    t = Tarea.query.get_or_404(tid)
    t.activa = True
    # Si la reactivas (te equivocaste), limpiamos la fecha_fin
    t.fecha_fin = None
    db.session.commit()
    try:
        log_event("tarea_estado", tarea_id=tid, detalle={"activa": True, "fecha_fin": None})
    except Exception:
        pass

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify(ok=True)

    flash("Tarea activada.", "success")
    return redirect(url_for('admin_tareas'))


# ---------------------- ACCOUNT / MISC ----------------------
@app.route("/logout", methods=["GET"], endpoint="logout")
def do_logout():
    logout_user()
    session.clear()
    return redirect(url_for("index"))

# ---------------------- MIGRACIÓN SQLITE (one-shot) ----------------------
from sqlalchemy import text
import secrets

def migrate_sqlite():
    with app.app_context():
        engine = db.engine
        with engine.connect() as conn:
            # =================================================================
            # 1) --- ASIGNACION: asegurar SOLO columnas del modelo
            # =================================================================
            deseadas_asign = [
                ("id", "INTEGER"),
                ("usuario_id", "INTEGER"),
                ("tarea_id", "INTEGER"),
                ("tarifa_hora", "FLOAT"),
                ("estado", "VARCHAR(20)"),
                ("hora_inicio_override", "VARCHAR(5)"),
                ("tolerancia_override", "INTEGER"),
                ("turno_num", "INTEGER"),
                ("doble_turno", "INTEGER"),            # boolean como entero 0/1
                ("codigo_asignacion", "VARCHAR(24)"),  # unique
            ]

            cols_asign = [row[1] for row in conn.execute(text("PRAGMA table_info(asignacion)"))]
            nombres_deseados = [c[0] for c in deseadas_asign]
            # ¿Hace falta rebuild? Si hay columnas extra o falta alguna
            hay_extras = any(c not in nombres_deseados for c in cols_asign)
            hay_faltantes = any(c not in cols_asign for c in nombres_deseados)

            if hay_extras or hay_faltantes:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(text("BEGIN"))

                # Eliminar índice único viejo si existiera (antes de soltar la tabla)
                conn.execute(text("DROP INDEX IF EXISTS uq_asign_activa"))

                # Crear tabla nueva, con el esquema exacto del modelo
                conn.execute(text("""
                    CREATE TABLE asignacion__new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        usuario_id INTEGER,
                        tarea_id INTEGER,
                        tarifa_hora FLOAT DEFAULT 0.0,
                        estado VARCHAR(20) DEFAULT 'activa',
                        hora_inicio_override VARCHAR(5),
                        tolerancia_override INTEGER,
                        turno_num INTEGER,
                        doble_turno INTEGER DEFAULT 0,
                        codigo_asignacion VARCHAR(24) UNIQUE,
                        FOREIGN KEY(usuario_id) REFERENCES usuario(id),
                        FOREIGN KEY(tarea_id) REFERENCES tarea(id)
                    )
                """))

                # Intersección de columnas (para copiar lo que exista)
                inter_cols = [c for c in nombres_deseados if c in cols_asign and c != "codigo_asignacion"]
                sel_cols = ", ".join(inter_cols) if inter_cols else ""
                ins_cols = ", ".join(inter_cols) if inter_cols else ""

                if sel_cols:
                    # Copiar datos (sin codigo_asignacion porque queremos asegurar no-nulos/únicos)
                    conn.execute(text(f"""
                        INSERT INTO asignacion__new ({ins_cols})
                        SELECT {sel_cols}
                        FROM asignacion
                    """))
                else:
                    # No hay nada que copiar (raro, pero válido). Continúa.
                    pass

                # Rellenar codigo_asignacion nulos con un token aleatorio SQL-side
                # substr(hex(randomblob(12)),1,24) = 24 hex chars
                conn.execute(text("""
                    UPDATE asignacion__new
                    SET codigo_asignacion = COALESCE(
                        codigo_asignacion,
                        substr(hex(randomblob(12)),1,24)
                    )
                """))

                # Normalizar doble_turno a 0/1 por si venía como texto/NULL
                conn.execute(text("""
                    UPDATE asignacion__new
                    SET doble_turno = CASE
                        WHEN doble_turno IN (1, '1', 't', 'true', 'TRUE') THEN 1
                        ELSE 0
                    END
                """))

                # Soltar tabla vieja y renombrar
                conn.execute(text("DROP TABLE asignacion"))
                conn.execute(text("ALTER TABLE asignacion__new RENAME TO asignacion"))

                # Índice único correcto (una asignación activa por tarea/usuario/turno)
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_asign_activa
                    ON asignacion (tarea_id, usuario_id, COALESCE(turno_num, -1))
                    WHERE estado = 'activa'
                """))

                conn.execute(text("COMMIT"))
                conn.execute(text("PRAGMA foreign_keys = ON"))

            else:
                # Si NO hubo rebuild, nos aseguramos de tener las columnas clave (por si faltaba alguna)
                cols_asign = {row[1] for row in conn.execute(text("PRAGMA table_info(asignacion)"))}
                if "turno_num" not in cols_asign:
                    conn.execute(text("ALTER TABLE asignacion ADD COLUMN turno_num INTEGER"))
                if "doble_turno" not in cols_asign:
                    conn.execute(text("ALTER TABLE asignacion ADD COLUMN doble_turno INTEGER DEFAULT 0"))
                if "hora_inicio_override" not in cols_asign:
                    conn.execute(text("ALTER TABLE asignacion ADD COLUMN hora_inicio_override VARCHAR(5)"))
                if "tolerancia_override" not in cols_asign:
                    conn.execute(text("ALTER TABLE asignacion ADD COLUMN tolerancia_override INTEGER"))
                if "codigo_asignacion" not in cols_asign:
                    conn.execute(text("ALTER TABLE asignacion ADD COLUMN codigo_asignacion VARCHAR(24)"))
                    conn.execute(text("""
                        UPDATE asignacion
                        SET codigo_asignacion = substr(hex(randomblob(12)),1,24)
                        WHERE codigo_asignacion IS NULL OR TRIM(codigo_asignacion) = ''
                    """))
                # Índice único (por si faltara)
                conn.execute(text("""
                    CREATE UNIQUE INDEX IF NOT EXISTS uq_asign_activa
                    ON asignacion (tarea_id, usuario_id, COALESCE(turno_num, -1))
                    WHERE estado = 'activa'
                """))

            # =================================================================
            # 2) --- TAREA (igual que tenías)
            # =================================================================
            cols_tarea = {row[1] for row in conn.execute(text("PRAGMA table_info(tarea)"))}
            if "tz_name" not in cols_tarea:
                conn.execute(text("ALTER TABLE tarea ADD COLUMN tz_name VARCHAR(64)"))
            if "tolerancia_min" not in cols_tarea:
                conn.execute(text("ALTER TABLE tarea ADD COLUMN tolerancia_min INTEGER"))
            if "fecha_inicio" not in cols_tarea:
                conn.execute(text("ALTER TABLE tarea ADD COLUMN fecha_inicio DATE"))
            cols_tarea = {row[1] for row in conn.execute(text("PRAGMA table_info(tarea)"))}
            if "fecha_fin" not in cols_tarea:
                conn.execute(text("ALTER TABLE tarea ADD COLUMN fecha_fin DATE"))

            # =================================================================
            # X) --- REPORTE (rebuild si faltan/exceden columnas)
            # =================================================================
            deseadas_rep = [
                ("id", "INTEGER"),
                ("tarea_id", "INTEGER"),
                ("usuario_id", "INTEGER"),
                ("tipo", "VARCHAR(30)"),
                ("foto_path", "VARCHAR(300)"),
                ("gps", "VARCHAR(100)"),
                ("timestamp", "DATETIME"),
                ("valido", "INTEGER"),
                ("tipo_especial", "VARCHAR(20)"),
                ("nota", "TEXT"),
                ("turno_num", "SMALLINT"),
                ("report_ventana_id", "INTEGER"),
                ("payload", "JSON"),  # afinidad JSON (en SQLite es TEXT con check mínimo)
            ]

            cols_rep_info = list(conn.execute(text("PRAGMA table_info(reporte)")))
            cols_rep = [row[1] for row in cols_rep_info]
            nombres_rep = [c[0] for c in deseadas_rep]

            hay_extras_rep = any(c not in nombres_rep for c in cols_rep)
            hay_faltantes_rep = any(c not in cols_rep for c in nombres_rep)

            if hay_extras_rep or hay_faltantes_rep:
                conn.execute(text("PRAGMA foreign_keys = OFF"))
                conn.execute(text("BEGIN"))

                conn.execute(text("""
                    CREATE TABLE reporte__new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        tarea_id INTEGER,
                        usuario_id INTEGER,
                        tipo VARCHAR(30) NOT NULL,
                        foto_path VARCHAR(300),
                        gps VARCHAR(100),
                        timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        valido INTEGER NOT NULL DEFAULT 1,
                        tipo_especial VARCHAR(20),
                        nota TEXT,
                        turno_num SMALLINT,
                        report_ventana_id INTEGER,
                        payload JSON,
                        FOREIGN KEY(tarea_id) REFERENCES tarea(id),
                        FOREIGN KEY(usuario_id) REFERENCES usuario(id),
                        FOREIGN KEY(report_ventana_id) REFERENCES report_ventana(id)
                    )
                """))

                # Intersección para copiar datos existentes
                inter_cols = [c for c in nombres_rep if c in cols_rep]
                if inter_cols:
                    cols_csv = ", ".join(inter_cols)
                    conn.execute(text(f"""
                        INSERT INTO reporte__new ({cols_csv})
                        SELECT {cols_csv}
                        FROM reporte
                    """))

                conn.execute(text("DROP TABLE reporte"))
                conn.execute(text("ALTER TABLE reporte__new RENAME TO reporte"))

                # Índices recomendados
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reporte_tarea_user_ts ON reporte(tarea_id, usuario_id, timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reporte_tipo ON reporte(tipo)"))

                conn.execute(text("COMMIT"))
                conn.execute(text("PRAGMA foreign_keys = ON"))
            else:
                # Si no rebuild, añade columnas faltantes individualmente
                cols_rep_set = {row[1] for row in cols_rep_info}
                if "foto_path" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN foto_path VARCHAR(300)"))
                if "gps" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN gps VARCHAR(100)"))
                if "tipo_especial" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN tipo_especial VARCHAR(20)"))
                if "nota" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN nota TEXT"))
                if "turno_num" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN turno_num SMALLINT"))
                if "report_ventana_id" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN report_ventana_id INTEGER"))
                if "payload" not in cols_rep_set:
                    conn.execute(text("ALTER TABLE reporte ADD COLUMN payload JSON"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reporte_tarea_user_ts ON reporte(tarea_id, usuario_id, timestamp)"))
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_reporte_tipo ON reporte(tipo)"))

            # =================================================================
            # 4) --- USUARIO (igual que tenías)
            # =================================================================
            cols_user = {row[1] for row in conn.execute(text("PRAGMA table_info(usuario)"))}
            if "bloqueado" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN bloqueado INTEGER DEFAULT 0"))
            if "baneado" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN baneado INTEGER DEFAULT 0"))
            if "motivo_bloqueo" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN motivo_bloqueo TEXT"))
            if "motivo_baneo" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN motivo_baneo TEXT"))
            if "nombre_legal_firma" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN nombre_legal_firma VARCHAR(150)"))
            if "onboarding_completo" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN onboarding_completo INTEGER DEFAULT 0"))
            if "last_login" not in cols_user:
                conn.execute(text("ALTER TABLE usuario ADD COLUMN last_login DATETIME"))
            conn.execute(text("UPDATE usuario SET email = NULL WHERE email = ''"))

            # =================================================================
            # 5) --- REPORTE (solo 'nota' como pusiste)
            # =================================================================
            cols_rep = {row[1] for row in conn.execute(text("PRAGMA table_info(reporte)"))}
            if "nota" not in cols_rep:
                conn.execute(text("ALTER TABLE reporte ADD COLUMN nota TEXT"))

            # =================================================================
            # 6) --- HISTORIAL (igual)
            # =================================================================
            conn.execute(text("""CREATE TABLE IF NOT EXISTS hist_evento (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tarea_id INTEGER NULL,
                  empleado_id INTEGER NULL,
                  usuario_id INTEGER NULL,
                  tipo VARCHAR(40) NOT NULL,
                  detalle TEXT NULL,
                  creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hist_evento_tarea ON hist_evento(tarea_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_hist_evento_tipo ON hist_evento(tipo)"))

            # =================================================================
            # 7) --- backfill TZ default
            # =================================================================
            conn.execute(text("""
                UPDATE tarea
                   SET tz_name = 'America/New_York'
                 WHERE (tz_name IS NULL OR TRIM(tz_name) = '')
            """))

            # =================================================================
            # 8) --- pago_semana (igual)
            # =================================================================
            conn.execute(text("""CREATE TABLE IF NOT EXISTS pago_semana (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                usuario_id INTEGER NOT NULL,
                semana_fin DATE NOT NULL,
                horas REAL DEFAULT 0,
                subtotal REAL DEFAULT 0,
                recibo_path VARCHAR(255),
                verificado_empleado INTEGER DEFAULT 0,
                creado_en DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(usuario_id) REFERENCES usuario (id)
            )"""))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_pago_semana_user_week ON pago_semana(usuario_id, semana_fin)"))

            # =================================================================
            # 9) --- jornada (igual)
            # =================================================================
            conn.execute(text("""CREATE TABLE IF NOT EXISTS jornada (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  tarea_id INTEGER NOT NULL,
                  usuario_id INTEGER NOT NULL,
                  fecha DATE NOT NULL,
                  turno_num SMALLINT DEFAULT 1,
                  hora_inicio DATETIME,
                  hora_salida DATETIME,
                  duracion_horas FLOAT,
                  fuente VARCHAR(20) DEFAULT 'reporte',
                  nota VARCHAR(255),
                  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                  FOREIGN KEY(tarea_id) REFERENCES tarea (id),
                  FOREIGN KEY(usuario_id) REFERENCES usuario (id)
            )"""))
            conn.execute(text("""CREATE UNIQUE INDEX IF NOT EXISTS uq_jornada
                                 ON jornada (tarea_id, usuario_id, fecha, turno_num)"""))
            conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_jornada_tarea_fecha
                                 ON jornada (tarea_id, fecha)"""))
            conn.execute(text("""CREATE INDEX IF NOT EXISTS ix_jornada_user_fecha
                                 ON jornada (usuario_id, fecha)"""))

            # Si NO hubo rebuild, nos aseguramos de tener las columnas clave (por si faltaba alguna)
            cols_asign = {row[1] for row in conn.execute(text("PRAGMA table_info(asignacion)"))}

            if "turno_num" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN turno_num INTEGER"))
            if "doble_turno" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN doble_turno INTEGER DEFAULT 0"))
            if "hora_inicio_override" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN hora_inicio_override VARCHAR(5)"))
            if "tolerancia_override" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN tolerancia_override INTEGER"))
            if "codigo_asignacion" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN codigo_asignacion VARCHAR(24)"))
                conn.execute(text("""
                    UPDATE asignacion
                    SET codigo_asignacion = substr(hex(randomblob(12)),1,24)
                    WHERE codigo_asignacion IS NULL OR TRIM(codigo_asignacion) = ''
                """))

            # 🟢 NUEVOS CAMPOS PARA CONFIRMAR ASISTENCIA
            if "confirmado" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN confirmado INTEGER DEFAULT 0"))
            if "confirmado_at" not in cols_asign:
                conn.execute(text("ALTER TABLE asignacion ADD COLUMN confirmado_at DATETIME NULL"))

            # Índice único (por si faltara)
            conn.execute(text("""
                CREATE UNIQUE INDEX IF NOT EXISTS uq_asign_activa
                ON asignacion (tarea_id, usuario_id, COALESCE(turno_num, -1))
                WHERE estado = 'activa'
            """))
            conn.commit()

        # Recomendable (fuera de la transacción) para compactar después del rebuild:
        with engine.connect() as conn:
            try:
                conn.execute(text("VACUUM"))
            except Exception:
                pass


# ---------------------- ARRANQUE ----------------------
if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        migrate_sqlite()
        seed_if_empty()

    # Solo arranca Flask si lo ejecutas localmente
    app.run(debug=True)
else:
    # En producción (Railway), también aseguramos migración
    with app.app_context():
        db.create_all()
        migrate_sqlite()
        seed_if_empty()

# CSRF error handler
@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    try:
        return render_template("errors/400.html", reason=e.description), 400
    except Exception:
        return f"Bad Request: CSRF failed - {e.description}", 400


# >>> SAFE BLOQUE: helpers/telefono (solo si faltan) ================
if 'normalize_e164' not in globals():
    import re as _re_tel
    def only_digits(s: str) -> str:
        return _re_tel.sub(r"\D+", "", (s or ""))
    def normalize_e164(raw: str) -> str:
        s = (raw or '').strip()
        s = _re_tel.sub(r'[\s().-]', '', s)
        if s.startswith('00') and s[2:].isdigit():
            s = '+' + s[2:]
        if not s.startswith('+') and s.isdigit() and 8 <= len(s) <= 15:
            s = '+' + s
        return s
# ===================================================================



# >>> SAFE BLOQUE: helpers/informes (solo si faltan) ================
from datetime import datetime as _dt, timedelta as _td
try:
    from zoneinfo import ZoneInfo as _ZI
except Exception:
    _ZI = None
if 'ahora_local' not in globals():
    def tarea_tz(tarea):
        tzname = getattr(tarea, "tz_name", None) or "UTC"
        return _ZI(tzname) if _ZI else None
    def ahora_local(tarea):
        tz = tarea_tz(tarea)
        return _dt.now(tz) if tz else _dt.utcnow()
    def local_day_bounds(dt_loc):
        start = dt_loc.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = dt_loc.replace(hour=23, minute=59, second=59, microsecond=0)
        return start, end
    def aware_to_utc_naive(dt_aw):
        try:
            return dt_aw.astimezone(_ZI("UTC")).replace(tzinfo=None)
        except Exception:
            return dt_aw
    def parse_hhmm(s: str, fallback="08:00"):
        try:
            h, m = map(int, (s or fallback).split(":"))
            return h, m
        except Exception:
            return 8, 0
    def confirm_window_checker(mode: str, start_local, hours: int, now_local):
        hours = max(0, int(hours or 0))
        pivot = start_local - _td(hours=hours)
        if (mode or "").lower() == "closes_3h_before":
            return now_local <= pivot
        return now_local >= pivot
# ===================================================================



# >>> SAFE BLOQUE: helpers/reportes (solo si faltan) ================
from datetime import timedelta as _td2
if 'ventana_loc_with_tol' not in globals():
    def tolerancia_td(tarea):
        return _td2(minutes=int(getattr(tarea, "tolerancia_min", 0) or 0))
    def ventana_loc_with_tol(tarea, base_loc, ini_str, fin_str):
        tol = tolerancia_td(tarea)
        try:
            h1, m1 = map(int, (ini_str or "00:00").split(":"))
            h2, m2 = map(int, (fin_str or "23:59").split(":"))
        except Exception:
            h1, m1, h2, m2 = 0, 0, 23, 59
        ini = base_loc.replace(hour=h1, minute=m1, second=0, microsecond=0) - tol
        fin = base_loc.replace(hour=h2, minute=m2, second=0, microsecond=0) + tol
        return ini, fin
    def hay_reporte_en_franja(session, usuario_id, tarea_id, desde_utc_naive, hasta_utc_naive,
                              report_ventana_id=None, tipo=None, tipo_especial=None):
        q = session.query(Reporte.id).filter(
            Reporte.usuario_id == usuario_id,
            Reporte.tarea_id == tarea_id,
            Reporte.timestamp >= desde_utc_naive,
            Reporte.timestamp <= hasta_utc_naive
        )
        if report_ventana_id:
            q = q.filter(Reporte.report_ventana_id == report_ventana_id)
        if tipo:
            q = q.filter(Reporte.tipo == tipo)
        if tipo_especial:
            q = q.filter(Reporte.tipo_especial == tipo_especial)
        return session.query(q.exists()).scalar()
# ===================================================================



# >>> SAFE BLOQUE: route guard helper ==================================
def _route_exists(rule: str) -> bool:
    try:
        return any(r.rule == rule for r in app.url_map.iter_rules())
    except Exception:
        return False
# =====================================================================

