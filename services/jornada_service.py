# services/jornada_service.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple, Dict, Any, List
from zoneinfo import ZoneInfo

from sqlalchemy import func  # (and_ no se usa aquí)

# Helpers propios
from services.empleado_helpers import (
    aware_start_dt, tz_of_task, now_in_tz, local_day, calc_lateness
)

__all__ = [
    # ventanas/asistencia
    "asistencia_window_3h",
    "compute_entrada_regla",
    "upsert_jornada_entrada",
    "ensure_entrada_en_start_si_falta",
    # builders de reportes
    "build_reporte_confirmacion",
    "build_reporte_asistencia",
    "build_reporte_lunch",
    "build_reporte_ventana",
    # cierre de jornada y emergencias
    "cerrar_jornada_con_salida",
    "marcar_pendientes_emergencia_hoy",
    # checks/listados
    "hoy_tiene_asistencia",
    "get_emergencias_del_empleado",
    # tiempos centralizados
    "parse_device_ts_to_utc",
    "choose_used_utc",
    "utcaware_to_db_naive_utc",
    "resolve_times_for_report",
]

# -------------------------------------------------------------------
# Ventana de asistencia (primer reporte)
# -------------------------------------------------------------------
def asistencia_window_3h(tarea) -> Tuple[datetime, datetime, str, datetime]:
    """
    Abre 3 h antes del inicio y cierra 30 min después.
    Retorna: (ini_loc, fin_loc, tz_name, start_loc)  -> todos AWARE (TZ de la tarea).
    """
    tz = tz_of_task(tarea) or "America/New_York"
    tzinfo = ZoneInfo(tz)
    start = aware_start_dt(tarea)
    if not start:
        now = datetime.now(tzinfo)
        return now - timedelta(minutes=180), now + timedelta(minutes=30), tz, now
    ini = start - timedelta(minutes=180)
    fin = start + timedelta(minutes=30)
    return ini, fin, tz, start


# antes devolvía UTC naive; ahora devolvemos LOCAL naive para DB



def resolve_times_for_report(tarea, device_ts: Optional[str]) -> Tuple[datetime, datetime, str, Dict[str, Any]]:
    device_utc, server_utc, used_utc, used_source, drift_sec = choose_used_utc(device_ts)

    tz_name = tz_of_task(tarea)  # p.ej. "America/Bogota"
    now_loc = used_utc.astimezone(ZoneInfo(tz_name))

    # ✅ GUARDAR SIEMPRE **LOCAL NAIVE** (hora de la tarea / del dispositivo)
    ts_db = now_loc.replace(tzinfo=None)

    meta = {
        "device_ts": device_ts,
        "device_utc": device_utc.isoformat() if device_utc else None,
        "server_received_utc": server_utc.isoformat(),
        "used_source": used_source,
        "clock_drift_seconds": drift_sec,
        "tz_name": tz_name,
        "submitted_ts_loc": now_loc.isoformat(),
        "fecha_dia": local_day(tz_name, now_loc).isoformat(),
    }
    return ts_db, now_loc, tz_name, meta



# -------------------------------------------------------------------
# Reglas entrada (a tiempo / tarde) y hora_inicio efectiva
# -------------------------------------------------------------------
def compute_entrada_regla(now_loc: datetime, start_loc: datetime):
    """
    Si confirmas antes del inicio => entrada = start_loc; si no => entrada = now_loc.
    Retorna: (entrada_loc, status, minutes_late) con status 'on_time'|'late'.
    """
    status, minutes_late = calc_lateness(now_loc, start_loc, 0)
    entrada_loc = start_loc if now_loc < start_loc else now_loc
    return entrada_loc, status, minutes_late

# -------------------------------------------------------------------
# Upsert jornada.hora_inicio (naive; no sobreescribe si ya existe)
# -------------------------------------------------------------------
def upsert_jornada_entrada(db, Jornada, tarea, asign, entrada_loc: datetime, start_loc: datetime):
    """
    - Fecha de la jornada = día LOCAL del start_loc (día de la tarea).
    - No pisa hora_inicio si ya existe.
    - Guarda DATETIME naive (tu modelo actual).
    """
    fecha_local = start_loc.date()
    tn = getattr(asign, "turno_num", None) or 1
    j = (Jornada.query
         .filter_by(tarea_id=tarea.id, usuario_id=asign.usuario_id,
                    fecha=fecha_local, turno_num=tn)
         .first())
    entrada_naive = entrada_loc.replace(tzinfo=None)
    if j:
        if not j.hora_inicio:
            j.hora_inicio = entrada_naive
            db.session.add(j)
        return j
    j = Jornada(
        tarea_id=tarea.id,
        usuario_id=asign.usuario_id,
        fecha=fecha_local,
        turno_num=tn,
        hora_inicio=entrada_naive,
        fuente="reporte",
    )
    db.session.add(j)
    return j

