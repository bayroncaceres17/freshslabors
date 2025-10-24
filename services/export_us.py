# services/export_us.py
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
import os, tempfile



# ----------------- acceso a modelos -----------------
def _models(app):
    M = app.config["MODELS"]
    Usuario    = M["Usuario"]
    Tarea      = M["Tarea"]
    Asignacion = M["Asignacion"]
    Reporte    = M["Reporte"]
    session = Usuario.query.session
    return session, Usuario, Tarea, Asignacion, Reporte

# ----------------- utilidades de fecha/ruta/nombre -----------------
def _snap_to_friday(d: date) -> date:
    # 0=Mon..4=Fri; ancla al viernes correspondiente
    weekday = d.weekday()
    delta = (weekday - 4) % 7
    return d - timedelta(days=delta)

def _month_bounds(d: date):
    start = d.replace(day=1)
    if start.month == 12:
        nxt = start.replace(year=start.year+1, month=1, day=1)
    else:
        nxt = start.replace(month=start.month+1, day=1)
    end = nxt - timedelta(days=1)
    return start, end

def _weeks_in_month(month_start: date, month_end: date):
    """Semanas (vie→jue) que caen en el mes, recortadas a sus días del mes."""
    first_anchor = _snap_to_friday(month_start)
    weeks = []
    s = first_anchor
    while s <= month_end:
        e = s + timedelta(days=6)
        clip_start = max(s, month_start)
        clip_end   = min(e, month_end)
        days = [clip_start + timedelta(days=i) for i in range((clip_end - clip_start).days + 1)]
        weeks.append((s, clip_end, days))  # guardamos s para etiqueta
        s = s + timedelta(days=7)
    return weeks

def _compose_filename(tag: str, start: date, end: date) -> str:
    # tag: "Weekly" o "Monthly"
    return f"{tag}_Report_{start.isoformat()}_to_{end.isoformat()}.xlsx"

def _resolve_out_path(file_name: str) -> str:
    folder = tempfile.gettempdir()
    return os.path.join(folder, file_name)

def _hhmm_to_time(v):
    if not v: return None
    h, m = map(int, v.split(":"))
    from datetime import time
    return time(h, m)

def _row_totals(day_hours):
    total = round(sum(day_hours), 2)
    regular = min(40.0, total)
    overtime = round(total - regular, 2)
    return total, regular, overtime

