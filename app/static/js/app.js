/* ============================================================
   WhatsApp Scheduler — Frontend JS
   Sin dependencias. Vanilla JS puro.
   ============================================================ */

const API = window.location.origin;  // mismo origen que el backend

// Nombre visible de una sesión: número de teléfono > nombre_sesion > instancia
function _nombreSesion(s) {
  if (s.numero_telefono) return '+' + s.numero_telefono;
  if (s.nombre_sesion)   return s.nombre_sesion;
  return s.instancia_evolution;
}

// ============================================================
// AUTH
// ============================================================

let currentUser = null;  // { id, email, rol }

function mostrarAuthScreen(show = true) {
  document.getElementById('auth-screen').style.display = show ? 'flex' : 'none';
  document.getElementById('app-shell').style.display  = show ? 'none' : '';
}

function mostrarLogin() {
  document.getElementById('auth-login-panel').style.display = '';
  document.getElementById('auth-setup-panel').style.display = 'none';
  document.getElementById('auth-verify-panel').style.display = 'none';
  document.getElementById('alert-login').innerHTML = '';
}

function mostrarSetup() {
  document.getElementById('auth-login-panel').style.display = 'none';
  document.getElementById('auth-setup-panel').style.display = '';
  document.getElementById('auth-verify-panel').style.display = 'none';
  document.getElementById('alert-setup').innerHTML = '';
}

let _verifyEmail = '';

function mostrarVerify(email) {
  _verifyEmail = email;
  document.getElementById('auth-login-panel').style.display = 'none';
  document.getElementById('auth-setup-panel').style.display = 'none';
  document.getElementById('auth-verify-panel').style.display = '';
  document.getElementById('verify-email-hint').textContent =
    `Ingresa el código de 6 dígitos enviado a ${email}`;
  document.getElementById('verify-codigo').value = '';
  document.getElementById('alert-verify').innerHTML = '';
  document.getElementById('verify-codigo').focus();
}

async function checkAuth() {
  try {
    const user = await apiFetch('/auth/me');
    currentUser = user;
    mostrarAuthScreen(false);
    actualizarUISegunRol();
  } catch (e) {
    if (e.status === 401) {
      mostrarAuthScreen(true);
      mostrarLogin();
    }
  }
}

async function loginUser() {
  const email = document.getElementById('login-email').value.trim();
  const password = document.getElementById('login-password').value;
  if (!email || !password) {
    showAlert('alert-login', 'Ingresa email y contraseña', 'error');
    return;
  }
  const btn = document.getElementById('btn-login');
  btn.disabled = true; btn.textContent = 'Entrando...';
  try {
    const data = await apiFetch('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    currentUser = { email: data.email, rol: data.rol };
    mostrarAuthScreen(false);
    actualizarUISegunRol();
    navigate('dashboard');
  } catch (e) {
    if (e.data?.detail === 'email_no_verificado') {
      mostrarVerify(email);
    } else {
      showAlert('alert-login', e.data?.detail || 'Credenciales incorrectas', 'error');
    }
  } finally {
    btn.disabled = false; btn.textContent = 'Iniciar sesión';
  }
}

async function setupUser() {
  const email = document.getElementById('setup-email').value.trim();
  const password = document.getElementById('setup-password').value;
  if (!email || !password) {
    showAlert('alert-setup', 'Completa todos los campos', 'error');
    return;
  }
  const btn = document.getElementById('btn-setup');
  btn.disabled = true; btn.textContent = 'Creando...';
  try {
    await apiFetch('/auth/setup', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    });
    mostrarVerify(email);
  } catch (e) {
    showAlert('alert-setup', e.data?.detail || e.message, 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Crear cuenta';
  }
}

async function logoutUser() {
  await apiFetch('/auth/logout', { method: 'POST' }).catch(() => {});
  currentUser = null;
  mostrarAuthScreen(true);
  mostrarLogin();
}

async function verificarCodigo() {
  const codigo = document.getElementById('verify-codigo').value.trim();
  if (codigo.length !== 6) {
    showAlert('alert-verify', 'El código debe tener 6 dígitos', 'error');
    return;
  }
  const btn = document.getElementById('btn-verify');
  btn.disabled = true; btn.textContent = 'Verificando...';
  try {
    const data = await apiFetch('/auth/verificar-codigo', {
      method: 'POST',
      body: JSON.stringify({ email: _verifyEmail, codigo }),
    });
    currentUser = { email: data.email, rol: data.rol };
    mostrarAuthScreen(false);
    actualizarUISegunRol();
    navigate('dashboard');
  } catch (e) {
    showAlert('alert-verify', e.data?.detail || 'Código incorrecto o expirado', 'error');
  } finally {
    btn.disabled = false; btn.textContent = 'Verificar';
  }
}

async function reenviarCodigo() {
  if (!_verifyEmail) return;
  try {
    await apiFetch('/auth/reenviar-codigo', {
      method: 'POST',
      body: JSON.stringify({ email: _verifyEmail }),
    });
    showAlert('alert-verify', 'Código reenviado. Revisa tu correo.', 'success');
  } catch (e) {
    showAlert('alert-verify', e.data?.detail || 'Error al reenviar', 'error');
  }
}

function actualizarUISegunRol() {
  const badge = document.getElementById('user-badge');
  if (badge && currentUser) {
    badge.textContent = currentUser.email + (currentUser.rol === 'viewer' ? ' (viewer)' : '');
  }
  // viewer: ocultar el botón de programar en la nav
  const btnProgramar = document.querySelector('[data-page="programar"]');
  if (btnProgramar) btnProgramar.style.display = currentUser?.rol === 'viewer' ? 'none' : '';
}

// ============================================================
// Utilidades globales
// ============================================================

const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];
const fmt = new Intl.DateTimeFormat('es-EC', {
  dateStyle: 'short', timeStyle: 'short', timeZone: 'America/Guayaquil'
});
const fmtDate = d => d ? fmt.format(new Date(d)) : '—';

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.detail || 'Error ' + res.status), { data, status: res.status });
  return data;
}

// Devuelve "YYYY-MM-DDTHH:MM" en la zona horaria de Ecuador (America/Guayaquil)
function _ecuadorNow() {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'America/Guayaquil',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  }).format(new Date()).replace(' ', 'T');
}

// Pre-rellena el campo inp-fecha con la hora actual de Ecuador
function _prefillFecha() {
  const inp = document.getElementById('inp-fecha');
  if (!inp) return;
  inp.value = _ecuadorNow();
}

function showAlert(containerId, msg, type = 'info') {
  const el = document.getElementById(containerId);
  if (!el) return;
  el.innerHTML = `<div class="alert alert-${type}"><span>${msg}</span></div>`;
  setTimeout(() => { if (el) el.innerHTML = ''; }, 6000);
}

// ============================================================
// NAVEGACIÓN SPA
// ============================================================

let currentPage = null;

function navigate(pageId) {
  $$('.page').forEach(p => p.classList.remove('active'));
  $$('.nav-btn').forEach(b => b.classList.remove('active'));
  const page = document.getElementById('page-' + pageId);
  const btn = document.querySelector(`[data-page="${pageId}"]`);
  if (page) page.classList.add('active');
  if (btn) btn.classList.add('active');
  currentPage = pageId;
  // Actualizar título del topbar
  const titles = { dashboard: 'Dashboard', programar: 'Programar Mensaje', mensajes: 'Mensajes Programados', sesion: 'Sesión WhatsApp' };
  const titleEl = document.getElementById('topbar-title');
  if (titleEl) titleEl.textContent = titles[pageId] || pageId;
  // Cargar datos según la página
  if (pageId === 'dashboard') loadDashboard();
  if (pageId === 'mensajes') loadMensajes();
  if (pageId === 'programar') loadGrupos();
  if (pageId === 'sesion') loadSesion();
}

// ============================================================
// ESTADO GLOBAL (sesión activa)
// ============================================================

let sesionActiva = null;      // sesión preferida (primera conectada, o primera)
let todasLasSesiones = [];    // lista completa de sesiones
let qrInterval = null;        // timer del countdown QR

async function cargarSesionActiva() {
  try {
    todasLasSesiones = await apiFetch('/sesiones/');
    sesionActiva = todasLasSesiones.find(s => s.estado_conexion === 'conectado')
               || todasLasSesiones[0]
               || null;
  } catch {
    todasLasSesiones = [];
    sesionActiva = null;
  }
}

// ============================================================
// PAGE: DASHBOARD
// ============================================================

let _histDias = 7;

async function loadDashboard() {
  await cargarSesionActiva();
  renderEstadoSesion();
  // load all sections in parallel
  await Promise.all([
    loadStatsResumen(),
    loadHistorial(_histDias),
    loadProximos(),
    loadRecientes(),
  ]);
}

