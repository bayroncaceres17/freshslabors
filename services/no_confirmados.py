# services/no_confirmados.py
from datetime import datetime, date, time
from zoneinfo import ZoneInfo
from urllib.parse import quote

DEFAULT_TZ = "America/New_York"
UMBRAL_MIN = 180  # 3 horas para "≤3h"

# ----------------- Normalización y utilidades -----------------

def _tz_of_task(t):
    return (getattr(t, "tz_name", None)
            or getattr(t, "zona_iana", None)
            or getattr(t, "zona_horaria", None)
            or getattr(t, "timezone", None)
            or DEFAULT_TZ)

def _coerce_date(d):
    if d is None: return None
    if isinstance(d, date): return d
    if isinstance(d, str):
        s = d.strip()
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
            try: return datetime.strptime(s, fmt).date()
            except ValueError: pass
    return None

def _coerce_time(h):
    if h is None: return None
    if isinstance(h, time): return h
    if isinstance(h, str):
        s = h.strip()
        if s.isdigit() and len(s) in (4, 6):
            try:
                hh, mm = int(s[:2]), int(s[2:4])
                ss = int(s[4:6]) if len(s) == 6 else 0
                return time(hh, mm, ss)
            except Exception:
                pass
        for fmt in ("%H:%M:%S", "%H:%M"):
            try: return datetime.strptime(s, fmt).time()
            except ValueError: pass
    return None

def _aware_start_dt(t):
    if hasattr(t, "start_ts") and getattr(t, "start_ts"):
        ts = getattr(t, "start_ts")
        return ts if ts.tzinfo else ts.replace(tzinfo=ZoneInfo("UTC"))
    d = _coerce_date(getattr(t, "fecha_inicio", None))
    h = _coerce_time(getattr(t, "hora_inicio", None))
    if not (d and h): return None
    return datetime.combine(d, h).replace(tzinfo=ZoneInfo(_tz_of_task(t)))

def _wa_link(numero: str, texto: str) -> str:
    """
    Acepta:
      - E.164: +17865551234  (se usa tal cual)
      - 10 dígitos US: 7865551234 -> +1 7865551234
    """
    n = "".join(ch for ch in (numero or "") if ch.isdigit() or ch == "+").strip()
    if n.startswith("+"):
        pass
    else:
        only = "".join(ch for ch in n if ch.isdigit())
        if len(only) == 10:  # US local
            n = "+1" + only
        else:
            n = only  # si no podemos inferir, dejamos bruto
    return f"https://wa.me/{n.replace('+','') }?text={quote(texto)}" if n else "#"

def _to_same_tz(dt, tzinfo):
    if dt is None: return None
    if dt.tzinfo is None: return dt.replace(tzinfo=tzinfo)
    return dt.astimezone(tzinfo)

# ----------------- Confirmación: autodetect -----------------
def _esta_confirmado(session, a, Reporte, start_local, mode="before"):
    """
    Prioridad:
      1) Asignacion.confirmado_at (datetime)
      2) Asignacion.confirmado (bool)
      3) Reporte(tipo_especial='confirmacion')
    """
    # 1) Confirmado con timestamp
    if hasattr(a, "confirmado_at") and getattr(a, "confirmado_at"):
        cat = getattr(a, "confirmado_at")
        try:
            tzinfo = start_local.tzinfo if start_local else ZoneInfo("UTC")
            cat_local = _to_same_tz(cat, tzinfo)
            if mode == "before" and start_local:
                return cat_local <= start_local
            elif mode == "today" and start_local:
                start_today = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
                return cat_local >= start_today
            else:
                return True
        except Exception:
            return True

    # 2) Confirmado (boolean simple)
    if hasattr(a, "confirmado") and getattr(a, "confirmado"):
        return True

    # 3) Reporte de tipo confirmacion (histórico)
    if Reporte is None:
        return False

    rep = (session.query(Reporte)
           .filter(Reporte.usuario_id == a.usuario_id,
                   Reporte.tarea_id == a.tarea_id,
                   getattr(Reporte, "tipo_especial") == "confirmacion")
           .order_by(Reporte.timestamp.desc())
           .first())

    if not rep:
        return False

    rep_ts_local = _to_same_tz(getattr(rep, "timestamp"), start_local.tzinfo if start_local else ZoneInfo("UTC"))
    if mode == "before" and start_local:
        return rep_ts_local <= start_local
    elif mode == "today" and start_local:
        start_today = start_local.replace(hour=0, minute=0, second=0, microsecond=0)
        return rep_ts_local >= start_today
    return True

# ----------------- Servicio principal -----------------

