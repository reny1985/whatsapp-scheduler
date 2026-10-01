"""
Pruebas de la API de mensajes programados.
Simula respuestas de Evolution API para no enviar mensajes reales.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.database import get_db
from app.auth import get_current_user, require_admin

pytestmark = pytest.mark.asyncio(loop_scope="function")

SESION_ID = uuid.UUID('00000000-0000-0000-0000-000000000010')
USUARIO_ID = uuid.UUID('00000000-0000-0000-0000-000000000001')
FUTURO = datetime.now(timezone.utc) + timedelta(hours=2)


def _usuario_mock():
    u = MagicMock()
    u.id_usuario = USUARIO_ID
    u.email = 'test@test.com'
    u.rol = 'admin'
    u.estado_suscripcion = 'activa'
    return u


def _sesion_mock():
    s = MagicMock()
    s.id_sesion = SESION_ID
    s.instancia_evolution = 'wa-test'
    s.estado_conexion = 'conectado'
    s.token_autorizacion = 'test-token'
    s.id_usuario = USUARIO_ID
    return s


class MockDB:
    async def get(self, model, pk):
        return _sesion_mock()

    async def execute(self, stmt, *args, **kwargs):
        r = MagicMock()
        r.scalar_one_or_none.return_value = _sesion_mock()
        r.scalars.return_value.all.return_value = []
        return r

    def add(self, obj): pass
    async def commit(self): pass

    async def refresh(self, obj):
        from app.models import MensajeProgramado
        from datetime import datetime, timezone
        if isinstance(obj, MensajeProgramado):
            if not obj.id_mensaje:
                obj.id_mensaje = uuid.uuid4()
            if not obj.estado_envio:
                obj.estado_envio = 'programado'
            if not obj.recurrencia:
                obj.recurrencia = 'none'
            if not obj.fecha_creacion:
                obj.fecha_creacion = datetime.now(timezone.utc)

    async def delete(self, obj): pass
    async def flush(self): pass


@pytest.fixture(autouse=True)
def setup_overrides():
    db = MockDB()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _usuario_mock()
    app.dependency_overrides[require_admin] = lambda: _usuario_mock()
    yield
    app.dependency_overrides.clear()


async def test_programar_mensaje_grupo():
    """Programar mensaje a grupo (@g.us)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Hola grupo',
            'fecha_hora_disparo': FUTURO.isoformat(),
            'recurrencia': 'none',
        })
    assert resp.status_code == 201


async def test_programar_mensaje_personal():
    """Programar mensaje a número personal (@s.whatsapp.net)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '593969829845@s.whatsapp.net',
            'texto_mensaje': 'Hola personal',
            'fecha_hora_disparo': FUTURO.isoformat(),
            'recurrencia': 'none',
        })
    assert resp.status_code == 201


async def test_programar_recurrencia_diaria():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Diario',
            'fecha_hora_disparo': FUTURO.isoformat(),
            'recurrencia': 'daily',
        })
    assert resp.status_code == 201


async def test_programar_recurrencia_semanal():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Semanal',
            'fecha_hora_disparo': FUTURO.isoformat(),
            'recurrencia': 'weekly',
        })
    assert resp.status_code == 201


async def test_programar_recurrencia_mensual():
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Mensual',
            'fecha_hora_disparo': FUTURO.isoformat(),
            'recurrencia': 'monthly',
        })
    assert resp.status_code == 201


async def test_mensaje_pasado_sin_forzar():
    """Fecha en el pasado sin forzar → 422."""
    pasado = datetime.now(timezone.utc) - timedelta(hours=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Pasado',
            'fecha_hora_disparo': pasado.isoformat(),
            'recurrencia': 'none',
            'forzar': False,
        })
    assert resp.status_code == 422


async def test_mensaje_pasado_con_forzar():
    """Fecha en el pasado con forzar=True → aceptado."""
    pasado = datetime.now(timezone.utc) - timedelta(hours=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as c:
        resp = await c.post('/mensajes/', json={
            'id_sesion': str(SESION_ID),
            'id_grupo': '120363406271783192@g.us',
            'texto_mensaje': 'Forzar ahora',
            'fecha_hora_disparo': pasado.isoformat(),
            'recurrencia': 'none',
            'forzar': True,
        })
    assert resp.status_code == 201
