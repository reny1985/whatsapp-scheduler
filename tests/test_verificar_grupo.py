"""
Pruebas de GET /sesiones/{id}/verificar-grupo, de la sincronización forzada de grupos y del
candado por sesión. Simulan Evolution API (sin red real).
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
CUENTA = uuid.UUID("00000000-0000-0000-0000-000000000001")
JID = "120363406271783192@g.us"
TOKEN_INSTANCIA = "token-secreto-instancia"


def _info(size=3):
    return {"id": JID, "subject": "Pruebas Enviafast", "size": size,
            "participants": [{"id": "111@lid", "jid": "593900000001@s.whatsapp.net", "admin": None}]}


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
        s.id_cuenta = CUENTA
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
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_cuenta=CUENTA)

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


async def test_endpoint_no_evalua_administradores(api):
    """Aunque el grupo sea solo-admins y el número no sea admin, solo informa nombre y participantes."""
    info = _info(size=50); info["announce"] = True
    _FakeClient.respuesta = (200, info)
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 200 and r.json()["estado"] == "ok"
    assert r.json()["participantes"] == 50 and "admin" not in r.json()["mensaje"].lower()


async def test_endpoint_participantes_sin_size_usa_la_lista(api):
    info = _info(); del info["size"]
    _FakeClient.respuesta = (200, info)
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.json()["participantes"] == 1


async def test_endpoint_grupo_inexistente(api):
    _FakeClient.respuesta = (404, {"status": 404, "response": {"message": ["Error: item-not-found"]}})
    r = await api(_DB(), f"/sesiones/{SID}/verificar-grupo?jid={JID}")
    assert r.status_code == 200 and r.json()["estado"] == "no_existe"
    assert r.json()["mensaje"] == "El grupo ya no existe o tu número ya no pertenece a él"


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


async def test_endpoint_evolution_error_500(api):
    _FakeClient.respuesta = (500, {"error": "boom"})
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


# ── Candado por sesión ───────────────────────────────────────────────────────

import asyncio


@pytest.fixture
def sync_lento(monkeypatch):
    """Sustituye la sincronización real por una lenta que cuenta cuántas veces corre."""
    sesiones._sync_locks.clear(); sesiones._sync_ultimo_ok.clear()
    estado = {"corridas": 0, "activas": 0, "max_activas": 0, "resultado": True}

    async def falsa(id_sesion, instancia, token):
        estado["corridas"] += 1
        estado["activas"] += 1
        estado["max_activas"] = max(estado["max_activas"], estado["activas"])
        await asyncio.sleep(0.15)
        estado["activas"] -= 1
        return estado["resultado"]

    monkeypatch.setattr(sesiones, "_traer_y_guardar_grupos", falsa)
    return estado


async def test_dos_sincronizaciones_de_la_misma_sesion_no_corren_a_la_vez(sync_lento):
    r = await asyncio.gather(*[sesiones._refrescar_grupos_bg(SID, "wa-test", "t") for _ in range(3)])
    assert r == [True, True, True]
    assert sync_lento["corridas"] == 1 and sync_lento["max_activas"] == 1


async def test_sesiones_distintas_si_pueden_sincronizar_en_paralelo(sync_lento):
    otra = uuid.UUID("00000000-0000-0000-0000-000000000031")
    await asyncio.gather(sesiones._refrescar_grupos_bg(SID, "wa-a", "t"),
                         sesiones._refrescar_grupos_bg(otra, "wa-b", "t"))
    assert sync_lento["corridas"] == 2 and sync_lento["max_activas"] == 2


async def test_quien_espera_recibe_el_fallo_de_la_sincronizacion_en_curso(sync_lento):
    sync_lento["resultado"] = False
    r = await asyncio.gather(sesiones._refrescar_grupos_bg(SID, "wa-test", "t"),
                             sesiones._refrescar_grupos_bg(SID, "wa-test", "t"))
    assert r == [False, False] and sync_lento["corridas"] == 1


async def test_candado_se_libera_y_permite_una_nueva_sincronizacion(sync_lento):
    await sesiones._refrescar_grupos_bg(SID, "wa-test", "t")
    await sesiones._refrescar_grupos_bg(SID, "wa-test", "t")
    assert sync_lento["corridas"] == 2


async def test_candado_se_libera_si_la_sincronizacion_lanza_excepcion(monkeypatch):
    sesiones._sync_locks.clear(); sesiones._sync_ultimo_ok.clear()

    async def rota(*a): raise RuntimeError("boom")

    monkeypatch.setattr(sesiones, "_traer_y_guardar_grupos", rota)
    with pytest.raises(RuntimeError):
        await sesiones._refrescar_grupos_bg(SID, "wa-test", "t")
    assert not sesiones._sync_locks[SID].locked()