function renderEstadoSesion() {
  const el = document.getElementById('dash-sesion');
  if (!el) return;
  const badgeClass = e => e === 'conectado' ? 'badge-green' : e === 'qr_pendiente' ? 'badge-orange' : 'badge-red';
  const labels = { conectado: 'Conectado', qr_pendiente: 'Esperando QR', desconectado: 'Desconectado' };

  if (!todasLasSesiones.length) {
    el.innerHTML = `
      <div class="card-title">Sesión WhatsApp</div>
      <div class="alert alert-info" style="margin:0">Sin sesión.
        <button class="btn btn-sm btn-primary" style="margin-left:12px" onclick="navigate('sesion')">Conectar</button>
      </div>`;
    return;
  }
  el.innerHTML = `<div class="card-title">Sesiones WhatsApp</div>` +
    todasLasSesiones.map(s => `
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:6px 0;border-bottom:1px solid var(--color-border)">
        <div class="text-mono" style="flex:1;min-width:120px;font-size:.82rem">${_nombreSesion(s)}</div>
        <span class="badge ${badgeClass(s.estado_conexion)}">${labels[s.estado_conexion] || s.estado_conexion}</span>
        ${s.estado_conexion !== 'conectado' ? `<button class="btn btn-sm btn-secondary" onclick="navigate('sesion')">QR</button>` : ''}
      </div>`).join('');
}

async function loadStatsResumen() {
  const el = document.getElementById('dash-stats');
  if (!el) return;
  try {
    const stats = await apiFetch('/mensajes/stats');
    const tasaColor = stats.tasa_exito === null ? 'var(--color-muted)'
      : stats.tasa_exito >= 90 ? 'var(--color-green)'
      : stats.tasa_exito >= 70 ? 'var(--color-warning)'
      : 'var(--color-danger)';
    el.innerHTML = `
      <div class="stats-row">
        <div class="stat-card">
          <div class="stat-value text-green">${stats.programados}</div>
          <div class="stat-label">Programados</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">${stats.enviados}</div>
          <div class="stat-label">Enviados</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color:var(--color-danger)">${stats.fallidos}</div>
          <div class="stat-label">Fallidos</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color:var(--color-muted)">${stats.cancelados}</div>
          <div class="stat-label">Cancelados</div>
        </div>
        <div class="stat-card">
          <div class="stat-value" style="color:${tasaColor}">${stats.tasa_exito !== null ? stats.tasa_exito + '%' : '—'}</div>
          <div class="stat-label">Tasa de éxito</div>
        </div>
      </div>`;
  } catch {
    el.innerHTML = '';
  }
}

async function loadHistorial(dias) {
  _histDias = dias;
  // update active button
  $$('.hist-rango').forEach(b => {
    b.classList.toggle('active', Number(b.dataset.dias) === dias);
  });
  const el = document.getElementById('dash-chart');
  if (!el) return;
  el.innerHTML = '<div class="spinner"></div>';
  try {
    const data = await apiFetch(`/mensajes/historial?dias=${dias}`);
    if (!data.length) {
      el.innerHTML = '<div class="chart-empty">Sin actividad en este período</div>';
      return;
    }
    // Fill missing days so chart always shows full range
    const filled = _fillDias(data, dias);
    const maxTotal = Math.max(...filled.map(d => d.enviados + d.fallidos), 1);
    const HEIGHT = 100; // px available for bars

    el.innerHTML = `<div class="chart-wrap">${filled.map(d => {
      const total = d.enviados + d.fallidos;
      const hEnv = total ? Math.round((d.enviados / maxTotal) * HEIGHT) : 0;
      const hFal = total ? Math.round((d.fallidos / maxTotal) * HEIGHT) : 0;
      const label = d.fecha.slice(5); // MM-DD
      return `<div class="chart-col" title="${d.fecha}: ${d.enviados} enviados, ${d.fallidos} fallidos">
        <div class="chart-stacked" style="height:${HEIGHT}px">
          ${hFal ? `<div class="chart-bar chart-bar-fallido" style="height:${hFal}px"></div>` : ''}
          ${hEnv ? `<div class="chart-bar chart-bar-enviado" style="height:${hEnv}px"></div>` : ''}
          ${!total ? `<div style="height:2px;background:var(--color-border);border-radius:2px"></div>` : ''}
        </div>
        <div class="chart-label">${label}</div>
      </div>`;
    }).join('')}</div>`;
  } catch {
    el.innerHTML = '<div class="chart-empty">Error al cargar historial</div>';
  }
}

function _fillDias(data, dias) {
  // Build a map fecha → row, then fill all days in range
  const map = {};
  data.forEach(d => { map[d.fecha] = d; });
  const result = [];
  const hoy = new Date();
  for (let i = dias - 1; i >= 0; i--) {
    const d = new Date(hoy);
    d.setDate(d.getDate() - i);
    // Format as YYYY-MM-DD in local time
    const key = d.toLocaleDateString('sv'); // 'sv' locale gives YYYY-MM-DD
    result.push(map[key] || { fecha: key, enviados: 0, fallidos: 0 });
  }
  return result;
}

async function loadProximos() {
  const el = document.getElementById('dash-proximos');
  if (!el) return;
  try {
    const lista = await apiFetch('/mensajes/proximos?limite=5');
    if (!lista.length) {
      el.innerHTML = '<p class="text-muted" style="font-size:.83rem">No hay mensajes programados.</p>';
      return;
    }
    el.innerHTML = lista.map(m => {
      const dest = m.id_grupo.replace('@g.us', '').replace('@s.whatsapp.net', '');
      const texto = m.texto_mensaje ? m.texto_mensaje.substring(0, 35) + (m.texto_mensaje.length > 35 ? '…' : '') : (m.tipo_media ? `${ICON_MAP[m.tipo_media] || '📎'} ${m.tipo_media}` : '—');
      const recBadge = m.recurrencia && m.recurrencia !== 'none' ? ` <span style="font-size:.68rem;color:var(--color-info)">🔁</span>` : '';
      return `<div class="dash-list-item">
        <span class="badge badge-orange" style="font-size:.68rem;flex-shrink:0">📅</span>
        <div class="dash-list-dest">${texto}${recBadge}</div>
        <div class="dash-list-fecha">${fmtDate(m.fecha_hora_disparo)}</div>
      </div>`;
    }).join('');
  } catch {
    el.innerHTML = '<p class="text-muted" style="font-size:.83rem">—</p>';
  }
}

async function loadRecientes() {
  const el = document.getElementById('dash-recientes');
  if (!el) return;
  try {
    const lista = await apiFetch('/mensajes/recientes?limite=5');
    if (!lista.length) {
      el.innerHTML = '<p class="text-muted" style="font-size:.83rem">Sin historial de envíos.</p>';
      return;
    }
    el.innerHTML = lista.map(m => {
      const dest = m.id_grupo.replace('@g.us', '').replace('@s.whatsapp.net', '');
      const texto = m.texto_mensaje ? m.texto_mensaje.substring(0, 35) + (m.texto_mensaje.length > 35 ? '…' : '') : (m.tipo_media ? `${ICON_MAP[m.tipo_media] || '📎'} ${m.tipo_media}` : '—');
      const isOk = m.estado_envio === 'enviado';
      return `<div class="dash-list-item">
        <span style="font-size:.9rem;flex-shrink:0">${isOk ? '✅' : '❌'}</span>
        <div class="dash-list-dest">${texto}</div>
        <div class="dash-list-fecha">${fmtDate(m.fecha_hora_disparo)}</div>
      </div>`;
    }).join('');
  } catch {
    el.innerHTML = '<p class="text-muted" style="font-size:.83rem">—</p>';
  }
}

// ============================================================
// PAGE: SESIÓN / QR
// ============================================================

async function loadSesion() {
  await cargarSesionActiva();
  renderPanelSesion();
}

function renderPanelSesion() {
  const el = document.getElementById('sesion-content');
  if (!el) return;
  const badgeClass = e => e === 'conectado' ? 'badge-green' : e === 'qr_pendiente' ? 'badge-orange' : 'badge-red';
  const labels = { conectado: 'Conectado', qr_pendiente: 'Esperando QR', desconectado: 'Desconectado' };

  const tarjetas = todasLasSesiones.map(s => `
    <div class="card" id="card-sesion-${s.id_sesion}">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="flex:1;min-width:0">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span class="text-mono">${_nombreSesion(s)}</span>
            <span class="badge ${badgeClass(s.estado_conexion)}">${labels[s.estado_conexion] || s.estado_conexion}</span>
          </div>
          ${!s.numero_telefono ? `<div class="text-muted" style="font-size:.72rem;margin-top:3px">Escaneando QR…</div>` : ''}
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${s.estado_conexion !== 'conectado' ? `<button class="btn btn-sm btn-primary" onclick="mostrarQR('${s.id_sesion}')">📱 QR</button>` : ''}
          <button class="btn btn-sm btn-secondary" onclick="verificarEstado('${s.id_sesion}')">🔄 Verificar</button>
          ${s.estado_conexion === 'conectado' ? `<button class="btn btn-sm btn-secondary" onclick="cargarGruposSesion('${s.id_sesion}')">🔃 Grupos</button>` : ''}
          <button class="btn btn-sm btn-danger" onclick="eliminarSesion('${s.id_sesion}')">Eliminar</button>
        </div>
      </div>
      <div id="qr-container-${s.id_sesion}" style="display:none;margin-top:16px"></div>
      <div id="grupos-container-${s.id_sesion}" style="display:none;margin-top:16px"></div>
    </div>`).join('');

  el.innerHTML = `
    ${tarjetas}
    <div class="card">
      <div class="card-title">Añadir cuenta de WhatsApp</div>
      <div id="alert-sesion"></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${_esMobile() ? `
          <button class="btn btn-primary" onclick="elegirMetodoPairing()">🔢 Vincular con código</button>
          <button class="btn btn-secondary" onclick="crearSesion()">📷 Escanear QR</button>
        ` : `
          <button class="btn btn-primary" onclick="crearSesion()">📷 Escanear QR</button>
          <button class="btn btn-secondary" onclick="elegirMetodoPairing()">🔢 Vincular con código</button>
        `}
      </div>
      <div id="form-pairing-code" style="display:none;margin-top:16px"></div>
      <div id="qr-nueva-sesion" style="display:none;margin-top:16px"></div>
    </div>`;
}

