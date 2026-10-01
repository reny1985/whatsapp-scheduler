"""
Pruebas del endpoint de grupos.
Simula Evolution API para no hacer llamadas reales.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import get_db
from app.auth import get_current_user, require_admin

pytestmark = pytest.mark.asyncio(loop_scope="function")

SESION_ID = uuid.UUID('00000000-0000-0000-0000-000000000020')
USUARIO_ID = uuid.UUID('00000000-0000-0000-0000-000000000001')

GRUPOS_MOCK = [
    {'id': '120363406271783192@g.us', 'subject': 'Dolaritos', 'size': 50},
    {'id': '120363000000000001@g.us', 'subject': 'Amigos',    'size': 10},
]


def _sesion():
    s = MagicMock()
    s.id_sesion = SESION_ID
    s.instancia_evolution = 'wa-test'
    s.estado_conexion = 'conectado'
    s.token_autorizacion = 'test-token'
    return s


def _usuario():
    u = MagicMock()
    u.id_usuario = USUARIO_ID
    return u


class FakeRow:
    def __init__(self, g):
        self._g = g
    def __getitem__(self, i):
        return [self._g['id'], self._g['subject'], self._g['size']][i]


class MockDB:
    def __init__(self, grupos=None):
        self._grupos = grupos or []
        self._call = 0

    async def get(self, model, pk):
        return _sesion() if pk == SESION_ID else None

    async def execute(self, stmt, *args, **kwargs):
        self._call += 1
        r = MagicMock()
        r.fetchall.return_value = [FakeRow(g) for g in self._grupos]
        r.scalars.return_value.all.return_value = []
        return r

    def add(self, obj): pass
    async def commit(self): pass
    async def refresh(self, obj): pass
    async def flush(self): pass


async def test_grupos_desde_cache():
    """Sesión con caché llena devuelve grupos inmediatamente."""
    app.dependency_overrides[get_db] = lambda: MockDB(grupos=GRUPOS_MOCK)
    app.dependency_overrides[get_current_user] = lambda: _usuario()

    with patch('app.routers.sesiones.asyncio.create_task'):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
            resp = await c.get(f'/sesiones/{SESION_ID}/grupos')

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[0]['id'] == '120363406271783192@g.us'
    assert data[0]['subject'] == 'Dolaritos'


async def test_grupos_sesion_no_encontrada():
    """Sesión inexistente → 404."""
    class EmptyDB:
        async def get(self, model, pk): return None

    app.dependency_overrides[get_db] = lambda: EmptyDB()
    app.dependency_overrides[get_current_user] = lambda: _usuario()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.get(f'/sesiones/{uuid.uuid4()}/grupos')

    app.dependency_overrides.clear()
    assert resp.status_code == 404


async def test_grupos_cache_vacia_llama_fetch_sincronico():
    """Caché vacía: el endpoint llama _refrescar_grupos_bg de forma sincrónica."""
    llamadas_refrescar = []

    async def mock_refrescar(id_sesion, instancia, token):
        llamadas_refrescar.append(instancia)

    app.dependency_overrides[get_db] = lambda: MockDB(grupos=[])
    app.dependency_overrides[get_current_user] = lambda: _usuario()

    with patch('app.routers.sesiones._refrescar_grupos_bg', side_effect=mock_refrescar):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
            resp = await c.get(f'/sesiones/{SESION_ID}/grupos')

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    # Verificar que se llamó el refresco sincrónico (no background task)
    assert 'wa-test' in llamadas_refrescar
