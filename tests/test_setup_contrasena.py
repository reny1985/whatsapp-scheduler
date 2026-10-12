"""
Pruebas de la confirmación de contraseña en POST /auth/setup.
Contraseñas distintas → 422 con mensaje claro (sin tocar la BD ni enviar correo);
iguales → sigue el flujo normal. Las contraseñas nunca aparecen en el log.
"""
import logging
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient, ASGITransport

from app.database import get_db
from app.main import app
from app.routers import auth
from tests.test_correo import _FakeDB, _piloto

pytestmark = pytest.mark.asyncio(loop_scope="function")

CLAVE_A = "ClaveSecreta-ABC-123"
CLAVE_B = "OtraClave-XYZ-999"


@pytest.fixture
def api(monkeypatch):
    enviar = AsyncMock(return_value="id-correo")
    monkeypatch.setattr(auth, "_enviar_email", enviar)
    db = _FakeDB(_piloto())
    app.dependency_overrides[get_db] = lambda: db

    async def _post(body):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.post("/auth/setup", json=body)

    yield _post, enviar, db
    app.dependency_overrides.pop(get_db, None)


async def test_contrasenas_distintas_devuelve_422_con_mensaje_claro(api):
    post, enviar, db = api
    r = await post({"email": "a@x.com", "password": CLAVE_A, "confirmar_password": CLAVE_B})
    assert r.status_code == 422
    assert r.json()["detail"] == "Las contraseñas no coinciden"
    enviar.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert db.added == [] and db.user.password_hash == "piloto-no-auth"


async def test_diferencia_de_mayusculas_tambien_cuenta(api):
    post, enviar, db = api
    r = await post({"email": "a@x.com", "password": "Abcdefgh1", "confirmar_password": "abcdefgh1"})
    assert r.status_code == 422 and r.json()["detail"] == "Las contraseñas no coinciden"


async def test_contrasenas_iguales_sigue_el_flujo_normal(api):
    post, enviar, db = api
    r = await post({"email": "a@x.com", "password": CLAVE_A, "confirmar_password": CLAVE_A})
    assert r.status_code == 201
    assert r.json() == {"ok": True, "email": "a@x.com", "verificacion_requerida": True}
    enviar.assert_awaited_once()
    db.commit.assert_awaited_once()
    assert db.user.password_hash != CLAVE_A          # se guarda el hash, no la contraseña
    assert db.user.email == "a@x.com" and db.user.email_verificado is False


async def test_falta_la_confirmacion_es_422(api):
    post, enviar, db = api
    r = await post({"email": "a@x.com", "password": CLAVE_A})
    assert r.status_code == 422
    enviar.assert_not_awaited()


async def test_minimo_8_caracteres_se_mantiene(api):
    post, enviar, db = api
    r = await post({"email": "a@x.com", "password": "corta", "confirmar_password": "corta"})
    assert r.status_code == 422
    enviar.assert_not_awaited()
    db.commit.assert_not_awaited()


async def test_las_contrasenas_no_aparecen_en_log_ni_en_la_respuesta(api, caplog):
    post, enviar, db = api
    with caplog.at_level(logging.DEBUG):
        r1 = await post({"email": "a@x.com", "password": CLAVE_A, "confirmar_password": CLAVE_B})
        r2 = await post({"email": "a@x.com", "password": CLAVE_A, "confirmar_password": CLAVE_A})
    for texto in (caplog.text, r1.text, r2.text):
        assert CLAVE_A not in texto and CLAVE_B not in texto