let qrCountdown = null;

async function mostrarQR(id_sesion) {
  const container = document.getElementById(`qr-container-${id_sesion}`);
  if (!container) return;
  container.style.display = '';
  container.innerHTML = '<div class="qr-wrap"><div class="spinner"></div><p class="text-muted">Obteniendo QR...</p></div>';
  try {
    const data = await apiFetch(`/sesiones/${id_sesion}/qr`);
    const b64 = data.base64;

    if (data.estado === 'conectado') {
      container.innerHTML = `
        <div class="alert alert-success" style="margin:0">
          <p style="margin:0 0 12px">✅ Ya está conectada.</p>
          <button class="btn btn-secondary btn-sm" onclick="loadSesion()">🔄 Actualizar panel</button>
        </div>`;
      setTimeout(() => loadSesion(), 1500);
      return;
    }

    if (!b64) {
      const raw = data.raw || {};
      const msg = raw.message || raw.error || 'Sin QR disponible. Espera unos segundos y vuelve a intentar.';
      container.innerHTML = `
        <div class="alert alert-info" style="margin:0">
          <p style="margin:0 0 12px">${msg}</p>
          <button class="btn btn-primary btn-sm" onclick="mostrarQR('${id_sesion}')">🔄 Reintentar</button>
          <button class="btn btn-secondary btn-sm" onclick="verificarEstado('${id_sesion}')" style="margin-left:8px">🔍 Verificar</button>
        </div>`;
      return;
    }

    let segundos = 60;
    const imgSrc = b64.startsWith('data:') ? b64 : `data:image/png;base64,${b64}`;
    const timerId = `qr-timer-${id_sesion}`;
    container.innerHTML = `
      <div class="qr-wrap">
        <img src="${imgSrc}" alt="QR WhatsApp" style="width:260px;height:260px;border-radius:8px">
        <div class="qr-timer" id="${timerId}">${segundos}s</div>
        <p class="qr-instructions">Abre WhatsApp → Dispositivos vinculados → Vincular un dispositivo</p>
        <button class="btn btn-secondary btn-sm" onclick="mostrarQR('${id_sesion}')">🔄 Renovar QR</button>
      </div>`;
    clearInterval(qrCountdown);
    qrCountdown = setInterval(() => {
      segundos--;
      const t = document.getElementById(timerId);
      if (t) t.textContent = segundos + 's';
      if (segundos <= 0) { clearInterval(qrCountdown); if (t) t.textContent = 'Expirado'; }
    }, 1000);
  } catch (e) {
    container.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}

async function verificarEstado(id_sesion) {
  try {
    const data = await apiFetch(`/sesiones/${id_sesion}/verificar`);
    await cargarSesionActiva();
    renderPanelSesion();
    showAlert('alert-sesion', `${id_sesion.slice(0,8)}… → ${data.estado_evolution}`, data.estado_evolution === 'open' ? 'success' : 'info');
  } catch (e) {
    showAlert('alert-sesion', e.message, 'error');
  }
}

async function crearSesion() {
  const btn = document.getElementById('btn-crear-sesion');
  if (btn) { btn.disabled = true; btn.textContent = 'Conectando...'; }
  // Nombre de instancia auto-generado (no visible para el usuario)
  const instancia = 'wa-' + Date.now().toString(36);
  try {
    const nueva = await apiFetch('/sesiones/', {
      method: 'POST',
      body: JSON.stringify({ instancia_evolution: instancia }),
    });
    await cargarSesionActiva();
    renderPanelSesion();
    setTimeout(() => mostrarQR(nueva.id_sesion), 300);
  } catch (e) {
    const msg = typeof e.data?.detail === 'string'
      ? e.data.detail
      : Array.isArray(e.data?.detail) ? e.data.detail.map(d => d.msg).join('; ') : e.message;
    showAlert('alert-sesion', msg, 'error');
    if (btn) { btn.disabled = false; btn.textContent = 'Conectar nueva cuenta'; }
  }
}

// ── Pairing code ─────────────────────────────────────────────────────────

function _esMobile() {
  return window.innerWidth <= 640 || /Android|iPhone|iPad|iPod/i.test(navigator.userAgent);
}

function elegirMetodoPairing() {
  const div = document.getElementById('form-pairing-code');
  if (!div) return;
  div.style.display = '';
  div.innerHTML = `
    <div class="form-group">
      <label class="form-label">País</label>
      <select id="sel-pais-code" class="form-control" style="max-width:260px">
        <option value="593" selected>🇪🇨 Ecuador (+593)</option>
        <option value="57">🇨🇴 Colombia (+57)</option>
        <option value="51">🇵🇪 Perú (+51)</option>
        <option value="56">🇨🇱 Chile (+56)</option>
        <option value="54">🇦🇷 Argentina (+54)</option>
        <option value="55">🇧🇷 Brasil (+55)</option>
        <option value="52">🇲🇽 México (+52)</option>
        <option value="1">🇺🇸 USA/Canada (+1)</option>
      </select>
    </div>
    <div class="form-group">
      <label class="form-label">Número de WhatsApp</label>
      <input id="inp-tel-code" type="tel" class="form-control" placeholder="Ej: 0969829845" style="max-width:260px" />
      <p style="font-size:.78rem;color:var(--color-muted);margin-top:4px">Sin código de país — solo el número local.</p>
    </div>
    <button class="btn btn-primary" id="btn-get-code" onclick="obtenerCodigoPairing()">Obtener código</button>`;
}

let _pairingPolls = {};

async function obtenerCodigoPairing() {
  const pais   = document.getElementById('sel-pais-code')?.value || '593';
  const telRaw = document.getElementById('inp-tel-code')?.value?.trim() || '';

  // Normalizar: quitar espacios, guiones, '+', y el 0 inicial del número local
  let digits = telRaw.replace(/[\s\-+]/g, '').replace(/\D/g, '');
  if (digits.startsWith('0')) digits = digits.slice(1);

  if (!digits || digits.length < 7) {
    showAlert('alert-sesion', 'Ingresa un número de teléfono válido', 'error');
    return;
  }
  const numero = pais + digits;

  const btn = document.getElementById('btn-get-code');
  if (btn) { btn.disabled = true; btn.textContent = 'Generando código (~15 s)...'; }

  try {
    // Un solo endpoint que hace todo: limpia pendientes, crea instancia con número,
    // espera WS, sondea pairingCode y devuelve {id_sesion, pairing_code}
    const resp = await apiFetch('/sesiones/iniciar-pairing', {
      method: 'POST',
      body: JSON.stringify({ numero }),
    });

    await cargarSesionActiva();
    renderPanelSesion();
    _mostrarPanelPairing(resp.id_sesion, resp.pairing_code, numero);
    _iniciarPollingPairing(resp.id_sesion);

  } catch (e) {
    const msg = e.data?.detail || e.message;
    // Recargar lista (pendientes pueden haber sido eliminadas) y mostrar error SIN QR
    await cargarSesionActiva();
    renderPanelSesion();
    elegirMetodoPairing();
    showAlert('alert-sesion', msg, 'error');
  }
}

function _mostrarPanelPairing(id_sesion, code, numero) {
  const container = document.getElementById(`qr-container-${id_sesion}`);
  if (!container) return;
  container.style.display = '';
  container.innerHTML = `
    <div style="padding:4px 0">
      <div style="font-size:2.2rem;font-weight:700;letter-spacing:.12em;font-family:monospace;color:var(--color-primary);margin:10px 0 8px">${code}</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px">
        <button class="btn btn-sm btn-secondary" onclick="_copiarCodigoPairing('${code}')">Copiar</button>
        <button class="btn btn-sm btn-secondary" onclick="_renovarCodigo('${id_sesion}','${numero}')">Generar nuevo código</button>
        <button class="btn btn-sm btn-secondary" onclick="mostrarQR('${id_sesion}')">Ver QR</button>
      </div>
      <ol style="font-size:.83rem;color:var(--color-text);margin:0;padding-left:18px;line-height:1.9">
        <li>Abre <strong>WhatsApp</strong> en tu teléfono</li>
        <li>Ve a <strong>Dispositivos vinculados</strong></li>
        <li>Toca <strong>Vincular un dispositivo</strong></li>
        <li>Toca <strong>Vincular con número de teléfono</strong></li>
        <li>Escribe el código: <strong>${code}</strong></li>
      </ol>
      <div id="pairing-status-${id_sesion}" style="margin-top:10px"></div>
    </div>`;
}

function _copiarCodigoPairing(code) {
  const plain = code.replace('-', '');
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(plain).then(() => {
      showAlert('alert-sesion', '✅ Código copiado', 'success');
    }).catch(() => showAlert('alert-sesion', 'Copia: ' + code, 'info'));
  } else {
    showAlert('alert-sesion', 'Copia: ' + code, 'info');
  }
}

