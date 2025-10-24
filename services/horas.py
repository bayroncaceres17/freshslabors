# services/horas.py
from __future__ import annotations

from datetime import datetime, date, timedelta
from collections import defaultdict
from typing import Optional, Dict, List, Tuple

from flask import current_app
from sqlalchemy import func


# ======================== acceso a modelos/sesión ========================
def _models():
    """
    Recupera los modelos registrados en app.config["MODELS"] y la sesión.
    Deben existir: Usuario, Tarea, Asignacion, Jornada. El resto son opcionales.
    """
    app = current_app
    M = app.config["MODELS"]

    Usuario       = M["Usuario"]
    Tarea         = M["Tarea"]
    Asignacion    = M["Asignacion"]
    # opcionales (no usados para calcular horas):
    Reporte       = M.get("Reporte")
    Sancion       = M.get("Sancion")
    ReportVentana = M.get("ReportVentana")
    Turno         = M.get("Turno")
    Jornada       = M["Jornada"]

    session = Usuario.query.session
    return session, Usuario, Tarea, Asignacion, Reporte, Sancion, ReportVentana, Turno, Jornada


# ============================== utilidades ===============================
# services/horas.py (agrega esto arriba, con las utilidades)


def week_bounds_fri_thu(any_day: date):
    """Devuelve (inicio, fin) de la semana anclada a viernes→jueves."""
    shift_to_fri = (any_day.weekday() - 4) % 7  # 0=lun..4=vie..6=dom
    start = any_day - timedelta(days=shift_to_fri)
    end   = start + timedelta(days=6)
    return start, end


def _daterange(d_from: date, d_to: date):
    d = d_from
    while d <= d_to:
        yield d
        d += timedelta(days=1)


def _round2(x) -> float:
    return round(float(x or 0.0) + 1e-9, 2)


def _round1(x) -> float:
    return round(float(x or 0.0) + 1e-9, 1)


def _initials(name: str) -> str:
    parts = (name or "?").split()
    return "".join(p[0].upper() for p in parts[:2]) or "?"


def _safe_hours(j) -> float:
    """
    Horas de la jornada: usa j.duracion_horas si existe, o calcula con inicio/salida.
    """
    if getattr(j, "duracion_horas", None) is not None:
        return float(j.duracion_horas or 0.0)
    if j.hora_inicio and j.hora_salida:
        return max(0.0, (j.hora_salida - j.hora_inicio).total_seconds() / 3600.0)
    return 0.0


# =============== recolector principal basado en Jornada ==================
def _collect_jornadas(d_from: date, d_to: date, usuario_id: Optional[int], tarea_id: Optional[int]):
    session, Usuario, Tarea, Asignacion, _Reporte, Sancion, _ReportVentana, _Turno, Jornada = _models()

    # Jornadas en rango
    jq = session.query(Jornada).filter(Jornada.fecha >= d_from, Jornada.fecha <= d_to)
    if usuario_id:
        jq = jq.filter(Jornada.usuario_id == usuario_id)
    if tarea_id:
        jq = jq.filter(Jornada.tarea_id == tarea_id)
    jornadas = jq.all()

    # Asignaciones (para tarifa_hora y existencia de pares sin jornada)
    asg_q = (
        session.query(Asignacion)
        .join(Tarea, Tarea.id == Asignacion.tarea_id)
        .join(Usuario, Usuario.id == Asignacion.usuario_id)
    )
    if usuario_id:
        asg_q = asg_q.filter(Asignacion.usuario_id == usuario_id)
    if tarea_id:
        asg_q = asg_q.filter(Asignacion.tarea_id == tarea_id)
    asigns = asg_q.all()

    pairs = {(a.usuario_id, a.tarea_id) for a in asigns} | {(j.usuario_id, j.tarea_id) for j in jornadas}
    if not pairs:
        # estructuras vacías para el caller
        return set(), {}, {}, {}, defaultdict(list), defaultdict(int)

    uids = {u for (u, _) in pairs}
    tids = {t for (_, t) in pairs}

    usuarios = {u.id: u for u in session.query(Usuario).filter(Usuario.id.in_(uids or {0})).all()}
    tareas   = {t.id: t for t in session.query(Tarea).filter(Tarea.id.in_(tids or {0})).all()}
    asg_map  = {(a.usuario_id, a.tarea_id): a for a in asigns}

    # (uid, tid, fecha) -> lista de jornadas del día
    daybox: Dict[Tuple[int, int, date], List] = defaultdict(list)
    for j in jornadas:
        daybox[(j.usuario_id, j.tarea_id, j.fecha)].append(j)

    sanc_act: Dict[int, int] = defaultdict(int)
    if Sancion is not None:
        for uid, cnt in (
            session.query(Sancion.usuario_id, func.count(Sancion.id))
            .filter(Sancion.resuelta == False)
            .group_by(Sancion.usuario_id)
        ):
            sanc_act[int(uid)] = int(cnt or 0)

    return pairs, usuarios, tareas, asg_map, daybox, sanc_act


