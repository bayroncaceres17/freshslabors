# services/empleado_helpers.py
from __future__ import annotations
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
from typing import Tuple, Optional

DEFAULT_TZ = "America/New_York"   # USA por defecto

# ----------------------------- TZ & parsing -----------------------------

def tz_of_task(t) -> str:
    """Devuelve tz_name de la tarea o DEFAULT_TZ si falta."""
    return (getattr(t, "tz_name", None) or DEFAULT_TZ).strip() or DEFAULT_TZ

def _parse_hhmm(val: str | time | None, fallback: str = "08:00") -> time:
    """Acepta 'HH:MM' o time; si falla => fallback."""
    if isinstance(val, time):
        return val
    s = (val or fallback).strip()
    try:
        hh, mm = map(int, s.split(":")[:2])
        return time(hh, mm, 0)
    except Exception:
        hh, mm = map(int, fallback.split(":"))
        return time(hh, mm, 0)

def aware_start_dt(t) -> Optional[datetime]:
    """
    Datetime de inicio (aware) usando:
      - t.fecha_inicio (date) + t.hora_inicio ('HH:MM')
      - si falta fecha/hora, devuelve None
    """
    d = getattr(t, "fecha_inicio", None)
    h = _parse_hhmm(getattr(t, "hora_inicio", None))
    if not d:
        return None
    tz = ZoneInfo(tz_of_task(t))
    return datetime(d.year, d.month, d.day, h.hour, h.minute, 0, tzinfo=tz)

def now_in_tz(tz_name: str) -> datetime:
    """Ahora aware en la TZ indicada."""
    return datetime.now(ZoneInfo(tz_name))

def local_day(tz_name: str, dt_aw: datetime) -> date:
    """Fecha local (date) para mostrar/particionar archivos por día."""
    return dt_aw.astimezone(ZoneInfo(tz_name)).date()

# ----------------------------- Ventanas & tardanza -----------------------------

def calc_lateness(now_aw: datetime, target_aw: datetime, tolerancia_min: int = 0) -> Tuple[str, int]:
    """
    Devuelve ('on_time'|'late', minutes_late) comparando now vs target + tolerancia.
    Los dos datetimes deben ser aware en la misma TZ.
    """
    limit = target_aw + timedelta(minutes=max(0, int(tolerancia_min or 0)))
    late = int(max(0, (now_aw - limit).total_seconds()) // 60)
    return ("late" if late > 0 else "on_time", late)

def asistencia_window(t) -> Tuple[Optional[datetime], Optional[datetime], str]:
    """
    Ventana del REPORTE de asistencia (con foto/GPS).
    Regla de negocio:
      - habilita 30 min ANTES del inicio
      - cierra 20 min DESPUÉS del inicio
    La tolerancia 't.tolerancia_min' se respeta adicionalmente.
    """
    tz = tz_of_task(t)
    start = aware_start_dt(t)
    if not start:
        return None, None, tz
    # tolerancia propia de la tarea
    tol_min = int(getattr(t, "tolerancia_min", 0) or 0)
    ini = start - timedelta(minutes=30 + tol_min)
    fin = start + timedelta(minutes=20 + tol_min)
    return ini, fin, tz

def can_confirm_assistance(t) -> Tuple[bool, int]:
    """
    Confirmación LIGERA (botón) previa al reporte formal.
    Regla:
      - habilita desde 3 horas ANTES del inicio (y permite después, marcando tardanza)
    Retorna (can_confirm, minutes_late respecto al inicio).
    """
    tz = tz_of_task(t)
    start = aware_start_dt(t)
    if not start:
        # si no tenemos inicio configurado, dejamos confirmar (sin penalizar)
        return True, 0
    now = now_in_tz(tz)
    can = now >= (start - timedelta(hours=3))
    _, late = calc_lateness(now, start, 0)
    return can, late


def ahora_local(tarea):
    """
    'Ahora' con tzinfo de la tarea.
    """
    tz = (getattr(tarea, "tz_name", None) or DEFAULT_TZ).strip() or DEFAULT_TZ
    return datetime.now(ZoneInfo(tz))


def dentro_ventana_reporte(tarea, ventana, now_loc: datetime) -> bool:
    """
    True si now_loc cae dentro de la ventana de 'ventana' considerando tolerancia.
    - Soporta con_horario=False (si no hay horario, siempre permitido).
    - Soporta ventanas que cruzan medianoche (ej. 22:00–02:00).
    - Usa tolerancia_min de la tarea.
    IMPORTANTE: now_loc debe venir aware en la TZ de la tarea (usa ahora_local()).
    """
    # sin horario: siempre permitido
    if not getattr(ventana, "con_horario", True):
        return True

    tol_min = int(getattr(tarea, "tolerancia_min", 0) or 0)
    tol = timedelta(minutes=tol_min)

    # Reaprovechamos tu _parse_hhmm que retorna time
    try:
        h_ini = _parse_hhmm(getattr(ventana, "hora_ini", "00:00"))
        h_fin = _parse_hhmm(getattr(ventana, "hora_fin", "23:59"))
    except Exception:
        # fallback defensivo
        from datetime import time
        h_ini, h_fin = time(0, 0), time(23, 59)

    ini = now_loc.replace(hour=h_ini.hour, minute=h_ini.minute, second=0, microsecond=0)
    fin = now_loc.replace(hour=h_fin.hour, minute=h_fin.minute, second=0, microsecond=0)

    # ventana normal (mismo día)
    if fin >= ini:
        return (ini - tol) <= now_loc <= (fin + tol)

    # cruza medianoche: interpretamos fin como del día siguiente
    fin = fin + timedelta(days=1)
    now_norm = now_loc + timedelta(days=1) if now_loc < ini else now_loc
    return (ini - tol) <= now_norm <= (fin + tol)