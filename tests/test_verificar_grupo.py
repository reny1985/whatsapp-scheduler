"""
Pruebas de GET /sesiones/{id}/verificar-grupo y de la sincronización forzada de grupos.
Simulan Evolution API (sin red real).
"""
import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.routers import sesiones

pytestmark = pytest.mark.asyncio(loop_scope="function")

SID = uuid.UUID("00000000-0000-0000-0000-000000000030")
MI = "593961409996"
JID = "120363406271783192@g.us"
TOKEN_INSTANCIA = "token-secreto-instancia"


def _info(announce=False, yo_admin=None, incluir_yo=True, size=3):
    ps = [{"id": "111@lid", "jid": "593900000001@s.whatsapp.net", "admin": "superadmin"},
          {"id": "222@lid", "jid": "593900000002@s.whatsapp.net", "admin": None}]
    if incluir_yo:
        ps.append({"id": "333@lid", "jid": f"{MI}@s.whatsapp.net", "admin": yo_admin})
    return {"id": JID, "subject": "Pruebas Enviafast", "size": size, "announce": announce, "participants": ps}


# ── evaluar_grupo (lógica pura) ──────────────────────────────────────────────

async def test_grupo_normal_ok():
    r = sesiones.evaluar_grupo(_info(), MI)
    assert r["estado"] == "ok" and r["nombre"] == "Pruebas Enviafast" and r["participantes"] == 3


async def test_solo_admins_y_no_soy_admin_avisa():
    r = sesiones.evaluar_grupo(_info(announce=True, yo_admin=None), MI)
    assert r["estado"] == "solo_admins"
    assert "Solo los administradores pueden enviar mensajes" in r["mensaje"] and "fallará" in r["mensaje"]


@pytest.mark.parametrize("rol", ["admin", "superadmin"])
async def test_solo_admins_pero_soy_admin_ok(rol):
    assert sesiones.evaluar_grupo(_info(announce=True, yo_admin=rol), MI)["estado"] == "ok"


async def test_sesion_ya_no_es_miembro():
    r = sesiones.evaluar_grupo(_info(incluir_yo=False), MI)
    assert r["estado"] == "no_miembro" and "ya no pertenece" in r["mensaje"]


async def test_sin_numero_de_sesion_no_inventa_no_miembro():
    assert sesiones.evaluar_grupo(_info(), None)["estado"] == "ok"


# ── Endpoint ─────────────────────────────────────────────────────────────────

class _DB:
    def __init__(self, estado="conectado", grupos=()):
        self.estado, self.grupos = estado, list(grupos)

    async def get(self, model, pk):
        if pk != SID:
            return None
        s = MagicMock()
        s.id_sesion, s.instancia_evolution, s.estado_conexion = SID, "wa-test", self.estado
        s.token_autorizacion, s.numero_telefono = TOKEN_INSTANCIA, MI
        return s

    async def execute(self, stmt, *a, **k):
        r = MagicMock()
        r.fetchall.return_value = [[g["id"], g["subject"], g["size"]] for g in self.grupos]
        return r


class _FakeClient:
    respuesta = (200, {})
    excepcion = None
    llamadas: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get(self, url, params=None, headers=None):
        _FakeClient.llamadas.append((url, params))
        if _FakeClient.excepcion:
            raise _FakeClient.excepcion
        st, body = _FakeClient.respuesta
        return httpx.Response(st, json=body, request=httpx.Request("GET", url))


@pytest.fixture
def api(monkeypatch):
    _FakeClient.respuesta, _FakeClient.excepcion, _FakeClient.llamadas = (200, {}), None, []
    monkeypatch.setattr(sesiones.httpx, "AsyncClient", _FakeClient)
    app.dependency_overrides[get_current_user] = lambda: MagicMock()

    async def _llamar(db, path):
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(path)

    yield _llamar
    app.dependency_overrides.clear()


async def test_endpoint_grupo_ok_usa_findGroupInfos(api):
    _FakeClient.respuesta = (200, _info())
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 200 and r.json()["estado"] == "ok"
    assert r.json()["nombre"] == "Pruebas Enviafast" and r.json()["participantes"] == 3
    url, params = _FakeClient.llamadas[0]
    assert url.endswith("/group/findGroupInfos/wa-test") and params == {"groupJid": JID}
    assert TOKEN_INSTANCIA not in r.text


async def test_endpoint_solo_admins(api):
    _FakeClient.respuesta = (200, _info(announce=True))
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.json()["estado"] == "solo_admins"


async def test_endpoint_grupo_inexistente(api):
    _FakeClient.respuesta = (404, {"status": 404, "response": {"message": ["Error: item-not-found"]}})
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 200 and r.json()["estado"] == "no_existe"
    assert "ya no existe o tu sesión ya no pertenece" in r.json()["mensaje"]


async def test_endpoint_jid_no_es_de_grupo(api):
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid=593961409996@s.whatsapp.net")
    assert r.status_code == 400 and not _FakeClient.llamadas


async def test_endpoint_sesion_desconectada(api):
    r = await api(_DB(estado="desconectado"), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 409 and not _FakeClient.llamadas


async def test_endpoint_sesion_inexistente(api):
    r = await api(_DB(), f"/sesiones/{uuid.uuid4()}/verificar-grupo?jid={JID}")
    assert r.status_code == 404


async def test_endpoint_evolution_no_responde(api):
    _FakeClient.excepcion = httpx.ReadTimeout("timeout")
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 502 and "No se pudo consultar WhatsApp" in r.json()["detail"]


# ── Sincronización forzada de grupos (botón Recargar) ────────────────────────

GRUPOS = [{"id": JID, "subject": "Dolaritos", "size": 50}]


async def test_forzar_sincroniza_aunque_haya_cache(api):
    llamadas = []

    async def refrescar(*a): llamadas.append(a[1]); return True

    with patch("app.routers.sesiones._refrescar_grupos_bg", side_effect=refrescar):
        r = await api(_DB(grupos=GRUPOS), f"/sesiones/{SID}/grupos?forzar=true")
    assert r.status_code == 200 and r.json()[0]["subject"] == "Dolaritos"
    assert llamadas == ["wa-test"]


async def test_sin_forzar_con_cache_no_espera_sincronizacion(api):
    with patch("app.routers.sesiones._refrescar_grupos_bg") as refrescar, \
         patch("app.routers.sesiones.asyncio.create_task"):
        r = await api(_DB(grupos=GRUPOS), f"/sesiones/{SID}/grupos")
    assert r.status_code == 200
    refrescar.assert_not_awaited()


async def test_forzar_falla_devuelve_mensaje_claro(api):
    async def refrescar(*a): return False

    with patch("app.routers.sesiones._refrescar_grupos_bg", side_effect=refrescar):
        r = await api(_DB(grupos=GRUPOS), f"/sesiones/{SID}/grupos?forzar=true")
    assert r.status_code == 502 and "No se pudieron sincronizar los grupos" in r.json()["detail"]


async def test_cache_vacia_y_sincronizacion_falla_avisa_en_vez_de_lista_vacia(api):
    async def refrescar(*a): return False

    with patch("app.routers.sesiones._refrescar_grupos_bg", side_effect=refrescar):
        r = await api(_DB(grupos=[]), f"/sesiones/{SID}/grupos")
    assert r.status_code == 502
