# services/respaldo.py
from __future__ import annotations

import csv
import io
import json
import os
import re
import zipfile
from datetime import datetime, date, time
from typing import Optional, Tuple

from flask import current_app


# ==========================
# Helpers de entorno/modelos
# ==========================
def _get_db_and_models():
    """
    Obtiene db y modelos desde la app sin imports circulares.
    Requiere en app.py:
        app.config["DB"] = db
        app.config["MODELS"].update({
            "Usuario": Usuario,
            "PagoSemana": PagoSemana,
            # "HistEvento": HistEvento,  # solo si usarás el XLSX de historial de empleados
        })
    """
    db = current_app.config.get("DB")
    if not db:
        ext = current_app.extensions.get("sqlalchemy")
        if hasattr(ext, "db"):
            db = ext.db
        elif isinstance(ext, dict) and "db" in ext:
            db = ext["db"]
        else:
            raise RuntimeError("Configura app.config['DB'] = db en app.py")

    models = current_app.config.get("MODELS", {})
    Usuario = models.get("Usuario")
    PagoSemana = models.get("PagoSemana")
    HistEvento = models.get("HistEvento")  # puede ser None si no usas el XLSX
    return db, Usuario, PagoSemana, HistEvento


# =================
# Helpers de paths
# =================
def _cfg_paths() -> Tuple[str, str]:
    """
    Usa appcfg si existe. Fallback:
      base_uploads = static/uploads
      pagos_dir    = static/uploads/pagos
    Devuelve rutas absolutas.
    """
    cfg = current_app.jinja_env.globals.get("appcfg", {}) or {}
    base_uploads = cfg.get("dir_colillas") or cfg.get("dir_pagos") or "static/uploads"
    pagos_dir = cfg.get("dir_pagos") or os.path.join(base_uploads, "pagos")

    root = current_app.root_path
    abs_base = base_uploads if os.path.isabs(base_uploads) else os.path.join(root, base_uploads)
    abs_pagos = pagos_dir if os.path.isabs(pagos_dir) else os.path.join(root, pagos_dir)
    return abs_base, abs_pagos


def _norm_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._\- áéíóúÁÉÍÓÚñÑ()]", "_", (name or "").strip())


def _resolve_recibo_path(recibo_path: str) -> Optional[str]:
    """
    Resuelve la ruta de una colilla a un path absoluto existente.
    Prueba:
      root/static/<rel> ; root/<rel> ; dir_pagos/basename ; dir_colillas/basename
    """
    if not recibo_path:
        return None

    rel = recibo_path.replace("\\", "/").lstrip("/")
    root = current_app.root_path
    abs_base, abs_pagos = _cfg_paths()

    # 1) static/<rel> o root/<rel> si ya empieza con static/
    candidate = os.path.join(root, "static", rel) if not rel.startswith("static/") else os.path.join(root, rel)
    if os.path.exists(candidate):
        return candidate

    candidate = os.path.join(root, rel)
    if os.path.exists(candidate):
        return candidate

    # 2) pagos_dir + basename
    candidate = os.path.join(abs_pagos, os.path.basename(rel))
    if os.path.exists(candidate):
        return candidate

    # 3) base_uploads + basename
    candidate = os.path.join(abs_base, os.path.basename(rel))
    if os.path.exists(candidate):
        return candidate

    current_app.logger.warning("Colilla no encontrada: %s", recibo_path)
    return None


def _to_dt_range(start: Optional[date], end: Optional[date]):
    if start and end and start > end:
        start, end = end, start
    sdt = datetime.combine(start, time.min) if start else None
    edt = datetime.combine(end, time.max) if end else None
    return sdt, edt


def _us_date(d: date) -> str:
    """MM-DD-YYYY"""
    return f"{d.month:02d}-{d.day:02d}-{d.year}"


# ===================================================
# 1) Empleados activos (CSV bonito y Excel opcional)
# ===================================================
def export_empleados_activos_csv_bytes() -> bytes:
    """
    CSV con BOM (Excel-friendly) de empleados activos, headers bonitos:
    Nombre, Número, Zelle a nombre de, Zelle (cuenta), Correo, Confirmado
    """
    db, Usuario, _, _ = _get_db_and_models()
    rows = (
        db.session.query(Usuario)
        .filter(Usuario.rol == "empleado")
        .filter(Usuario.activo == True)
        .filter(Usuario.bloqueado == False)
        .filter(Usuario.baneado == False)
        .order_by(Usuario.nombre.asc())
        .all()
    )

    s = io.StringIO(newline="")
    w = csv.writer(s)
    w.writerow(["Nombre", "Número", "Zelle a nombre de", "Zelle (cuenta)", "Correo", "Confirmado"])

    for u in rows:
        z_nombre = getattr(u, "zelle_nombre", None) or getattr(u, "zelle_titular", None) or ""
        z_cuenta = getattr(u, "zelle_cuenta", None) or ""
        confirmado = bool(getattr(u, "nombre_confirmado", False) or getattr(u, "nombre_legal_firma", None))
        w.writerow([
            u.nombre or "",
            u.telefono or "",
            z_nombre,
            z_cuenta,
            u.email or "",
            "SI" if confirmado else "NO"
        ])

    b = io.BytesIO()
    b.write(b"\xef\xbb\xbf")              # BOM
    b.write(s.getvalue().encode("utf-8")) # contenido
    b.seek(0)
    return b.getvalue()


