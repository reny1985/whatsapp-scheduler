/* ============================================================
   Enviafast — Panel de superadministrador (v1.1.1)
   Vanilla JS. Se sirve solo detrás del candado de /admin.
   Todo dato del cliente se escapa (esc) antes de entrar al HTML.
   Nunca muestra contenido de mensajes: el servidor solo envía conteos.
   ============================================================ */

const ZONA = 'America/Guayaquil';
const ESTADOS = [
  ['prueba', 'Prueba'], ['activa', 'Activa'], ['cortesia', 'Cortesía'], ['vencida', 'Vencida'], ['suspendida', 'Suspendida'],
];
const NOMBRE_ESTADO = Object.fromEntries(ESTADOS);
const PLANES = { pro: 'Pro', team: 'Team', enterprise: 'Enterprise' };
const CAMPOS = {
  plan: 'Plan', estado: 'Estado', prueba_fin: 'Fin de la prueba', periodo_fin: 'Fin del período',
  motivo_suspension: 'Motivo de suspensión', estado_antes_suspension: 'Estado antes de suspender',
};
const ACCIONES = {
  activar: 'Activar 30 días', cambiar_plan: 'Cambiar plan', extender_prueba: 'Extender prueba',
  suspender: 'Suspender', reactivar: 'Reactivar',
};
const NOMBRE_AUDITORIA = {
  activar_periodo: 'Activó un período pagado', cambiar_plan: 'Cambió el plan', extender_prueba: 'Extendió la prueba',
  suspender: 'Suspendió la cuenta', reactivar: 'Reactivó la cuenta', mostrar_numero: 'Vio un número completo',
};
const TIPO_PERIODO = { prueba: 'Prueba', extension_prueba: 'Extensión de prueba', pagado: 'Pagado', cortesia: 'Cortesía' };

const $ = id => document.getElementById(id);

