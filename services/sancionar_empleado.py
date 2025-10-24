# services/sancionar_empleado.py
from datetime import datetime
from sqlalchemy.exc import IntegrityError

class SancionError(Exception):
    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status

def sancionar_empleado_core(
    uid,
    tipo,
    motivo,
    *,
    tarea_id=None,
    db,
    models,
    log_event=lambda *a, **k: None
):
    Usuario    = models["Usuario"]
    Sancion    = models["Sancion"]
    Asignacion = models["Asignacion"]

    u = Usuario.query.get(uid)
    if not u:
        raise SancionError("Empleado no encontrado.", 404)

    tipo = (tipo or "").lower().strip()
    if tipo not in ("amonestacion", "bloquear", "banear"):
        raise SancionError("Tipo de sanción inválido.", 400)

    if not motivo or not motivo.strip():
        raise SancionError("Debes indicar un motivo.", 400)

    now = datetime.utcnow()

    # === 1. Crear registro en la tabla Sancion ===
    s = Sancion(
        usuario_id=u.id,
        tarea_id=tarea_id,
        tipo=tipo,
        motivo=motivo.strip(),
        fecha=now,
        activo=True
    )
    db.session.add(s)

    # === 2. Aplicar efectos según tipo ===
    if tipo == "bloquear":
        u.bloqueado = True
        # Remover de tareas activas (no lo borramos, solo desactivamos asignación)
        asigs = Asignacion.query.filter_by(usuario_id=u.id, estado="activa").all()
        for a in asigs:
            a.estado = "inactiva"

    elif tipo == "banear":
        u.bloqueado = True
        u.baneado = True
        asigs = Asignacion.query.filter_by(usuario_id=u.id, estado="activa").all()
        for a in asigs:
            a.estado = "inactiva"

    # === 3. Guardar todo ===
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise SancionError("No se pudo guardar la sanción (duplicado o error DB).", 500)

    # === 4. Log de evento ===
    try:
        log_event("empleado_sancion", empleado_id=u.id, tarea_id=tarea_id, detalle={
            "tipo": tipo,
            "motivo": motivo,
            "fecha": now.isoformat()
        })
    except Exception:
        pass

    return {"ok": True, "msg": f"Sanción registrada ({tipo})."}