async function _renovarCodigo(id_sesion, numero) {
  try {
    const resp = await apiFetch(`/sesiones/${id_sesion}/pairing-code?numero=${numero}`);
    _mostrarPanelPairing(id_sesion, resp.pairing_code, numero);
    _iniciarPollingPairing(id_sesion);
  } catch (e) {
    showAlert('alert-sesion', 'No se pudo renovar: ' + (e.data?.detail || e.message), 'error');
  }
}

function _iniciarPollingPairing(id_sesion) {
  if (_pairingPolls[id_sesion]) clearInterval(_pairingPolls[id_sesion].timer);
  const startTime = Date.now();
  const TIMEOUT_MS = 10 * 60 * 1000;

  const timer = setInterval(async () => {
    if (Date.now() - startTime > TIMEOUT_MS) {
      clearInterval(timer);
      delete _pairingPolls[id_sesion];
      const el = document.getElementById(`pairing-status-${id_sesion}`);
      if (el) el.innerHTML = '<div class="alert alert-error" style="margin:0">Tiempo agotado. La sesión se eliminará automáticamente.</div>';
      return;
    }
    try {
      const data = await apiFetch(`/sesiones/${id_sesion}/verificar`);
      if (data.estado_evolution === 'open') {
        clearInterval(timer);
        delete _pairingPolls[id_sesion];
        const container = document.getElementById(`qr-container-${id_sesion}`);
        if (container) container.innerHTML = '<div class="alert alert-success" style="margin:0">✅ Cuenta vinculada correctamente</div>';
        setTimeout(async () => { await cargarSesionActiva(); renderPanelSesion(); }, 2000);
      }
    } catch { /* ignorar errores de red, seguir polling */ }
  }, 3000);

  _pairingPolls[id_sesion] = { timer, startTime };
}

async function eliminarSesion(id_sesion) {
  if (!confirm('¿Eliminar esta sesión y todos sus mensajes programados?')) return;
  try {
    await apiFetch(`/sesiones/${id_sesion}`, { method: 'DELETE' });
    await cargarSesionActiva();
    renderPanelSesion();
  } catch (e) {
    alert('Error al eliminar: ' + e.message);
  }
}