function esc(v) {
  return String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fecha(iso, conHora = false) {
  if (!iso) return '—';
  const opc = { timeZone: ZONA, day: '2-digit', month: 'short', year: 'numeric' };
  if (conHora) Object.assign(opc, { hour: 'numeric', minute: '2-digit' });
  return new Intl.DateTimeFormat('es-EC', opc).format(new Date(iso)).replace(/\./g, '');
}

function dinero(v) { return '$' + Number(v || 0).toFixed(2); }

function badgeEstado(c) {
  if (c.estado === 'activa' && c.en_gracia) return '<span class="badge badge-orange">Activa · gracia</span>';
  const cls = { prueba: 'badge-info', activa: 'badge-green', cortesia: 'badge-vencido', vencida: 'badge-red', suspendida: 'badge-muted' }[c.estado] || 'badge-muted';
  return `<span class="badge ${cls}">${esc(NOMBRE_ESTADO[c.estado] || c.estado)}</span>`;
}

function valorCampo(campo, v) {
  if (v === null || v === undefined || v === '') return '—';
  if (campo.endsWith('_fin')) return fecha(v);
  if (campo === 'plan') return PLANES[v] || v;
  if (campo.startsWith('estado')) return NOMBRE_ESTADO[v] || v;
  return v;
}

function cambiosHTML(cambios) {
  if (!cambios || !cambios.length) return '<div class="adm-cambio">Sin cambios en la cuenta.</div>';
  return cambios.map(c => `<div class="adm-cambio">${esc(CAMPOS[c.campo] || c.campo)}: ${esc(valorCampo(c.campo, c.antes))} → <b>${esc(valorCampo(c.campo, c.despues))}</b></div>`).join('');
}

function claveNueva() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = crypto.getRandomValues(new Uint8Array(1))[0] % 16;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

class ErrorApi extends Error {}

async function api(ruta, opciones = {}) {
  const res = await fetch(ruta, {
    ...opciones,
    headers: { 'Content-Type': 'application/json', ...(opciones.headers || {}) },
    credentials: 'same-origin',
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 404 && data.detail === 'Not Found') {
    // el candado respondió: la sesión terminó o ya no es superadministrador
    $('vista').innerHTML = '<div class="empty-state"><p>Tu sesión terminó. <a href="/">Vuelve a entrar en la app</a>.</p></div>';
    throw new ErrorApi('sin acceso');
  }
  if (!res.ok) {
    const det = Array.isArray(data.detail) ? data.detail.map(d => d.msg).join('. ') : (data.detail || `Error ${res.status}`);
    throw Object.assign(new ErrorApi(det), { status: res.status });
  }
  return data;
}

// ── Lista y resumen ──────────────────────────────────────────

const filtros = { q: '', estado: '', plan: '' };
let _temporizador = null;

async function vistaLista() {
  $('vista').innerHTML = `
    <section id="resumen"><p class="adm-sub">Cargando…</p></section>
    <div class="adm-filtros">
      <input id="f-q" class="form-control" type="search" placeholder="Buscar por correo" value="${esc(filtros.q)}" aria-label="Buscar por correo" autocomplete="off" />
      <select id="f-estado" class="form-control" aria-label="Estado">
        <option value="">Todos los estados</option>
        ${ESTADOS.map(([v, n]) => `<option value="${v}"${filtros.estado === v ? ' selected' : ''}>${n}</option>`).join('')}
      </select>
      <select id="f-plan" class="form-control" aria-label="Plan">
        <option value="">Todos los planes</option>
        ${Object.entries(PLANES).map(([v, n]) => `<option value="${v}"${filtros.plan === v ? ' selected' : ''}>${n}</option>`).join('')}
        <option value="sin_plan"${filtros.plan === 'sin_plan' ? ' selected' : ''}>Sin plan</option>
      </select>
    </div>
    <div id="lista" class="adm-lista"></div>`;
  $('f-q').addEventListener('input', e => {
    filtros.q = e.target.value;
    clearTimeout(_temporizador);
    _temporizador = setTimeout(cargarLista, 300);
  });
  $('f-estado').addEventListener('change', e => { filtros.estado = e.target.value; cargarLista(); pintarTilesActivos(); });
  $('f-plan').addEventListener('change', e => { filtros.plan = e.target.value; cargarLista(); });
  await Promise.all([cargarResumen(), cargarLista()]);
}

function pintarTilesActivos() {
  document.querySelectorAll('.adm-tile[data-estado]').forEach(t => t.classList.toggle('activo', t.dataset.estado === filtros.estado));
}

async function cargarResumen() {
  const r = await api('/admin/api/resumen');
  const vencen = r.vencen_7_dias.length
    ? r.vencen_7_dias.map(v => `
        <div class="adm-vence" role="link" tabindex="0" data-cuenta="${esc(v.id_cuenta)}">
          <span>${esc(v.email || '(sin usuario)')} · ${esc(v.plan_nombre || 'Sin plan')} · ${esc({ prueba: 'prueba', pagado: 'pagado', cortesia: 'cortesía', gracia: 'fin de la gracia' }[v.tipo])}</span>
          <span>${fecha(v.fecha)}</span>
        </div>`).join('')
    : '<p class="adm-sub">Ninguna cuenta vence en los próximos 7 días.</p>';
  $('resumen').innerHTML = `
    <div class="adm-estados">
      ${ESTADOS.map(([v, n]) => `
        <button type="button" class="adm-tile" data-estado="${v}">
          <div class="v">${r.conteos[v] ?? 0}</div><div class="n">${n}</div>
        </button>`).join('')}
    </div>
    <div class="adm-dinero">
      <div class="adm-caja">
        <div class="t">Ingreso mensual estimado</div>
        <div class="v">${dinero(r.ingreso_mensual)}</div>
        <div class="adm-sub">${r.en_gracia ? `+ ${dinero(r.ingreso_en_gracia)} en gracia (${r.en_gracia} ${r.en_gracia === 1 ? 'cuenta' : 'cuentas'})` : 'Cuentas activas al día'}</div>
      </div>
      <div class="adm-caja">
        <div class="t">Vencen en 7 días: ${r.vencen_7_dias.length}</div>
        ${vencen}
      </div>
    </div>`;
  document.querySelectorAll('.adm-tile[data-estado]').forEach(t => t.addEventListener('click', () => {
    filtros.estado = filtros.estado === t.dataset.estado ? '' : t.dataset.estado;
    $('f-estado').value = filtros.estado;
    pintarTilesActivos();
    cargarLista();
  }));
  document.querySelectorAll('.adm-vence').forEach(el => {
    const ir = () => { location.hash = '#/cuenta/' + el.dataset.cuenta; };
    el.addEventListener('click', ir);
    el.addEventListener('keydown', e => { if (e.key === 'Enter') ir(); });
  });
  pintarTilesActivos();
}

async function cargarLista() {
  const p = new URLSearchParams();
  if (filtros.q.trim()) p.set('q', filtros.q.trim());
  if (filtros.estado) p.set('estado', filtros.estado);
  if (filtros.plan) p.set('plan', filtros.plan);
  const cuentas = await api('/admin/api/cuentas?' + p.toString());
  const lista = $('lista');
  if (!lista) return;
  if (!cuentas.length) {
    lista.innerHTML = '<div class="empty-state"><p>No hay cuentas con esos filtros.</p></div>';
    return;
  }
  lista.innerHTML = `
    <div class="adm-fila cab"><span>Correo</span><span>Plan</span><span>Estado</span><span>Fin</span><span>Números</span><span>Mensajes del mes</span><span>Creada</span></div>
    ${cuentas.map(c => `
      <button type="button" class="adm-fila" data-cuenta="${esc(c.id_cuenta)}">
        <span class="adm-email">${esc(c.email || '(sin usuario)')}${c.es_interna ? ' <span class="badge badge-muted">Interna</span>' : ''}</span>
        <span><span class="adm-etq">Plan: </span>${esc(c.plan_nombre || 'Sin plan')}</span>
        <span>${badgeEstado(c)}</span>
        <span><span class="adm-etq">Fin: </span>${c.es_interna ? 'Sin vencimiento' : fecha(c.fin)}</span>
        <span><span class="adm-etq">Números: </span>${c.numeros}${c.numeros_max ? '/' + c.numeros_max : ''}</span>
        <span><span class="adm-etq">Mensajes del mes: </span>${c.enviados_mes}</span>
        <span><span class="adm-etq">Creada: </span>${fecha(c.creada_en)}</span>
      </button>`).join('')}`;
  lista.querySelectorAll('.adm-fila[data-cuenta]').forEach(b => b.addEventListener('click', () => {
    location.hash = '#/cuenta/' + b.dataset.cuenta;
  }));
}

// ── Detalle ──────────────────────────────────────────────────

let _cuenta = null;

async function vistaCuenta(id) {
  $('vista').innerHTML = '<p class="adm-sub">Cargando…</p>';
  let c;
  try {
    c = await api('/admin/api/cuentas/' + encodeURIComponent(id));
  } catch (e) {
    if (e.status === 404 || e.status === 422) {
      $('vista').innerHTML = '<div class="empty-state"><p>La cuenta no existe. <a href="#/">Volver a las cuentas</a></p></div>';
      return;
    }
    throw e;
  }
  _cuenta = c;
  const finTexto = c.es_interna ? 'Sin vencimiento'
    : c.estado_guardado === 'prueba' ? `Prueba hasta ${fecha(c.prueba_fin)}`
    : `Período hasta ${fecha(c.periodo_fin)}${c.en_gracia ? ` (gracia hasta ${fecha(c.corte)})` : ''}`;
  const k = c.conteos;
  $('vista').innerHTML = `
    <p style="margin-bottom:12px"><a href="#/">← Cuentas</a></p>
    <div class="card">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px">
        <h2 class="adm-email" style="font-size:1.15rem;margin:0">${esc(c.email || '(sin usuario)')}</h2>
        ${badgeEstado(c)} ${c.es_interna ? '<span class="badge badge-muted">Interna · solo lectura</span>' : ''}
      </div>
      <div class="adm-ficha">
        <div><div class="t">Plan</div>${esc(c.plan_nombre || 'Sin plan')}</div>
        <div><div class="t">Vigencia</div>${esc(finTexto)}</div>
        <div><div class="t">Creada</div>${fecha(c.creada_en)}</div>
        <div><div class="t">Números vinculados</div>${c.numeros}${c.numeros_max ? ' de ' + c.numeros_max : ''}</div>
        ${c.motivo_suspension ? `<div><div class="t">Motivo de suspensión</div>${esc(c.motivo_suspension)}</div>` : ''}
        ${c.usuarios.length > 1 ? `<div><div class="t">Usuarios</div>${c.usuarios.map(esc).join('<br>')}</div>` : ''}
      </div>
    </div>
    <div class="card">
      <div class="card-title">Mensajes (solo conteos)</div>
      <div class="adm-ficha">
        <div><div class="t">Enviados este mes</div>${k.enviados_mes}</div>
        <div><div class="t">Fallidos este mes</div>${k.fallidos_mes}</div>
        <div><div class="t">Vencidos este mes</div>${k.vencidos_mes}</div>
        <div><div class="t">Pendientes</div>${k.pendientes}</div>
        ${c.estado_guardado === 'prueba' ? `<div><div class="t">Usados en la prueba</div>${k.mensajes_prueba_usados}</div>` : ''}
      </div>
    </div>
    <div class="card">
      <div class="card-title">Números</div>
      ${c.numeros_lista.length ? c.numeros_lista.map(n => `
        <div class="adm-num">
          <span data-num="${esc(n.id_sesion)}">${esc(n.numero)}</span>
          <span class="badge ${n.estado_conexion === 'conectado' ? 'badge-green' : 'badge-red'}">${n.estado_conexion === 'conectado' ? 'Conectado' : 'Desconectado'}</span>
          <span style="flex:1"></span>
          <button type="button" class="btn btn-secondary btn-sm" data-mostrar="${esc(n.id_sesion)}">Mostrar</button>
        </div>`).join('') : '<p class="adm-sub">Sin números vinculados.</p>'}
    </div>
    ${c.acciones.length ? `
    <div class="card">
      <div class="card-title">Acciones</div>
      <div class="adm-acciones">
        ${c.acciones.map(a => `<button type="button" class="btn ${a === 'suspender' ? 'btn-danger' : 'btn-secondary'}" data-accion="${a}">${ACCIONES[a]}</button>`).join('')}
      </div>
    </div>` : ''}
    <div class="card">
      <div class="card-title">Historial</div>
      <div id="historial"><p class="adm-sub">Cargando…</p></div>
    </div>`;
  document.querySelectorAll('[data-mostrar]').forEach(b => b.addEventListener('click', () => mostrarNumero(c.id_cuenta, b)));
  document.querySelectorAll('[data-accion]').forEach(b => b.addEventListener('click', () => abrirAccion(b.dataset.accion)));
  cargarHistorial(c.id_cuenta);
}

async function mostrarNumero(idCuenta, boton) {
  boton.disabled = true;
  try {
    const r = await api(`/admin/api/cuentas/${idCuenta}/numeros/${boton.dataset.mostrar}/mostrar`, { method: 'POST', body: '{}' });
    document.querySelector(`[data-num="${boton.dataset.mostrar}"]`).textContent = '+' + r.numero;
    boton.remove();
    cargarHistorial(idCuenta);
  } catch (e) {
    boton.disabled = false;
    if (e.message !== 'sin acceso') alert(e.message);
  }
}

async function cargarHistorial(idCuenta) {
  const eventos = await api(`/admin/api/cuentas/${idCuenta}/historial`);
  const caja = $('historial');
  if (!caja) return;
  if (!eventos.length) { caja.innerHTML = '<p class="adm-sub">Sin movimientos todavía.</p>'; return; }
  caja.innerHTML = eventos.map(e => {
    if (e.tipo === 'periodo') {
      return `<div class="adm-hist">
        <div class="f">${fecha(e.fecha, true)}</div>
        <div><b>${esc(TIPO_PERIODO[e.periodo_tipo] || e.periodo_tipo)}</b>${e.plan ? ' · ' + esc(PLANES[e.plan] || e.plan) : ''}
          · ${fecha(e.inicio)} → ${fecha(e.fin)}${e.precio_usd ? ' · ' + dinero(e.precio_usd) : ''}</div>
        ${e.nota ? `<div class="adm-sub">“${esc(e.nota)}”</div>` : ''}
      </div>`;
    }
    const cambios = e.accion === 'mostrar_numero'
      ? `<div class="adm-cambio">Número ${esc(e.despues?.numero || '')}</div>`
      : cambiosHTML(Object.keys(e.despues || {}).filter(k => (e.antes || {})[k] !== e.despues[k])
          .map(k => ({ campo: k, antes: (e.antes || {})[k], despues: e.despues[k] })));
    return `<div class="adm-hist">
      <div class="f">${fecha(e.fecha, true)} · ${esc(e.email_admin)}</div>
      <div><b>${esc(NOMBRE_AUDITORIA[e.accion] || e.accion)}</b></div>
      ${cambios}
      ${e.nota && e.accion !== 'mostrar_numero' ? `<div class="adm-sub">“${esc(e.nota)}”</div>` : ''}
    </div>`;
  }).join('');
}

// ── Acciones: formulario → Revisar (simulación en el servidor) → Confirmar ──

let _accion = null;   // { tipo, clave, ruta, cuerpo() }

function opcionesPlan(excluir) {
  return Object.entries(_cuenta.planes)
    .filter(([k]) => k !== excluir)
    .map(([k, p]) => `<option value="${k}">${esc(p.nombre)} · ${p.numeros} ${p.numeros === 1 ? 'número' : 'números'} · ${dinero(p.precio_usd)}</option>`).join('');
}

function abrirAccion(tipo) {
  const c = _cuenta;
  const id = encodeURIComponent(c.id_cuenta);
  const formularios = {
    activar: {
      ruta: `/admin/api/cuentas/${id}/activar`,
      html: `
        <div class="form-group">
          <label class="form-label" for="a-plan">Plan</label>
          <select id="a-plan" class="form-control">
            ${c.estado_guardado === 'prueba' ? '<option value="">Elige el plan…</option>' : ''}
            ${Object.entries(c.planes).map(([k, p]) => `<option value="${k}"${k === c.plan ? ' selected' : ''}>${esc(p.nombre)} · ${dinero(p.precio_usd)}</option>`).join('')}
          </select>
        </div>
        <div class="form-group">
          <label class="form-label" for="a-nota">Nota (opcional)</label>
          <input id="a-nota" class="form-control" maxlength="200" placeholder="ej. transferencia 21-oct" />
        </div>
        <p class="adm-sub">Se suman ${c.dias_periodo} días al período vigente (también si está en gracia); si ya venció, empieza hoy.</p>`,
      cuerpo: () => ({ plan: $('a-plan').value || null, nota: $('a-nota').value.trim() || null }),
    },
    cambiar_plan: {
      ruta: `/admin/api/cuentas/${id}/plan`,
      html: `
        <div class="form-group">
          <label class="form-label" for="a-plan">Plan nuevo</label>
          <select id="a-plan" class="form-control">${opcionesPlan(c.plan)}</select>
        </div>
        <p class="adm-sub">No cambia las fechas del período.</p>`,
      cuerpo: () => ({ plan: $('a-plan').value }),
    },
    extender_prueba: {
      ruta: `/admin/api/cuentas/${id}/extender-prueba`,
      html: `
        <div class="form-group">
          <label class="form-label" for="a-dias">Días a extender (1 a 30)</label>
          <input id="a-dias" class="form-control" type="number" inputmode="numeric" min="1" max="30" value="7" />
        </div>`,
      cuerpo: () => ({ dias: parseInt($('a-dias').value, 10) }),
    },
    suspender: {
      ruta: `/admin/api/cuentas/${id}/suspender`,
      html: `
        <div class="form-group">
          <label class="form-label" for="a-motivo">Motivo (obligatorio)</label>
          <textarea id="a-motivo" class="form-control" maxlength="300" style="min-height:70px"></textarea>
        </div>
        <p class="adm-sub">El cliente no podrá entrar ni enviar: se cierran sus sesiones abiertas y sus mensajes (también los que están en cola) pasan a vencido a su hora.</p>`,
      cuerpo: () => ({ motivo: $('a-motivo').value.trim() }),
    },
    reactivar: {
      ruta: `/admin/api/cuentas/${id}/reactivar`,
      html: '<p class="adm-sub">La cuenta vuelve al estado que tenía antes de suspenderla. Si su fecha ya pasó, quedará vencida.</p>',
      cuerpo: () => ({}),
    },
  };
  const f = formularios[tipo];
  _accion = { tipo, clave: claveNueva(), ruta: f.ruta, cuerpo: f.cuerpo };
  $('dlg-titulo').textContent = ACCIONES[tipo];
  $('dlg-cuenta').textContent = c.email || '';
  $('dlg-form').innerHTML = f.html;
  reiniciarRevision();
  $('dlg-form').querySelectorAll('input, select, textarea').forEach(el => el.addEventListener('input', reiniciarRevision));
  $('dlg-accion').showModal();
}

function reiniciarRevision() {
  $('dlg-alerta').innerHTML = '';
  $('dlg-revision').style.display = 'none';
  $('dlg-confirmar').style.display = 'none';
  $('dlg-revisar').style.display = '';
  $('dlg-revisar').disabled = false;
}

function alertaDialogo(texto) {
  $('dlg-alerta').innerHTML = `<div class="alert alert-error" style="margin-top:12px">${esc(texto)}</div>`;
}

async function revisarAccion() {
  const boton = $('dlg-revisar');
  boton.disabled = true;
  $('dlg-alerta').innerHTML = '';
  try {
    const r = await api(_accion.ruta, { method: 'POST', body: JSON.stringify({ ..._accion.cuerpo(), confirmar: false }) });
    $('dlg-revision').innerHTML = '<div><b>Esto va a cambiar:</b></div>' + cambiosHTML(r.cambios);
    $('dlg-revision').style.display = '';
    boton.style.display = 'none';
    const confirmar = $('dlg-confirmar');
    confirmar.style.display = '';
    confirmar.disabled = false;
    confirmar.className = 'btn ' + (_accion.tipo === 'suspender' ? 'btn-danger' : 'btn-primary');
  } catch (e) {
    boton.disabled = false;
    if (e.message !== 'sin acceso') alertaDialogo(e.message);
  }
}

async function confirmarAccion() {
  const boton = $('dlg-confirmar');
  boton.disabled = true;           // un doble toque no hace nada (y la clave lo garantiza en el servidor)
  try {
    await api(_accion.ruta, { method: 'POST', body: JSON.stringify({ ..._accion.cuerpo(), confirmar: true, clave: _accion.clave }) });
    $('dlg-accion').close();
    await vistaCuenta(_cuenta.id_cuenta);
  } catch (e) {
    if (e.message !== 'sin acceso') alertaDialogo(e.message);
  }
}

$('dlg-cancelar').addEventListener('click', () => $('dlg-accion').close());
$('dlg-revisar').addEventListener('click', revisarAccion);
$('dlg-confirmar').addEventListener('click', confirmarAccion);

// ── Navegación ───────────────────────────────────────────────

async function enrutar() {
  const m = location.hash.match(/^#\/cuenta\/([0-9a-fA-F-]{36})$/);
  try {
    if (m) await vistaCuenta(m[1]);
    else await vistaLista();
    window.scrollTo(0, 0);
  } catch (e) {
    if (e.message !== 'sin acceso') {
      $('vista').innerHTML = `<div class="alert alert-error">No se pudo cargar: ${esc(e.message)}</div>`;
    }
  }
}

window.addEventListener('hashchange', enrutar);
enrutar();