def export_empleados_activos_xlsx_bytes() -> bytes:
    """
    XLSX estilizado con las mismas columnas.
    Si no hay openpyxl, retorna el CSV (fallback).
    """
    db, Usuario, _, _ = _get_db_and_models()
    rows = (
        db.session.query(Usuario)
        .filter(Usuario.rol == "empleado")
        .filter(Usuario.activo == True)
        .filter(Usuario.bloqueado == False)
        .filter(Usuario.baneado == False)
        .order_by(Usuario.nombre.asc())
        .all()
    )

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "EmpleadosActivos"

        headers = ["Nombre", "Número", "Zelle a nombre de", "Zelle (cuenta)", "Correo", "Confirmado"]
        ws.append(headers)

        bold = Font(bold=True)
        fill = PatternFill("solid", fgColor="E8EEF9")
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = bold
            cell.fill = fill
            cell.alignment = Alignment(vertical="center")

        for u in rows:
            z_nombre = getattr(u, "zelle_nombre", None) or getattr(u, "zelle_titular", None) or ""
            z_cuenta = getattr(u, "zelle_cuenta", None) or ""
            confirmado = bool(getattr(u, "nombre_confirmado", False) or getattr(u, "nombre_legal_firma", None))
            ws.append([
                u.nombre or "",
                u.telefono or "",
                z_nombre,
                z_cuenta,
                u.email or "",
                "SI" if confirmado else "NO"
            ])

        # autosize
        for idx in range(1, len(headers) + 1):
            max_len = len(headers[idx - 1])
            for r in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                val = r[0].value
                if val:
                    max_len = max(max_len, len(str(val)))
            ws.column_dimensions[get_column_letter(idx)].width = min(max_len + 2, 60)

        ws.freeze_panes = "A2"

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        return out.getvalue()

    except Exception:
        # fallback → CSV bonito
        return export_empleados_activos_csv_bytes()


# ==============================================
# 2) ZIP de colillas por rango (AÑO/MES/fichero)
# ==============================================
def export_colillas_zip_bytes(start: date, end: date) -> bytes:
    """
    Estructura en ZIP: AÑO/MES/(Nombre)_MM-DD-YYYY_(Telefono).ext
    Incluye MANIFEST.txt con conteo y rango.
    """
    db, Usuario, PagoSemana, _ = _get_db_and_models()

    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        pagos = (
            db.session.query(PagoSemana)
            .filter(PagoSemana.semana_fin >= start)
            .filter(PagoSemana.semana_fin <= end)
            .order_by(PagoSemana.semana_fin.asc(), PagoSemana.id.asc())
            .all()
        )

        ok = 0
        for p in pagos:
            abs_path = _resolve_recibo_path(getattr(p, "recibo_path", "") or "")
            if not abs_path:
                continue

            emp = getattr(p, "usuario", None) or db.session.get(Usuario, getattr(p, "usuario_id", None))
            nombre = _norm_filename(
                getattr(p, "usuario_nombre", None) or (emp.nombre if emp else "") or "Empleado"
            )
            telefono = _norm_filename((emp.telefono if emp else "") or "")

            fecha: Optional[date] = getattr(p, "semana_fin", None)
            if isinstance(fecha, date):
                year = f"{fecha.year}"
                month = f"{fecha.month:02d}"
                fecha_str = _us_date(fecha)  # MM-DD-YYYY
            else:
                year, month, fecha_str = "sin_fecha", "00", "00-00-0000"

            ext = os.path.splitext(abs_path)[1] or ".jpg"
            fname = f"({nombre})_{fecha_str}_{telefono}{ext}"
            arcpath = os.path.join(year, month, fname)

            try:
                zf.write(abs_path, arcname=arcpath)
                ok += 1
            except Exception as ex:
                current_app.logger.exception("Error agregando %s: %s", abs_path, ex)

        zf.writestr(
            "MANIFEST.txt",
            f"Colillas exportadas: {ok}\nRango: {start} a {end}\nEstructura: AÑO/MES/(Nombre)_MM-DD-YYYY_(Telefono).ext\n"
        )

    mem.seek(0)
    return mem.getvalue()


