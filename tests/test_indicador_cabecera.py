"""
Prueba del indicador de conexión de la cabecera ejecutando el app.js REAL en Node (con un DOM y un
fetch simulados). Cubre la causa del error: antes se consultaba al cargar la página, sin login, y
mostraba "Sin sesión" hasta 30 s después aunque hubiera una sesión conectada.
Se omite si Node no está instalado.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node no está instalado")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const escenarios = JSON.parse(process.argv[2]);
const el = () => ({ className: 'badge badge-muted', textContent: 'Cargando...', innerHTML: '', title: '', style: {}, focus() {}, value: '' });
(async () => {
  const salida = [];
  for (const esc of escenarios) {
    const els = { 'conn-badge': el(), 'perfil-avatar': el(), 'auth-screen': el(), 'app-shell': el() };
    const pedidos = [];
    const ctx = {
      window: { location: { origin: 'http://t' } },
      document: { addEventListener() {}, getElementById: id => els[id] || null, querySelector: () => null, querySelectorAll: () => [] },
      fetch: async (u) => { pedidos.push(String(u)); return { ok: esc.status < 400, status: esc.status, json: async () => esc.body }; },
      console, Intl, setInterval: () => 1, clearInterval() {}, setTimeout, Promise, URLSearchParams, encodeURIComponent,
    };
    vm.createContext(ctx); vm.runInContext(src, ctx);
    if (esc.accion === 'entrar') {            // lo que ocurre al iniciar sesión o abrir la app ya logueado
      vm.runInContext('mostrarAuthScreen(false)', ctx);
      await new Promise(r => setTimeout(r, 20));
    } else {
      await vm.runInContext('actualizarBadgeConexion()', ctx);
    }
    salida.push({ txt: els['conn-badge'].textContent, cls: els['conn-badge'].className, avatar: els['perfil-avatar'].innerHTML, pedidos });
  }
  console.log(JSON.stringify(salida));
})();
"""


def _correr(escenarios):
    r = subprocess.run(["node", "-e", HARNESS, str(APP_JS), json.dumps(escenarios)],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _sesion(estado, **extra):
    return {"id_sesion": "aaaa-1111", "estado_conexion": estado, "perfil_nombre": None, "perfil_foto_en": None, **extra}


def test_sin_login_no_muestra_un_estado_enganoso():
    [r] = _correr([{"status": 401, "body": {"detail": "No autenticado"}}])
    assert r["txt"] == "Cargando..." and "Sin sesión" not in r["txt"]


def test_con_sesion_conectada_muestra_el_estado_real_al_instante():
    [r] = _correr([{"status": 200, "accion": "entrar", "body": [_sesion("conectado")]}])
    assert r["pedidos"] == ["http://t/sesiones/"]          # consulta apenas entra, sin esperar los 30 s
    assert r["txt"] == "Conectado" and "badge-green" in r["cls"]


def test_prefiere_la_sesion_conectada_sobre_la_primera():
    [r] = _correr([{"status": 200, "body": [_sesion("desconectado"), _sesion("conectado")]}])
    assert r["txt"] == "Conectado"


def test_estados_desconectado_qr_y_sin_sesion():
    a, b, c = _correr([
        {"status": 200, "body": [_sesion("desconectado")]},
        {"status": 200, "body": [_sesion("qr_pendiente")]},
        {"status": 200, "body": []},
    ])
    assert (a["txt"], b["txt"], c["txt"]) == ("Desconectado", "QR Pendiente", "Sin sesión")
    assert "badge-red" in a["cls"] and "badge-orange" in b["cls"]


def test_error_del_servidor_muestra_sin_conexion():
    [r] = _correr([{"status": 500, "body": {"detail": "boom"}}])
    assert r["txt"] == "Sin conexión"


def test_cabecera_usa_la_foto_si_existe_y_si_no_las_iniciales_o_el_icono():
    foto, ini, icono = _correr([
        {"status": 200, "body": [_sesion("conectado", perfil_nombre="Renato Astudillo", perfil_foto_en="2026-10-12T01:00:00+00:00")]},
        {"status": 200, "body": [_sesion("conectado", perfil_nombre="Renato Astudillo")]},
        {"status": 200, "body": [_sesion("conectado")]},
    ])
    assert "/sesiones/aaaa-1111/foto?v=" in foto["avatar"] and "onerror" in foto["avatar"]   # si falla, queda el respaldo
    assert ">RA<" in ini["avatar"].replace("RA<", ">RA<") and "<img" not in ini["avatar"]
    assert "<svg" in icono["avatar"] and "<img" not in icono["avatar"]


def test_el_nombre_no_permite_inyectar_html():
    [r] = _correr([{"status": 200, "body": [_sesion("conectado", perfil_nombre='<script>alert(1)</script> <b>x</b>')]}])
    assert "<script" not in r["avatar"] and "<b>" not in r["avatar"]
