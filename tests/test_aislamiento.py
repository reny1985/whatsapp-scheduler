"""
v1.1.0 — Aislamiento total entre cuentas (Postgres real y desechable; se omiten si no hay Postgres local).

· PRUEBA GUARDIANA: toda ruta de la app tiene que estar en PUBLICAS, PROPIAS o en un caso de CASOS_AJENOS. Si alguien
  agrega un endpoint sin decidir cómo se aísla, esta prueba falla y dice cuál.
· Sin login, toda ruta que no es pública responde 401 (se recorren TODAS automáticamente).
· La cuenta B intenta leer, editar y borrar los datos de la cuenta A en TODOS los endpoints con id (por URL, por query y
  cambiando ids en el cuerpo): siempre 404/422, nunca se llama a Evolution, ninguna respuesta trae datos de A y al
  final los datos de A están idénticos.
· Candados de la BD, webhook con secreto, límite de intentos de login y cierre de sesiones (version_token).
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.auth import create_access_token, hash_password
from app.database import get_db
from app.main import app
from app.models import MensajeProgramado, Plantilla, SesionWhatsApp
from app.routers import media, sesiones
from app.services import intentos, webhook_config
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

# ── Cuenta A (la interna, …0001: la crea la migración con el usuario piloto) y cuenta B ──────────────────────────
CA = UA = uuid.UUID("00000000-0000-0000-0000-000000000001")
CB = uuid.UUID("00000000-0000-0000-0000-0000000000b0")
UB = uuid.UUID("00000000-0000-0000-0000-0000000000b1")
SA, SA_PEND, SB = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
MA, MA_SIN, MB, PA = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
SECRETO_A, SECRETO_B = "a" * 64, "b" * 64
ADJUNTO_A = "/static/uploads/0123456789abcdef_legado_a.jpg"      # adjunto antiguo (antes de v1.1.0, en la raíz)
# Si alguna de estas cadenas aparece en una respuesta a B, se filtró un dato de A
MARCAS_A = [str(SA), str(SA_PEND), str(MA), str(MA_SIN), str(PA), "wa-cuenta-a", "593900000001",
            "texto-secreto-de-A", "Grupo de A", "duena-a@x.com", "legado_a", "Plantilla de A"]

# Rutas que no exigen login (el webhook se protege con el secreto; ver sus pruebas)
PUBLICAS = {
    ("GET", "/"), ("GET", "/health"),
    ("POST", "/auth/login"), ("POST", "/auth/logout"), ("POST", "/auth/setup"), ("POST", "/auth/enviar-codigo"),
    ("POST", "/auth/reenviar-codigo"), ("POST", "/auth/verificar-codigo"),
    ("POST", "/webhook/evolution"), ("POST", "/webhook/evolution/{secreto}"),
}
# Rutas con login que no reciben ids: B solo debe ver lo suyo (se comprueba que no aparezca ninguna marca de A)
PROPIAS = {
    ("GET", "/auth/me"), ("GET", "/sesiones/"), ("POST", "/sesiones/"), ("POST", "/sesiones/iniciar-pairing"),
    ("GET", "/mensajes/stats"), ("GET", "/mensajes/historial"), ("GET", "/mensajes/proximos"),
    ("GET", "/mensajes/recientes"), ("GET", "/mensajes/sin-sesion/conteo"), ("GET", "/mensajes/"),
    ("GET", "/dashboard/resumen"), ("GET", "/plantillas/"), ("POST", "/plantillas/"), ("POST", "/media/subir"),
}
FUTURO = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
# (método, ruta-plantilla, url concreta, cuerpo json, códigos aceptados): B usando ids de A
CASOS_AJENOS = [
    ("GET", "/sesiones/{id_sesion}", f"/sesiones/{SA}", None, {404}),
    ("GET", "/sesiones/{id_sesion}/pairing-code", f"/sesiones/{SA}/pairing-code?numero=593900000009", None, {404}),
    ("GET", "/sesiones/{id_sesion}/pendientes", f"/sesiones/{SA}/pendientes", None, {404}),
    ("DELETE", "/sesiones/{id_sesion}", f"/sesiones/{SA}", None, {404}),
    ("DELETE", "/sesiones/{id_sesion}", f"/sesiones/{SA_PEND}", None, {404}),
    ("PATCH", "/sesiones/{id_sesion}/estado", f"/sesiones/{SA}/estado?estado=desconectado", None, {404}),
    ("GET", "/sesiones/{id_sesion}/qr", f"/sesiones/{SA}/qr", None, {404}),
    ("GET", "/sesiones/{id_sesion}/foto", f"/sesiones/{SA}/foto", None, {404}),
    ("GET", "/sesiones/{id_sesion}/verificar", f"/sesiones/{SA}/verificar", None, {404}),
    ("GET", "/sesiones/{id_sesion}/grupos", f"/sesiones/{SA}/grupos", None, {404}),
    ("GET", "/sesiones/{id_sesion}/grupos", f"/sesiones/{SA}/grupos?forzar=true", None, {404}),
    ("GET", "/sesiones/{id_sesion}/verificar-grupo", f"/sesiones/{SA}/verificar-grupo?jid=1@g.us", None, {404}),
    ("GET", "/sesiones/{id_sesion}/resolver-enlace", f"/sesiones/{SA}/resolver-enlace?codigo=ABCDEFGH", None, {404}),
    ("GET", "/sesiones/{id_sesion}/contactos", f"/sesiones/{SA}/contactos", None, {404}),
    ("POST", "/sesiones/{id_sesion}/sincronizar", f"/sesiones/{SA}/sincronizar", None, {404}),
    ("POST", "/sesiones/{id_sesion}/configurar-webhook", f"/sesiones/{SA}/configurar-webhook", None, {404}),
    ("POST", "/sesiones/{id_sesion}/verificar-numero", f"/sesiones/{SA}/verificar-numero", {"numero": "593900000009"}, {404}),
    # id de A en el CUERPO o en la query
    ("POST", "/mensajes/", "/mensajes/",
     {"id_sesion": str(SA), "id_grupo": "1@g.us", "texto_mensaje": "intruso", "fecha_hora_disparo": FUTURO}, {404}),
    ("POST", "/mensajes/reasignar-sin-sesion", "/mensajes/reasignar-sin-sesion", {"id_sesion": str(SA)}, {404}),
    ("GET", "/mensajes/estimacion", f"/mensajes/estimacion?id_sesion={SA}&fecha_hora={FUTURO.replace('+', '%2B')}", None, {404}),
    ("PATCH", "/mensajes/{id_mensaje}/cancelar", f"/mensajes/{MA}/cancelar", None, {404}),
    ("PATCH", "/mensajes/{id_mensaje}/reasignar", f"/mensajes/{MA_SIN}/reasignar", {"id_sesion": str(SB)}, {404}),
    ("PATCH", "/mensajes/{id_mensaje}/reasignar", f"/mensajes/{MB}/reasignar", {"id_sesion": str(SA)}, {404}),
    ("PATCH", "/mensajes/{id_mensaje}", f"/mensajes/{MA}", {"texto_mensaje": "editado por B"}, {404}),
    ("PATCH", "/mensajes/{id_mensaje}", f"/mensajes/{MB}", {"tipo_media": "image", "url_media": ADJUNTO_A}, {422}),
    ("DELETE", "/mensajes/{id_mensaje}", f"/mensajes/{MA}", None, {404}),
    ("GET", "/mensajes/{id_mensaje}", f"/mensajes/{MA}", None, {404}),
    ("DELETE", "/plantillas/{id_plantilla}", f"/plantillas/{PA}", None, {404}),
    ("POST", "/plantillas/", "/plantillas/", {"nombre": "robada", "tipo_media": "image", "url_media": ADJUNTO_A}, {422}),
    ("POST", "/mensajes/", "/mensajes/",
     {"id_sesion": str(SB), "id_grupo": "1@g.us", "tipo_media": "image", "url_media": ADJUNTO_A,
      "fecha_hora_disparo": FUTURO}, {422}),
    ("POST", "/mensajes/", "/mensajes/",
     {"id_sesion": str(SB), "id_grupo": "1@g.us", "tipo_media": "image",
      "url_media": f"/static/uploads/{CA}/archivo.jpg", "fecha_hora_disparo": FUTURO}, {422}),
]


def _rutas_app() -> set[tuple[str, str]]:
    """TODAS las rutas de la API, también las de routers incluidos (FastAPI ≥ 0.14x los agrupa) y las ocultas del
    esquema OpenAPI (include_in_schema=False)."""
    rutas: set[tuple[str, str]] = set()

    def recorrer(lista, prefijo=""):
        for r in lista:
            if isinstance(r, APIRoute):
                rutas.update((m, prefijo + r.path) for m in r.methods if m != "HEAD")
            elif getattr(r, "original_router", None) is not None:
                recorrer(r.original_router.routes, prefijo + (r.include_context.prefix or ""))
    recorrer(app.routes)
    return rutas


# ── Datos ────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def mundo(Session, monkeypatch, tmp_path):
    maker = Session
    monkeypatch.setattr(webhook_config, "AsyncSessionLocal", maker)
    monkeypatch.setattr(sesiones, "AsyncSessionLocal", maker)
    monkeypatch.setattr(media, "UPLOAD_DIR", tmp_path)
    async with maker() as db:
        await db.execute(text("UPDATE usuarios SET email='duena-a@x.com', password_hash=:h, email_verificado=TRUE "
                              "WHERE id_usuario=:u"), {"u": UA, "h": hash_password("clave-de-A-123")})
        await db.execute(text("INSERT INTO cuentas (id_cuenta, nombre, estado) VALUES (:c, 'Cliente B', 'prueba')"), {"c": CB})
        await db.execute(text("INSERT INTO usuarios (id_usuario, id_cuenta, email, password_hash, rol, email_verificado) "
                              "VALUES (:u, :c, 'cliente-b@x.com', :h, 'admin', TRUE)"),
                         {"u": UB, "c": CB, "h": hash_password("clave-de-B-123")})
        db.add_all([
            SesionWhatsApp(id_sesion=SA, id_usuario=UA, instancia_evolution="wa-cuenta-a", token_autorizacion="tok-a",
                           estado_conexion="conectado", numero_telefono="593900000001", secreto_webhook=SECRETO_A),
            SesionWhatsApp(id_sesion=SA_PEND, id_usuario=UA, instancia_evolution="wa-cuenta-a-pend",
                           token_autorizacion="tok-a2", estado_conexion="qr_pendiente"),
            SesionWhatsApp(id_sesion=SB, id_usuario=UB, instancia_evolution="wa-cuenta-b", token_autorizacion="tok-b",
                           estado_conexion="conectado", numero_telefono="593900000002", secreto_webhook=SECRETO_B),
        ])
        await db.flush()
        db.add_all([
            MensajeProgramado(id_mensaje=MA, id_usuario=UA, id_sesion=SA, id_grupo="1@g.us",
                              texto_mensaje="texto-secreto-de-A", tipo_media="image", url_media=ADJUNTO_A,
                              fecha_hora_disparo=datetime.now(timezone.utc) + timedelta(hours=2)),
            MensajeProgramado(id_mensaje=MA_SIN, id_usuario=UA, id_sesion=None, id_grupo="2@g.us",
                              texto_mensaje="texto-secreto-de-A", numero_origen="593900000001",
                              fecha_hora_disparo=datetime.now(timezone.utc) + timedelta(hours=2)),
            MensajeProgramado(id_mensaje=MB, id_usuario=UB, id_sesion=SB, id_grupo="3@g.us", texto_mensaje="de B",
                              fecha_hora_disparo=datetime.now(timezone.utc) + timedelta(hours=2)),
            Plantilla(id_plantilla=PA, id_usuario=UA, nombre="Plantilla de A", texto_mensaje="texto-secreto-de-A"),
        ])
        await db.execute(text("INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size) "
                              "VALUES ('1@g.us', :s, 'Grupo de A', 5)"), {"s": SA})
        await db.commit()

    async def _db():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_db] = _db
    yield maker
    app.dependency_overrides.clear()


async def _foto_de_a(maker) -> list:
    """Todo lo de la cuenta A, para comprobar que nada cambió."""
    async with maker() as db:
        filas = []
        for q in ("SELECT * FROM sesiones_whatsapp WHERE id_cuenta = :c ORDER BY id_sesion",
                  "SELECT * FROM mensajes_programados WHERE id_cuenta = :c ORDER BY id_mensaje",
                  "SELECT * FROM plantillas WHERE id_cuenta = :c ORDER BY id_plantilla",
                  "SELECT g.* FROM grupos_whatsapp g JOIN sesiones_whatsapp s USING (id_sesion) "
                  "WHERE s.id_cuenta = :c ORDER BY g.id_grupo"):
            filas.append([tuple(r) for r in (await db.execute(text(q), {"c": CA})).all()])
        return filas


def _cliente(usuario: uuid.UUID | None, version: int = 0) -> AsyncClient:
    cookies = {"access_token": create_access_token(usuario, "admin", version)} if usuario else {}
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t", cookies=cookies)


class _EvolutionProhibida:
    """Sustituye httpx.AsyncClient: si un endpoint ajeno llega a llamar a Evolution, queda registrado."""
    llamadas: list = []

    def __init__(self, *a, **k):
        _EvolutionProhibida.llamadas.append(("cliente", a, k))

    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    def __getattr__(self, nombre):
        async def _llamada(*a, **k):
            _EvolutionProhibida.llamadas.append((nombre, a, k))
            raise RuntimeError("Evolution no debía llamarse")
        return _llamada


# ── Prueba guardiana ─────────────────────────────────────────────────────────

async def test_guardiana_toda_ruta_tiene_definido_su_aislamiento():
    cubiertas = PUBLICAS | PROPIAS | {(m, plantilla) for m, plantilla, *_ in CASOS_AJENOS}
    sin_decidir = _rutas_app() - cubiertas
    assert not sin_decidir, (
        f"Rutas sin prueba de aislamiento: {sorted(sin_decidir)}. Agrégalas a PUBLICAS, PROPIAS o CASOS_AJENOS "
        f"en tests/test_aislamiento.py (con un caso que intente usar datos de otra cuenta).")
    sobrantes = cubiertas - _rutas_app()
    assert not sobrantes, f"Rutas en la matriz que ya no existen: {sorted(sobrantes)}"


async def test_sin_login_toda_ruta_no_publica_responde_401(mundo):
    async with _cliente(None) as c:
        for metodo, ruta in sorted(_rutas_app() - PUBLICAS):
            url = (ruta.replace("{id_sesion}", str(SA)).replace("{id_mensaje}", str(MA))
                       .replace("{id_plantilla}", str(PA)))
            r = await c.request(metodo, url)
            assert r.status_code == 401, f"{metodo} {ruta} sin login respondió {r.status_code}"


# ── B contra los datos de A en todos los endpoints ───────────────────────────

async def test_la_cuenta_b_no_puede_ver_ni_tocar_nada_de_la_cuenta_a(mundo, monkeypatch):
    maker = mundo
    antes = await _foto_de_a(maker)
    _EvolutionProhibida.llamadas = []
    for modulo in (sesiones, webhook_config):
        monkeypatch.setattr(modulo.httpx, "AsyncClient", _EvolutionProhibida)
    evo = AsyncMock(side_effect=RuntimeError("Evolution no debía llamarse"))
    monkeypatch.setattr(sesiones, "_evo_get", evo)
    monkeypatch.setattr(sesiones, "_evo_post", evo)

    errores = []
    async with _cliente(UB) as c:
        for metodo, plantilla, url, cuerpo, esperados in CASOS_AJENOS:
            r = await c.request(metodo, url, json=cuerpo)
            if r.status_code not in esperados:
                errores.append(f"{metodo} {url} → {r.status_code} (esperado {esperados}): {r.text[:150]}")
            fugas = [m for m in MARCAS_A if m in r.text]
            if fugas:
                errores.append(f"{metodo} {url} filtró datos de A: {fugas}")
        for metodo, ruta in sorted(PROPIAS):
            if metodo != "GET":
                continue
            r = await c.get(ruta)
            if r.status_code != 200:
                errores.append(f"GET {ruta} → {r.status_code}: {r.text[:150]}")
            fugas = [m for m in MARCAS_A if m in r.text]
            if fugas:
                errores.append(f"GET {ruta} filtró datos de A: {fugas}")

    assert not errores, "\n".join(errores)
    assert not _EvolutionProhibida.llamadas and not evo.await_count, "un endpoint ajeno llegó a llamar a Evolution"
    assert await _foto_de_a(maker) == antes, "los datos de la cuenta A cambiaron"


async def test_b_ve_solo_lo_suyo_en_listas_e_inicio(mundo):
    async with _cliente(UB) as c:
        sesiones_b = (await c.get("/sesiones/")).json()
        mensajes_b = (await c.get("/mensajes/")).json()
        resumen = (await c.get("/dashboard/resumen")).json()
        yo = (await c.get("/auth/me")).json()
    assert [s["id_sesion"] for s in sesiones_b] == [str(SB)]
    assert [m["id_mensaje"] for m in mensajes_b] == [str(MB)]
    assert [s["id_sesion"] for s in resumen["sesiones"]] == [str(SB)]
    assert yo["email"] == "cliente-b@x.com"
    async with _cliente(UA) as c:                                  # y A sigue viendo lo suyo
        assert {s["id_sesion"] for s in (await c.get("/sesiones/")).json()} == {str(SA), str(SA_PEND)}


async def test_crear_sesion_ignora_id_usuario_cuenta_e_instancia_del_cliente(mundo, monkeypatch):
    monkeypatch.setattr(sesiones, "_evo_post", AsyncMock(return_value={"hash": {"apikey": "tok-nuevo"}}))
    monkeypatch.setattr(sesiones, "_configurar_instancia_bg", AsyncMock())
    async with _cliente(UB) as c:
        r = await c.post("/sesiones/", json={"instancia_evolution": "wa-cuenta-a", "id_usuario": str(UA),
                                             "id_cuenta": str(CA), "token_autorizacion": "robado"})
    assert r.status_code == 201, r.text
    async with mundo() as db:
        nueva = await db.get(SesionWhatsApp, uuid.UUID(r.json()["id_sesion"]))
    assert nueva.id_cuenta == CB and nueva.id_usuario == UB
    assert nueva.instancia_evolution != "wa-cuenta-a" and nueva.token_autorizacion == "tok-nuevo"
    assert nueva.secreto_webhook and len(nueva.secreto_webhook) == 64


async def test_pairing_de_b_no_borra_las_vinculaciones_pendientes_de_a(mundo, monkeypatch):
    monkeypatch.setattr(sesiones, "_evo_post", AsyncMock(return_value={"hash": {"apikey": "tok-p"}}))
    monkeypatch.setattr(sesiones, "_evo_get", AsyncMock(return_value={"pairingCode": "ABCD1234"}))
    monkeypatch.setattr(sesiones, "_configurar_instancia_bg", AsyncMock())
    monkeypatch.setattr(sesiones.asyncio, "sleep", AsyncMock())
    async with _cliente(UB) as c:
        r = await c.post("/sesiones/iniciar-pairing", json={"numero": "593900000003"})
    assert r.status_code == 200, r.text
    async with mundo() as db:
        assert await db.get(SesionWhatsApp, SA_PEND) is not None               # la de A sigue ahí
        nueva = await db.get(SesionWhatsApp, uuid.UUID(r.json()["id_sesion"]))
    assert nueva.id_cuenta == CB


async def test_adjuntos_cada_cuenta_en_su_carpeta_y_los_antiguos_siguen_sirviendo(mundo):
    async with _cliente(UB) as c:
        r = await c.post("/media/subir", files={"file": ("foto.png", b"\x89PNG....", "image/png")})
        assert r.status_code == 201, r.text
        url_b = r.json()["url"]
        assert url_b.startswith(f"/static/uploads/{CB}/")
        ok = await c.post("/plantillas/", json={"nombre": "mía", "tipo_media": "image", "url_media": url_b})
        assert ok.status_code == 201, ok.text
        for mala in (f"/static/uploads/{CB}/../{CA}/x.jpg", "/static/uploads/avatars/x.jpg"):
            r = await c.post("/plantillas/", json={"nombre": "x", "tipo_media": "image", "url_media": mala})
            assert r.status_code == 422, mala
    async with _cliente(UA) as c:
        # A puede seguir usando (y editar) su adjunto antiguo, que sigue en la raíz de uploads
        r = await c.patch(f"/mensajes/{MA}", json={"texto_mensaje": "nuevo texto", "tipo_media": "image",
                                                  "url_media": ADJUNTO_A})
        assert r.status_code == 200, r.text
        r = await c.post("/plantillas/", json={"nombre": "usa el viejo", "tipo_media": "image", "url_media": ADJUNTO_A})
        assert r.status_code == 201, r.text
        r = await c.post("/plantillas/", json={"nombre": "de B", "tipo_media": "image", "url_media": url_b})
        assert r.status_code == 422                                              # A no puede usar el de B


# ── Candados de la base de datos ─────────────────────────────────────────────

async def test_la_bd_completa_la_cuenta_desde_el_usuario_y_rechaza_mezclas(mundo):
    maker = mundo
    async with maker() as db:                       # sin id_cuenta: la BD la toma del usuario
        mid = uuid.uuid4()
        db.add(MensajeProgramado(id_mensaje=mid, id_usuario=UB, id_sesion=SB, id_grupo="1@g.us", texto_mensaje="x",
                                 fecha_hora_disparo=datetime.now(timezone.utc)))
        await db.commit()
        assert (await db.get(MensajeProgramado, mid)).id_cuenta == CB
    async with maker() as db:                       # cuenta distinta a la de su usuario → error
        db.add(Plantilla(id_usuario=UB, id_cuenta=CA, nombre="mezcla", texto_mensaje="x"))
        with pytest.raises(DBAPIError):
            await db.commit()
    async with maker() as db:                       # mensaje de B con la sesión de A → error
        db.add(MensajeProgramado(id_usuario=UB, id_sesion=SA, id_grupo="1@g.us", texto_mensaje="x",
                                 fecha_hora_disparo=datetime.now(timezone.utc)))
        with pytest.raises(IntegrityError):
            await db.commit()
    async with maker() as db:                       # mover un mensaje de A a la sesión de B → error
        with pytest.raises(IntegrityError):
            await db.execute(text("UPDATE mensajes_programados SET id_sesion = :s WHERE id_mensaje = :m"),
                             {"s": SB, "m": MA})


async def test_borrar_la_sesion_deja_el_mensaje_en_su_cuenta(mundo):
    maker = mundo
    async with maker() as db:
        await db.execute(text("DELETE FROM sesiones_whatsapp WHERE id_sesion = :s"), {"s": SA})
        await db.commit()
        m = await db.get(MensajeProgramado, MA)
    assert m.id_sesion is None and m.id_cuenta == CA


# ── Webhook con secreto ──────────────────────────────────────────────────────

def _evento_grupo(instancia: str, nombre: str) -> dict:
    return {"event": "groups.upsert", "instance": instancia, "data": [{"id": "999@g.us", "subject": nombre, "size": 2}]}


async def _grupos(maker, sid) -> set:
    async with maker() as db:
        return {r[0] for r in (await db.execute(text("SELECT subject FROM grupos_whatsapp WHERE id_sesion=:s"), {"s": sid}))}


async def test_webhook_con_secreto_solo_acepta_eventos_de_su_propia_instancia(mundo):
    maker = mundo
    async with _cliente(None) as c:
        r = await c.post(f"/webhook/evolution/{SECRETO_B}", json=_evento_grupo("wa-cuenta-a", "Intruso"))
        assert r.json().get("ignorado") == "instancia no coincide"
        assert (await c.post(f"/webhook/evolution/{'c' * 64}", json=_evento_grupo("wa-cuenta-a", "Intruso"))).status_code == 404
        assert "Intruso" not in await _grupos(maker, SA)

        r = await c.post(f"/webhook/evolution/{SECRETO_A}", json=_evento_grupo("wa-cuenta-a", "Nuevo de A"))
        assert r.json().get("grupos_actualizados") == 1
    assert "Nuevo de A" in await _grupos(maker, SA)
    async with maker() as db:
        assert (await db.get(SesionWhatsApp, SA)).webhook_confirmado_en is not None      # su primer evento lo confirma


async def test_ruta_vieja_funciona_hasta_confirmar_la_nueva_y_despues_se_ignora(mundo):
    maker = mundo
    async with _cliente(None) as c:
        r = await c.post("/webhook/evolution", json=_evento_grupo("wa-cuenta-b", "Por ruta vieja"))
        assert r.json().get("grupos_actualizados") == 1                           # aún sin confirmar: funciona
        async with maker() as db:
            await db.execute(text("UPDATE sesiones_whatsapp SET webhook_confirmado_en = now() WHERE id_sesion=:s"), {"s": SB})
            await db.commit()
        r = await c.post("/webhook/evolution", json=_evento_grupo("wa-cuenta-b", "Falsificado"))
        assert r.json().get("ignorado") == "usa la ruta con secreto"
    assert await _grupos(maker, SB) == {"Por ruta vieja"}


async def test_configurar_webhook_usa_la_url_con_secreto_y_confirma_solo_si_evolution_la_muestra(mundo, monkeypatch):
    maker = mundo
    monkeypatch.setenv("BASE_PUBLIC_URL", "https://app.test")
    esperado = f"https://app.test/webhook/evolution/{SECRETO_B}"
    llamadas = []

    class _Evo:
        url_guardada = None
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, json=None):
            llamadas.append(url)
            if "/webhook/set/" in url:
                _Evo.url_guardada = json["url"]
            return webhook_config.httpx.Response(201, json={}, request=webhook_config.httpx.Request("POST", url))
        async def get(self, url, headers=None):
            return webhook_config.httpx.Response(200, json={"enabled": True, "url": _Evo.url_guardada},
                                                 request=webhook_config.httpx.Request("GET", url))

    monkeypatch.setattr(webhook_config.httpx, "AsyncClient", _Evo)
    assert await webhook_config.configurar_y_confirmar(SB, con_ajustes=False) is True
    assert _Evo.url_guardada == esperado and not any("settings" in u for u in llamadas)
    async with maker() as db:
        assert (await db.get(SesionWhatsApp, SB)).webhook_confirmado_en is not None

    _Evo.url_guardada = None
    monkeypatch.setattr(_Evo, "get", lambda self, url, headers=None: _respuesta_vieja(url))
    async with maker() as db:
        await db.execute(text("UPDATE sesiones_whatsapp SET webhook_confirmado_en = NULL"))
        await db.commit()
    assert await webhook_config.migrar_pendientes() == 0                     # Evolution no la muestra: no se confirma
    async with maker() as db:
        assert (await db.get(SesionWhatsApp, SA)).webhook_confirmado_en is None


async def _respuesta_vieja(url):
    return webhook_config.httpx.Response(200, json={"enabled": True, "url": "https://app.test/webhook/evolution"},
                                         request=webhook_config.httpx.Request("GET", url))


# ── Login: límite de intentos y cierre de sesiones ───────────────────────────

async def test_login_se_bloquea_tras_5_fallos_del_mismo_correo(mundo):
    async with _cliente(None) as c:
        for _ in range(intentos.LOGIN_MAX_FALLOS_CORREO):
            r = await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "mala"})
            assert r.status_code == 401
        r = await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "clave-de-B-123"})
        assert r.status_code == 429 and "Espera" in r.json()["detail"] and int(r.headers["retry-after"]) > 0
        # otro correo desde la misma IP todavía puede entrar (el límite por IP es más alto)
        r = await c.post("/auth/login", json={"email": "duena-a@x.com", "password": "clave-de-A-123"})
        assert r.status_code == 200


async def test_login_correcto_limpia_los_fallos_y_el_correo_inexistente_responde_igual(mundo):
    async with _cliente(None) as c:
        for _ in range(intentos.LOGIN_MAX_FALLOS_CORREO - 1):
            await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "mala"})
        assert (await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "clave-de-B-123"})).status_code == 200
        assert (await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "mala"})).status_code == 401
        r = await c.post("/auth/login", json={"email": "nadie@x.com", "password": "mala"})
        assert r.status_code == 401 and r.json()["detail"] == "Email o contraseña incorrectos"


def _railway(cliente: str, borde: str) -> dict:
    """Encabezados tal como llegan detrás de Railway (vistos en staging): X-Real-IP = cliente (Railway la
    sobrescribe), X-Forwarded-For = "cliente, nodo de borde"; el nodo cambia entre peticiones."""
    return {"x-real-ip": cliente, "x-forwarded-for": f"{cliente}, {borde}"}


async def test_login_se_bloquea_por_ip_real_aunque_cambie_el_nodo_de_borde(mundo):
    bordes = ["79.127.217.66", "152.233.76.9", "152.233.76.10"]
    async with _cliente(None) as c:
        for i in range(intentos.LOGIN_MAX_FALLOS_IP):
            await c.post("/auth/login", json={"email": f"x{i}@x.com", "password": "mala"},
                         headers=_railway("186.47.74.254", bordes[i % 3]))
        r = await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "clave-de-B-123"},
                         headers=_railway("186.47.74.254", "152.233.23.194"))
        assert r.status_code == 429                                  # misma IP real, otro nodo: sigue bloqueada
        r = await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "clave-de-B-123"},
                         headers=_railway("200.85.80.199", bordes[0]))
        assert r.status_code == 200                                  # otro cliente por el MISMO nodo: no le afecta


async def test_ip_de_xff_u_otros_encabezados_inventados_no_cuentan():
    from starlette.requests import Request
    def req(h):
        return Request({"type": "http", "headers": [(k.encode(), v.encode()) for k, v in h.items()],
                        "client": ("100.64.0.2", 1234)})
    assert intentos.ip_cliente(req(_railway("186.47.74.254", "152.233.76.9"))) == "186.47.74.254"
    falsos = {"x-forwarded-for": "6.6.6.6", "x-envoy-external-address": "8.8.4.4", "forwarded": "for=4.4.4.4",
              "cf-connecting-ip": "5.5.5.5", "true-client-ip": "9.9.9.9"}
    assert intentos.ip_cliente(req(falsos)) is None                  # sin X-Real-IP: ninguno se usa (ni client.host)
    assert intentos.ip_cliente(req({"x-real-ip": "no-es-una-ip"})) is None


async def test_sin_x_real_ip_no_se_limita_por_ip_pero_si_por_correo(mundo, caplog):
    async with _cliente(None) as c:                                  # sin encabezados de proxy
        for i in range(intentos.LOGIN_MAX_FALLOS_IP + 5):
            await c.post("/auth/login", json={"email": f"y{i}@x.com", "password": "mala"})
        r = await c.post("/auth/login", json={"email": "cliente-b@x.com", "password": "clave-de-B-123"})
        assert r.status_code == 200                                  # nadie queda bloqueado por una IP compartida
        for _ in range(intentos.LOGIN_MAX_FALLOS_CORREO):
            await c.post("/auth/login", json={"email": "duena-a@x.com", "password": "mala"})
        r = await c.post("/auth/login", json={"email": "duena-a@x.com", "password": "clave-de-A-123"})
        assert r.status_code == 429                                  # el límite por correo sigue funcionando
    assert "sin X-Real-IP" in caplog.text


async def test_subir_version_token_cierra_las_sesiones_abiertas(mundo):
    async with _cliente(UB, version=0) as c:
        assert (await c.get("/auth/me")).status_code == 200
        async with mundo() as db:
            await db.execute(text("UPDATE usuarios SET version_token = version_token + 1 WHERE id_usuario=:u"), {"u": UB})
            await db.commit()
        r = await c.get("/auth/me")
        assert r.status_code == 401 and "cerró" in r.json()["detail"]
    async with _cliente(UB, version=1) as c:
        assert (await c.get("/auth/me")).status_code == 200


async def test_token_sin_version_de_antes_de_v110_sigue_valiendo(mundo):
    from jose import jwt
    from app.auth import ALGORITHM, SECRET_KEY
    viejo = jwt.encode({"sub": str(UA), "rol": "admin", "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       SECRET_KEY, algorithm=ALGORITHM)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", cookies={"access_token": viejo}) as c:
        assert (await c.get("/auth/me")).status_code == 200


async def test_nada_secreto_en_el_log_al_vincular(mundo, monkeypatch, caplog):
    monkeypatch.setattr(sesiones, "_evo_post", AsyncMock(return_value={"hash": {"apikey": "TOKEN-SECRETO-XYZ"}}))
    monkeypatch.setattr(sesiones, "_evo_get", AsyncMock(return_value={"pairingCode": "PAIR9876", "code": "2@QRSECRETO"}))
    monkeypatch.setattr(sesiones, "_configurar_instancia_bg", AsyncMock())
    monkeypatch.setattr(sesiones.asyncio, "sleep", AsyncMock())
    caplog.set_level("DEBUG")
    async with _cliente(UB) as c:
        assert (await c.post("/sesiones/", json={})).status_code == 201          # (antes: B tiene 1 de máx. 2)
        assert (await c.post("/sesiones/iniciar-pairing", json={"numero": "593900000004"})).status_code == 200
    for secreto in ("TOKEN-SECRETO", "PAIR9876", "QRSECRETO", "clave-de"):
        assert secreto not in caplog.text
