# services/horas_turno.py
from __future__ import annotations
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional
from flask import current_app as app

# ---------------------------------------------------------------------------
# Helpers de acceso a modelos y TZ
# ---------------------------------------------------------------------------

def _models(app_):
    """
    Devuelve (db, Tarea, Usuario, Asignacion, Turno, Jornada) desde la app.
    Compatible con app.config["DB"] o con app.config["MODELS"]["db"].
    """
    M = app_.config["MODELS"]
    db = M.get("db") or app_.config.get("DB")
    if db is None:
        raise KeyError("No se encontró 'db' en app.config['MODELS']['db'] ni en app.config['DB']")
    return (
        db,
        M["Tarea"],
        M["Usuario"],
        M["Asignacion"],
        M["Turno"],
        M["Jornada"],
    )


def _get_task_tz(tarea) -> str:
    """Devuelve la zona horaria preferida de la tarea o un default."""
    return getattr(tarea, "tz_name", None) or "America/Bogota"


def _safe_str(x: Any) -> str:
    return "" if x is None else str(x)


def _hm_to_time(hhmm: str) -> time:
    """Convierte 'HH:MM' a time."""
    hhmm = (hhmm or "").strip()
    hh, mm = hhmm.split(":")
    return time(int(hh), int(mm))


def _combine_dt(fecha: date, hhmm: str) -> datetime:
    """Combina fecha + 'HH:MM' en datetime naive (hora servidor)."""
    t = _hm_to_time(hhmm)
    return datetime(fecha.year, fecha.month, fecha.day, t.hour, t.minute, 0)


def _recalc_duracion(j) -> None:
    """Recalcula j.duracion_horas si hay hora_inicio y hora_salida."""
    hi = getattr(j, "hora_inicio", None)
    hs = getattr(j, "hora_salida", None)
    if hi and hs:
        delta = hs - hi
        j.duracion_horas = round(max(0.0, delta.total_seconds() / 3600.0), 2)


def _whatsapp_link(telefono: Optional[str]) -> Optional[str]:
    """Genera link de WhatsApp a partir del número."""
    tel = (telefono or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")
    if not tel:
        return None
    if tel.startswith("+"):
        tel = tel[1:]
    if not tel.isdigit():
        return None
    return f"https://wa.me/{tel}"


# ---------------------------------------------------------------------------
# Snapshot para el modal (tarea + fecha)
# ---------------------------------------------------------------------------

def collect_task_day_snapshot(tid: int, fecha: date) -> Dict[str, Any]:
    """
    Genera snapshot de la tarea (con sus turnos y empleados) para el modal de jornada.
    Incluye los turnos definidos y los turnos reales por empleado.
    Sincroniza automáticamente las jornadas con el turno_num actual de Asignacion.
    """
    db, Tarea, Usuario, Asignacion, Turno, Jornada = _models(app)
    tarea = Tarea.query.get_or_404(tid)
    tz = _get_task_tz(tarea)

    # --- Turnos definidos en la tarea
    turnos_def = (
        Turno.query
        .filter(Turno.tarea_id == tid)
        .order_by(Turno.numero.asc())
        .all()
    )
    tiene_turnos = len(turnos_def) > 0

    def _hora_str(v) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, str):
            return v[:5]
        if isinstance(v, time):
            return f"{v.hour:02d}:{v.minute:02d}"
        return None

    turnos_meta = [
        {
            "numero": t.numero,
            "label": f"T{t.numero}",
            "hora_inicio": _hora_str(getattr(t, "hora_inicio", None)),
            "hora_fin": _hora_str(getattr(t, "hora_fin", None)),
        }
        for t in turnos_def
    ]

    # --- Asignaciones activas (siempre actualizadas)
    asigns = (
        Asignacion.query
        .filter_by(tarea_id=tid, estado="activa")
        .all()
    )

    for a in asigns:
        try:
            db.session.refresh(a)
        except Exception:
            pass

    if not asigns:
        return {
            "tarea": {
                "id": tarea.id,
                "nombre": getattr(tarea, "nombre", f"Tarea {tarea.id}"),
                "tiene_turnos": tiene_turnos,
                "turnos": turnos_meta,
                "tz": tz,
            },
            "fecha": fecha.isoformat(),
            "empleados": [],
        }

    uids = [a.usuario_id for a in asigns]

    # --- Jornadas reales del día
    jornadas = (
        Jornada.query
        .filter(Jornada.tarea_id == tid, Jornada.usuario_id.in_(uids), Jornada.fecha == fecha)
        .order_by(Jornada.turno_num.asc())
        .all()
    )

    by_user: Dict[int, List[Any]] = {}
    for j in jornadas:
        by_user.setdefault(j.usuario_id, []).append(j)

    empleados: List[Dict[str, Any]] = []

    for a in asigns:
        try:
            db.session.refresh(a)
        except Exception:
            pass

        u = a.usuario
        nombre = getattr(u, "nombre", f"Usuario {a.usuario_id}")
        telefono = getattr(u, "telefono", None)
        whatsapp = _whatsapp_link(telefono)

        # Turno real actual de asignación
        turno_asignado_raw = getattr(a, "turno_num", None)
        try:
            turno_asignado = int(turno_asignado_raw) if turno_asignado_raw is not None else 1
        except Exception:
            turno_asignado = 1

        tarifa_hora = float(a.tarifa_hora or 0.0)

        jlist = by_user.get(a.usuario_id, [])
        turnos_list: List[Dict[str, Any]] = []

        # 🧩 Sincronizar cada jornada con el turno actual
        for j in jlist:
            if j.turno_num != turno_asignado:
                j.turno_num = turno_asignado
                db.session.add(j)
            turnos_list.append({
                "turno_num": j.turno_num,
                "hora_inicio": j.hora_inicio.isoformat() if j.hora_inicio else None,
                "hora_salida": j.hora_salida.isoformat() if j.hora_salida else None,
            })

        if not jlist:
            turnos_list.append({
                "turno_num": turno_asignado,
                "hora_inicio": None,
                "hora_salida": None,
            })

        sugerencias = []
        for tm in turnos_meta:
            hn, hf = tm.get("hora_inicio"), tm.get("hora_fin")
            sug_ini = _combine_dt(fecha, hn).isoformat() if hn else None
            sug_fin = _combine_dt(fecha, hf).isoformat() if hf else None
            sugerencias.append({
                "turno_num": tm["numero"],
                "hora_inicio_sugerida": sug_ini,
                "hora_salida_sugerida": sug_fin,
            })

        empleados.append({
            "usuario_id": a.usuario_id,
            "nombre": nombre,
            "telefono": telefono,
            "whatsapp": whatsapp,
            "tarifa_hora": tarifa_hora,
            "asignacion_turno": turno_asignado,
            "turno_num": turno_asignado,
            "turnos": turnos_list,
            "sugerencias": sugerencias,
        })

    # 🔄 Commit final: sincroniza todos los turnos modificados
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    empleados.sort(key=lambda e: _safe_str(e["nombre"]).lower())

    return {
        "tarea": {
            "id": tarea.id,
            "nombre": getattr(tarea, "nombre", f"Tarea {tarea.id}"),
            "tiene_turnos": tiene_turnos,
            "turnos": turnos_meta,
            "tz": tz,
        },
        "fecha": fecha.isoformat(),
        "empleados": empleados,
    }


