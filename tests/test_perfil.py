"""
Pruebas del perfil de WhatsApp (nombre y foto): servicio, endpoint autenticado, bloqueo del acceso
público a avatars/, disparadores (conexión y Verificar) y resumen del Inicio.
Usan un Postgres desechable (se omiten si no hay uno local) y simulan Evolution/WhatsApp (sin red real).
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from app import main as app_main
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.models import SesionWhatsApp
from app.routers import media, sesiones, webhook
from app.services import perfil
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
CLAVE = "clave-global-secreta"
TOKEN = "token-instancia-secreto"
URL_FOTO = "https://pps.whatsapp.net/v/t61/foto.jpg?oh=firma-secreta&oe=123"
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 200


class _Evo:
    """Simula Evolution (POST chat/fetchProfile) y la descarga de la foto (GET)."""
    perfil = (200, {"name": "Renato Astudillo", "picture": URL_FOTO, "email": "privado@x.com"})
    foto = (200, JPEG, "image/jpeg")
    instancias = (200, [{"instance": {"profileName": "RENY"}}])
    excepcion = None
    llamadas: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        _Evo.llamadas.append(("POST", url, headers))
        if _Evo.excepcion:
            raise _Evo.excepcion
        st, body = _Evo.perfil
        return httpx.Response(st, json=body, request=httpx.Request("POST", url))

    async def get(self, url, **k):
        _Evo.llamadas.append(("GET", url, k.get("headers")))
        if url.endswith("/instance/fetchInstances"):          # nombre de perfil (pushName) de Evolution
            st, body = _Evo.instancias
            return httpx.Response(st, json=body, request=httpx.Request("GET", url))
        st, contenido, tipo = _Evo.foto
        return httpx.Response(st, content=contenido, headers={"content-type": tipo}, request=httpx.Request("GET", url))


@pytest.fixture
def mundo(Session, monkeypatch, tmp_path):
    _Evo.llamadas, _Evo.excepcion = [], None
    _Evo.perfil = (200, {"name": "Renato Astudillo", "picture": URL_FOTO, "email": "privado@x.com"})
    _Evo.foto = (200, JPEG, "image/jpeg")
    _Evo.instancias = (200, [{"instance": {}}])
    monkeypatch.setattr(perfil.httpx, "AsyncClient", _Evo)
    monkeypatch.setattr(perfil, "AsyncSessionLocal", Session)
    monkeypatch.setattr(perfil, "EVOLUTION_API_KEY", CLAVE)
    monkeypatch.setattr(perfil, "EVOLUTION_API_URL", "http://evo")
    monkeypatch.setattr(media, "UPLOAD_DIR", tmp_path)
    yield Session
    app.dependency_overrides.clear()


async def _sesion(maker, *, usuario=U1, estado="conectado", numero="593961409996", consultado=None):
    sid = uuid.uuid4()
    async with maker() as db:
        db.add(SesionWhatsApp(id_sesion=sid, id_usuario=usuario, instancia_evolution=f"wa-{sid.hex[:8]}",
                              token_autorizacion=TOKEN, estado_conexion=estado, numero_telefono=numero,
                              perfil_consultado_en=consultado))
        await db.commit()
    return sid


async def _leer(maker, sid):
    async with maker() as db:
        s = await db.get(SesionWhatsApp, sid)
        await db.refresh(s)
        return s


# ── Servicio ─────────────────────────────────────────────────────────────────

async def test_con_foto_guarda_nombre_y_descarga_la_foto_en_disco(mundo, tmp_path):
    sid = await _sesion(mundo)
    assert await perfil.actualizar_perfil_si_corresponde(sid) is True
    s = await _leer(mundo, sid)
    assert s.perfil_nombre == "Renato Astudillo"
    assert s.perfil_foto_en is not None and s.perfil_consultado_en is not None
    archivo = tmp_path / "avatars" / f"{sid}.jpg"
    assert archivo.read_bytes() == JPEG                      # la foto se GUARDA, no solo el enlace
    # usa el token de la instancia, no la clave global
    post = next(l for l in _Evo.llamadas if l[0] == "POST")
    assert post[1] == f"http://evo/chat/fetchProfile/{s.instancia_evolution}" and post[2]["apikey"] == TOKEN
    # la URL de la foto no queda guardada en ningún campo
    assert URL_FOTO not in str([s.perfil_nombre, s.perfil_foto_en, s.perfil_consultado_en])


async def test_sin_foto_usa_respaldo_y_borra_la_anterior(mundo, tmp_path):
    sid = await _sesion(mundo)
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / f"{sid}.jpg").write_bytes(JPEG)       # foto vieja
    _Evo.perfil = (200, {"name": "Renato", "picture": None})       # ahora la oculta por privacidad
    assert await perfil.actualizar_perfil_si_corresponde(sid) is True
    s = await _leer(mundo, sid)
    assert s.perfil_nombre == "Renato" and s.perfil_foto_en is None
    assert not (tmp_path / "avatars" / f"{sid}.jpg").exists()


async def test_error_de_evolution_no_rompe_conserva_datos_y_reintenta_en_1_hora(mundo, caplog):
    sid = await _sesion(mundo)
    await perfil.actualizar_perfil_si_corresponde(sid)             # primero bien
    antes = await _leer(mundo, sid)
    _Evo.perfil = (500, {"error": "boom"})
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        assert await perfil.actualizar_perfil_si_corresponde(sid, forzar=True) is False
    despues = await _leer(mundo, sid)
    assert despues.perfil_nombre == antes.perfil_nombre and despues.perfil_foto_en == antes.perfil_foto_en
    assert "HTTP 500" in caplog.text and CLAVE not in caplog.text and TOKEN not in caplog.text
    ahora = datetime.now(timezone.utc)
    assert not perfil.debe_actualizarse(despues, ahora)                          # no insiste de inmediato
    assert perfil.debe_actualizarse(despues, ahora + timedelta(hours=1, minutes=1))  # reintenta en ~1 h


async def test_timeout_de_evolution_no_lanza(mundo):
    sid = await _sesion(mundo)
    _Evo.excepcion = httpx.ReadTimeout("timeout")
    assert await perfil.actualizar_perfil_si_corresponde(sid) is False
    s = await _leer(mundo, sid)
    assert s.perfil_nombre is None and s.perfil_foto_en is None


async def test_falla_la_descarga_guarda_el_nombre_y_deja_el_motivo_en_el_log(mundo, tmp_path, caplog):
    sid = await _sesion(mundo)
    _Evo.foto = (404, b"", "text/plain")
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        assert await perfil.actualizar_perfil_si_corresponde(sid) is False
    s = await _leer(mundo, sid)
    assert s.perfil_nombre == "Renato Astudillo" and s.perfil_foto_en is None
    assert not (tmp_path / "avatars" / f"{sid}.jpg").exists()
    assert "no se pudo descargar la foto" in caplog.text and "HTTP 404" in caplog.text
    assert "firma-secreta" not in caplog.text                       # la URL firmada no se registra


@pytest.mark.parametrize("contenido", [b"<html>no soy una imagen</html>", b"\xff\xd8\xff" + b"0" * (3 * 1024 * 1024)])
async def test_rechaza_descargas_que_no_son_imagen_o_son_enormes(mundo, tmp_path, contenido):
    sid = await _sesion(mundo)
    _Evo.foto = (200, contenido, "image/jpeg")
    await perfil.actualizar_perfil_si_corresponde(sid)
    assert not (tmp_path / "avatars" / f"{sid}.jpg").exists()


async def test_como_maximo_una_vez_al_dia(mundo):
    sid = await _sesion(mundo)
    await perfil.actualizar_perfil_si_corresponde(sid)
    n = len([l for l in _Evo.llamadas if l[0] == "POST"])
    assert await perfil.actualizar_perfil_si_corresponde(sid) is False          # mismo día: no consulta
    assert len([l for l in _Evo.llamadas if l[0] == "POST"]) == n
    async with mundo() as db:                                                   # pasan 25 h
        s = await db.get(SesionWhatsApp, sid)
        s.perfil_consultado_en = datetime.now(timezone.utc) - timedelta(hours=25)
        await db.commit()
    assert await perfil.actualizar_perfil_si_corresponde(sid) is True
    assert len([l for l in _Evo.llamadas if l[0] == "POST"]) == n + 1


async def test_sesion_desconectada_o_sin_numero_no_consulta(mundo):
    a = await _sesion(mundo, estado="desconectado")
    b = await _sesion(mundo, numero=None)
    assert await perfil.actualizar_perfil_si_corresponde(a) is False
    assert await perfil.actualizar_perfil_si_corresponde(b) is False
    assert _Evo.llamadas == []


async def test_nombre_se_limpia(mundo):
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "  Ana\x00\x07 " + "X" * 300, "picture": None})
    await perfil.actualizar_perfil_si_corresponde(sid)
    n = (await _leer(mundo, sid)).perfil_nombre
    assert n.startswith("Ana") and "\x00" not in n and len(n) <= 100


# ── Endpoint de la foto (con login) ──────────────────────────────────────────

async def _get(maker, path, usuario=U1, autenticado=True):
    if autenticado:
        app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=usuario, id_cuenta=usuario)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.get(path)


async def test_endpoint_foto_sirve_la_imagen_con_login(mundo):
    sid = await _sesion(mundo)
    await perfil.actualizar_perfil_si_corresponde(sid)
    r = await _get(mundo, f"/sesiones/{sid}/foto")
    assert r.status_code == 200 and r.content == JPEG and r.headers["content-type"] == "image/jpeg"
    assert "private" in r.headers["cache-control"]
    assert URL_FOTO not in r.text and CLAVE not in r.headers.__str__()


async def test_endpoint_foto_sin_login_es_401(mundo):
    sid = await _sesion(mundo)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/sesiones/{sid}/foto")
    assert r.status_code == 401


async def test_endpoint_foto_404_si_no_hay_foto_o_es_de_otro_usuario(mundo, tmp_path):
    sid = await _sesion(mundo)
    assert (await _get(mundo, f"/sesiones/{sid}/foto")).status_code == 404         # sin foto
    (tmp_path / "avatars").mkdir()
    (tmp_path / "avatars" / f"{sid}.jpg").write_bytes(JPEG)
    otro = uuid.UUID("00000000-0000-0000-0000-000000000099")
    assert (await _get(mundo, f"/sesiones/{sid}/foto", usuario=otro)).status_code == 404   # ajena
    assert (await _get(mundo, f"/sesiones/{sid}/foto")).status_code == 200


async def test_avatars_no_es_accesible_por_la_ruta_estatica_publica():
    carpeta = app_main.UPLOAD_DIR / "avatars"
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = f"prueba-{uuid.uuid4().hex}"
    (carpeta / f"{nombre}.jpg").write_bytes(JPEG)
    (app_main.UPLOAD_DIR / f"{nombre}.txt").write_text("hola")
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            privada = await c.get(f"/static/uploads/avatars/{nombre}.jpg")
            normal = await c.get(f"/static/uploads/{nombre}.txt")
    finally:
        (carpeta / f"{nombre}.jpg").unlink(missing_ok=True)
        (app_main.UPLOAD_DIR / f"{nombre}.txt").unlink(missing_ok=True)
    assert privada.status_code == 404          # las fotos solo salen por /sesiones/{id}/foto
    assert normal.status_code == 200           # los adjuntos siguen sirviéndose igual


# ── Disparadores ─────────────────────────────────────────────────────────────

async def test_al_conectarse_el_webhook_pide_el_perfil(mundo, monkeypatch):
    sid = await _sesion(mundo, estado="qr_pendiente")
    monkeypatch.setattr(webhook, "AsyncSessionLocal", mundo)
    monkeypatch.setattr(webhook, "_fetch_instancia_info", AsyncMock(return_value={"apikey": "k", "numero": "593961409996"}))
    pedido = AsyncMock(return_value=True)
    monkeypatch.setattr(webhook.perfil, "actualizar_perfil_si_corresponde", pedido)
    instancia = (await _leer(mundo, sid)).instancia_evolution
    await webhook._sincronizar_conexion(instancia, "open")
    await asyncio.sleep(0)
    pedido.assert_awaited_once_with(sid)
    # y NO lo pide si la sesión se desconecta
    pedido.reset_mock()
    await webhook._sincronizar_conexion(instancia, "close")
    await asyncio.sleep(0)
    pedido.assert_not_awaited()


async def test_verificar_pide_el_perfil_solo_si_esta_conectada(mundo, monkeypatch):
    sid = await _sesion(mundo)
    pedido = AsyncMock(return_value=True)
    monkeypatch.setattr(sesiones.perfil, "actualizar_perfil_si_corresponde", pedido)
    monkeypatch.setattr(sesiones, "_evo_get", AsyncMock(return_value={"instance": {"state": "open"}}))
    monkeypatch.setattr(sesiones, "_obtener_numero_instancia", AsyncMock(return_value="593961409996"))
    app.dependency_overrides[require_admin] = lambda: MagicMock(id_usuario=U1, id_cuenta=U1)
    r = await _get(mundo, f"/sesiones/{sid}/verificar")
    assert r.status_code == 200
    pedido.assert_awaited_once_with(sid)
    pedido.reset_mock()
    monkeypatch.setattr(sesiones, "_evo_get", AsyncMock(return_value={"instance": {"state": "close"}}))
    await _get(mundo, f"/sesiones/{sid}/verificar")
    pedido.assert_not_awaited()


# ── Lecturas que usa el frontend ─────────────────────────────────────────────

async def test_listado_de_sesiones_y_resumen_incluyen_nombre_y_foto(mundo):
    sid = await _sesion(mundo)
    await perfil.actualizar_perfil_si_corresponde(sid)
    r = await _get(mundo, "/sesiones/")
    assert r.status_code == 200
    s = r.json()[0]
    assert s["perfil_nombre"] == "Renato Astudillo" and s["perfil_foto_en"]
    assert "token_autorizacion" not in s
    j = (await _get(mundo, "/dashboard/resumen")).json()
    assert j["perfil"]["nombre"] == "Renato Astudillo" and j["perfil"]["foto_v"] and j["perfil"]["id_sesion"] == str(sid)
    assert j["sesiones"][0]["nombre"] == "Renato Astudillo"


async def test_resumen_sin_perfil_no_falla(mundo):
    await _sesion(mundo, estado="desconectado")
    j = (await _get(mundo, "/dashboard/resumen")).json()
    assert j["perfil"]["nombre"] is None and j["perfil"]["foto_v"] is None
    vacio = (await _get(mundo, "/dashboard/resumen", usuario=uuid.UUID("00000000-0000-0000-0000-000000000099"))).json()
    assert vacio["perfil"] == {"id_sesion": None, "nombre": None, "foto_v": None}


# ── Nombre de perfil: orden de los campos ────────────────────────────────────

async def test_cuenta_personal_usa_profileName_porque_fetchProfile_trae_el_numero(mundo):
    """Caso real de production: fetchProfile.name = '+593 96 982 9845' y fetchInstances.profileName = 'RENY'."""
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "+593 96 982 9845", "picture": None})
    _Evo.instancias = (200, [{"instance": {"instanceName": "x", "profileName": "RENY"}}])
    assert await perfil.actualizar_perfil_si_corresponde(sid) is True
    assert (await _leer(mundo, sid)).perfil_nombre == "RENY"
    get = next(l for l in _Evo.llamadas if l[0] == "GET" and l[1].endswith("/instance/fetchInstances"))
    assert get[2]["apikey"] == CLAVE                      # fetchInstances solo acepta la clave global (en el servidor)


async def test_si_fetchInstances_no_trae_nombre_usa_el_campo_alternativo_de_fetchProfile(mundo):
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "Renato Astudillo", "picture": None})
    _Evo.instancias = (200, [{"instance": {"profileName": None}}])
    await perfil.actualizar_perfil_si_corresponde(sid)
    assert (await _leer(mundo, sid)).perfil_nombre == "Renato Astudillo"


async def test_si_fetchInstances_falla_el_nombre_sale_de_fetchProfile(mundo):
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "Renato", "picture": None})
    _Evo.instancias = (500, {"error": "boom"})
    await perfil.actualizar_perfil_si_corresponde(sid)
    assert (await _leer(mundo, sid)).perfil_nombre == "Renato"


async def test_pushName_de_fetchProfile_tambien_sirve(mundo):
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "+593 96 982 9845", "pushName": "Reny", "picture": None})
    await perfil.actualizar_perfil_si_corresponde(sid)
    assert (await _leer(mundo, sid)).perfil_nombre == "Reny"


async def test_ningun_campo_trae_nombre_se_deja_como_esta_y_se_reintenta_en_1_hora(mundo):
    sid = await _sesion(mundo)
    _Evo.perfil = (200, {"name": "+593 96 982 9845", "picture": None})
    _Evo.instancias = (200, [{"instance": {"profileName": " "}}])
    assert await perfil.actualizar_perfil_si_corresponde(sid) is False
    s = await _leer(mundo, sid)
    assert s.perfil_nombre is None                         # sigue "Hola 👋" sin nombre
    ahora = datetime.now(timezone.utc)
    assert not perfil.debe_actualizarse(s, ahora) and perfil.debe_actualizarse(s, ahora + timedelta(hours=1, minutes=1))


async def test_no_pisa_un_nombre_guardado_con_un_numero(mundo):
    sid = await _sesion(mundo)
    await perfil.actualizar_perfil_si_corresponde(sid)      # guarda "Renato Astudillo"
    _Evo.perfil = (200, {"name": "+593 96 982 9845", "picture": None})
    await perfil.actualizar_perfil_si_corresponde(sid, forzar=True)
    assert (await _leer(mundo, sid)).perfil_nombre == "Renato Astudillo"


async def test_dos_eventos_casi_simultaneos_consultan_una_sola_vez(mundo):
    sid = await _sesion(mundo)
    await asyncio.gather(perfil.actualizar_perfil_si_corresponde(sid), perfil.actualizar_perfil_si_corresponde(sid))
    assert len([l for l in _Evo.llamadas if l[0] == "POST"]) == 1