# ======================= API: vista /admin/horas ========================
def compute_range(
    d_from: date, d_to: date, usuario_id: Optional[int] = None, tarea_id: Optional[int] = None
):
    """
    Calcula KPIs, tarjetas por empleado, filas detalladas por día y grupos por tarea,
    usando EXCLUSIVAMENTE Jornada (entrada/salida/duración) + Asignacion.tarifa_hora.
    """
    pairs, usuarios, tareas, asg_map, daybox, sanc_act = _collect_jornadas(d_from, d_to, usuario_id, tarea_id)

    horas_por_user = defaultdict(float)
    pago_por_user  = defaultdict(float)
    dias_por_user  = defaultdict(set)
    chips_por_user = defaultdict(set)

    rows: List[Dict] = []
    groups = defaultdict(lambda: {"horas": 0.0, "empleados": set(), "pago": 0.0, "empleado_det": []})

    for (uid, tid) in sorted(pairs):
        u = usuarios.get(uid)
        t = tareas.get(tid)
        if not (u and t):
            continue

        tarifa = float(getattr(asg_map.get((uid, tid)), "tarifa_hora", 0.0) or 0.0)

        for f in _daterange(d_from, d_to):
            js = daybox.get((uid, tid, f), [])
            if not js:
                continue

            horas_dia = sum(_safe_hours(j) for j in js)
            if horas_dia <= 0:
                continue

            total = _round2(horas_dia * tarifa)
            dias_por_user[uid].add(f)
            chips_por_user[uid].add(t.nombre)

            rows.append(
                dict(
                    usuario_id=uid,
                    usuario=u.nombre,
                    tarea_id=tid,
                    tarea=t.nombre,
                    fecha=f.strftime("%Y-%m-%d"),
                    horas=_round1(horas_dia),
                    tarifa=_round2(tarifa),
                    total=total,
                    estado="con jornada",
                    reportes="--",
                    rep_ok=0,
                    rep_tot=0,
                )
            )

            horas_por_user[uid] += horas_dia
            pago_por_user[uid] += total

            g = groups[tid]
            g["horas"] += horas_dia
            g["empleados"].add(uid)
            g["pago"] += total
            g["empleado_det"].append(
                {
                    "uid": uid,
                    "nombre": u.nombre,
                    "horas": _round1(horas_dia),
                    "pago": total,
                    "reportes": "--",
                    "rep_ok": 0,
                    "rep_tot": 0,
                }
            )
            g["tarea_nombre"] = t.nombre

    # cards por empleado
    cards = []
    seen = set()
    for uid, _tid in sorted(pairs):
        if uid in seen:
            continue
        seen.add(uid)
        u = usuarios.get(uid)
        if not u:
            continue
        horas = _round1(horas_por_user.get(uid, 0.0))
        total = _round2(pago_por_user.get(uid, 0.0))
        cards.append(
            dict(
                usuario_id=uid,
                iniciales=_initials(u.nombre),
                nombre=u.nombre,
                identificacion=getattr(u, "identificacion", "") or "",
                telefono=getattr(u, "telefono", "") or "",
                horas=horas,
                total=total,
                tareas=len({tid for (u2, tid) in pairs if u2 == uid}),
                reportes_pct=0,  # ya no aplica
                dias_trabajados=len(dias_por_user.get(uid, set())),
                chips=sorted(list(chips_por_user.get(uid, set())))[:4],
                amonestaciones=sanc_act.get(uid, 0),
                activo=bool(getattr(u, "activo", True)),
                asignaciones=len({k for k in (asg_map.keys()) if k[0] == uid}),
            )
        )

    # grupos por tarea
    grupos = []
    for tid, g in groups.items():
        horas = _round1(g["horas"])
        pago = _round2(g["pago"])
        prom_hora = _round2((pago / horas) if horas > 0 else 0.0)
        grupos.append(
            dict(
                tarea_id=tid,
                tarea=g.get("tarea_nombre", "Tarea"),
                horas=horas,
                empleados=len(g["empleados"]),
                promedio_hora=prom_hora,
                pct_reportes=0,  # ya no aplica
                empleado_det=g["empleado_det"],
            )
        )

    # KPIs
    kpis = dict(
        empleados_trabajando=sum(1 for c in cards if c["horas"] > 0),
        horas_totales=_round1(sum(c["horas"] for c in cards)),
        total_pagado=_round2(sum(c["total"] for c in cards)),
        reportes_pendientes=0,  # ya no aplica
    )

    # ordenamientos
    cards.sort(key=lambda c: (-c["horas"], c["nombre"].lower()))
    rows.sort(key=lambda r: (r["fecha"], r["usuario"].lower()))
    grupos.sort(key=lambda g: (-g["horas"], g["tarea"].lower()))

    return kpis, cards, rows, grupos


