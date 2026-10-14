"""
Pantalla Sesión: botón "Desvincular", su diálogo y la nota visible sobre desvincular. Ejecuta el app.js REAL en Node
(con un DOM, un apiFetch y un confirm simulados). Se omite si Node no está instalado.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node no está instalado")

NOTA = ("Puedes salir de Enviafast desde cualquier dispositivo y tus mensajes se enviarán igual. "
        "Pero si desvinculas tu WhatsApp (con 'Desvincular' aquí, o desde WhatsApp → Dispositivos vinculados en tu teléfono), "
        "tus mensajes programados quedarán en pausa hasta que lo vuelvas a vincular.")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const esc = JSON.parse(process.argv[2]);
const el = () => ({ className: '', textContent: '', innerHTML: '', title: '', style: {}, focus() {}, value: '' });
(async () => {
  const els = { 'sesion-content': el() };
  const llamadas = [], dialogos = [], alertas = [];
  const ctx = {
    window: { location: { origin: 'http://t' } },
    navigator: { userAgent: 'Mozilla/5.0 (Macintosh)', maxTouchPoints: 0 },
    document: { addEventListener() {}, getElementById: id => els[id] || null, querySelector: () => null, querySelectorAll: () => [] },
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}) }),
    confirm: (m) => { dialogos.push(m); return esc.acepta; },
    alert: (m) => alertas.push(m),
    console, Intl, setInterval: () => 1, clearInterval() {}, setTimeout, Promise, URLSearchParams, encodeURIComponent,
  };
  vm.createContext(ctx); vm.runInContext(src, ctx);
  ctx.__sesiones = esc.sesiones; ctx.__pendientes = esc.pendientes; ctx.__llamadas = llamadas;
  vm.runInContext(`
    todasLasSesiones = __sesiones;
    apiFetch = async (path, opts = {}) => { __llamadas.push([opts.method || 'GET', path]); return path.endsWith('/pendientes') ? { pendientes: __pendientes } : {}; };
    cargarSesionActiva = async () => {};
  `, ctx);
  vm.runInContext('renderPanelSesion()', ctx);
  const html = els['sesion-content'].innerHTML;
  if (esc.id_desvincular) await vm.runInContext(`desvincularSesion('${esc.id_desvincular}')`, ctx);
  console.log(JSON.stringify({ html, dialogos, llamadas, alertas }));
})();
"""


def _correr(**kw):
    kw.setdefault("sesiones", [])
    kw.setdefault("pendientes", 0)
    kw.setdefault("acepta", True)
    r = subprocess.run(["node", "-e", HARNESS, str(APP_JS), json.dumps(kw)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _sesion(**extra):
    return {"id_sesion": "s-1", "estado_conexion": "conectado", "numero_telefono": "593961409996",
            "perfil_nombre": "Renato Astudillo Mosquera de la Torre", "perfil_foto_en": None,
            "instancia_evolution": "wa-x", "nombre_sesion": None, **extra}


def test_el_boton_rojo_dice_desvincular_y_ya_no_eliminar():
    html = _correr(sesiones=[_sesion()])["html"]
    assert "Desvincular</button>" in html and "desvincularSesion('s-1')" in html
    assert "Eliminar" not in html and "eliminarSesion" not in html


def test_la_nota_aparece_debajo_de_las_tarjetas_de_los_numeros_con_el_texto_exacto():
    html = _correr(sesiones=[_sesion(), _sesion(id_sesion="s-2")])["html"]
    assert NOTA.replace("'", "'") in html
    assert html.index('class="nota-sesion"') > html.rindex('id="card-sesion-s-2"')       # después de TODAS las tarjetas
    assert html.index('class="nota-sesion"') < html.index("Añadir cuenta de WhatsApp")   # y antes de "Añadir cuenta"


def test_sin_numeros_vinculados_no_se_muestra_la_nota():
    assert "nota-sesion" not in _correr(sesiones=[])["html"]


def test_tarjeta_con_identidad_arriba_y_botones_en_una_fila_aparte():
    html = _correr(sesiones=[_sesion()])["html"]
    assert html.index('class="sesion-perfil"') < html.index('class="sesion-acciones"')
    identidad, acciones = html.split('class="sesion-acciones"')
    assert "Renato Astudillo Mosquera de la Torre" in identidad and "+593961409996" in identidad
    assert "Verificar" not in identidad and "Desvincular" not in identidad               # los botones no comparten bloque
    assert "Verificar" in acciones and "Grupos" in acciones and "Desvincular" in acciones


@pytest.mark.parametrize("pendientes,texto", [
    (3, "Este número tiene 3 mensajes programados; quedarán en pausa y no se enviarán."),
    (1, "Este número tiene 1 mensaje programado; quedará en pausa y no se enviará."),
])
def test_el_dialogo_conserva_el_aviso_de_pendientes_y_agrega_la_advertencia(pendientes, texto):
    r = _correr(sesiones=[_sesion()], id_desvincular="s-1", pendientes=pendientes)
    (dialogo,) = r["dialogos"]
    assert dialogo.splitlines()[0] == "Desvincular este número"
    assert texto in dialogo
    assert "Tus mensajes programados quedarán en pausa hasta que vuelvas a vincular este número." in dialogo
    assert ["DELETE", "/sesiones/s-1"] in r["llamadas"]                                  # al aceptar, se desvincula


def test_el_dialogo_sin_pendientes_solo_trae_la_advertencia():
    (dialogo,) = _correr(sesiones=[_sesion()], id_desvincular="s-1", pendientes=0)["dialogos"]
    assert dialogo.startswith("Desvincular este número") and "Este número tiene" not in dialogo
    assert "quedarán en pausa hasta que vuelvas a vincular este número" in dialogo


def test_si_se_cancela_el_dialogo_no_se_desvincula():
    r = _correr(sesiones=[_sesion()], id_desvincular="s-1", pendientes=2, acepta=False)
    assert len(r["dialogos"]) == 1 and not any(m == "DELETE" for m, _ in r["llamadas"])