def get_pendientes(
    session, Asignacion, Tarea, Usuario, Reporte=None,
    horas_ventana=None, confirm_mode="before",
    include_past=False, now_tz: str = DEFAULT_TZ,
    debug: bool = False, search_text: str | None = None,
) -> dict:
    all_mode = (horas_ventana in (None, "all", "0", 0, "-1", -1))
    h = None if all_mode else max(1, min(int(horas_ventana or 3), 24))
    now = datetime.now(ZoneInfo(now_tz))

    def _col(model, *names):
        for n in names:
            if hasattr(model, n):
                return getattr(model, n)
        return None

    usuario_nombre_col = _col(Usuario, "nombre", "name")
    tarea_nombre_col   = _col(Tarea,   "nombre", "name", "titulo", "title")

    from sqlalchemy import or_
    q = (session.query(Asignacion)
         .join(Tarea, Asignacion.tarea_id == Tarea.id)
         .join(Usuario, Asignacion.usuario_id == Usuario.id)
         .filter(getattr(Asignacion, "estado").in_(["activa", "asignado", "activo"]))
         .filter(getattr(Tarea, "activa") == True)
         .filter(getattr(Usuario, "activo") == True))
    if search_text:
        terms = [t.strip() for t in search_text.split() if t.strip()]
        for t in terms:
            like = f"%{t}%"
            ors = []
            if usuario_nombre_col is not None:
                ors.append(usuario_nombre_col.ilike(like))
            if tarea_nombre_col is not None:
                ors.append(tarea_nombre_col.ilike(like))
            if ors:
                q = q.filter(or_(*ors))

    items, muestras = [], []
    stats = {"tot_asign": 0, "sin_start": 0, "fuera_ventana": 0, "ya_confirmado": 0, "incluidos": 0}
    cont  = {"CONFIRMADO": 0, "A_TIEMPO": 0, "POR_CONFIRMAR": 0, "INCUMPLIMIENTO": 0, "SIN_HORA": 0}

    for a in q.all():
        stats["tot_asign"] += 1
        t, u = a.tarea, a.usuario
        start = _aware_start_dt(t)

        if start is None:
            stats["sin_start"] += 1
            now_local = now
            mins_to = None
            start_txt = "SIN HORA"
            tz_name = _tz_of_task(t)
            confirmado = _esta_confirmado(session, a, Reporte, now, mode=confirm_mode)
            estado_conf = "SIN_HORA"; puede_amonestar = False; puede_remover = False
        else:
            now_local = now.astimezone(start.tzinfo)
            mins_to = int((start - now_local).total_seconds() // 60)
            if all_mode:
                if not include_past and mins_to < 0:
                    stats["fuera_ventana"] += 1
                    if debug and len(muestras) < 10:
                        muestras.append({"razon": "pasada", "tarea_id": t.id, "mins_to": mins_to})
                    continue
            else:
                if mins_to < 0 or mins_to > h * 60:
                    stats["fuera_ventana"] += 1
                    if debug and len(muestras) < 10:
                        muestras.append({"razon": "fuera_ventana", "tarea_id": t.id, "mins_to": mins_to})
                    continue
            confirmado = _esta_confirmado(session, a, Reporte, start, mode=confirm_mode)
            start_txt = start.strftime("%Y-%m-%d %H:%M")
            tz_name = _tz_of_task(t)
            if confirmado:
                estado_conf = "CONFIRMADO"; puede_amonestar = False; puede_remover = False
            else:
                if mins_to > UMBRAL_MIN:
                    estado_conf = "A_TIEMPO";      puede_amonestar = False; puede_remover = False
                elif 0 <= mins_to <= UMBRAL_MIN:
                    estado_conf = "POR_CONFIRMAR"; puede_amonestar = True;  puede_remover = False
                else:
                    estado_conf = "INCUMPLIMIENTO"; puede_amonestar = True; puede_remover = True

        if estado_conf == "CONFIRMADO":
            stats["ya_confirmado"] += 1
            cont["CONFIRMADO"] += 1
        else:
            cont[estado_conf] += 1

        emp_nom   = getattr(u, "nombre", f"Empleado {getattr(u, 'id', '?')}")
        tel       = getattr(u, "telefono", "")
        tarea_nom = getattr(t, "nombre", f"Tarea {getattr(t, 'id', '?')}")
        wa_msg = (f"Hola {emp_nom}, "
                  f"{('faltan ' + str(mins_to) + ' min ') if (mins_to is not None and mins_to >= 0) else ''}"
                  f"para tu turno en '{tarea_nom}'. Por favor confirma tu asistencia en el sistema.")
        items.append({
            "asignacion_id": a.id,
            "empleado_id": u.id,
            "empleado": emp_nom,
            "telefono": tel,
            "tarea_id": getattr(t, "id", None),
            "tarea": tarea_nom,
            "inicio_txt": start_txt,
            "tz": tz_name,
            "mins_to_start": mins_to if mins_to is not None else 10**9,
            "wa": _wa_link(tel, wa_msg),
            "estado_conf": estado_conf,
            "puede_amonestar": puede_amonestar,
            "puede_remover": puede_remover,
        })
        stats["incluidos"] += 1

    items.sort(key=lambda x: (x["mins_to_start"], x["tarea"]))
    out = {"items": items, "stats": stats, "all_mode": all_mode, "confirm_mode": confirm_mode, "cont": cont}
    if debug: out["muestras_excluidos"] = muestras
    return out

# ----------------- Acciones -----------------

def aplicar_accion(session, Sancion, Asignacion, action: str, asign_ids: list[int],
                   msg: str, nivel: str = "warn") -> int:
    """
    Crea sanciones o remueve asignaciones según 'action'.
    action ∈ {'amonestar', 'remover'}
    """
    total = 0
    for sid in asign_ids or []:
        try:
            sid_int = int(sid)
        except Exception:
            continue
        a = session.get(Asignacion, sid_int)
        if not a:
            continue
        if action == "amonestar":
            # registra sanción mínima
            session.add(Sancion(usuario_id=a.usuario_id, tarea_id=a.tarea_id,
                                tipo="asistencia", mensaje=msg, nivel=nivel))
            total += 1
        elif action == "remover":
            # marca estado o elimina
            if hasattr(a, "estado"):
                a.estado = "removida"
            else:
                session.delete(a)
            total += 1
    if total:
        session.commit()
    return total