# -------------------------------------------------------------------
# Asegurar entrada en el inicio del turno/tarea si falta
# -------------------------------------------------------------------
def ensure_entrada_en_start_si_falta(db, Jornada, tarea, asign, now_aw: datetime):
    """
    Si no hay jornada del día con hora_inicio, la crea con el inicio del turno/tarea
    (asign.hora_inicio_override HH:MM > aware_start_dt(tarea) > now_aw).
    """
    tz = tz_of_task(tarea)
    fecha_local = local_day(tz, now_aw)
    tn = getattr(asign, "turno_num", None) or 1

    j = (Jornada.query
         .filter_by(tarea_id=tarea.id, usuario_id=asign.usuario_id,
                    fecha=fecha_local, turno_num=tn)
         .first())

    if j and j.hora_inicio:
        return j

    hora_ini = getattr(asign, "hora_inicio_override", None)
    if hora_ini:
        try:
            hh, mm = map(int, hora_ini.split(":"))
            start_dt = now_aw.replace(hour=hh, minute=mm, second=0, microsecond=0)
        except Exception:
            start_dt = aware_start_dt(tarea) or now_aw
    else:
        start_dt = aware_start_dt(tarea) or now_aw

    start_naive = start_dt.replace(tzinfo=None)

    if j:
        j.hora_inicio = start_naive
        db.session.add(j)
        return j

    j = Jornada(
        tarea_id=tarea.id,
        usuario_id=asign.usuario_id,
        fecha=fecha_local,
        turno_num=tn,
        hora_inicio=start_naive,
        fuente="reporte",
    )
    db.session.add(j)
    return j

# -------------------------------------------------------------------
# Builders de Reporte (NO comitean) – timestamps coherentes
# -------------------------------------------------------------------
def build_reporte_confirmacion(
    Reporte, user_id: int, tarea, *,
    ts_db: datetime,            # ← UTC naive para DB
    now_loc: datetime,          # ← hora local (payload/reglas)
    start_loc: datetime,
    tz_name: str,
    meta: Dict[str, Any] | None = None,
):
    _, status, minutes_late = compute_entrada_regla(now_loc, start_loc)
    rep = Reporte(
        usuario_id=user_id,
        tarea_id=tarea.id,
        tipo="asistencia_ligera",
        timestamp=ts_db,  # UTC naive
        valido=True,
    )
    if hasattr(Reporte, "tipo_especial"):
        rep.tipo_especial = "confirmacion"
    base = {
        "source": "confirmar_tarea_btn",
        "late": (status == "late"),
        "minutes_late": minutes_late,
        "target_ts": start_loc.isoformat(),
        "submitted_ts": now_loc.isoformat(),
        "fecha_dia": local_day(tz_name, start_loc).isoformat(),
        "tz_name": tz_name,
    }
    if hasattr(Reporte, "payload"):
        rep.payload = {**base, **(meta or {})}
    return rep

def build_reporte_asistencia(
    Reporte, user_id: int, tarea, *,
    ts_db: datetime, now_loc: datetime, start_loc: datetime, tz_name: str,
    in_window: bool,
    nota: Optional[str],
    lat: Optional[str],
    lng: Optional[str],
    device_ts: Optional[str],
    path_abs: Optional[str] = None,
    meta: Dict[str, Any] | None = None,
):
    _, status, minutes_late = compute_entrada_regla(now_loc, start_loc)
    rep = Reporte(
        usuario_id=user_id,
        tarea_id=tarea.id,
        tipo="asistencia",
        timestamp=ts_db,  # UTC naive
        valido=True,
    )
    if hasattr(Reporte, "payload"):
        rep.payload = {
            "nota": nota,
            "coords": {"lat": lat, "lng": lng},
            "device_ts": device_ts,
            "target_ts": start_loc.isoformat(),
            "submitted_ts": now_loc.isoformat(),
            "in_window": bool(in_window),
            "status": status,
            "minutes_late": minutes_late,
            "fecha_dia": local_day(tz_name, start_loc).isoformat(),
            "path": (path_abs or "").replace("\\", "/") if path_abs else None,
            "tz_name": tz_name,
            **(meta or {})
        }
    return rep

