# services/asign_sync.py
from __future__ import annotations
from typing import Optional, Iterable
from datetime import time

def _norm_hhmm(v: Optional[str|time]) -> Optional[str]:
    if not v:
        return None
    if isinstance(v, time):
        return f"{v.hour:02d}:{v.minute:02d}"
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit() and len(s) in (3,4):  # 700 -> 07:00
        s = s.zfill(4)
        return f"{s[:2]}:{s[2:]}"
    if ":" in s:
        p = s.split(":")
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            return f"{int(p[0]):02d}:{int(p[1]):02d}"
    return None

def _asistencia_window_hora_ini(ventanas: Iterable) -> Optional[str]:
    if not ventanas:
        return None
    aliases = {"asistencia", "entrada", "check-in", "check in", "ingreso"}
    best = None
    for v in ventanas:
        nom = (getattr(v, "nombre", "") or "").strip().lower()
        if nom in aliases or any(a in nom for a in aliases):
            best = getattr(v, "hora_ini", None) or best
    return _norm_hhmm(best)

def _hora_por_turno_db(Turno, tarea_id: int, turno_num: Optional[int]) -> Optional[str]:
    if not Turno or not turno_num:
        return None
    try:
        tu = (Turno.query
              .filter(Turno.tarea_id == int(tarea_id),
                      Turno.numero   == int(turno_num))
              .first())
        if not tu:
            return None
        return _norm_hhmm(getattr(tu, "hora_inicio", None))
    except Exception:
        return None

def derivar_hora_entrada(Turno, t, asign, ventanas: Optional[Iterable]=None) -> Optional[str]:
    # 1) Ventana asistencia
    if ventanas:
        h = _asistencia_window_hora_ini(ventanas)
        if h:
            return h
    # 2) Turno en BD
    if getattr(asign, "turno_num", None):
        h = _hora_por_turno_db(Turno, getattr(t, "id"), getattr(asign, "turno_num"))
        if h:
            return h
    # 3) Fallback
    return _norm_hhmm(getattr(t, "hora_inicio", None))

def sync_asignaciones_entrada(db, Tarea, ReportVentana, Asignacion, Turno, tarea_id: int, force: bool=False) -> int:
    t = Tarea.query.get(int(tarea_id))
    if not t:
        return 0
    # Ventanas (si el modelo existe)
    ventanas = []
    try:
        ventanas = (ReportVentana.query
                    .filter(ReportVentana.tarea_id == t.id)
                    .order_by(ReportVentana.orden.asc())
                    .all())
    except Exception:
        ventanas = []
    updated = 0
    for a in Asignacion.query.filter_by(tarea_id=t.id).all():
        nueva = derivar_hora_entrada(Turno, t, a, ventanas)
        if not nueva:
            continue
        cur = (getattr(a, "hora_inicio_override", None) or "").strip()
        if force or not cur or cur != nueva:
            a.hora_inicio_override = nueva
            updated += 1
    if updated:
        db.session.commit()
    return updated

def sync_asignacion_entrada(db, Tarea, ReportVentana, Asignacion, Turno, asign_id: int, force: bool=False) -> bool:
    a = Asignacion.query.get(int(asign_id))
    if not a:
        return False
    t = a.tarea
    ventanas = []
    try:
        ventanas = (ReportVentana.query
                    .filter(ReportVentana.tarea_id == t.id)
                    .order_by(ReportVentana.orden.asc())
                    .all())
    except Exception:
        ventanas = []
    nueva = derivar_hora_entrada(Turno, t, a, ventanas)
    if not nueva:
        return False
    cur = (getattr(a, "hora_inicio_override", None) or "").strip()
    if not force and cur and cur == nueva:
        return False
    a.hora_inicio_override = nueva
    db.session.commit()
    return True