# ----------------- colecta de datos genérica (rango) -----------------
def _collect_range_data(app, start: date, end: date, include_inactive_tasks=False, include_inactive_people=False):
    """
    Devuelve:
      per_task_hours:     dict[task_name][(employee_name, day)] = hours
      per_emp_day_rate:   dict[(employee_name, day)] = [(hours, rate), ...]
      employees_all:      lista ordenada (asignaciones + quienes reportaron)
      task_names_all:     lista ordenada de nombres de tareas (asignadas o reportadas)
      days:               lista de dates en el rango
      task_to_empnames:   dict[task_name] = set(names)  (empleados asignados a esa tarea)
    """
    session, Usuario, Tarea, Asignacion, Reporte = _models(app)

    # Asignaciones base
    asg_q = (session.query(Asignacion)
             .join(Tarea, Tarea.id == Asignacion.tarea_id)
             .join(Usuario, Usuario.id == Asignacion.usuario_id))

    # Filtrado de inactivos:
    # - Si include_inactive_tasks=False -> solo tareas activas
    # - Si include_inactive_people=False -> solo usuarios activos
    if not include_inactive_tasks:
        asg_q = asg_q.filter(Tarea.activa == True)
    if not include_inactive_people:
        asg_q = asg_q.filter(Usuario.activo == True)

    # Regla para estado de Asignacion:
    # Si ambos flags son False -> solo asignaciones activas.
    # Si alguno es True -> NO filtramos por estado (para incluir histórico).
    if not include_inactive_tasks and not include_inactive_people:
        asg_q = asg_q.filter(Asignacion.estado == "activa")

    asigs = asg_q.all()

    emp_names_by_id = {}   # id -> nombre
    task_names_set = set()
    task_to_empnames = defaultdict(set)  # task_name -> set(employee_name)

    for a in asigs:
        emp_name = a.usuario.nombre if (a.usuario and getattr(a.usuario, "nombre", None)) else None
        task_name = a.tarea.nombre if (a.tarea and getattr(a.tarea, "nombre", None)) else None
        if emp_name:
            emp_names_by_id[a.usuario_id] = emp_name
        if task_name:
            task_names_set.add(task_name)
            if emp_name:
                task_to_empnames[task_name].add(emp_name)

    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    per_task_hours = defaultdict(dict)    # task_name -> { (emp_name, day) : hours }
    per_emp_day_rate = defaultdict(list)  # (emp_name, day) -> [(hours, rate), ...]

    # Reportes del rango
    reps = (session.query(Reporte)
            .filter(Reporte.timestamp >= datetime.combine(start, datetime.min.time()),
                    Reporte.timestamp <= datetime.combine(end,   datetime.max.time()))
            .order_by(Reporte.usuario_id, Reporte.tarea_id, Reporte.timestamp)
            .all())

    # cache de tarifas por (usuario,tarea)
    tarifa_cache = {}
    def _get_tarifa(u_id, t_obj):
        key = (u_id, t_obj.id)
        if key in tarifa_cache:
            return tarifa_cache[key]
        val = 0.0
        for a in (getattr(t_obj, "asignaciones", None) or []):
            try:
                # priorizamos activa; si no hay activa y estamos incluyendo inactivos, tomamos la disponible
                if a.usuario_id == u_id:
                    if a.estado == "activa":
                        val = float(a.tarifa_hora or 0.0)
                        break
                    elif include_inactive_tasks or include_inactive_people:
                        val = float(a.tarifa_hora or 0.0)
                        # no rompemos por si encontramos una activa más adelante
            except:
                pass
        tarifa_cache[key] = val
        return val

    # Agrupar por (usuario, tarea, fecha, turno)
    box = defaultdict(list)
    for r in reps:
        t = getattr(r, "tarea", None)
        if not t:
            continue
        tzname = getattr(t, "tz_name", None) or "UTC"
        ts = r.timestamp.astimezone(ZoneInfo(tzname))
        key = (r.usuario_id, r.tarea_id, ts.date(), getattr(r, "turno_num", 1) or 1)
        box[key].append(r)

        # asegurar nombres/tareas aunque vengan solo por reporte
        if getattr(r, "usuario", None) and getattr(r.usuario, "nombre", None):
            emp_names_by_id[r.usuario_id] = r.usuario.nombre
        if getattr(t, "nombre", None):
            task_names_set.add(t.nombre)

    # Calcular horas estrictas + poblar per_task_hours
    for (uid, tid, d, turno), regs in box.items():
        t = regs[0].tarea if regs and getattr(regs[0], "tarea_id", None) else None
        if not t:
            continue

        h_ini = _hhmm_to_time(getattr(t, "hora_inicio", None))
        h_out = _hhmm_to_time(getattr(t, "hora_salida", None))
        tz = ZoneInfo(getattr(t, "tz_name", None) or "UTC")
        ini_of = datetime.combine(d, h_ini, tzinfo=tz) if h_ini else datetime(d.year, d.month, d.day, tzinfo=tz)
        fin_of = datetime.combine(d, h_out, tzinfo=tz) if h_out else ini_of + timedelta(hours=23, minutes=59)

        asist = next((x for x in regs if (getattr(x, "tipo", "") or "").lower() == "asistencia" and getattr(x, "valido", True)), None)
        salida = next((x for x in regs if (getattr(x, "tipo", "") or "").lower() == "salida"     and getattr(x, "valido", True)), None)
        if not (asist and salida):
            continue

        ini_real = max(asist.timestamp.astimezone(tz), ini_of)
        fin_real = max(ini_real, min(salida.timestamp.astimezone(tz), fin_of))
        hours = round((fin_real - ini_real).total_seconds() / 3600.0, 2)

        emp_name = emp_names_by_id.get(uid) or (asist.usuario.nombre if getattr(asist, "usuario", None) else f"U{uid}")
        task_name = f"{getattr(t, 'nombre', 'Task')}"

        # suma por tarea-empleado-día
        per_task_hours[task_name][(emp_name, d)] = per_task_hours[task_name].get((emp_name, d), 0.0) + hours

        # guarda tarifas por empleado-día (para "People")
        rate = _get_tarifa(uid, t)
        per_emp_day_rate[(emp_name, d)].append((hours, rate))

        # asegura que ese empleado quede asociado a esa tarea (aunque no tenga asignación activa)
        task_to_empnames[task_name].add(emp_name)

    employees_all = sorted(set(emp_names_by_id.values()))
    task_names_all = sorted(task_names_set)

    return per_task_hours, per_emp_day_rate, employees_all, task_names_all, days, task_to_empnames

