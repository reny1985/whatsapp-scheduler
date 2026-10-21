"""
v1.1.0.1 — /auth/verificar-codigo nunca inicia sesión sin un código válido (Postgres real y desechable; se omiten si
no hay Postgres local).
· La falla: con un correo YA verificado respondía 200 con cookie de sesión sin mirar el código. Ahora 400 genérico.
· El código sale de secrets (no de random) y la verificación tiene límite de intentos por correo y por IP; al llegar
  al máximo de fallos el código se anula.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.auth import hash_password
from app.database import get_db
from app.main import app
from app.routers import auth
from app.services import intentos
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

VERIFICADO = "duena@x.com"            # el usuario piloto (…0001), ya verificado
PENDIENTE = "nuevo@x.com"
U_PEND = uuid.UUID("00000000-0000-0000-0000-0000000000c1")
CODIGO = "482913"
MSG = "Código incorrecto o expirado"


@pytest_asyncio.fixture
async def mundo(Session):
    maker = Session
    expira = datetime.now(timezone.utc) + timedelta(minutes=30)
    async with maker() as db:
        await db.execute(text("UPDATE usuarios SET email=:e, password_hash=:h, email_verificado=TRUE, "
                              "codigo_verificacion=NULL WHERE id_usuario='00000000-0000-0000-0000-000000000001'"),
                         {"e": VERIFICADO, "h": hash_password("clave-correcta-1")})
        await db.execute(text("INSERT INTO usuarios (id_usuario, id_cuenta, email, password_hash, rol, email_verificado,"
                              " codigo_verificacion, codigo_expira_en) VALUES (:u, '00000000-0000-0000-0000-000000000001',"
                              " :e, :h, 'admin', FALSE, :c, :x)"),
                         {"u": U_PEND, "e": PENDIENTE, "h": hash_password("clave-nueva-12"), "c": CODIGO, "x": expira})
        await db.commit()

    async def _db():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_db] = _db
    yield maker
    app.dependency_overrides.clear()


def _cliente():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _verificar(c, email, codigo, ip=None):
    return await c.post("/auth/verificar-codigo", json={"email": email, "codigo": codigo},
                        headers={"x-real-ip": ip} if ip else {})


async def _usuario(maker, email):
    async with maker() as db:
        return (await db.execute(text("SELECT email_verificado, codigo_verificacion FROM usuarios WHERE email=:e"),
                                 {"e": email})).first()


# ── La falla ─────────────────────────────────────────────────────────────────

async def test_correo_verificado_con_codigo_inventado_no_inicia_sesion(mundo):
    async with _cliente() as c:
        r = await _verificar(c, VERIFICADO, "000000")
        assert r.status_code == 400 and r.json() == {"detail": MSG}
        assert "access_token" not in r.headers.get("set-cookie", "")
        assert (await c.get("/auth/me")).status_code == 401          # no quedó ninguna sesión abierta


async def test_correo_verificado_responde_igual_que_un_correo_inexistente(mundo):
    async with _cliente() as c:
        a = await _verificar(c, VERIFICADO, "123456")
        b = await _verificar(c, "nadie@x.com", "123456")
    assert (a.status_code, a.json()) == (b.status_code, b.json()) == (400, {"detail": MSG})


async def test_el_login_normal_con_contrasena_sigue_funcionando(mundo):
    async with _cliente() as c:
        r = await c.post("/auth/login", json={"email": VERIFICADO, "password": "clave-correcta-1"})
        assert r.status_code == 200 and "access_token" in r.headers["set-cookie"]
        assert (await c.get("/auth/me")).json()["email"] == VERIFICADO


# ── El flujo legítimo se mantiene ────────────────────────────────────────────

async def test_codigo_correcto_verifica_e_inicia_sesion_una_sola_vez(mundo):
    async with _cliente() as c:
        r = await _verificar(c, PENDIENTE, CODIGO)
        assert r.status_code == 200 and r.json()["email"] == PENDIENTE and "access_token" in r.headers["set-cookie"]
    assert tuple(await _usuario(mundo, PENDIENTE)) == (True, None)
    async with _cliente() as c:                                       # el mismo código no sirve otra vez
        r = await _verificar(c, PENDIENTE, CODIGO)
        assert r.status_code == 400 and "access_token" not in r.headers.get("set-cookie", "")


async def test_codigo_vencido_no_sirve(mundo):
    async with mundo() as db:
        await db.execute(text("UPDATE usuarios SET codigo_expira_en = now() - interval '1 minute' WHERE email=:e"),
                         {"e": PENDIENTE})
        await db.commit()
    async with _cliente() as c:
        assert (await _verificar(c, PENDIENTE, CODIGO)).status_code == 400
    assert (await _usuario(mundo, PENDIENTE)).email_verificado is False


async def test_codigo_con_caracteres_raros_responde_400_no_500(mundo):
    async with _cliente() as c:
        assert (await _verificar(c, PENDIENTE, "ñ12345")).status_code == 400


# ── Límite de intentos ───────────────────────────────────────────────────────

async def test_tras_5_fallos_se_bloquea_y_el_codigo_queda_anulado(mundo):
    async with _cliente() as c:
        for _ in range(intentos.VERIFICAR_MAX_FALLOS_CORREO):
            assert (await _verificar(c, PENDIENTE, "111111")).status_code == 400
        r = await _verificar(c, PENDIENTE, CODIGO)                     # incluso el código correcto
        assert r.status_code == 429 and "Espera" in r.json()["detail"] and int(r.headers["retry-after"]) > 0
    fila = await _usuario(mundo, PENDIENTE)
    assert fila.email_verificado is False and fila.codigo_verificacion is None
    async with mundo() as db:                                         # pasada la espera, el código viejo ya no sirve
        await db.execute(text("DELETE FROM intentos_acceso"))
        await db.commit()
    async with _cliente() as c:
        assert (await _verificar(c, PENDIENTE, CODIGO)).status_code == 400


async def test_los_intentos_contra_un_correo_verificado_tambien_cuentan(mundo):
    async with _cliente() as c:
        for _ in range(intentos.VERIFICAR_MAX_FALLOS_CORREO):
            await _verificar(c, VERIFICADO, "222222")
        assert (await _verificar(c, VERIFICADO, "222222")).status_code == 429
        # el login con contraseña es otro límite: sigue entrando
        assert (await c.post("/auth/login", json={"email": VERIFICADO, "password": "clave-correcta-1"})).status_code == 200


async def test_bloqueo_por_ip_aunque_cambie_el_correo(mundo):
    async with _cliente() as c:
        for i in range(intentos.VERIFICAR_MAX_FALLOS_IP):
            await _verificar(c, f"x{i}@x.com", "333333", ip="186.47.74.254")
        assert (await _verificar(c, PENDIENTE, CODIGO, ip="186.47.74.254")).status_code == 429
        assert (await _verificar(c, PENDIENTE, CODIGO, ip="200.85.80.199")).status_code == 200   # otra IP sí


async def test_un_acierto_limpia_los_fallos_del_correo(mundo):
    async with _cliente() as c:
        for _ in range(intentos.VERIFICAR_MAX_FALLOS_CORREO - 1):
            await _verificar(c, PENDIENTE, "444444")
        assert (await _verificar(c, PENDIENTE, CODIGO)).status_code == 200
    async with mundo() as db:
        clave = intentos.clave("verificar", "correo", PENDIENTE)
        assert (await db.execute(text("SELECT count(*) FROM intentos_acceso WHERE clave=:k"), {"k": clave})).scalar() == 0


# ── El código sale de secrets ────────────────────────────────────────────────

async def test_el_codigo_usa_secrets_y_siempre_tiene_6_digitos(monkeypatch):
    llamadas = []
    monkeypatch.setattr(auth.secrets, "randbelow", lambda n: llamadas.append(n) or 0)
    assert auth._generar_codigo() == "100000" and llamadas == [900000]
    monkeypatch.undo()
    assert not hasattr(auth, "random")
    for _ in range(300):
        cod = auth._generar_codigo()
        assert len(cod) == 6 and cod.isdigit() and 100000 <= int(cod) <= 999999
