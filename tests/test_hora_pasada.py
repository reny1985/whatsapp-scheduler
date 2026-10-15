"""
Hora ya pasada al programar (backend + aviso + selector del móvil) y texto del aviso siempre sobre ESTE mensaje.
"""
import json
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.services import estimacion
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)
from tests.test_sin_sesion import _api, _m, _sesion

pytestmark = pytest.mark.asyncio(loop_scope="function")

INDIV = "593900000001@s.whatsapp.net"
VIEJA = lambda: datetime.now(timezone.utc) - timedelta(days=30)
ROOT = Path(__file__).resolve().parent.parent


def _ec(h, m, s=0, dia=15):
    return datetime(2026, 10, dia, h, m, s, tzinfo=timezone(timedelta(hours=-5)))


def _ped(hora, n=1, destino=INDIV):
    return [SimpleNamespace(id_mensaje=uuid.uuid4(), fecha_hora_disparo=hora, id_grupo=destino) for _ in range(n)]


# ══ Backend: hora pasada ═════════════════════════════════════════════════════

async def _programar(maker, sid, hace_min, forzar=False):
    cuerpo = {"id_sesion": str(sid), "id_grupo": INDIV, "texto_mensaje": "hola", "forzar": forzar,
              "fecha_hora_disparo": (datetime.now(timezone.utc) - timedelta(minutes=hace_min)).isoformat()}
    return await _api(maker, "POST", "/mensajes/", json=cuerpo)