# ----------------- estilos Excel -----------------
def _build_headers_days(ws, days, include_pay: bool, tag: str):
    headers = ["Names"]
    for d in days:
        headers.append(f"{d.strftime('%A')} {d.day}")
    if tag == "Weekly":
        headers += ["TOTAL HOURS - WEEKLY", "TOTAL REGULAR HOURS - WEEKLY", "TOTAL OVERTIME HOURS - WEEKLY"]
    else:
        headers += ["TOTAL HOURS - MONTHLY", "TOTAL REGULAR HOURS - MONTHLY", "TOTAL OVERTIME HOURS - MONTHLY"]
    if include_pay:
        headers += ["PAGOS"]

    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=2, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.fill = PatternFill("solid", fgColor="6B7280")

    for col in range(1, len(headers)+1):
        ws.column_dimensions[get_column_letter(col)].width = 18 if col == 1 else 16

    ws.freeze_panes = "A3"
    return headers

def _build_headers_weeks(ws, weeks_labels, tag: str, include_pay: bool):
    headers = ["Names"]
    headers += weeks_labels
    if tag == "Monthly":
        headers += ["TOTAL HOURS - MONTHLY", "TOTAL REGULAR HOURS - MONTHLY", "TOTAL OVERTIME HOURS - MONTHLY"]
    else:
        headers += ["TOTAL HOURS - WEEKLY", "TOTAL REGULAR HOURS - WEEKLY", "TOTAL OVERTIME HOURS - WEEKLY"]
    if include_pay:
        headers += ["PAGOS"]

    for j, h in enumerate(headers, start=1):
        c = ws.cell(row=2, column=j, value=h)
        c.font = Font(bold=True, color="FFFFFF")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        c.fill = PatternFill("solid", fgColor="6B7280")

    main_cols = len(weeks_labels)
    last_data = 1 + main_cols
    tot1, tot2, tot3 = last_data+1, last_data+2, last_data+3
    pay_col = last_data+4 if include_pay else None

    ws.column_dimensions[get_column_letter(1)].width = 26
    for col in range(2, 2 + main_cols):
        ws.column_dimensions[get_column_letter(col)].width = 20
    ws.column_dimensions[get_column_letter(tot1)].width = 18
    ws.column_dimensions[get_column_letter(tot2)].width = 18
    ws.column_dimensions[get_column_letter(tot3)].width = 18
    if pay_col:
        ws.column_dimensions[get_column_letter(pay_col)].width = 18

    ws.freeze_panes = "A3"
    return headers

def _write_title(ws, title: str, cols: int):
    ws.merge_cells(start_row=1, start_column=2, end_row=1, end_column=min(cols, 10))
    t = ws.cell(row=1, column=2, value=title)
    t.font = Font(size=16, bold=True, color="FFFFFF")
    t.alignment = Alignment(horizontal="center")
    for c in range(2, min(cols, 10)+1):
        ws.cell(row=1, column=c).fill = PatternFill("solid", fgColor="374151")

def _apply_table_style(ws, nrows: int, ncols: int, main_cols: int, has_pay: bool,
                       row_height: int | None = None, header_height: int | None = None, title_height: int | None = None):
    thin = Side(style="thin", color="CCCCCC")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    last_data = 1 + main_cols
    tot1, tot2, tot3 = last_data+1, last_data+2, last_data+3
    pay_col = last_data+4 if has_pay else None

    if title_height:
        ws.row_dimensions[1].height = title_height
    if header_height:
        ws.row_dimensions[2].height = header_height

    for r in range(3, 3 + max(nrows,1)):
        if row_height:
            ws.row_dimensions[r].height = row_height
        for c in range(1, ncols + 1):
            cell = ws.cell(row=r, column=c)
            cell.border = border
            if c == 1:
                cell.alignment = Alignment(horizontal="left", vertical="center")
                cell.fill = PatternFill("solid", fgColor="FDE68A")
            elif 2 <= c <= last_data:
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.fill = PatternFill("solid", fgColor="C7B8F3")
            elif c == tot1:
                cell.fill = PatternFill("solid", fgColor="A7F3D0")
            elif c == tot2:
                cell.fill = PatternFill("solid", fgColor="FDE68A")
            elif c == tot3:
                cell.fill = PatternFill("solid", fgColor="BFDBFE")
            elif pay_col and c == pay_col:
                cell.fill = PatternFill("solid", fgColor="E5E7EB")
            cell.number_format = "0.00"