async function cargarGruposSesion(id_sesion) {
  const container = document.getElementById(`grupos-container-${id_sesion}`);
  if (!container) return;
  container.style.display = '';
  container.innerHTML = '<div class="spinner"></div>';
  try {
    const grupos = await apiFetch(`/sesiones/${id_sesion}/grupos`);
    if (!grupos.length) {
      container.innerHTML = '<p class="text-muted" style="font-size:.88rem">Sin grupos.</p>';
      return;
    }
    container.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>Nombre</th><th>JID</th><th>Participantes</th></tr></thead>
          <tbody>
            ${grupos.map(g => `<tr>
              <td>${g.subject || '—'}</td>
              <td class="text-mono" style="font-size:.75rem">${g.id}</td>
              <td>${g.size || '—'}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>`;
  } catch (e) {
    container.innerHTML = `<div class="alert alert-error">${e.message}</div>`;
  }
}


// ============================================================
// PAGE: PROGRAMAR MENSAJE
// ============================================================

let gruposCache = [];

function toggleDestino(radio) {
  const val = radio.value;
  document.getElementById('grp-grupo').style.display    = val === 'grupo'    ? '' : 'none';
  document.getElementById('grp-personal').style.display = val === 'personal' ? '' : 'none';
  document.getElementById('grp-manual').style.display   = val === 'manual'   ? '' : 'none';

  document.getElementById('lbl-grupo').classList.toggle('active', val === 'grupo');
  document.getElementById('lbl-personal').classList.toggle('active', val === 'personal');
  document.getElementById('lbl-manual').classList.toggle('active', val === 'manual');

  // required dinámico
  // sel-grupo is hidden; validation is handled manually in programarMensaje
  document.getElementById('inp-numero').required  = val === 'personal';
  document.getElementById('inp-jid-manual').required = val === 'manual';

  // Al cambiar de opción, limpiar el JID resuelto
  if (val !== 'manual') {
    document.getElementById('jid-resuelto').style.display = 'none';
    document.getElementById('inp-jid-manual').value = '';
    document.getElementById('hid-jid-manual').value = '';
  }
}


// ---- Combo buscable de grupos ----
function _renderListaGrupos(grupos) {
  const drop = document.getElementById('lista-grupos-drop');
  if (!drop) return;
  if (!grupos || !grupos.length) {
    drop.innerHTML = '<p style="padding:10px 14px;font-size:.82rem;color:var(--color-muted)">Sin resultados</p>';
    return;
  }
  const selId = document.getElementById('sel-grupo')?.value || '';
  drop.innerHTML = grupos.map(g => {
    const nombre = (g.subject || g.id).replace(/</g,'&lt;');
    const size = g.size ? ` · ${g.size} miembros` : '';
    const sel = g.id === selId ? ' selected' : '';
    return `<div class="grupo-opt${sel}" onmousedown="seleccionarGrupo('${g.id}','${nombre.replace(/'/g,"\'")}')">`
      + `<span style="font-size:.85rem;font-weight:500">${nombre}</span>`
      + `<span style="font-size:.72rem;color:var(--color-muted);display:block">${g.id}${size}</span>`
      + `</div>`;
  }).join('');
}

function filtrarGrupos(q) {
  const drop = document.getElementById('lista-grupos-drop');
  if (!drop) return;
  drop.style.display = '';
  const term = q.trim().toLowerCase();
  const filtrado = term
    ? gruposCache.filter(g => (g.subject||'').toLowerCase().includes(term) || g.id.includes(term))
    : gruposCache;
  _renderListaGrupos(filtrado);
}

function abrirListaGrupos() {
  if (!gruposCache.length) return;
  const drop = document.getElementById('lista-grupos-drop');
  if (!drop) return;
  drop.style.display = '';
  const q = document.getElementById('inp-grupo-buscar')?.value || '';
  filtrarGrupos(q);
}

function cerrarListaGrupos() {
  const drop = document.getElementById('lista-grupos-drop');
  if (drop) drop.style.display = 'none';
}

function seleccionarGrupo(id, nombre) {
  document.getElementById('sel-grupo').value = id;
  document.getElementById('inp-grupo-buscar').value = nombre;
  cerrarListaGrupos();
}

function _poblarComboGrupos(grupos, estado) {
  const inp = document.getElementById('inp-grupo-buscar');
  const sel = document.getElementById('sel-grupo');
  if (!inp || !sel) return;
  if (estado === 'cargando') {
    inp.placeholder = 'Cargando grupos...';
    inp.disabled = true;
    gruposCache = [];
    return;
  }
  inp.disabled = false;
  if (estado === 'error') {
    inp.placeholder = 'Error al cargar grupos';
    gruposCache = [];
    return;
  }
  if (!grupos.length) {
    inp.placeholder = '— Sin grupos disponibles —';
    gruposCache = [];
    return;
  }
  gruposCache = grupos;
  inp.placeholder = `Buscar entre ${grupos.length} grupos...`;
  sel.value = '';
  inp.value = '';
}

async function loadGrupos() {
  const sel = document.getElementById('sel-grupo');
  if (!sel) return;
  await cargarSesionActiva();
  if (!sesionActiva) {
    sel.innerHTML = '<option value="">— Sin sesión activa —</option>';
    return;
  }

  // Poblar selector de sesión si hay más de una
  const selSesion = document.getElementById('sel-sesion');
  const grpSesion = document.getElementById('grp-sesion-selector');
  if (selSesion && grpSesion) {
    if (todasLasSesiones.length > 1) {
      grpSesion.style.display = '';
      selSesion.innerHTML = todasLasSesiones.map(s =>
        `<option value="${s.id_sesion}" ${s.id_sesion === sesionActiva.id_sesion ? 'selected' : ''}>
          ${_nombreSesion(s)} (${s.estado_conexion})
        </option>`
      ).join('');
    } else {
      grpSesion.style.display = 'none';
    }
  }

  // Usar la sesión seleccionada en el selector (si existe), o la activa
  const idSesionUsar = selSesion?.value || sesionActiva.id_sesion;
  const hidSesion = document.getElementById('hid-sesion');
  if (hidSesion) hidSesion.value = idSesionUsar;

  _poblarComboGrupos([], 'cargando');
  try {
    const grupos = await apiFetch(`/sesiones/${idSesionUsar}/grupos`);
    _poblarComboGrupos(grupos, 'ok');
  } catch (e) {
    _poblarComboGrupos([], 'error');
    showAlert('alert-programar', e.message, 'error');
  }
}

async function onSesionChange() {
  const sel = document.getElementById('sel-sesion');
  const hidSesion = document.getElementById('hid-sesion');
  if (hidSesion && sel) hidSesion.value = sel.value;
  _contactosCache = [];
  _contactosCargados = false;  // forzar recarga al cambiar de sesión
  // recargar grupos con la nueva sesión
  _poblarComboGrupos([], 'cargando');
  try {
    const grupos = await apiFetch(`/sesiones/${sel.value}/grupos`);
    _poblarComboGrupos(grupos, 'ok');
  } catch (e) {
    _poblarComboGrupos([], 'error');
  }
}

// ---- Resolver enlace de grupo WhatsApp ----

/**
 * Intenta convertir un enlace https://chat.whatsapp.com/XXXX
 * en un JID de grupo usando el endpoint /sesiones/{id}/resolver-enlace.
 * Si el usuario ya escribió un JID directo (termina en @g.us) lo acepta tal cual.
 */
async function resolverEnlaceGrupo() {
  const input = document.getElementById('inp-jid-manual');
  const resueltoDiv = document.getElementById('jid-resuelto');
  const resueltoVal = document.getElementById('jid-resuelto-valor');
  const resueltoNombre = document.getElementById('jid-nombre-grupo');
  const hidJid = document.getElementById('hid-jid-manual');
  const btn = document.getElementById('btn-resolver-jid');

  const raw = input.value.trim();
  if (!raw) {
    showAlert('alert-programar', 'Escribe un enlace o JID primero', 'error');
    return;
  }

  // Si ya es un JID directo (p.ej. 120363xxx@g.us o 593961xxx@s.whatsapp.net)
  if (raw.includes('@')) {
    hidJid.value = raw;
    resueltoVal.textContent = raw;
    resueltoNombre.textContent = '';
    resueltoDiv.style.display = '';
    return;
  }

  // Extraer el código de invitación del enlace
  // https://chat.whatsapp.com/CODIGO  →  CODIGO
  const match = raw.match(/chat\.whatsapp\.com\/([A-Za-z0-9]+)/);
  if (!match) {
    showAlert('alert-programar', 'Enlace no reconocido. Debe ser de la forma https://chat.whatsapp.com/XXXXX o un JID como 1234@g.us', 'error');
    return;
  }
  const codigoInvitacion = match[1];

  if (!sesionActiva) {
    showAlert('alert-programar', 'No hay sesión activa para resolver el enlace', 'error');
    return;
  }

  btn.disabled = true;
  btn.textContent = '⏳';
  resueltoDiv.style.display = 'none';

  try {
    const data = await apiFetch(
      `/sesiones/${sesionActiva.id_sesion}/resolver-enlace?codigo=${codigoInvitacion}`
    );
    // Respuesta esperada: { jid: "120363xxx@g.us", subject: "Nombre del grupo" }
    if (!data.jid) throw new Error('El servidor no devolvió un JID válido');

    hidJid.value = data.jid;
    resueltoVal.textContent = data.jid;
    resueltoNombre.textContent = data.subject ? `— ${data.subject}` : '';
    resueltoDiv.style.display = '';
    showAlert('alert-programar', `✅ Grupo resuelto: ${data.subject || data.jid}`, 'success');
  } catch (err) {
    showAlert('alert-programar', `No se pudo resolver el enlace: ${err.message}`, 'error');
    hidJid.value = '';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Resolver';
  }
}

// ---- Agenda de contactos ----

let _contactosCache = [];
let _contactosCargados = false;
let _panelContactosAbierto = false;

async function togglePanelContactos() {
  const panel = document.getElementById('panel-contactos');
  if (!panel) return;
  _panelContactosAbierto = !_panelContactosAbierto;
  panel.style.display = _panelContactosAbierto ? '' : 'none';
  if (_panelContactosAbierto) {
    document.getElementById('inp-buscar-contacto')?.focus();
    if (!_contactosCargados) await cargarContactos();
    else renderContactos(_contactosCache);
  }
}

async function cargarContactos() {
  const lista = document.getElementById('lista-contactos');
  if (!lista) return;
  if (!sesionActiva) {
    lista.innerHTML = '<p class="text-muted" style="font-size:.82rem;padding:10px 14px">Sin sesión WhatsApp activa.</p>';
    return;
  }
  lista.innerHTML = '<div style="padding:12px 14px"><div class="spinner"></div></div>';
  try {
    _contactosCache = await apiFetch(`/sesiones/${sesionActiva.id_sesion}/contactos`);
    _contactosCargados = true;
    renderContactos(_contactosCache);
  } catch (e) {
    _contactosCargados = false;
    lista.innerHTML = `<div style="padding:12px 14px">
      <p class="text-muted" style="font-size:.82rem;margin:0 0 10px">Error al cargar contactos: ${e.message}</p>
      <button class="btn btn-sm btn-secondary" onclick="usarNumeroManual()" style="width:100%">Ingresar número manualmente</button>
    </div>`;
  }
}

function filtrarContactos(query) {
  const q = query.toLowerCase().trim();
  const filtrados = q
    ? _contactosCache.filter(c =>
        c.nombre.toLowerCase().includes(q) || c.numero.includes(q)
      )
    : _contactosCache;
  renderContactos(filtrados);
}

function renderContactos(lista) {
  const el = document.getElementById('lista-contactos');
  if (!el) return;
  if (!lista.length) {
    // Si no hay contactos en caché (no es filtrado), ofrecer entrada manual
    const esFiltrando = document.getElementById('inp-buscar-contacto')?.value?.trim().length > 0;
    if (!esFiltrando && !_contactosCache.length) {
      el.innerHTML = `
        <div style="padding:12px 14px">
          <p class="text-muted" style="font-size:.82rem;margin:0 0 10px">Sin contactos disponibles.</p>
          <button class="btn btn-sm btn-secondary" onclick="usarNumeroManual()" style="width:100%">
            Ingresar número manualmente
          </button>
        </div>`;
    } else {
      el.innerHTML = '<p class="text-muted" style="font-size:.82rem;padding:10px 14px">Sin resultados.</p>';
    }
    return;
  }
  el.innerHTML = lista.map(c => `
    <div style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--color-border);cursor:pointer"
         onclick="seleccionarContacto('${c.numero}','${(c.nombre || '').replace(/'/g, '&#39;')}')"
         onmouseover="this.style.background='rgba(255,255,255,.04)'"
         onmouseout="this.style.background=''">
      <div style="width:32px;height:32px;border-radius:50%;background:var(--color-surface2);display:flex;align-items:center;justify-content:center;font-size:.8rem;flex-shrink:0">
        ${c.nombre ? c.nombre[0].toUpperCase() : '#'}
      </div>
      <div style="flex:1;min-width:0">
        <div style="font-size:.85rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${c.nombre || '<span style="color:var(--color-muted)">Sin nombre</span>'}</div>
        <div style="font-size:.75rem;color:var(--color-muted)">${c.numero}</div>
      </div>
    </div>`).join('');
}

function usarNumeroManual() {
  const panel = document.getElementById('panel-contactos');
  if (panel) panel.style.display = 'none';
  _panelContactosAbierto = false;
  const inp = document.getElementById('inp-numero');
  if (inp) { inp.focus(); inp.value = ''; }
}

function seleccionarContacto(numero, nombre) {
  const inp = document.getElementById('inp-numero');
  if (inp) {
    inp.value = numero;
    inp.dispatchEvent(new Event('input'));
  }
  // Cerrar panel
  const panel = document.getElementById('panel-contactos');
  if (panel) panel.style.display = 'none';
  _panelContactosAbierto = false;
  // Resetear búsqueda
  const search = document.getElementById('inp-buscar-contacto');
  if (search) search.value = '';
  renderContactos(_contactosCache);
  // Mostrar nombre del contacto seleccionado
  if (nombre) {
    const verif = document.getElementById('numero-verificado');
    if (verif) {
      verif.style.display = '';
      verif.style.background = 'rgba(83,189,235,.1)';
      verif.style.color = 'var(--color-info)';
      verif.innerHTML = `👤 Contacto seleccionado: <strong>${nombre}</strong>`;
    }
  }
}

// ---- Verificar número personal ----

async function verificarNumeroWA() {
  const input = document.getElementById('inp-numero');
  const resultDiv = document.getElementById('numero-verificado');
  const btn = document.getElementById('btn-verificar-numero');

  const numero = input.value.trim();
  if (!numero) {
    showAlert('alert-programar', 'Ingresa un número primero', 'error');
    return;
  }
  if (!sesionActiva) {
    showAlert('alert-programar', 'No hay sesión activa para verificar', 'error');
    return;
  }

  btn.disabled = true;
  btn.textContent = '⏳';
  resultDiv.style.display = 'none';

  try {
    const data = await apiFetch(`/sesiones/${sesionActiva.id_sesion}/verificar-numero`, {
      method: 'POST',
      body: JSON.stringify({ numero }),
    });
    resultDiv.style.display = '';
    if (data.existe === true) {
      resultDiv.style.background = 'var(--color-success-bg, #d1fae5)';
      resultDiv.style.color = 'var(--color-success, #065f46)';
      resultDiv.innerHTML = `✅ Número activo en WhatsApp${data.nombre ? ` — <strong>${data.nombre}</strong>` : ''}<br><small style="opacity:.8">${data.jid}</small>`;
    } else if (data.existe === false) {
      resultDiv.style.background = 'var(--color-danger-bg, #fee2e2)';
      resultDiv.style.color = 'var(--color-danger, #991b1b)';
      resultDiv.innerHTML = '⚠️ Este número no tiene WhatsApp activo';
    } else {
      resultDiv.style.background = 'var(--color-info-bg, #dbeafe)';
      resultDiv.style.color = 'var(--color-info, #1e40af)';
      resultDiv.innerHTML = `ℹ️ No se pudo verificar — el número se usará tal cual: <strong>${data.jid}</strong>`;
    }
  } catch (e) {
    showAlert('alert-programar', 'Error al verificar: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Verificar';
  }
}

// ---- Subida de archivos adjuntos ----

const ICON_MAP = { image: '🖼️', video: '🎬', audio: '🎵', document: '📄' };

async function subirArchivo(input) {
  const file = input.files[0];
  if (!file) return;

  const placeholder = document.getElementById('upload-placeholder');
  const preview = document.getElementById('upload-preview');
  const progress = document.getElementById('upload-progress');
  const alertUpload = document.getElementById('alert-upload');
  alertUpload.innerHTML = '';

  // Mostrar preview (nombre)
  placeholder.style.display = 'none';
  preview.style.display = '';
  document.getElementById('upload-nombre').textContent = file.name;
  document.getElementById('upload-tipo').textContent = `${(file.size / 1024).toFixed(0)} KB · subiendo…`;
  progress.style.display = '';
  progress.style.width = '30%';

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch(`${API}/media/subir`, { method: 'POST', body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);

    progress.style.width = '100%';
    document.getElementById('hid-url-media').value = data.url;
    document.getElementById('hid-tipo-media').value = data.tipo_media;
    document.getElementById('upload-nombre').textContent = file.name;
    document.getElementById('upload-tipo').textContent =
      `${ICON_MAP[data.tipo_media] || '📎'} ${data.tipo_media} · ${(data.bytes / 1024).toFixed(0)} KB`;
    document.getElementById('upload-icon').textContent = ICON_MAP[data.tipo_media] || '📎';

    setTimeout(() => { progress.style.display = 'none'; }, 800);
  } catch (err) {
    // Revertir
    placeholder.style.display = '';
    preview.style.display = 'none';
    progress.style.display = 'none';
    input.value = '';
    alertUpload.innerHTML = `<div class="alert alert-error" style="margin-top:8px">${err.message}</div>`;
  }
}

function limpiarAdjunto(e) {
  e.stopPropagation();
  document.getElementById('hid-url-media').value = '';
  document.getElementById('hid-tipo-media').value = '';
  document.getElementById('inp-archivo').value = '';
  document.getElementById('upload-placeholder').style.display = '';
  document.getElementById('upload-preview').style.display = 'none';
  document.getElementById('upload-progress').style.display = 'none';
  document.getElementById('alert-upload').innerHTML = '';
}


// ──────────────────────────────────────────────────────────
// Grabación de audio / video (MediaRecorder API)
// ──────────────────────────────────────────────────────────
let _grabRecorder = null;
let _grabChunks   = [];
let _grabStream   = null;
let _grabTimer    = null;
let _grabSecs     = 0;
let _grabMime     = '';

function _grabIds(ctx) {
  const p = ctx === 'edit' ? 'edit-' : '';
  return {
    btns:    document.getElementById(p + 'grab-btns'),
    estado:  document.getElementById(p + 'grab-estado'),
    label:   document.getElementById(p + 'grab-label'),
    timer:   document.getElementById(p + 'grab-timer'),
    preview: document.getElementById(p + 'grab-preview'),
  };
}

async function iniciarGrabacion(tipo, ctx = 'main') {
  // Alerta cercana a los botones (no en el tope del form)
  const alertCercano = ctx === 'edit' ? 'alert-editar' : 'alert-upload';

  // Soporte del navegador
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    showAlert(alertCercano, 'Tu navegador no soporta grabación de audio/video. Usa Chrome o Firefox.', 'error');
    return;
  }
  if (typeof MediaRecorder === 'undefined') {
    showAlert(alertCercano, 'Tu navegador no soporta MediaRecorder. Actualiza el navegador.', 'error');
    return;
  }

  if (_grabRecorder) {
    detenerGrabacion(ctx === 'main' ? 'edit' : 'main');
  }

  const ids = _grabIds(ctx);

  try {
    const constraints = tipo === 'video'
      ? { video: { facingMode: 'user', width: { ideal: 640 }, height: { ideal: 480 } }, audio: true }
      : { audio: true };
    _grabStream = await navigator.mediaDevices.getUserMedia(constraints);

    const preferidos = tipo === 'video'
      ? ['video/webm;codecs=vp9,opus', 'video/webm;codecs=vp8,opus', 'video/webm', 'video/mp4']
      : ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4'];
    _grabMime = preferidos.find(m => {
      try { return MediaRecorder.isTypeSupported(m); } catch { return false; }
    }) || '';

    _grabRecorder = new MediaRecorder(_grabStream, _grabMime ? { mimeType: _grabMime } : {});
    _grabChunks = [];
    _grabRecorder.ondataavailable = e => { if (e.data && e.data.size > 0) _grabChunks.push(e.data); };
    _grabRecorder.onstop = () => _onGrabStop(tipo, ctx);
    _grabRecorder.start(100);

    ids.btns.style.display = 'none';
    ids.label.textContent = tipo === 'video' ? 'Grabando video...' : 'Grabando audio...';
    ids.estado.style.display = '';
    if (tipo === 'video') {
      ids.preview.style.display = '';
      ids.preview.srcObject = _grabStream;
    }
    _grabSecs = 0;
    ids.timer.textContent = '0:00';
    _grabTimer = setInterval(() => {
      _grabSecs++;
      const m = Math.floor(_grabSecs / 60), s = _grabSecs % 60;
      ids.timer.textContent = m + ':' + String(s).padStart(2, '0');
    }, 1000);

  } catch (err) {
    _grabStream?.getTracks().forEach(t => t.stop());
    _grabStream = null;
    _grabRecorder = null;
    const recurso = tipo === 'video' ? 'cámara/micrófono' : 'micrófono';
    let msg = `No se pudo acceder al ${recurso}: ${err.message}`;
    if (err.name === 'NotAllowedError') {
      msg = `Permiso denegado. Haz clic en el ícono de ${recurso} en la barra de direcciones y permite el acceso.`;
    } else if (err.name === 'NotFoundError') {
      msg = `No se encontró ${recurso === 'micrófono' ? 'un micrófono' : 'cámara ni micrófono'} en este dispositivo.`;
    }
    showAlert(alertCercano, msg, 'error');
    // Hacer scroll para que el error sea visible
    document.getElementById(alertCercano)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
}

function detenerGrabacion(ctx = 'main') {
  if (!_grabRecorder) return;
  clearInterval(_grabTimer);
  _grabTimer = null;
  _grabRecorder.stop();
  _grabStream.getTracks().forEach(t => t.stop());
  const ids = _grabIds(ctx);
  ids.preview.srcObject = null;
  ids.preview.style.display = 'none';
  ids.estado.style.display = 'none';
  ids.btns.style.display = '';
}

async function _onGrabStop(tipo, ctx) {
  const mime = _grabMime || (tipo === 'video' ? 'video/webm' : 'audio/webm');
  const blob = new Blob(_grabChunks, { type: mime });
  _grabRecorder = null;
  _grabChunks = [];
  _grabStream = null;

  const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.mp4' : '.webm';
  const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -1);
  const filename = `grabacion_${tipo}_${ts}${ext}`;

  const formData = new FormData();
  formData.append('file', blob, filename);

  if (ctx === 'edit') {
    const info = document.getElementById('edit-upload-info');
    info.textContent = 'Subiendo grabación…';
    info.style.display = '';
    try {
      const res = await fetch(`${API}/media/subir`, { method: 'POST', body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      document.getElementById('edit-url-media').value = data.url;
      document.getElementById('edit-tipo-media').value = data.tipo_media;
      info.textContent = `${ICON_MAP[data.tipo_media] || '📎'} ${filename} (${(data.bytes / 1024).toFixed(0)} KB)`;
      info.style.color = 'var(--color-green)';
    } catch (err) {
      info.textContent = '❌ Error al subir: ' + err.message;
      info.style.color = 'var(--color-danger)';
    }
  } else {
    const alertEl = document.getElementById('alert-upload');
    const placeholder = document.getElementById('upload-placeholder');
    const preview = document.getElementById('upload-preview');
    placeholder.style.display = 'none';
    preview.style.display = '';
    document.getElementById('upload-nombre').textContent = filename;
    document.getElementById('upload-tipo').textContent = 'Subiendo…';
    alertEl.innerHTML = '';
    try {
      const res = await fetch(`${API}/media/subir`, { method: 'POST', body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.status);
      document.getElementById('hid-url-media').value = data.url;
      document.getElementById('hid-tipo-media').value = data.tipo_media;
      document.getElementById('upload-nombre').textContent = filename;
      document.getElementById('upload-tipo').textContent = `${ICON_MAP[data.tipo_media] || '📎'} ${data.tipo_media} · ${(data.bytes / 1024).toFixed(0)} KB`;
      document.getElementById('upload-icon').textContent = ICON_MAP[data.tipo_media] || '📎';
    } catch (err) {
      placeholder.style.display = '';
      preview.style.display = 'none';
      alertEl.innerHTML = `<div class="alert alert-error">Error: ${err.message}</div>`;
    }
  }
}

// ---- Drag & drop sobre la zona (registrado en INIT) ----
function initUploadZone() {
  const zone = document.getElementById('upload-zone');
  if (!zone) return;
  zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
  zone.addEventListener('dragleave', () => zone.classList.remove('dragover'));
  zone.addEventListener('drop', e => {
    e.preventDefault();
    zone.classList.remove('dragover');
    const files = e.dataTransfer?.files;
    if (files?.length) {
      const inp = document.getElementById('inp-archivo');
      try {
        const dt = new DataTransfer();
        dt.items.add(files[0]);
        inp.files = dt.files;
        subirArchivo(inp);
      } catch { /* Safari sin soporte DataTransfer en input */ }
    }
  });
}

async function programarMensaje(e) {
  e.preventDefault();
  const form = e.target;

  // Determinar destino (grupo, número personal o JID manual)
  const tipoDestino = form.querySelector('input[name="tipo-destino"]:checked')?.value || 'grupo';
  let idGrupo;
  if (tipoDestino === 'grupo') {
    idGrupo = document.getElementById('sel-grupo')?.value;
  } else if (tipoDestino === 'personal') {
    idGrupo = document.getElementById('inp-numero')?.value?.trim();
  } else {
    // manual: usar el JID ya resuelto (campo oculto) o el valor crudo si incluye @
    idGrupo = document.getElementById('hid-jid-manual')?.value?.trim()
           || document.getElementById('inp-jid-manual')?.value?.trim();
  }

  const texto = document.getElementById('txt-mensaje')?.value?.trim() || null;
  const fechaHora = document.getElementById('inp-fecha')?.value;
  const idSesion = document.getElementById('hid-sesion')?.value;
  const urlMedia = document.getElementById('hid-url-media')?.value || null;
  const tipoMedia = document.getElementById('hid-tipo-media')?.value || null;

  // Validaciones
  if (!idGrupo) {
    const msgs = {
      grupo: 'Selecciona un grupo de la lista',
      personal: 'Ingresa un número de teléfono',
      manual: 'Escribe un enlace de WhatsApp o JID, y pulsa "Resolver"',
    };
    showAlert('alert-programar', msgs[tipoDestino] || 'Selecciona un destinatario', 'error');
    return;
  }
  // Para manual: si no resolvió pero tiene @ puede usarse directamente
  if (tipoDestino === 'manual' && !idGrupo.includes('@')) {
    showAlert('alert-programar', 'Pulsa "Resolver" para convertir el enlace en JID antes de programar', 'error');
    return;
  }
  if (!texto && !urlMedia) {
    showAlert('alert-programar', 'Escribe un mensaje o adjunta un archivo', 'error');
    return;
  }
  if (!fechaHora) {
    showAlert('alert-programar', 'Selecciona la fecha y hora de envío', 'error');
    return;
  }
  // Verificar si la hora ya pasó (comparando con Ecuador UTC-5)
  let forzar = false;
  {
    const fechaElegida = new Date(fechaHora + ':00-05:00');
    if (fechaElegida <= new Date()) {
      const ok = confirm('La hora seleccionada ya pasó. ¿Enviar ahora?');
      if (!ok) return;
      forzar = true;
    }
  }
  if (!idSesion) {
    showAlert('alert-programar', 'No hay sesión WhatsApp activa', 'error');
    return;
  }

  // datetime-local devuelve "YYYY-MM-DDTHH:MM" — la hora que el usuario escribió.
  // La UI dice "Hora Ecuador (UTC-5)", así que el usuario siempre ingresa hora Ecuador.
  // Simplemente pegamos el offset -05:00 al string, sin ninguna conversión extra.
  // Pydantic en el backend recibe "2026-09-25T11:00:00-05:00" y lo convierte a UTC correctamente.
  const fechaISO = fechaHora + ':00-05:00';

  const btn = form.querySelector('[type=submit]');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Programando...';

  try {
    const recurrencia = document.getElementById('sel-recurrencia')?.value || 'none';
    const payload = {
      id_sesion: idSesion,
      id_grupo: idGrupo,
      fecha_hora_disparo: fechaISO,
      recurrencia,
      forzar,
    };
    if (texto) payload.texto_mensaje = texto;
    if (urlMedia && tipoMedia) {
      payload.url_media = urlMedia;
      payload.tipo_media = tipoMedia;
    }

    await apiFetch('/mensajes/', {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    showAlert('alert-programar', '✅ Mensaje programado correctamente', 'success');
    form.reset();
    _prefillFecha();
    limpiarAdjunto({ stopPropagation: () => {} });
    toggleDestino({ value: 'grupo' });  // resetear a grupo
    await loadGrupos();
  } catch (err) {
    showAlert('alert-programar', err.data?.detail || err.message, 'error');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '📅 Programar mensaje';
  }
}

// ============================================================
// PAGE: MENSAJES
// ============================================================

let msgFiltro = 'todos';
let msgPagina = 1;
const MSG_POR_PAG = 20;
let _mensajesCache = {};   // id → objeto mensaje, para el modal de edición

async function loadMensajes() {
  const el = document.getElementById('mensajes-table-body');
  const empty = document.getElementById('mensajes-empty');
  if (!el) return;
  el.innerHTML = '<tr><td colspan="6"><div class="spinner"></div></td></tr>';

  try {
    const params = new URLSearchParams({ limit: MSG_POR_PAG, offset: (msgPagina - 1) * MSG_POR_PAG });
    if (msgFiltro !== 'todos') params.set('estado', msgFiltro);
    const lista = await apiFetch('/mensajes/?' + params);
    if (!lista.length) {
      el.innerHTML = '';
      if (empty) empty.style.display = '';
      return;
    }
    if (empty) empty.style.display = 'none';
    _mensajesCache = {};
    el.innerHTML = lista.map(m => {
      _mensajesCache[m.id_mensaje] = m;
      const estadoClass = {
        programado: 'badge-orange', enviando: 'badge-info',
        enviado: 'badge-green', fallido: 'badge-red', cancelado: 'badge-muted',
      }[m.estado_envio] || 'badge-muted';
      return `<tr>
        <td class="text-mono" style="font-size:.75rem">${m.id_mensaje.split('-')[0]}…</td>
        <td>${m.id_grupo}</td>
        <td>
          ${m.texto_mensaje ? m.texto_mensaje.substring(0, 50) + (m.texto_mensaje.length > 50 ? '…' : '') : ''}
          ${m.tipo_media ? `<span class="badge badge-info" style="font-size:.7rem">${ICON_MAP[m.tipo_media] || '📎'} ${m.tipo_media}</span>` : (!m.texto_mensaje ? '<span class="text-muted">—</span>' : '')}
        </td>
        <td>${fmtDate(m.fecha_hora_disparo)}</td>
        <td>
          <span class="badge ${estadoClass}">${m.estado_envio}</span>
          ${m.recurrencia && m.recurrencia !== 'none' ? `<span class="badge badge-info" style="font-size:.7rem;margin-left:4px" title="Recurrencia: ${m.recurrencia}">🔁 ${m.recurrencia === 'daily' ? 'Diario' : m.recurrencia === 'weekly' ? 'Semanal' : 'Mensual'}</span>` : ''}
        </td>
        <td style="white-space:nowrap">
          ${currentUser?.rol === 'admin' ? `
            ${m.estado_envio === 'programado' ? `
              <button class="btn btn-sm btn-secondary" onclick="abrirModalEditar('${m.id_mensaje}')" style="margin-right:4px">Editar</button>
              <button class="btn btn-sm btn-danger" onclick="cancelarMensaje('${m.id_mensaje}')" style="margin-right:4px">Cancelar</button>
            ` : ''}
            ${['enviado','fallido','cancelado'].includes(m.estado_envio) ? `
              <button class="btn btn-sm btn-danger" onclick="eliminarMensaje('${m.id_mensaje}')">Eliminar</button>
            ` : ''}
          ` : '—'}
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    el.innerHTML = `<tr><td colspan="6"><div class="alert alert-error">${e.message}</div></td></tr>`;
  }
}

function filtrarMensajes(estado) {
  msgFiltro = estado;
  msgPagina = 1;
  $$('.filter-btn').forEach(b => b.classList.remove('active'));
  const btn = document.querySelector(`[data-filter="${estado}"]`);
  if (btn) btn.classList.add('active');
  loadMensajes();
}

async function cancelarMensaje(id) {
  if (!confirm('¿Cancelar este mensaje?')) return;
  try {
    await apiFetch(`/mensajes/${id}/cancelar`, { method: 'PATCH' });
    loadMensajes();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function eliminarMensaje(id) {
  if (!confirm('¿Eliminar este mensaje permanentemente?')) return;
  try {
    await apiFetch(`/mensajes/${id}`, { method: 'DELETE' });
    loadMensajes();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ---- Modal Editar ----

function abrirModalEditar(id) {
  const m = _mensajesCache[id];
  if (!m) return;
  document.getElementById('edit-id').value = m.id_mensaje;
  document.getElementById('edit-texto').value = m.texto_mensaje || '';
  document.getElementById('edit-url-media').value = m.url_media || '';
  document.getElementById('edit-tipo-media').value = m.tipo_media || '';

  const mediaActual = document.getElementById('edit-media-actual');
  if (m.url_media) {
    mediaActual.textContent = `${ICON_MAP[m.tipo_media] || '📎'} ${m.tipo_media} — ${m.url_media.split('/').pop()}`;
    mediaActual.style.color = '';
  } else {
    mediaActual.textContent = 'Sin adjunto';
    mediaActual.style.color = 'var(--color-muted)';
  }

  document.getElementById('edit-upload-info').style.display = 'none';
  document.getElementById('edit-upload-info').textContent = '';
  document.getElementById('edit-inp-archivo').value = '';

  // Convertir fecha UTC a datetime-local Ecuador
  if (m.fecha_hora_disparo) {
    const d = new Date(m.fecha_hora_disparo);
    const offset = -5 * 60; // Ecuador UTC-5
    const local = new Date(d.getTime() + (offset - d.getTimezoneOffset()) * 60000);
    document.getElementById('edit-fecha').value = local.toISOString().slice(0, 16);
  }

  const selRecurrencia = document.getElementById('edit-recurrencia');
  if (selRecurrencia) selRecurrencia.value = m.recurrencia || 'none';

  document.getElementById('alert-editar').innerHTML = '';
  const modal = document.getElementById('modal-editar');
  modal.style.display = 'flex';
}

function cerrarModalEditar() {
  detenerGrabacion('edit');
  document.getElementById('modal-editar').style.display = 'none';
}

function editQuitarMedia() {
  document.getElementById('edit-url-media').value = '';
  document.getElementById('edit-tipo-media').value = '';
  document.getElementById('edit-media-actual').textContent = 'Sin adjunto';
  document.getElementById('edit-media-actual').style.color = 'var(--color-muted)';
  document.getElementById('edit-upload-info').style.display = 'none';
  document.getElementById('edit-inp-archivo').value = '';
}

async function editSubirArchivo(input) {
  const file = input.files[0];
  if (!file) return;
  const info = document.getElementById('edit-upload-info');
  info.style.display = '';
  info.textContent = `Subiendo ${file.name}…`;

  const formData = new FormData();
  formData.append('file', file);
  try {
    const res = await fetch(`${API}/media/subir`, { method: 'POST', body: formData });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Error ${res.status}`);
    document.getElementById('edit-url-media').value = data.url;
    document.getElementById('edit-tipo-media').value = data.tipo_media;
    info.textContent = `${ICON_MAP[data.tipo_media] || '📎'} ${file.name} (${(data.bytes / 1024).toFixed(0)} KB)`;
  } catch (err) {
    info.textContent = `Error: ${err.message}`;
    input.value = '';
  }
}

async function guardarEdicion() {
  const id = document.getElementById('edit-id').value;
  const texto = document.getElementById('edit-texto').value.trim() || null;
  const urlMedia = document.getElementById('edit-url-media').value || null;
  const tipoMedia = document.getElementById('edit-tipo-media').value || null;
  const fechaHora = document.getElementById('edit-fecha').value;

  if (!texto && !urlMedia) {
    showAlert('alert-editar', 'El mensaje necesita texto o adjunto', 'error');
    return;
  }
  if (!fechaHora) {
    showAlert('alert-editar', 'La fecha de envío es obligatoria', 'error');
    return;
  }

  const recurrencia = document.getElementById('edit-recurrencia')?.value || 'none';
  const payload = { fecha_hora_disparo: fechaHora + ':00-05:00', recurrencia };
  if (texto !== null) payload.texto_mensaje = texto;
  // Siempre enviar ambos o ninguno (validación backend)
  payload.tipo_media = tipoMedia;
  payload.url_media = urlMedia;

  const btn = document.getElementById('btn-guardar-edicion');
  btn.disabled = true;
  btn.textContent = 'Guardando…';
  try {
    await apiFetch(`/mensajes/${id}`, { method: 'PATCH', body: JSON.stringify(payload) });
    cerrarModalEditar();
    loadMensajes();
  } catch (e) {
    showAlert('alert-editar', e.data?.detail || e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Guardar cambios';
  }
}

// ============================================================
// PLANTILLAS
// ============================================================

let _plantillasCache = [];
let _panelPlantillasAbierto = false;

async function togglePanelPlantillas() {
  const panel = document.getElementById('panel-plantillas');
  if (!panel) return;
  _panelPlantillasAbierto = !_panelPlantillasAbierto;
  panel.style.display = _panelPlantillasAbierto ? '' : 'none';
  if (_panelPlantillasAbierto) await renderListaPlantillas();
}

async function renderListaPlantillas() {
  const el = document.getElementById('lista-plantillas');
  if (!el) return;
  try {
    _plantillasCache = await apiFetch('/plantillas/');
    if (!_plantillasCache.length) {
      el.innerHTML = '<p class="text-muted" style="font-size:.82rem;padding:8px 14px">No hay plantillas guardadas.</p>';
      return;
    }
    el.innerHTML = _plantillasCache.map(p => `
      <div style="display:flex;align-items:center;gap:8px;padding:7px 14px;border-bottom:1px solid var(--color-border,#1e2330)">
        <div style="flex:1;min-width:0">
          <div style="font-size:.85rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${p.nombre}</div>
          <div style="font-size:.75rem;color:var(--color-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            ${p.texto_mensaje ? p.texto_mensaje.substring(0, 60) + (p.texto_mensaje.length > 60 ? '…' : '') : ''}
            ${p.tipo_media ? `<span class="badge badge-info" style="font-size:.68rem">${ICON_MAP[p.tipo_media] || '📎'} ${p.tipo_media}</span>` : ''}
          </div>
        </div>
        <button type="button" class="btn btn-sm btn-secondary" onclick="cargarPlantilla('${p.id_plantilla}')" style="white-space:nowrap">Usar</button>
        <button type="button" class="btn btn-sm btn-danger" onclick="eliminarPlantilla('${p.id_plantilla}')" style="padding:4px 8px">✕</button>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = `<p class="text-muted" style="padding:8px 14px;font-size:.82rem">Error: ${e.message}</p>`;
  }
}

function cargarPlantilla(id) {
  const p = _plantillasCache.find(x => x.id_plantilla === id);
  if (!p) return;

  // Rellenar texto
  const txt = document.getElementById('txt-mensaje');
  if (txt) {
    txt.value = p.texto_mensaje || '';
    txt.dispatchEvent(new Event('input'));
  }

  // Rellenar media si tiene
  if (p.tipo_media && p.url_media) {
    document.getElementById('hid-url-media').value = p.url_media;
    document.getElementById('hid-tipo-media').value = p.tipo_media;
    document.getElementById('upload-placeholder').style.display = 'none';
    document.getElementById('upload-preview').style.display = '';
    document.getElementById('upload-nombre').textContent = p.url_media.split('/').pop();
    document.getElementById('upload-tipo').textContent = `${ICON_MAP[p.tipo_media] || '📎'} ${p.tipo_media}`;
    document.getElementById('upload-icon').textContent = ICON_MAP[p.tipo_media] || '📎';
  }

  // Cerrar panel
  document.getElementById('panel-plantillas').style.display = 'none';
  _panelPlantillasAbierto = false;
}

async function guardarPlantilla() {
  const texto = document.getElementById('txt-mensaje')?.value?.trim() || null;
  const urlMedia = document.getElementById('hid-url-media')?.value || null;
  const tipoMedia = document.getElementById('hid-tipo-media')?.value || null;

  if (!texto && !urlMedia) {
    showAlert('alert-programar', 'Escribe un mensaje o adjunta un archivo antes de guardar la plantilla', 'error');
    return;
  }

  const nombre = prompt('Nombre para esta plantilla:');
  if (!nombre?.trim()) return;

  try {
    await apiFetch('/plantillas/', {
      method: 'POST',
      body: JSON.stringify({ nombre: nombre.trim(), texto_mensaje: texto, tipo_media: tipoMedia || null, url_media: urlMedia || null }),
    });
    await renderListaPlantillas();
    showAlert('alert-programar', '✅ Plantilla guardada', 'success');
  } catch (e) {
    showAlert('alert-programar', e.data?.detail || e.message, 'error');
  }
}

async function eliminarPlantilla(id) {
  if (!confirm('¿Eliminar esta plantilla?')) return;
  try {
    await apiFetch(`/plantillas/${id}`, { method: 'DELETE' });
    await renderListaPlantillas();
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

// ============================================================
// INIT
// ============================================================

document.addEventListener('DOMContentLoaded', async () => {
  // Botones de navegación
  $$('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => navigate(btn.dataset.page));
  });

  // Form de programar
  const formProgramar = document.getElementById('form-programar');
  if (formProgramar) formProgramar.addEventListener('submit', programarMensaje);

  // Filtros tabla mensajes
  $$('.filter-btn').forEach(btn => {
    btn.addEventListener('click', () => filtrarMensajes(btn.dataset.filter));
  });

  // Rangos historial
  $$('.hist-rango').forEach(btn => {
    btn.addEventListener('click', () => loadHistorial(Number(btn.dataset.dias)));
  });

  // Pre-fill fecha con hora actual Ecuador (UTC-5), funciona en Android/iOS/desktop
  _prefillFecha();

  // Cerrar modal editar al hacer clic en el fondo
  document.getElementById('modal-editar')?.addEventListener('click', e => {
    if (e.target === e.currentTarget) cerrarModalEditar();
  });

  // Drag & drop en zona de subida
  initUploadZone();

  // Cerrar combo de grupos al hacer clic fuera
  document.addEventListener('mousedown', e => {
    const wrap = document.getElementById('lista-grupos-drop');
    const inp  = document.getElementById('inp-grupo-buscar');
    if (wrap && !wrap.contains(e.target) && e.target !== inp) {
      cerrarListaGrupos();
    }
  });

  // Enter en login y setup
  ['login-email','login-password'].forEach(id => {
    document.getElementById(id)?.addEventListener('keydown', e => { if (e.key === 'Enter') loginUser(); });
  });
  ['setup-email','setup-password'].forEach(id => {
    document.getElementById(id)?.addEventListener('keydown', e => { if (e.key === 'Enter') setupUser(); });
  });
  document.getElementById('verify-codigo')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') verificarCodigo();
  });

  // Verificar autenticación antes de mostrar la app
  await checkAuth();
  if (currentUser) navigate('dashboard');
});
