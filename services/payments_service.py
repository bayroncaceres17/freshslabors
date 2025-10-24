# services/payments_service.py
from datetime import date, datetime, timedelta, time
from sqlalchemy import func

# Utilidad: semana de canje (viernes→jueves)
def current_week_window(today:date):
    wd = today.weekday()  # Mon=0 ... Sun=6
    offset_since_friday = (wd - 4) % 7
    friday = today - timedelta(days=offset_since_friday)
    thursday = friday + timedelta(days=6)
    return friday, thursday

def week_window_for(any_day:date):
    return current_week_window(any_day)

def minutes_to_hours(mins:int) -> float:
    return round((mins or 0)/60.0, 2)

def subtotal_from_minutes(mins:int, tarifa_hora:float|None):
    th = tarifa_hora or 0.0
    return round(minutes_to_hours(mins) * th, 2)

# ---- motor de minutos (igual lógica que usas en app.py) ----
def _minutes_week_for_user(db, Reporte, Tarea, Asignacion, uid:int, start:date, end:date) -> int:
    minutos = 0
    for i in range(7):
        d = start + timedelta(days=i)
        day_start = datetime.combine(d, time.min)
        day_end   = datetime.combine(d, time.max)
        arr = (db.session.query(Reporte)
               .filter(Reporte.usuario_id==uid,
                       Reporte.timestamp>=day_start, Reporte.timestamp<=day_end)
               .order_by(Reporte.timestamp.asc()).all())
        by_task = {}
        for r in arr:
            by_task.setdefault(r.tarea_id, {"asis":None,"sal":None})
            if (r.tipo or "").lower()=="asistencia" and by_task[r.tarea_id]["asis"] is None:
                by_task[r.tarea_id]["asis"] = r.timestamp
            if (r.tipo or "").lower()=="salida":
                by_task[r.tarea_id]["sal"] = r.timestamp
        for tid, pair in by_task.items():
            asis = pair["asis"]; sal = pair["sal"]
            # hora oficial
            t = db.session.get(Tarea, tid) if tid else None
            h_in = (t.hora_inicio or "08:00") if t else "08:00"
            try:
                h, m = map(int, h_in.split(":"))
                start_of = datetime.combine(d, time(h, m))
            except Exception:
                start_of = None
            if asis and sal:
                start_real = max(asis, start_of) if start_of else asis
                minutos += max(0, int((sal - start_real).total_seconds()//60))
    return minutos

# Lista de NO pagados para un periodo: si include_zero=True, incluye a todos con 0h
# services/payments_service.py  (solo esta función)

def unpaid_list_for_period(db, Usuario, Reporte, PagoSemana, Tarea, Asignacion,
                           start:date, end:date, include_zero:bool=False, tarifa_default:float=0.0):
    """
    Devuelve TODOS los empleados activos NO pagados para la semana [start..end].
    Siempre incluye nombre y teléfono. Si include_zero=True, incluye también los que
    no tengan minutos/horas (0 h) para poder subirles la colilla igual.
    """
    # Pagos ya registrados en esa semana
    pagados_ids = {p.usuario_id for p in db.session.query(PagoSemana)
                   .filter(PagoSemana.semana_fin == end).all()}

    # Empleados activos (trae nombre y telefono)
    activos = (db.session.query(Usuario.id, Usuario.nombre, Usuario.telefono)
               .filter(Usuario.rol == "empleado", Usuario.activo == True)
               .order_by(Usuario.nombre.asc())
               .all())
    # Tarifa promedio por asignaciones activas
    asigs = (db.session.query(Asignacion.usuario_id, func.avg(func.coalesce(Asignacion.tarifa_hora, 0.0)))
             .group_by(Asignacion.usuario_id).all())
    tarifa_map = {uid: float(avg or 0.0) for uid, avg in asigs}

    out = []
    for uid, nom, tel in activos:
        if uid in pagados_ids:
            continue

        # minutos reales del usuario en esa semana (usa tu helper interno)
        mins = _minutes_week_for_user(db, Reporte, Tarea, Asignacion, uid, start, end)
        horas = minutes_to_hours(mins)
        tarifa = tarifa_map.get(uid, tarifa_default)
        subtotal = round(horas * tarifa, 2)

        if include_zero or horas > 0:
            out.append({
                "usuario_id": uid,
                "nombre": nom or f"UID {uid}",
                "telefono": tel or "",
                "horas": horas,
                "subtotal": subtotal,
                "semana_inicio": start,
                "semana_fin": end
            })
    return out


# Compat: lo que ya usabas (semanal actual)
def unpaid_employees_this_week(db, Usuario, Reporte, PagoSemana, tarifa_default:float=0.0, today:date|None=None):
    today = today or date.today()
    start, end = current_week_window(today)
    # Suma minutos por empleado que tuvo reportes; mantiene compat
    minutes_by_user = (
        db.session.query(
            Reporte.usuario_id,
            func.sum(Reporte.id).label("dummy")  # placeholder, no usamos aquí
        )
        .filter(Reporte.timestamp>=datetime.combine(start, time.min),
                Reporte.timestamp<=datetime.combine(end, time.max))
        .group_by(Reporte.usuario_id).all()
    )
    usuarios_ids = [uid for uid,_ in minutes_by_user]
    # Pagos ya hechos esta semana
    pagos = db.session.query(PagoSemana).filter(PagoSemana.semana_fin==end).all()
    pagados_ids = {p.usuario_id for p in pagos}

    out = []
    from sqlalchemy import distinct
    for uid in usuarios_ids:
        if uid in pagados_ids: 
            continue
        mins = _minutes_week_for_user(db, Reporte, None, None, uid, start, end)
        u = db.session.get(Usuario, uid)
        tarifa = tarifa_default
        asigs = db.session.query(Asignacion).filter_by(usuario_id=uid, estado="activa").all()
        if asigs:
            tarifa = round(sum(a.tarifa_hora or 0 for a in asigs)/max(1,len(asigs)), 2)
        out.append({
            "usuario": u, 
            "minutos": mins,
            "horas": minutes_to_hours(mins),
            "subtotal": subtotal_from_minutes(mins, tarifa),
            "semana_inicio": start, "semana_fin": end
        })
    return out

def register_payment_record(db, PagoSemana, usuario_id:int, semana_fin:date, horas:float, subtotal:float, recibo_path:str):
    rec = PagoSemana(
        usuario_id=usuario_id,
        semana_fin=semana_fin,
        horas=horas,
        subtotal=subtotal,
        recibo_path=recibo_path,
        verificado_empleado=False
    )
    db.session.add(rec)
    db.session.commit()
    return rec
