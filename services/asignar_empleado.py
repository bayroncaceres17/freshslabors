# services/asignar_empleado.py
from __future__ import annotations
import re
from sqlalchemy.exc import IntegrityError

# ===================== UTILIDAD =====================
def normalize_phone_e164(s: str | None) -> str | None:
    """Limpia el teléfono a formato E.164 (+ y dígitos)."""
    if not s:
        return None
    return re.sub(r"[^\d+]", "", s)


# ===================== ERRORES =====================
class AssignError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ===================== UPSERT DE USUARIO =====================
def upsert_user_by_phone(nombre: str, telefono: str, *, db, Usuario):
    """Busca o crea un empleado por teléfono."""
    tel = normalize_phone_e164(telefono or "")
    if not nombre or not tel:
        raise AssignError("Nombre y teléfono son obligatorios.", 400)
    u = Usuario.query.filter_by(telefono=tel).first()
    if not u:
        u = Usuario(nombre=nombre, telefono=tel, rol="empleado", activo=True)
        db.session.add(u)
        db.session.commit()
    return u


# ===================== AUXILIARES =====================
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")

def _hhmm_ok(v: str | None) -> bool:
    return bool(v and _HHMM_RE.match(v))

def _pick_hhmm(obj, *fields):
    """Devuelve el primer atributo HH:MM válido encontrado."""
    for f in fields:
        val = getattr(obj, f, None)
        if _hhmm_ok(val):
            return val
    return None

def _compute_base_overrides(tarea, turno_num: int | None, *, Turno):
    """Calcula hora_inicio_override (según turno o tarea) y tolerancia_override."""
    hora_base = None

    # Si hay turno válido, usa su hora_inicio
    if turno_num is not None:
        tur = Turno.query.filter_by(tarea_id=tarea.id, numero=turno_num).first()
        if tur:
            hora_base = _pick_hhmm(tur, "hora_inicio", "hora_ini")

    # Si no hay turno o no tiene hora válida, usar hora global de la tarea
    if not hora_base:
        hora_base = _pick_hhmm(tarea, "hora_inicio", "hora_ini")

    # Tolerancia desde la tarea
    tol_base = getattr(tarea, "tolerancia_min", None)
    try:
        tol_base = int(tol_base) if tol_base is not None else None
        if tol_base is not None and tol_base < 0:
            tol_base = None
    except Exception:
        tol_base = None

    return hora_base, tol_base


# ===================== ASIGNACIÓN PRINCIPAL =====================
def asignar_empleado_core(
    tid,
    usuario_id,
    tarifa_hora,
    turno_num,
    doble_turno,
    *,
    db,
    models,
    log_event=lambda *a, **k: None
):
    Usuario = models["Usuario"]
    Tarea = models["Tarea"]
    Asignacion = models["Asignacion"]
    Turno = models["Turno"]

    u = Usuario.query.get(usuario_id)
    t = Tarea.query.get(tid)
    if not u or not t:
        raise AssignError("Usuario o tarea no existe.", 404)

    # === 1) Validar tarifa (>0)
    try:
        tarifa = float(str(tarifa_hora or "").replace(",", "."))
        if tarifa <= 0:
            raise ValueError()
    except Exception:
        raise AssignError("Tarifa inválida.", 400)

    # === 2) Detectar si la tarea tiene turnos
    tiene_turnos = bool(t.turnos and len(t.turnos) > 0)

    # === 3) Validar turno según la tarea
    turno = None
    if tiene_turnos:
        # turno es obligatorio
        try:
            turno = int(turno_num) if turno_num not in (None, "", "null") else None
        except Exception:
            raise AssignError("Turno inválido.", 400)

        if turno is None:
            raise AssignError("Debes seleccionar un turno para esta tarea.", 400)

        ok = Turno.query.filter_by(tarea_id=tid, numero=turno).first()
        if not ok:
            raise AssignError("El turno seleccionado no existe en esta tarea.", 400)

        doble_turno = bool(doble_turno)
    else:
        turno = None
        doble_turno = False

    # === 4) Calcular overrides según turno/tarea
    hora_ini_base, tol_base = _compute_base_overrides(t, turno, Turno=Turno)

    # === 5) Buscar asignación activa existente (misma tarea + usuario)
    exists_any = Asignacion.query.filter_by(tarea_id=tid, usuario_id=usuario_id, estado="activa").first()

    if exists_any:
        updated = False

        # Actualizar turno
        if tiene_turnos and exists_any.turno_num != turno:
            exists_any.turno_num = turno
            updated = True
        elif not tiene_turnos and exists_any.turno_num is not None:
            exists_any.turno_num = None
            updated = True

        # Actualizar tarifa
        if float(exists_any.tarifa_hora or 0) != tarifa:
            exists_any.tarifa_hora = tarifa
            updated = True

        # Actualizar doble turno
        if bool(exists_any.doble_turno) != bool(doble_turno):
            exists_any.doble_turno = bool(doble_turno)
            updated = True

        # Actualizar overrides
        if exists_any.hora_inicio_override != hora_ini_base:
            exists_any.hora_inicio_override = hora_ini_base
            updated = True
        if exists_any.tolerancia_override != tol_base:
            exists_any.tolerancia_override = tol_base
            updated = True

        if updated:
            db.session.commit()
            try:
                log_event("asign_update", tarea_id=tid, empleado_id=usuario_id, detalle={
                    "tarifa_hora": tarifa,
                    "turno_num": turno,
                    "doble_turno": bool(doble_turno),
                    "hora_inicio_override": hora_ini_base,
                    "tolerancia_override": tol_base,
                })
            except Exception:
                pass
            return {"ok": True, "id": exists_any.id, "msg": "Asignación actualizada"}, 200

        return {"ok": True, "id": exists_any.id, "idempotent": True, "msg": "Ya estaba asignado."}, 200

    # === 6) Crear nueva asignación
    asg = Asignacion(
        usuario_id=usuario_id,
        tarea_id=tid,
        tarifa_hora=tarifa,
        turno_num=turno if tiene_turnos else None,
        doble_turno=bool(doble_turno) if tiene_turnos else False,
        hora_inicio_override=hora_ini_base,
        tolerancia_override=tol_base,
    )

    db.session.add(asg)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise AssignError("Este empleado ya está asignado a esta tarea.", 409)

    try:
        log_event("empleado_add", tarea_id=tid, empleado_id=usuario_id, detalle={
            "tarifa_hora": tarifa,
            "turno_num": turno if tiene_turnos else None,
            "doble_turno": bool(doble_turno) if tiene_turnos else False,
            "hora_inicio_override": hora_ini_base,
            "tolerancia_override": tol_base,
        })
    except Exception:
        pass

    return {"ok": True, "id": asg.id, "msg": "Empleado agregado a la tarea"}, 200
