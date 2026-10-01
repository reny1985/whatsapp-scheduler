"""
Pruebas del flujo de pairing code.
Simula Evolution API para no crear instancias reales.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import get_db
from app.auth import get_current_user, require_admin

pytestmark = pytest.mark.asyncio(loop_scope="function")

NUMERO_EC = '593969829845'


def _admin():
    u = MagicMock()
    u.id_usuario = uuid.UUID('00000000-0000-0000-0000-000000000001')
    u.rol = 'admin'
    return u


class MockDB:
    async def get(self, model, pk): return None

    async def execute(self, stmt, *args, **kwargs):
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        r.scalar_one_or_none.return_value = None
        return r

    def add(self, obj): pass
    async def commit(self): pass
    async def refresh(self, obj): pass
    async def delete(self, obj): pass
    async def flush(self): pass


async def test_iniciar_pairing_numero_invalido():
    """Número con menos de 8 dígitos → 400."""
    app.dependency_overrides[get_db] = lambda: MockDB()
    app.dependency_overrides[require_admin] = lambda: _admin()

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/sesiones/iniciar-pairing', json={'numero': '123'})

    app.dependency_overrides.clear()
    assert resp.status_code == 400


async def test_iniciar_pairing_simula_evolution_ok():
    """Simula Evolution API devolviendo pairingCode → 200 con código formateado."""
    app.dependency_overrides[get_db] = lambda: MockDB()
    app.dependency_overrides[require_admin] = lambda: _admin()

    async def mock_post(path, body, token=None):
        if '/instance/create' in path:
            return {'hash': {'apikey': 'tok-abc'}, 'instance': {'instanceName': body.get('instanceName')}}
        return {}

    async def mock_get(path, token=None, timeout=15):
        if '/instance/connect/' in path:
            return {'pairingCode': 'ABCD1234'}
        return {}

    async def noop(*a, **kw): pass

    with patch('app.routers.sesiones._evo_post', side_effect=mock_post), \
         patch('app.routers.sesiones._evo_get', side_effect=mock_get), \
         patch('app.routers.sesiones._configurar_instancia_bg', side_effect=noop), \
         patch('app.routers.sesiones.asyncio.sleep', side_effect=noop):

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
            resp = await c.post('/sesiones/iniciar-pairing', json={'numero': NUMERO_EC})

    app.dependency_overrides.clear()
    assert resp.status_code == 200
    data = resp.json()
    assert 'pairing_code' in data
    assert 'id_sesion' in data
    code = data['pairing_code']
    assert len(code.replace('-', '')) == 8
    assert '-' in code  # formato XXXX-XXXX


async def test_iniciar_pairing_evolution_sin_codigo():
    """Evolution no devuelve pairingCode → 503."""
    app.dependency_overrides[get_db] = lambda: MockDB()
    app.dependency_overrides[require_admin] = lambda: _admin()

    async def mock_post(path, body, token=None):
        if '/instance/create' in path:
            return {'hash': {'apikey': 'tok-abc'}}
        return {}

    async def mock_get(path, token=None, timeout=15):
        return {'pairingCode': None}  # nunca llega

    async def noop(*a, **kw): pass

    with patch('app.routers.sesiones._evo_post', side_effect=mock_post), \
         patch('app.routers.sesiones._evo_get', side_effect=mock_get), \
         patch('app.routers.sesiones._configurar_instancia_bg', side_effect=noop), \
         patch('app.routers.sesiones.asyncio.sleep', side_effect=noop):

        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
            resp = await c.post('/sesiones/iniciar-pairing', json={'numero': NUMERO_EC})

    app.dependency_overrides.clear()
    assert resp.status_code == 503
