# services/hours_service.py
from datetime import datetime, date, time, timedelta
from sqlalchemy import func, and_, or_
from zoneinfo import ZoneInfo

# Importa tu db y modelos reales:
# from app import db
# from models import Usuario, Tarea, Asignacion, Reporte  # ajusta a tus nombres reales

# ────────────────────────────────────────────────────────────────────
# NOTA: Adapta los nombres de tus modelos/campos si difieren.
# Asumo un modelo "Reporte" con campos: id, empleado_id, tarea_id, inicio_ts, fin_ts, minutos, fecha
# y Usuario(nombre, telefono, estado), Tarea(nombre, ubicacion)
# ────────────────────────────────────────────────────────────────────

def paginate(query, page:int, per_page:int=10):
    # Compatible con Flask-SQLAlchemy paginate
    return query.paginate(page=page, per_page=per_page, error_out=False)

def list_hours_base(db, Reporte, Usuario, Tarea, d_from=None, d_to=None, tarea_id=None, empleado_id=None):
    q = (db.session.query(
            Reporte.id.label("rep_id"),
            Reporte.empleado_id,
            Usuario.nombre.label("empleado_nombre"),
            Usuario.telefono.label("empleado_telefono"),
            Reporte.tarea_id,
            Tarea.nombre.label("tarea_nombre"),
            Tarea.ubicacion.label("tarea_ubicacion"),
            Reporte.inicio_ts, Reporte.fin_ts,
            Reporte.minutos, Reporte.fecha
        )
        .join(Usuario, Usuario.id==Reporte.empleado_id)
        .join(Tarea, Tarea.id==Reporte.tarea_id)
        .order_by(Usuario.nombre.asc(), Reporte.fecha.desc(), Reporte.id.desc())
    )
    if d_from: q = q.filter(Reporte.fecha>=d_from)
    if d_to:   q = q.filter(Reporte.fecha<=d_to)
    if tarea_id: q = q.filter(Reporte.tarea_id==tarea_id)
    if empleado_id: q = q.filter(Reporte.empleado_id==empleado_id)
    return q

def group_by_employee(rows):
    """rows: lista de dicts del query. Devuelve dict {empleado_nombre: [rows...]}"""
    grouped = {}
    for r in rows:
        key = r.empleado_nombre
        grouped.setdefault(key, []).append(r)
    return grouped

def task_totals_for_group(rows):
    """Calcula totales por tarea dentro de un grupo (empleado)"""
    tot = {}  # tarea_nombre -> minutos
    for r in rows:
        tot.setdefault(r.tarea_nombre, 0)
        tot[r.tarea_nombre] += (r.minutos or 0)
    # devuelve lista ordenada
    return sorted([(k, v) for k, v in tot.items()], key=lambda x: x[0].lower())

def get_edit_rows_by_task_and_day(db, Reporte, tarea_id:int, target_date:date):
    """Filas editables (una tabla simple)"""
    q = (db.session.query(Reporte)
         .filter(Reporte.tarea_id==tarea_id, Reporte.fecha==target_date)
         .order_by(Reporte.empleado_id.asc(), Reporte.inicio_ts.asc()))
    return q.all()

def get_edit_rows_by_user_and_day(db, Reporte, empleado_id:int, target_date:date):
    q = (db.session.query(Reporte)
         .filter(Reporte.empleado_id==empleado_id, Reporte.fecha==target_date)
         .order_by(Reporte.tarea_id.asc(), Reporte.inicio_ts.asc()))
    return q.all()

def validate_and_apply_update(row, inicio_ts, fin_ts, minutos):
    # Validaciones básicas
    if inicio_ts and fin_ts and fin_ts <= inicio_ts:
        return False, "La hora de fin debe ser mayor que la de inicio."
    if minutos is not None and minutos < 0:
        return False, "Los minutos no pueden ser negativos."
    # Aplica
    if inicio_ts: row.inicio_ts = inicio_ts
    if fin_ts:    row.fin_ts = fin_ts
    if minutos is not None: row.minutos = minutos
    return True, None

def bulk_update_hours(db, Reporte, updates:list[dict]):
    """
    updates: [{id, inicio_ts?, fin_ts?, minutos?}, ...]
    """
    ids = [u["id"] for u in updates]
    rows = db.session.query(Reporte).filter(Reporte.id.in_(ids)).all()
    rows_by_id = {r.id:r for r in rows}
    for u in updates:
        r = rows_by_id.get(u["id"])
        if not r: continue
        ok,msg = validate_and_apply_update(
            r,
            u.get("inicio_ts"), u.get("fin_ts"), u.get("minutos")
        )
        if not ok:
            return False, msg
    db.session.commit()
    return True, None