def build_reporte_lunch(
    Reporte, user_id: int, tarea, *,
    ts_db: datetime, now_loc: datetime, tz_name: str,
    opcion: str,                      # 'si' | 'no'
    foto_rel: Optional[str],
    gps_str: Optional[str],
    turno_num: Optional[int],
    lat: Optional[str] = None,
    lng: Optional[str] = None,
    device_ts: Optional[str] = None,
    meta: Dict[str, Any] | None = None,
):
    day = local_day(tz_name, now_loc).isoformat()
    rep = Reporte(
        usuario_id=user_id,
        tarea_id=tarea.id,
        tipo="lunch",
        timestamp=ts_db,  # UTC naive
        valido=True,
        foto_path=(foto_rel if opcion == "si" else None),
        gps=gps_str or None,
        turno_num=turno_num,
    )
    if hasattr(Reporte, "payload"):
        rep.payload = {
            "opcion": opcion,
            "coords": {"lat": lat, "lng": lng},
            "device_ts": device_ts,
            "submitted_ts": now_loc.isoformat(),
            "fecha_dia": day,
            "path": (foto_rel or None),
            "tz_name": tz_name,
            **(meta or {})
        }
    return rep

def build_reporte_ventana(
    Reporte, user_id: int, tarea, *,
    ts_db: datetime, now_loc: datetime, tz_name: str,
    ventana_id: int,
    turno_num: Optional[int],
    foto_rel: Optional[str],
    gps_str: Optional[str],
    device_ts: Optional[str] = None,
    nota: Optional[str] = None,
    meta: Dict[str, Any] | None = None,
):
    day = local_day(tz_name, now_loc).isoformat()
    rep = Reporte(
        usuario_id=user_id,
        tarea_id=tarea.id,
        tipo="ventana",
        timestamp=ts_db,  # UTC naive
        valido=True,
        report_ventana_id=ventana_id,
        turno_num=turno_num,
        foto_path=foto_rel or None,
        gps=gps_str or None,
        nota=nota or None,
    )
    if hasattr(Reporte, "payload"):
        base = {
            "device_ts": device_ts,
            "tz_name": tz_name,
            "fecha_dia": day,
            "source": "ventana",
        }
        if nota:
            base["nota"] = nota
        if foto_rel:
            base["path"] = foto_rel
        if meta:
            base.update(meta)
        rep.payload = base
    return rep

# -------------------------------------------------------------------
# Cierre de jornada (salida)
# -------------------------------------------------------------------
def cerrar_jornada_con_salida(db, Jornada, tarea, asign, now_aw: datetime):
    tz = tz_of_task(tarea)
    day_local = local_day(tz, now_aw)
    tn = getattr(asign, "turno_num", None) or 1

    j = (Jornada.query
         .filter_by(tarea_id=tarea.id, usuario_id=asign.usuario_id,
                    fecha=day_local, turno_num=tn)
         .first())

    # ✅ asegurar local de tarea antes de quitar tz
    salida_naive = now_aw.astimezone(ZoneInfo(tz)).replace(tzinfo=None)

    if j:
        if not j.hora_salida:
            j.hora_salida = salida_naive
            if j.hora_inicio:
                delta = salida_naive - j.hora_inicio
                j.duracion_horas = round(delta.total_seconds()/3600.0, 2)
            db.session.add(j)
        return j

    j = Jornada(
        tarea_id=tarea.id,
        usuario_id=asign.usuario_id,
        fecha=day_local,
        turno_num=tn,
        hora_salida=salida_naive,
        fuente="reporte",
    )
    db.session.add(j)
    return j


# -------------------------------------------------------------------
# Rango 'hoy' (UTC naive) y utilidades
# -------------------------------------------------------------------
def _hoy_rango_utc(tz_name: str, now_aw: datetime) -> Tuple[datetime, datetime]:
    """
    Devuelve (ini_utc_naive, fin_utc_naive) que cubre el 'hoy' local.
    """
    d = local_day(tz_name, now_aw)
    ini_loc = now_aw.astimezone(ZoneInfo(tz_name)).replace(
        year=d.year, month=d.month, day=d.day, hour=0, minute=0, second=0, microsecond=0
    )
    fin_loc = ini_loc + timedelta(days=1) - timedelta(microseconds=1)
    ini_utc = ini_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    fin_utc = fin_loc.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return ini_utc, fin_utc