# ----------------- export genérico por periodo -----------------
def export_period_us(app, any_date: date, period="week", ot_multiplier=1.0,
                     include_inactive_tasks=False, include_inactive_people=False, out_path=None):
    """
    period = 'week' (viernes→jueves) o 'month' (1→último día).
    Archivo:
      - Weekly:  Hoja 'People - Weekly' + 1 hoja por tarea (días vie→jue)
      - Monthly: Hoja 'People – Monthly (Weekly Buckets)' + 1 hoja por tarea con columnas por semana (W1..Wn)
    """
    if period == "week":
        start = _snap_to_friday(any_date)
        end   = start + timedelta(days=6)
        tag   = "Weekly"
    else:
        start, end = _month_bounds(any_date)
        tag = "Monthly"

    file_name = _compose_filename(tag, start, end)
    out_file = _resolve_out_path(file_name)

    (per_task_hours,
     per_emp_day_rate,
     employees_all,
     task_names_all,
     days,
     task_to_empnames) = _collect_range_data(
        app, start, end,
        include_inactive_tasks=include_inactive_tasks,
        include_inactive_people=include_inactive_people
    )

    wb = Workbook()

    # ============ WEEKLY ============
    if tag == "Weekly":
        # Hoja "People - Weekly"
        ws0 = wb.active
        ws0.title = "People - Weekly"
        title = "People Hours – Weekly"
        headers = _build_headers_days(ws0, days, include_pay=True, tag=tag)
        _write_title(ws0, title, len(headers))

        r = 3
        for emp in employees_all:
            ws0.cell(row=r, column=1, value=emp)
            dh = []
            base_pay = 0.0
            all_hours = 0.0
            rate_weighted_sum = 0.0
            for idx, d in enumerate(days):
                h = 0.0
                for tmap in per_task_hours.values():
                    h += round(tmap.get((emp, d), 0.0), 2)
                ws0.cell(row=r, column=2+idx, value=h)
                dh.append(h)
                for hh, rt in per_emp_day_rate.get((emp, d), []):
                    base_pay += hh * float(rt or 0.0)
                    rate_weighted_sum += hh * float(rt or 0.0)
                    all_hours += hh

            total, regular, overtime = _row_totals(dh)
            ws0.cell(row=r, column=2+len(days), value=total)
            ws0.cell(row=r, column=3+len(days), value=regular)
            ws0.cell(row=r, column=4+len(days), value=overtime)

            avg_rate = (rate_weighted_sum / all_hours) if all_hours else 0.0
            overtime_extra = overtime * avg_rate * max(0.0, (ot_multiplier - 1.0))
            total_pay = round(base_pay + overtime_extra, 2)
            ws0.cell(row=r, column=5+len(days), value=total_pay)
            r += 1

        _apply_table_style(ws0, nrows=len(employees_all), ncols=len(headers), main_cols=len(days), has_pay=True)

        # ---- Hojas por tarea (SIEMPRE, aunque no haya horas; filas = asignados ∪ reportados) ----
        for task_name in task_names_all:
            daymap = per_task_hours.get(task_name, {})
            # empleados asignados a esa tarea (según flags) ∪ empleados que reportaron en esa tarea
            task_emps = sorted(set(task_to_empnames.get(task_name, set())) |
                               { emp for (emp, _d) in daymap.keys() })

            ws = wb.create_sheet(title=(task_name[:31] or "Task"))
            headers = _build_headers_days(ws, days, include_pay=False, tag=tag)
            _write_title(ws, task_name, len(headers))

            r = 3
            if len(task_emps) == 0:
                # hoja vacía con headers y estilo suave
                _apply_table_style(ws, nrows=1, ncols=len(headers), main_cols=len(days), has_pay=False)
            else:
                for emp in task_emps:
                    ws.cell(row=r, column=1, value=emp)
                    dh = []
                    for idx, d in enumerate(days):
                        h = round(daymap.get((emp, d), 0.0), 2)
                        ws.cell(row=r, column=2+idx, value=h)
                        dh.append(h)
                    total, regular, overtime = _row_totals(dh)
                    ws.cell(row=r, column=2+len(days), value=total)
                    ws.cell(row=r, column=3+len(days), value=regular)
                    ws.cell(row=r, column=4+len(days), value=overtime)
                    r += 1
                _apply_table_style(ws, nrows=len(task_emps), ncols=len(headers), main_cols=len(days), has_pay=False)

    # ============ MONTHLY (weekly buckets) ============
    else:
        weeks = _weeks_in_month(start, end)
        ws0 = wb.active
        ws0.title = "People - Monthly"
        week_labels = []
        for i, (wk_start, wk_end_clip, days_clip) in enumerate(weeks, start=1):
            end_label = days_clip[-1] if days_clip else wk_start+timedelta(days=6)
            week_labels.append(f"W{i} ({wk_start.strftime('%b %d')}–{end_label.strftime('%b %d')})")

        title = "People Hours – Monthly (Weekly Buckets)"
        headers = _build_headers_weeks(ws0, week_labels, tag="Monthly", include_pay=True)
        _write_title(ws0, title, len(headers))

        def hours_sum_for_emp_days(emp, days_list):
            s = 0.0
            for d in days_list:
                for tmap in per_task_hours.values():
                    s += round(tmap.get((emp, d), 0.0), 2)
            return s

        r = 3
        for emp in employees_all:
            ws0.cell(row=r, column=1, value=emp)
            monthly_hours = 0.0
            monthly_reg   = 0.0
            monthly_ot    = 0.0
            monthly_pay   = 0.0
            col = 2
            for (_wk_start, _wk_end_clip, days_clip) in weeks:
                week_hours = hours_sum_for_emp_days(emp, days_clip)
                ws0.cell(row=r, column=col, value=week_hours)
                col += 1
                base_pay_week = 0.0
                all_h_week = 0.0
                rate_weighted_week = 0.0
                for d in days_clip:
                    for hh, rt in per_emp_day_rate.get((emp, d), []):
                        base_pay_week += hh * float(rt or 0.0)
                        rate_weighted_week += hh * float(rt or 0.0)
                        all_h_week += hh
                reg_w = min(40.0, week_hours)
                ot_w  = max(0.0, week_hours - reg_w)
                avg_rate_w = (rate_weighted_week / all_h_week) if all_h_week else 0.0
                overtime_extra_w = ot_w * avg_rate_w * max(0.0, (ot_multiplier - 1.0))
                total_pay_w = round(base_pay_week + overtime_extra_w, 2)

                monthly_hours += week_hours
                monthly_reg   += reg_w
                monthly_ot    += ot_w
                monthly_pay   += total_pay_w

            ws0.cell(row=r, column=1+len(week_labels)+1, value=round(monthly_hours,2))
            ws0.cell(row=r, column=1+len(week_labels)+2, value=round(monthly_reg,2))
            ws0.cell(row=r, column=1+len(week_labels)+3, value=round(monthly_ot,2))
            ws0.cell(row=r, column=1+len(week_labels)+4, value=round(monthly_pay,2))
            r += 1

        _apply_table_style(ws0, nrows=len(employees_all), ncols=len(headers),
                           main_cols=len(week_labels), has_pay=True,
                           row_height=22, header_height=28, title_height=30)

        # ---- Hojas por tarea (SIEMPRE; filas = asignados ∪ reportados; columnas = semanas) ----
        for task_name in task_names_all:
            daymap = per_task_hours.get(task_name, {})
            task_emps = sorted(set(task_to_empnames.get(task_name, set())) |
                               { emp for (emp, _d) in daymap.keys() })

            ws = wb.create_sheet(title=(task_name[:31] or "Task"))
            headers = _build_headers_weeks(ws, week_labels, tag="Monthly", include_pay=False)
            _write_title(ws, task_name, len(headers))

            r = 3
            if len(task_emps) == 0:
                _apply_table_style(ws, nrows=1, ncols=len(headers),
                                   main_cols=len(week_labels), has_pay=False,
                                   row_height=22, header_height=28, title_height=30)
            else:
                for emp in task_emps:
                    ws.cell(row=r, column=1, value=emp)
                    col = 2
                    task_total_month = 0.0
                    for (_wk_start, _wk_end_clip, days_clip) in weeks:
                        s = 0.0
                        for d in days_clip:
                            s += round(daymap.get((emp, d), 0.0), 2)
                        ws.cell(row=r, column=col, value=s)
                        task_total_month += s
                        col += 1
                    ws.cell(row=r, column=1+len(week_labels)+1, value=round(task_total_month,2))
                    ws.cell(row=r, column=1+len(week_labels)+2, value=min(40.0, task_total_month))
                    ws.cell(row=r, column=1+len(week_labels)+3, value=max(0.0, task_total_month - min(40.0, task_total_month)))
                    r += 1

                _apply_table_style(ws, nrows=len(task_emps), ncols=len(headers),
                                   main_cols=len(week_labels), has_pay=False,
                                   row_height=22, header_height=28, title_height=30)

    wb.save(out_file)
    return out_file, start, end

# --- Shims de compatibilidad ---
def export_weekly_us(app, any_date, city=None, ot_multiplier=1.0, out_path=None):
    return export_period_us(app, any_date, period="week", ot_multiplier=ot_multiplier, out_path=out_path)

def export_monthly_us(app, any_date, city=None, ot_multiplier=1.0, out_path=None):
    return export_period_us(app, any_date, period="month", ot_multiplier=ot_multiplier, out_path=out_path)