# ===========================================================
# 3) (Opcional) Excel/CSV del historial de cambios (empleados)
# ===========================================================
def _query_hist_empleados(db, HistEvento, start: Optional[date], end: Optional[date],
                          empleado_id: Optional[int], q: Optional[str]):
    qry = db.session.query(HistEvento).order_by(HistEvento.creado_en.desc())

    sdt, edt = _to_dt_range(start, end)
    if sdt:
        qry = qry.filter(HistEvento.creado_en >= sdt)
    if edt:
        qry = qry.filter(HistEvento.creado_en <= edt)

    if empleado_id:
        qry = qry.filter(HistEvento.empleado_id == empleado_id)
    else:
        from sqlalchemy import or_
        qry = qry.filter(
            or_(
                HistEvento.empleado_id.isnot(None),
                HistEvento.tipo.ilike("empleado_%"),
            )
        )

    if q:
        from sqlalchemy import func, cast, String, or_
        like = f"%{q.lower().strip()}%"
        qry = qry.filter(
            or_(
                func.lower(HistEvento.tipo).like(like),
                func.lower(cast(HistEvento.detalle, String)).like(like),
            )
        )
    return qry.all()


def export_historial_empleados_xlsx_bytes(
    start: Optional[date],
    end: Optional[date],
    empleado_id: Optional[int] = None,
    q: Optional[str] = None
) -> bytes:
    """
    XLSX con cambios de empleados (fallback a CSV si no hay openpyxl).
    Columnas: Fecha, Tipo, EmpleadoID, Empleado, UsuarioID, Usuario, Rol, TareaID, Detalle
    """
    db, Usuario, _, HistEvento = _get_db_and_models()
    if HistEvento is None or Usuario is None:
        raise RuntimeError("MODELS no contiene HistEvento/Usuario.")

    rows = _query_hist_empleados(db, HistEvento, start, end, empleado_id, q)

    try:
        import openpyxl
        from openpyxl.styles import Font, Alignment, PatternFill
        from openpyxl.utils import get_column_letter

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "CambiosEmpleados"

        headers = ["Fecha", "Tipo", "EmpleadoID", "Empleado", "UsuarioID", "Usuario", "Rol", "TareaID", "Detalle"]
        ws.append(headers)

        bold = Font(bold=True); fill = PatternFill("solid", fgColor="E8EEF9")
        for col in range(1, len(headers) + 1):
            c = ws.cell(row=1, column=col)
            c.font = bold; c.fill = fill; c.alignment = Alignment(vertical="center")

        for ev in rows:
            emp_name = ""
            if ev.empleado_id:
                eu = db.session.get(Usuario, ev.empleado_id)
                emp_name = (eu.nombre if eu else "") or ""
            actor_name, actor_role = "", ""
            if ev.usuario_id:
                au = db.session.get(Usuario, ev.usuario_id)
                actor_name = (au.nombre if au else "") or ""
                actor_role = (au.rol if au else "") or ""
            detalle_str = json.dumps(getattr(ev, "detalle", None), ensure_ascii=False) if getattr(ev, "detalle", None) else ""

            ws.append([
                ev.creado_en.strftime("%Y-%m-%d %H:%M:%S"),
                ev.tipo or "",
                ev.empleado_id or "",
                emp_name,
                ev.usuario_id or "",
                actor_name,
                actor_role,
                ev.tarea_id or "",
                detalle_str
            ])

        for idx, header in enumerate(headers, start=1):
            max_len = len(header)
            for r in ws.iter_rows(min_row=2, min_col=idx, max_col=idx):
                val = r[0].value
                if val:
                    max_len = max(max_len, len(str(val)))
            from openpyxl.utils import get_column_letter
            ws.column_dimensions[get_column_letter(idx)].width = min(max_len + 2, 60)

        ws.auto_filter.ref = ws.dimensions
        ws.freeze_panes = "A2"

        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        return out.getvalue()

    except Exception:
        s = io.StringIO(newline="")
        w = csv.writer(s)
        w.writerow(["fecha", "tipo", "empleado_id", "empleado", "usuario_id", "usuario", "rol", "tarea_id", "detalle"])
        for ev in rows:
            emp_name = ""
            if ev.empleado_id:
                eu = db.session.get(Usuario, ev.empleado_id)
                emp_name = (eu.nombre if eu else "") or ""
            actor_name, actor_role = "", ""
            if ev.usuario_id:
                au = db.session.get(Usuario, ev.usuario_id)
                actor_name = (au.nombre if au else "") or ""
                actor_role = (au.rol if au else "") or ""
            detalle_str = json.dumps(getattr(ev, "detalle", None), ensure_ascii=False) if getattr(ev, "detalle", None) else ""
            w.writerow([
                ev.creado_en.strftime("%Y-%m-%d %H:%M:%S"),
                ev.tipo or "",
                ev.empleado_id or "",
                emp_name,
                ev.usuario_id or "",
                actor_name,
                actor_role,
                ev.tarea_id or "",
                detalle_str
            ])

        b = io.BytesIO()
        b.write(b"\xef\xbb\xbf")
        b.write(s.getvalue().encode("utf-8"))
        b.seek(0)
        return b.getvalue()