# ===================== detalle para el modal (empleado) =================
def compute_employee_detail(uid: int, d_from: date, d_to: date):
    """
    Devuelve {usuario, resumen, sanciones, items} para el modal por empleado,
    calculando horas/pago desde Jornada en el rango.
    """
    session, Usuario, Tarea, Asignacion, _Reporte, Sancion, _ReportVentana, _Turno, Jornada = _models()
    u = session.get(Usuario, uid)
    if not u:
        return None

    # Asignaciones del empleado (para tener tarifa por tarea)
    asigs = (
        session.query(Asignacion)
        .join(Tarea, Tarea.id == Asignacion.tarea_id)
        .filter(Asignacion.usuario_id == uid)
        .all()
    )
    tmap = {a.tarea_id: a.tarea for a in asigs}   # tid -> Tarea
    amap = {a.tarea_id: a for a in asigs}         # tid -> Asignacion

    # Jornadas del rango
    jq = (
        session.query(Jornada)
        .filter(Jornada.usuario_id == uid, Jornada.fecha >= d_from, Jornada.fecha <= d_to)
        .order_by(Jornada.tarea_id, Jornada.fecha, Jornada.turno_num)
    )
    jornadas = jq.all()

    # Index por día y tarea
    by_day = defaultdict(lambda: defaultdict(list))  # by_day[date][tid] -> [jornadas]
    for j in jornadas:
        by_day[j.fecha][j.tarea_id].append(j)
        # si hay jornada de tarea sin asign registrada, mapea la tarea para mostrar nombre
        if j.tarea_id not in tmap:
            tmap[j.tarea_id] = session.get(Tarea, j.tarea_id)

    total_horas = 0.0
    total_pay = 0.0
    tareas_set = set()
    items = []

    for d in _daterange(d_from, d_to):
        for tid, t in tmap.items():
            js = by_day.get(d, {}).get(tid, [])
            if not js:
                continue

            horas_dia = sum(_safe_hours(j) for j in js)
            tarifa = float(getattr(amap.get(tid), "tarifa_hora", 0.0) or 0.0)
            pago = _round2(horas_dia * tarifa)

            total_horas += horas_dia
            total_pay += pago
            tareas_set.add(t.nombre)

            items.append(
                dict(
                    fecha=d.strftime("%Y-%m-%d"),
                    tarea=t.nombre,
                    turno="/".join(str(j.turno_num or 1) for j in js),
                    checks={"inicio": bool(js), "medio": False, "salida": bool(js)},  # decorativo
                    linea=f"{_round1(horas_dia)}h × ${_round2(tarifa)} = ${pago}",
                    horas=_round1(horas_dia),
                    pago=pago,
                )
            )

    resumen = dict(horas=_round1(total_horas), total=_round2(total_pay), tareas=len(tareas_set))
    sanciones = []
    if Sancion is not None:
        sanciones = (
            session.query(Sancion)
            .filter(Sancion.usuario_id == uid)
            .order_by(Sancion.id.desc())
            .all()
        )

    return dict(usuario=u, resumen=resumen, sanciones=sanciones, items=items)
