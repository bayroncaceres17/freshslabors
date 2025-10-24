// Freshs_labors/static/js/admin_modals.js
// Versión: fix modal empleados en blanco (amonestar/editar/guardar/volver)

(function () {
  // 1) Referencias al modal base que ya tienes en tu base.html
  const modalEl = document.getElementById('appModal');
  const body = document.getElementById('appModalBody');
  const title = document.getElementById('appModalTitle');

  if (!modalEl || !body || !title) {
    console.warn('Modal base no encontrado (#appModal, #appModalBody, #appModalTitle). Revísalo en base.html');
    return;
  }

  let bsModal = null;
  function ensureModal() {
    if (!bsModal) bsModal = new bootstrap.Modal(modalEl);
  }

  // 2) Utilidad: obtener TID desde varias fuentes
  function resolveTid(fromUrlOrHtml) {
    // a) URL tipo .../tarea/123/...
    const m1 = (fromUrlOrHtml || '').match(/tarea\/(\d+)/i);
    if (m1) return m1[1];
    // b) Texto "Tarea 123" dentro del modal
    const m2 = body.textContent.match(/Tarea\s+(\d+)/i);
    if (m2) return m2[1];
    // c) Atributo data-task-id en el contenedor del modal
    const holder = body.querySelector('[data-task-id]');
    if (holder?.getAttribute('data-task-id')) return holder.getAttribute('data-task-id');
    return null;
  }

  // 3) Mostrar contenido dentro del modal y re-cablear eventos
  async function show(url, ttl){
    try{
      ensureModal();
      title.textContent = ttl || '';
      body.innerHTML = '<div class="p-4 text-center text-muted">Cargando…</div>';

      // Anti-caché del navegador/CDN:
      const sep = url.includes('?') ? '&' : '?';
      const res = await fetch(url + sep + 'v=' + Date.now(), {
        credentials: 'same-origin',
        cache: 'no-store'
      });

      if (!res.ok) throw new Error('HTTP ' + res.status);
      const html = await res.text();

      // Si viene vacío, muestra mensaje (no lo dejes “plano”)
      if (!html || html.trim() === '') {
        body.innerHTML = '<div class="p-4 text-center text-danger">No hay contenido para mostrar.</div>';
      } else {
        body.innerHTML = html;
        // Fallback: si por alguna razón no está el wrapper esperado,
        // muestra un mensaje para que no parezca “cuerpo vacío”.
        if (!body.querySelector('[data-task-id]') && !body.querySelector('#empfxRows')) {
          body.insertAdjacentHTML('beforeend',
            '<div class="p-3 text-center text-muted">Contenido cargado, pero no se detectó el contenedor de empleados.</div>');
        }
      }

      bsModal.show();
      wireInside();
    }catch(err){
      console.error(err);
      body.innerHTML = '<div class="p-4 text-center text-danger">Ocurrió un error al cargar.</div>';
      bsModal?.show();
    }
  }


  // 5) Delegación de clicks desde la página (botones que abren modales)
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action]');
    if (!btn) return;

    const action = btn.getAttribute('data-action');
    if (action === 'empleados') {
      e.preventDefault();
      const tid = btn.getAttribute('data-task');
      if (!tid) return console.warn('Falta data-task en botón empleados');
      show(`/admin/tarea/${tid}/empleados/modal`, 'Empleados de la Tarea');
    } else if (action === 'historial') {
      e.preventDefault();
      const tid = btn.getAttribute('data-task');
      if (!tid) return console.warn('Falta data-task en botón historial');
      show(`/admin/tarea/${tid}/historial/modal`, 'Historial de la Tarea');
    }
  });

  // 6) Cableado INTERNO del modal (se vuelve a ejecutar en cada show())
  function wireInside() {
    // 0) Referencias básicas
    const container = body.querySelector('[data-task-id]');
    const tid = container?.getAttribute('data-task-id');
    // Fallback para que al menos se muestre la pestaña "lista"
    try {
      body.querySelectorAll('.empfx-panel').forEach(p => p.style.display = 'none');
      const lista = body.querySelector('.empfx-panel[data-panel="lista"]');
      if (lista) lista.style.display = 'block';
    } catch {}

    // -------- Botón "Atrás" (NO cierra el modal grande) --------
    body.querySelectorAll('[data-action="back-empleados"]').forEach((btn) => {
      btn.addEventListener('click', (ev) => {
        ev.preventDefault();
        if (tid) reloadEmpleados(tid); else reloadEmpleados();
      });
    });

    // -------- Tabs --------
    body.querySelectorAll('.empfx-tab').forEach((btn) => {
      btn.addEventListener('click', () => {
        body.querySelectorAll('.empfx-tab').forEach(x => x.classList.remove('empfx-active'));
        btn.classList.add('empfx-active');
        const tab = btn.getAttribute('data-tab');
        body.querySelectorAll('.empfx-panel').forEach(p => {
          p.style.display = (p.getAttribute('data-panel') === tab ? 'block' : 'none');
        });
      });
    });

    // -------- Lista: acciones por fila --------
    const CSRF = document.querySelector('meta[name="csrf-token"]')?.content || '';
    const rows = body.querySelector('#empfxRows');
    if (rows) rows.addEventListener('click', async (ev) => {
      const btn = ev.target.closest('[data-act]'); if (!btn) return;
      const row = ev.target.closest('.empfx-tr'); if (!row) return;

      const uid = row.getAttribute('data-uid');
      const aid = row.getAttribute('data-aid');

      // a) Reportes
      if (btn.dataset.act === 'reportes') {
        if (!tid) return alert('Tarea desconocida');
        show(`/admin/tarea/${tid}/reportes/modal?uid=${encodeURIComponent(uid)}`, 'Reportes del empleado');
        return;
      }

      // b) Editar → abre mini modal y precarga
      if (btn.dataset.act === 'edit') {
        const editEl = document.getElementById(`empfxEdit-${tid}`);
        if (!editEl) return;
        const m = new bootstrap.Modal(editEl);
        editEl.querySelector('[name=uid]').value = uid;
        editEl.querySelector('[name=aid]').value = aid;
        editEl.querySelector('[name=nombre]').value = row.getAttribute('data-name') || '';
        editEl.querySelector('[name=telefono]').value = row.getAttribute('data-phone') || '';
        editEl.querySelector('[name=tarifa_hora]').value = row.getAttribute('data-tarifa') || '';
        const turnoSel = editEl.querySelector('[name=turno_num]');
        if (turnoSel) turnoSel.value = row.getAttribute('data-turno') || '';
        m.show(); return;
      }

      // c) Remover asignación
      if (btn.dataset.act === 'remove') {
        if (!confirm('¿Remover esta asignación?')) return;
        try {
          const r = await fetch(`/admin/asignacion/${aid}/remover`, {
            method: 'POST', credentials: 'same-origin',
            headers: CSRF ? { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' }
          });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || j.ok === false) return alert(j.error || 'No se pudo remover');
          reloadEmpleados(tid);
        } catch { alert('Error de red'); }
        return;
      }

      // d) Sanción → abre mini modal de sanción y precarga
      if (btn.dataset.act === 'san') {
        const sanEl = document.getElementById(`empfxSan-${tid}`);
        if (!sanEl) return;
        const m = new bootstrap.Modal(sanEl);
        sanEl.querySelector('#empfxSanTitle').textContent =
          btn.dataset.tipo[0].toUpperCase() + btn.dataset.tipo.slice(1);
        sanEl.querySelector('[name=uid]').value = uid;
        sanEl.querySelector('[name=tipo]').value = btn.dataset.tipo;
        const hidTid = sanEl.querySelector('[name=tarea_id]');
        if (hidTid) hidTid.value = tid || '';
        sanEl.querySelector('[name=motivo]').value = '';
        m.show(); return;
      }

      // e) Desbanear
      if (btn.dataset.act === 'unban') {
        if (!confirm('¿Desbanear a este empleado?')) return;
        try {
          const r = await fetch(`/admin/empleado/${uid}/desbanear`, {
            method: 'POST', credentials: 'same-origin',
            headers: CSRF ? { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' }
          });
          if (!r.ok) return alert('No se pudo desbanear');
          reloadEmpleados(tid);
        } catch { alert('Error de red'); }
        return;
      }
    });

    // -------- Guardar edición (mini modal) --------
    const fEdit = body.querySelector('#empfxEditForm');
    if (fEdit) {
      fEdit.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(fEdit);
        const uid = fd.get('uid');
        const aid = fd.get('aid');
        const nombre = (fd.get('nombre') || '').toString();
        const telefono = (fd.get('telefono') || '').toString();
        const tarifa = (fd.get('tarifa_hora') || '').toString().replace(',', '.');
        const turno = (fd.get('turno_num') || '').toString();

        if (!nombre.trim() || !telefono.trim()) return alert('Nombre y teléfono son obligatorios.');
        if (!tarifa || isNaN(parseFloat(tarifa))) return alert('Tarifa inválida.');

        // 1) editar datos del usuario
        const r1 = await fetch(`/admin/empleado/${uid}/editar`, {
          method: 'POST', credentials: 'same-origin',
          headers: CSRF ? { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' },
          body: new URLSearchParams({ nombre, telefono })
        });
        const j1 = await r1.json().catch(() => ({}));
        if (!r1.ok || j1.ok === false) return alert(j1.error || 'No se pudo editar el empleado.');

        // 2) editar asignación
        const r2 = await fetch(`/admin/asignacion/${aid}/editar`, {
          method: 'POST', credentials: 'same-origin',
          headers: Object.assign({ 'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest' }, CSRF ? { 'X-CSRFToken': CSRF } : {}),
          body: JSON.stringify({ tarifa_hora: parseFloat(tarifa), turno_num: (turno || null) })
        });
        const j2 = await r2.json().catch(() => ({}));
        if (!r2.ok || j2.ok === false) return alert(j2.error || 'No se pudo guardar la asignación.');

        // cerrar mini-modal y recargar lista
        const editEl = document.getElementById(`empfxEdit-${tid}`);
        if (editEl) bootstrap.Modal.getInstance(editEl)?.hide();
        reloadEmpleados(tid);
      });
    }

    // -------- Guardar sanción (mini modal) --------
    const fSan = body.querySelector('#empfxSanForm');
    if (fSan) {
      fSan.addEventListener('submit', async (e) => {
        e.preventDefault();
        const fd = new FormData(fSan);
        const uid = (fd.get('uid') || '').toString().trim();
        if (!uid) return alert('UID vacío.');
        if (tid && !fd.get('tarea_id')) fd.append('tarea_id', tid);
        const r = await fetch(`/admin/empleado/${encodeURIComponent(uid)}/sancion`, {
          method: 'POST', credentials: 'same-origin',
          headers: CSRF ? { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' },
          body: fd
        });
        const j = await r.json().catch(() => ({}));
        if (!r.ok || j.ok === false) return alert(j.error || 'No se pudo aplicar la sanción.');
        const sanEl = document.getElementById(`empfxSan-${tid}`);
        if (sanEl) bootstrap.Modal.getInstance(sanEl)?.hide();
        reloadEmpleados(tid);
      });
    }

    // -------- Agregar (evitar doble envío) --------
    const fAdd = body.querySelector('#empfxAddForm');
    if (fAdd) {
      fAdd.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (fAdd.dataset.busy === '1') return;
        fAdd.dataset.busy = '1';
        try {
          const r = await fetch(fAdd.action, {
            method: 'POST', credentials: 'same-origin',
            headers: CSRF ? { 'X-CSRFToken': CSRF, 'X-Requested-With': 'XMLHttpRequest' } : { 'X-Requested-With': 'XMLHttpRequest' },
            body: new FormData(fAdd)
          });
          const j = await r.json().catch(() => ({}));
          if (!r.ok || j.ok === false) alert(j.error || 'No se pudo asignar');
          else reloadEmpleados(tid);
        } catch {
          alert('Error de red al asignar');
        } finally {
          fAdd.dataset.busy = '0';
        }
      });
    }
  }


  // Exponer show() si ya lo usas en otra parte
  window.__adminShowModal = show;
})();