# ---------------------------------------------------------------------------
# Registro de inicio/salida (individual)
# ---------------------------------------------------------------------------

def _get_or_create_jornada(Tarea, Jornada, t, usuario_id: int, fecha: date, turno_num: Optional[int]):
    """Obtiene o crea la jornada única (tarea_id, usuario_id, fecha, turno_num).
       Si ya existe y el turno_num difiere del actual, lo actualiza."""
    tn = int(turno_num or 1)
    j = (
        Jornada.query
        .filter_by(tarea_id=t.id, usuario_id=usuario_id, fecha=fecha)
        .first()
    )
    if j:
        if j.turno_num != tn:
            j.turno_num = tn
        return j
    return Jornada(tarea_id=t.id, usuario_id=usuario_id, fecha=fecha, turno_num=tn)


def registrar_inicio(t, usuario_id: int, fecha: date, hhmm: str, *, fuente="manual", turno_num=None) -> Dict[str, Any]:
    """Registra o actualiza hora_inicio en Jornada usando turno real si no se pasa."""
    db, Tarea, Usuario, Asignacion, Turno, Jornada = _models(app)
    if not isinstance(fecha, date):
        raise ValueError("fecha inválida")
    if not getattr(t, "id", None):
        t = Tarea.query.get_or_404(int(t))

    asign = Asignacion.query.filter_by(tarea_id=t.id, usuario_id=usuario_id, estado="activa").first()
    turno_real = (turno_num if turno_num is not None else getattr(asign, "turno_num", 1))
    try:
        turno_real = int(turno_real)
    except Exception:
        turno_real = 1

    j = _get_or_create_jornada(Tarea, Jornada, t, usuario_id, fecha, turno_real)
    j.turno_num = turno_real  # 🔒 fuerza sincronía
    j.hora_inicio = _combine_dt(fecha, hhmm)
    j.fuente = (fuente or "manual")[:20]
    _recalc_duracion(j)
    db.session.add(j)
    db.session.flush()
    return {"ok": True, "msg": "Hora de entrada registrada", "jornada_id": j.id}


def registrar_salida(t, usuario_id: int, fecha: date, hhmm: str, *, fuente="manual", turno_num=None) -> Dict[str, Any]:
    """Registra o actualiza hora_salida en Jornada usando turno real si no se pasa."""
    db, Tarea, Usuario, Asignacion, Turno, Jornada = _models(app)
    if not isinstance(fecha, date):
        raise ValueError("fecha inválida")
    if not getattr(t, "id", None):
        t = Tarea.query.get_or_404(int(t))

    asign = Asignacion.query.filter_by(tarea_id=t.id, usuario_id=usuario_id, estado="activa").first()
    turno_real = (turno_num if turno_num is not None else getattr(asign, "turno_num", 1))
    try:
        turno_real = int(turno_real)
    except Exception:
        turno_real = 1

    j = _get_or_create_jornada(Tarea, Jornada, t, usuario_id, fecha, turno_real)
    j.turno_num = turno_real  # 🔒 fuerza sincronía
    j.hora_salida = _combine_dt(fecha, hhmm)
    j.fuente = (fuente or "manual")[:20]
    _recalc_duracion(j)
    db.session.add(j)
    db.session.flush()
    return {"ok": True, "msg": "Hora de salida registrada", "jornada_id": j.id}