async def test_hora_pasada_por_mas_de_2_min_se_rechaza_con_mensaje_claro(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    r = await _programar(Session, sid, hace_min=5)
    assert r.status_code == 422
    detalle = r.json()["detail"]
    assert isinstance(detalle, str)                                   # texto simple, que la pantalla puede mostrar tal cual
    assert "ya pasó hace 5 min" in detalle and "Enviar ahora" in detalle
    async with Session() as db:
        assert (await db.execute(text("SELECT count(*) FROM mensajes_programados"))).scalar_one() == 0


async def test_hora_pasada_hace_menos_de_2_min_se_acepta_y_sale_de_inmediato(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    antes = datetime.now(timezone.utc)
    r = await _programar(Session, sid, hace_min=1)
    assert r.status_code == 201
    guardada = (await _m(Session, uuid.UUID(r.json()["id_mensaje"]))).fecha_hora_disparo
    assert guardada >= antes                                           # quedó "ahora", no hace 1 min


async def test_enviar_ahora_acepta_cualquier_hora_pasada(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    antes = datetime.now(timezone.utc)
    r = await _programar(Session, sid, hace_min=45, forzar=True)
    assert r.status_code == 201
    assert (await _m(Session, uuid.UUID(r.json()["id_mensaje"]))).fecha_hora_disparo >= antes


async def test_hora_futura_se_acepta_sin_cambios(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    r = await _programar(Session, sid, hace_min=-30)
    assert r.status_code == 201
    m = await _m(Session, uuid.UUID(r.json()["id_mensaje"]))
    assert m.fecha_hora_disparo > datetime.now(timezone.utc) + timedelta(minutes=29)


# ══ Aviso: siempre habla de ESTE mensaje ═════════════════════════════════════

async def test_caso_real_1_mensaje_a_las_0012_y_el_nuevo_a_las_0009_cuando_ya_son_las_0010():
    ahora = _ec(0, 10)
    r = estimacion.aviso_al_programar(_ped(_ec(0, 12)), _ec(0, 9), INDIV, ahora, rojo=False)
    assert r["n"] == 1 and r["hora_pasada"] is True and r["antes_que_los_otros"] is True
    assert r["mensaje"] == "Tu mensaje saldría aproximadamente a las 00:10 (antes que el mensaje de las 00:12)."


async def test_sale_antes_que_varios_mensajes_del_mismo_minuto():
    r = estimacion.aviso_al_programar(_ped(_ec(0, 12), n=3), _ec(0, 9), INDIV, _ec(0, 8), rojo=False)
    assert r["mensaje"] == "Tu mensaje saldría aproximadamente a las 00:09 (antes que los 3 mensajes de las 00:12)."
    assert r["hora_pasada"] is False


async def test_sale_antes_que_mensajes_de_horas_distintas():
    vecinos = _ped(_ec(0, 12), n=2) + _ped(_ec(0, 14), n=1)
    r = estimacion.aviso_al_programar(vecinos, _ec(0, 9), INDIV, _ec(0, 8), rojo=False)
    assert r["mensaje"] == "Tu mensaje saldría aproximadamente a las 00:09 (antes que los 3 mensajes de entre las 00:12 y las 00:14)."


async def test_sale_despues_conserva_el_texto_de_siempre():
    r = estimacion.aviso_al_programar(_ped(_ec(23, 57), n=4), _ec(23, 59), INDIV, _ec(23, 56, 30), rojo=False)
    assert r["antes_que_los_otros"] is False
    assert r["mensaje"] == "Este número ya tiene 4 mensajes a las 23:57. Tu mensaje saldría aproximadamente a las 23:59."


async def test_en_medio_de_otros_dice_cerca_de_esa_hora():
    vecinos = _ped(_ec(0, 8)) + _ped(_ec(0, 12))
    r = estimacion.aviso_al_programar(vecinos, _ec(0, 10), INDIV, _ec(0, 5), rojo=False)
    assert r["antes_que_los_otros"] is False
    assert r["mensaje"].startswith("Este número ya tiene 2 mensajes cerca de esa hora. Tu mensaje saldría aproximadamente a las ")


async def test_sin_mensajes_cercanos_no_hay_aviso_aunque_la_hora_haya_pasado():
    assert estimacion.aviso_al_programar([], _ec(0, 12), INDIV, _ec(0, 5), False) == {"n": 0}
    assert estimacion.aviso_al_programar(_ped(_ec(5, 0)), _ec(0, 12), INDIV, _ec(0, 5), False) == {"n": 0}   # lejos (> ±10 min)
    assert estimacion.aviso_al_programar([], _ec(0, 3), INDIV, _ec(0, 5), False) == {"n": 0, "hora_pasada": True}


# ══ Frontend (app.js real en Node) ═══════════════════════════════════════════

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const esc = JSON.parse(process.argv[2]);
const AHORA = new Date(esc.ahora).getTime();
class FechaFija extends Date {
  constructor(...a) { if (a.length) super(...a); else super(AHORA); }
  static now() { return AHORA; }
}
const el = (extra = {}) => ({ className: '', textContent: '', innerHTML: '', title: '', style: { display: '' }, value: '', min: '', ...extra });
const campo = el({ type: esc.tipoCampo, value: esc.valor });
const aviso = el({ style: { display: 'none' } });
const ctx = {
  window: { location: { origin: 'http://t' } },
  document: { addEventListener() {}, getElementById: id => ({ 'inp-fecha': campo, 'aviso-hora-pasada': aviso }[id] || null),
              querySelector: () => null, querySelectorAll: () => [] },
  navigator: { userAgent: 'x', maxTouchPoints: 0 },
  Date: FechaFija, fetch: async () => ({}), console, Intl, setInterval: () => 1, clearInterval() {}, setTimeout, Promise,
  URLSearchParams, encodeURIComponent,
};
vm.createContext(ctx); vm.runInContext(src, ctx);
vm.runInContext('_actualizarMinFecha(); _actualizarAvisoHoraPasada();', ctx);
console.log(JSON.stringify({ min: campo.min, avisoVisible: aviso.style.display !== 'none',
                             pasada: vm.runInContext(`_horaYaPaso(${JSON.stringify(esc.valor)})`, ctx) }));
"""


def _js(valor, tipo="datetime-local", ahora="2026-10-15T05:10:30Z"):     # 05:10:30 UTC = 00:10:30 en Ecuador
    if shutil.which("node") is None:
        pytest.skip("Node no está instalado")
    r = subprocess.run(["node", "-e", HARNESS, str(ROOT / "app/static/js/app.js"),
                        json.dumps({"valor": valor, "tipoCampo": tipo, "ahora": ahora})],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


async def test_el_campo_recibe_min_con_la_hora_actual_de_ecuador():
    assert _js("2026-10-15T00:12")["min"] == "2026-10-15T00:10"


async def test_aviso_amarillo_solo_cuando_la_hora_elegida_ya_paso():
    pasada = _js("2026-10-15T00:09")                                    # el caso real: 00:09 cuando ya eran las 00:10
    assert pasada["pasada"] is True and pasada["avisoVisible"] is True
    assert _js("2026-10-15T00:10")["avisoVisible"] is False             # la hora actual no está "pasada"
    assert _js("2026-10-15T00:12")["avisoVisible"] is False
    assert _js("")["avisoVisible"] is False                             # sin valor, sin aviso
    assert _js("2026-10-14T23:59")["avisoVisible"] is True              # día anterior


async def test_con_flatpickr_en_computador_el_campo_es_texto_y_no_se_le_pone_min():
    assert _js("2026-10-15T00:12", tipo="text")["min"] == ""


async def test_el_formulario_no_bloquea_el_envio_y_trae_el_aviso_amarillo():
    html = (ROOT / "app/templates/index.html").read_text(encoding="utf-8")
    assert '<form id="form-programar" novalidate>' in html            # "Enviar ahora" no queda bloqueado por el min nativo
    assert 'id="aviso-hora-pasada"' in html
    assert "Esa hora ya pasó: el mensaje se enviará de inmediato." in html