def marcar_pendientes_emergencia_hoy(db, Reporte, ReportVentana,
                                     tarea, usuario_id: int,
                                     tz_name: str, now_aw: datetime,
                                     gps: Optional[str], device_ts: Optional[str],
                                     turno_num: Optional[int]) -> int:
    """
    Si NO hay reporte hoy del usuario para una ventana, crea un Reporte 'ventana' con nota de emergencia.
    """
    ini_utc, fin_utc = _hoy_rango_utc(tz_name, now_aw)
    creados = 0
    ventanas = (ReportVentana.query
                .filter_by(tarea_id=tarea.id)
                .order_by(ReportVentana.orden)
                .all())

    for v in ventanas:
        existe = (Reporte.query
                  .filter(Reporte.tarea_id == tarea.id,
                          Reporte.usuario_id == usuario_id,
                          Reporte.report_ventana_id == v.id,
                          Reporte.timestamp >= ini_utc,
                          Reporte.timestamp <= fin_utc)
                  .first())
        if existe:
            continue

        r = Reporte(
            tarea_id=tarea.id,
            usuario_id=usuario_id,
            tipo="ventana",
            turno_num=turno_num,
            report_ventana_id=v.id,
            gps=gps,
            # ✅ local de la tarea, naive
            timestamp=now_aw.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None),
            valido=True,
            nota="Marcado por salida de emergencia"
        
        )
        if hasattr(Reporte, "payload"):
            r.payload = {
                "emergencia": True,
                "device_ts": device_ts,
                "tz_name": tz_name,
                "fecha_dia": local_day(tz_name, now_aw).isoformat(),
                "auto_generado": True
            }
        db.session.add(r)
        creados += 1

    return creados

# -------------------------------------------------------------------
# Checks y listados
# -------------------------------------------------------------------
def hoy_tiene_asistencia(db, Reporte, tarea, usuario_id: int, tz_name: str, now_aw: datetime) -> bool:
    """
    True si hoy (local) ya existe un reporte del usuario tipo asistencia/asistencia_ligera.
    """
    ini_utc, fin_utc = _hoy_rango_utc(tz_name, now_aw)
    q = (Reporte.query
         .filter(Reporte.tarea_id == tarea.id,
                 Reporte.usuario_id == usuario_id,
                 Reporte.timestamp >= ini_utc,
                 Reporte.timestamp <= fin_utc,
                 Reporte.tipo.in_(("asistencia", "asistencia_ligera"))))
    return db.session.query(q.exists()).scalar()

def get_emergencias_del_empleado(Tarea, Asignacion, usuario_id: int, *, exclude_tid: int | None = None) -> List[Dict[str, Any]]:
    q = (Tarea.query
         .join(Asignacion, Asignacion.tarea_id == Tarea.id)
         .filter(Asignacion.usuario_id == usuario_id,
                 Asignacion.estado == "activa"))

    # Solo activas
    if hasattr(Tarea, "activa"):
        q = q.filter((Tarea.activa == True) | (Tarea.activa == 1))  # soporta bool/int

    # Solo emergencias (según el campo disponible)
    if hasattr(Tarea, "prioridad"):
        q = q.filter(func.lower(Tarea.prioridad) == "emergencia")
    elif hasattr(Tarea, "tipo"):
        q = q.filter(func.lower(Tarea.tipo).in_(("emergencia", "emergency")))
    elif hasattr(Tarea, "categoria"):
        q = q.filter(func.lower(Tarea.categoria) == "emergencia")
    else:
        return []

    if exclude_tid:
        q = q.filter(Tarea.id != exclude_tid)

    items: List[Dict[str, Any]] = []
    for t in q.order_by(Tarea.nombre.asc()).all():
        items.append({
            "id": t.id,
            "nombre": t.nombre,
            "prioridad": getattr(t, "prioridad", None),
            "tipo": getattr(t, "tipo", None),
        })
    return items


# -------------------------------------------------------------------
# Tiempos centralizados
# -------------------------------------------------------------------
def parse_device_ts_to_utc(device_ts: Optional[str]) -> Optional[datetime]:
    """
    device_ts: ISO8601 del teléfono. Acepta 'Z' o con offset (+hh:mm).
    Retorna datetime AWARE en UTC o None si no parsea.
    """
    if not device_ts:
        return None
    try:
        s = device_ts.strip()
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(ZoneInfo("UTC"))
    except Exception:
        return None

def choose_used_utc(device_ts: Optional[str]) -> Tuple[Optional[datetime], datetime, datetime, str, Optional[float]]:
    """
    Aplica reglas de confianza:
      - Acepta device_utc si |drift|<=12h y no >10min en el futuro.
    Retorna: (device_utc, server_utc, used_utc, used_source, drift_seconds).
    """
    device_utc = parse_device_ts_to_utc(device_ts)
    server_utc = datetime.now(tz=timezone.utc)

    used_source = "server"
    drift_sec: Optional[float] = None
    used_utc = server_utc

    if device_utc is not None:
        drift_sec = (server_utc - device_utc).total_seconds()
        if abs(drift_sec) <= 12*3600 and device_utc <= server_utc + timedelta(minutes=10):
            used_utc = device_utc
            used_source = "device"

    return device_utc, server_utc, used_utc, used_source, drift_sec

def utcaware_to_db_naive_utc(dt_aw: datetime) -> datetime:
    """
    Convierte un datetime AWARE (cualquier TZ) a naive **en UTC** para guardar en DB.
    """
    return dt_aw.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)



