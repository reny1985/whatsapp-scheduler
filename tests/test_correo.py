"""
Pruebas del envío del código de verificación por Resend (sin red real).
Cubren: envío correcto, Resend responde error, Resend no responde (timeout),
y que /auth/setup y /auth/reenviar-codigo esperan el resultado y avisan al usuario.
"""
import logging
import uuid
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from app.database import get_db
from app.main import app
from app.models import Usuario
from app.routers import auth

pytestmark = pytest.mark.asyncio(loop_scope="function")

CLAVE = "re_clave_super_secreta_123"


class _FakeClient:
    llamadas: list = []
    modo = "ok"   # ok | error | timeout | red

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.llamadas.append((url, json, headers))
        if _FakeClient.modo == "timeout":
            raise httpx.ReadTimeout("timed out")
        if _FakeClient.modo == "red":
            raise httpx.ConnectError("sin red")
        req = httpx.Request("POST", url)
        if _FakeClient.modo == "error":
            return httpx.Response(403, json={"message": "The enviafast.net domain is not verified"}, request=req)
        return httpx.Response(200, json={"id": "re-id-123"}, request=req)


@pytest.fixture
def resend(monkeypatch):
    _FakeClient.llamadas, _FakeClient.modo = [], "ok"
    monkeypatch.setattr(auth.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(auth, "RESEND_API_KEY", CLAVE)
    monkeypatch.setattr(auth, "RESEND_FROM", "Enviafast <no-reply@enviafast.net>")
    return _FakeClient


# ── _enviar_email ────────────────────────────────────────────────────────────

async def test_envio_correcto_devuelve_id_y_usa_nombre_enviafast(resend):
    id_correo = await auth._enviar_email("a@b.com", "123456")
    assert id_correo == "re-id-123"
    url, payload, headers = resend.llamadas[0]
    assert url == "https://api.resend.com/emails"
    assert payload["from"] == "Enviafast <no-reply@enviafast.net>"
    assert payload["to"] == ["a@b.com"]
    assert "Enviafast" in payload["subject"] and "123456" in payload["subject"]
    assert "Enviafast" in payload["html"] and "WhatsApp Scheduler" not in payload["html"]
    assert "WhatsApp Scheduler" not in payload["subject"]


async def test_resend_responde_error_lanza_y_registra_sin_clave(resend, caplog):
    resend.modo = "error"
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        with pytest.raises(auth.EnvioCorreoError):
            await auth._enviar_email("a@b.com", "123456")
    assert "403" in caplog.text and "not verified" in caplog.text
    assert CLAVE not in caplog.text


async def test_resend_no_responde_timeout_lanza(resend, caplog):
    resend.modo = "timeout"
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        with pytest.raises(auth.EnvioCorreoError, match="timeout"):
            await auth._enviar_email("a@b.com", "123456")
    assert "no respondió" in caplog.text and CLAVE not in caplog.text


async def test_error_de_red_lanza(resend):
    resend.modo = "red"
    with pytest.raises(auth.EnvioCorreoError):
        await auth._enviar_email("a@b.com", "123456")


async def test_sin_clave_configurada_lanza(monkeypatch):
    monkeypatch.setattr(auth, "RESEND_API_KEY", "")
    with pytest.raises(auth.EnvioCorreoError):
        await auth._enviar_email("a@b.com", "123456")


# ── Endpoints ────────────────────────────────────────────────────────────────

class _FakeDB:
    def __init__(self, user):
        self.user, self.commit, self.rollback, self.added = user, AsyncMock(), AsyncMock(), []

    async def get(self, model, pk):
        return self.user

    async def execute(self, stmt, *a, **k):
        r = MagicMock()
        r.scalar_one_or_none.return_value = self.user if getattr(self, "buscar_por_email", False) else None
        return r

    def add(self, obj):
        self.added.append(obj)


def _piloto():
    return Usuario(id_usuario=auth.PILOTO_ID_USUARIO, email="admin@localhost",
                   password_hash="piloto-no-auth", rol="admin", estado_suscripcion="prueba",
                   email_verificado=False)


@pytest.fixture
def cliente():
    async def _make(db):
        app.dependency_overrides[get_db] = lambda: db
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")
    yield _make
    app.dependency_overrides.pop(get_db, None)


async def test_setup_ok_envia_antes_de_guardar(cliente, resend):
    db = _FakeDB(_piloto())
    async with await cliente(db) as c:
        r = await c.post("/auth/setup", json={"email": "nuevo@x.com", "password": "12345678"})
    assert r.status_code == 201 and r.json()["verificacion_requerida"] is True
    assert len(resend.llamadas) == 1
    db.commit.assert_awaited_once()


async def test_setup_resend_falla_devuelve_error_claro_y_revierte(cliente, resend):
    resend.modo = "error"
    db = _FakeDB(_piloto())
    async with await cliente(db) as c:
        r = await c.post("/auth/setup", json={"email": "nuevo@x.com", "password": "12345678"})
    assert r.status_code == 502
    assert r.json()["detail"] == "No pudimos enviar el correo, intenta de nuevo en unos minutos"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    assert CLAVE not in r.text


async def test_setup_timeout_devuelve_error_claro(cliente, resend):
    resend.modo = "timeout"
    db = _FakeDB(_piloto())
    async with await cliente(db) as c:
        r = await c.post("/auth/setup", json={"email": "nuevo@x.com", "password": "12345678"})
    assert r.status_code == 502 and "No pudimos enviar el correo" in r.json()["detail"]
    db.commit.assert_not_awaited()


async def test_reenviar_ok(cliente, resend):
    db = _FakeDB(_piloto()); db.buscar_por_email = True
    async with await cliente(db) as c:
        r = await c.post("/auth/reenviar-codigo", json={"email": "admin@localhost"})
    assert r.status_code == 200 and r.json() == {"ok": True}
    db.commit.assert_awaited_once()


async def test_reenviar_resend_falla_devuelve_error_y_revierte(cliente, resend):
    resend.modo = "error"
    db = _FakeDB(_piloto()); db.buscar_por_email = True
    async with await cliente(db) as c:
        r = await c.post("/auth/reenviar-codigo", json={"email": "admin@localhost"})
    assert r.status_code == 502
    assert r.json()["detail"] == "No pudimos enviar el correo, intenta de nuevo en unos minutos"
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
