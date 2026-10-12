"""
Correo de aviso cuando una sesión pasa de conectada a desconectada con mensajes pendientes, aviso del Inicio
y conteo de pendientes antes de eliminar un número. Postgres desechable; Resend simulado (sin red real).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import MensajeProgramado, SesionWhatsApp
from app.routers import sesiones, webhook
from app.services import aviso_desconexion, correo, reasignar
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def mundo(Session, monkeypatch):
    enviar = AsyncMock(return_value="id-correo")
    monkeypatch.setattr(correo, "enviar", enviar)
    monkeypatch.setattr(aviso_desconexion, "AsyncSessionLocal", Session)
    monkeypatch.setattr(aviso_desconexion, "GRACIA_SEG", 0)               # sin espera en las pruebas
    monkeypatch.setattr(reasignar, "AsyncSessionLocal", Session)
    monkeypatch.setattr(webhook, "AsyncSessionLocal", Session)
    monkeypatch.setattr(webhook, "_fetch_instancia_info", AsyncMock(return_value={"apikey": "k", "numero": "593961409996"}))
    monkeypatch.setattr(webhook.perfil, "actualizar_perfil_si_corresponde", AsyncMock(return_value=True))
    yield Session, enviar
    app.dependency_overrides.clear()


async def _preparar(maker, pendientes=2, estado="conectado", verificado=True, email="cliente@ejemplo.com"):
    async with maker() as db:
        await db.execute(text("UPDATE usuarios SET email=:e, email_verificado=:v WHERE id_usuario=:u"),
                         {"e": email, "v": verificado, "u": U1})
        sid = uuid.uuid4()
        db.add(SesionWhatsApp(id_sesion=sid, id_usuario=U1, instancia_evolution=f"wa-{sid.hex[:8]}",
                              token_autorizacion="t", estado_conexion=estado, numero_telefono="593961409996"))
        await db.flush()
        for h in range(pendientes):
            db.add(MensajeProgramado(id_usuario=U1, id_mensaje=uuid.uuid4(), id_sesion=sid, id_grupo="120363@g.us", texto_mensaje="x",
                                     fecha_hora_disparo=datetime.now(timezone.utc) + timedelta(hours=2 + h)))
        await db.commit()
        instancia = (await db.get(SesionWhatsApp, sid)).instancia_evolution
    return sid, instancia


async def _marca(maker, sid):
    async with maker() as db:
        s = await db.get(SesionWhatsApp, sid)
        await db.refresh(s)
        return s.aviso_desconexion_en


async def _tarea_pendiente():
    import asyncio
    await asyncio.sleep(0.05)       # deja correr las tareas en segundo plano del webhook


# ── Correo: una sola vez por desconexión ─────────────────────────────────────

async def test_correo_una_sola_vez_por_desconexion_y_se_reinicia_al_reconectar(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker)
    await webhook._sincronizar_conexion(inst, "close")
    await _tarea_pendiente()
    assert enviar.await_count == 1 and await _marca(maker, sid) is not None
    await webhook._sincronizar_conexion(inst, "close")             # el mismo corte, otro evento
    await _tarea_pendiente()
    assert enviar.await_count == 1                                  # NO se repite
    await webhook._sincronizar_conexion(inst, "open")              # vuelve a conectarse
    await _tarea_pendiente()
    assert await _marca(maker, sid) is None
    await webhook._sincronizar_conexion(inst, "close")             # nueva desconexión
    await _tarea_pendiente()
    assert enviar.await_count == 2                                  # esta sí avisa de nuevo


async def test_sin_pendientes_no_hay_correo(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker, pendientes=0)
    await webhook._sincronizar_conexion(inst, "close")
    await _tarea_pendiente()
    enviar.assert_not_awaited()
    assert await _marca(maker, sid) is None


async def test_mensajes_ya_enviados_o_vencidos_no_cuentan_como_pendientes(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker, pendientes=0)
    async with maker() as db:
        for e in ("enviado", "vencido", "fallido", "cancelado"):
            db.add(MensajeProgramado(id_usuario=U1, id_mensaje=uuid.uuid4(), id_sesion=sid, id_grupo="1@g.us", texto_mensaje="x",
                                     fecha_hora_disparo=datetime.now(timezone.utc), estado_envio=e))
        await db.commit()
    assert await aviso_desconexion.avisar_desconexion(sid) is False
    enviar.assert_not_awaited()


async def test_solo_avisa_si_pasa_de_conectada_a_desconectada(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker, estado="desconectado")      # ya estaba desconectada
    await webhook._sincronizar_conexion(inst, "close")
    await _tarea_pendiente()
    enviar.assert_not_awaited()


async def test_usuario_sin_correo_verificado_no_recibe_aviso(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker, verificado=False)
    await webhook._sincronizar_conexion(inst, "close")
    await _tarea_pendiente()
    enviar.assert_not_awaited()


async def test_contenido_del_correo(mundo):
    maker, enviar = mundo
    sid, inst = await _preparar(maker, pendientes=3)
    await webhook._sincronizar_conexion(inst, "close")
    await _tarea_pendiente()
    para, asunto, html, texto = enviar.await_args.args
    assert para == "cliente@ejemplo.com"
    assert asunto == "[Enviafast] Tu WhatsApp se desconectó"
    assert "+593961409996" in html and "3 mensajes pendientes" in html
    assert ("a. m." in html or "p. m." in html) and "(hora de Ecuador)" in html          # hora del próximo
    assert 'href="https://enviafast.net"' in html and "Volver a vincular" in html
    assert "Enviafast" in html and "#25D366" in html and "LOGO" in html                    # marca + lugar del logo
    assert "https://enviafast.net" in texto and "+593961409996" in texto


async def test_si_el_correo_falla_no_rompe_deja_log_y_permite_reintentar(mundo, caplog):
    maker, enviar = mundo
    enviar.side_effect = correo.CorreoError("HTTP 403 dominio no verificado")
    sid, inst = await _preparar(maker)
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        await webhook._sincronizar_conexion(inst, "close")        # no lanza
        await _tarea_pendiente()
    assert "no se pudo enviar el correo" in caplog.text and "HTTP 403" in caplog.text
    assert await _marca(maker, sid) is None                        # se libera: un próximo evento puede reintentar
    enviar.side_effect = None
    assert await aviso_desconexion.avisar_desconexion(sid) is True


async def test_verificar_tambien_avisa_una_vez(mundo, monkeypatch):
    maker, enviar = mundo
    sid, inst = await _preparar(maker)
    monkeypatch.setattr(sesiones, "_evo_get", AsyncMock(return_value={"instance": {"state": "close"}}))
    monkeypatch.setattr(sesiones.aviso_desconexion, "AsyncSessionLocal", maker)
    from app.auth import require_admin
    app.dependency_overrides[require_admin] = lambda: MagicMock(id_usuario=U1)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.get(f"/sesiones/{sid}/verificar")
            await _tarea_pendiente()
            r2 = await c.get(f"/sesiones/{sid}/verificar")
            await _tarea_pendiente()
    assert r1.status_code == r2.status_code == 200 and enviar.await_count == 1


async def test_envio_real_a_resend_usa_el_remitente_actual(monkeypatch):
    import httpx

    llamadas = []

    class Cli:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, headers=None):
            llamadas.append((url, json, headers))
            return httpx.Response(200, json={"id": "abc"}, request=httpx.Request("POST", url))

    from app.services import correo as c
    monkeypatch.setattr(c.httpx, "AsyncClient", Cli)
    monkeypatch.setattr(c, "RESEND_API_KEY", "re_clave_secreta")
    monkeypatch.setattr(c, "RESEND_FROM", "Enviafast <no-reply@enviafast.net>")
    assert await c.enviar("a@b.com", "asunto", "<b>x</b>", "x") == "abc"
    url, cuerpo, headers = llamadas[0]
    assert url == "https://api.resend.com/emails" and cuerpo["from"] == "Enviafast <no-reply@enviafast.net>"
    assert cuerpo["to"] == ["a@b.com"] and headers["Authorization"] == "Bearer re_clave_secreta"


# ── Aviso del Inicio y conteo antes de eliminar ──────────────────────────────

async def _resumen(maker):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=U1)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return (await c.get("/dashboard/resumen")).json()


async def test_inicio_avisa_si_esta_desconectado_y_hay_pendientes(mundo):
    maker, _ = mundo
    await _preparar(maker, pendientes=4, estado="desconectado")
    j = await _resumen(maker)
    assert j["aviso_desconexion"] == {"pendientes": 4}
    assert j["tarjetas"]["pendientes"] == 4


async def test_inicio_sin_aviso_si_esta_conectado_o_no_hay_pendientes(mundo):
    maker, _ = mundo
    sid, _i = await _preparar(maker, pendientes=4, estado="conectado")
    assert (await _resumen(maker))["aviso_desconexion"] is None
    async with maker() as db:
        await db.execute(text("UPDATE sesiones_whatsapp SET estado_conexion='desconectado'"))
        await db.execute(text("UPDATE mensajes_programados SET estado_envio='enviado'"))
        await db.commit()
    assert (await _resumen(maker))["aviso_desconexion"] is None


async def test_inicio_cuenta_los_vencidos_y_las_en_cola_como_pendientes(mundo):
    maker, _ = mundo
    sid, _i = await _preparar(maker, pendientes=1)
    async with maker() as db:
        for e in ("vencido", "vencido", "en_cola"):
            db.add(MensajeProgramado(id_usuario=U1, id_mensaje=uuid.uuid4(), id_sesion=sid, id_grupo="1@g.us", texto_mensaje="x",
                                     fecha_hora_disparo=datetime.now(timezone.utc) - timedelta(hours=1), estado_envio=e))
        await db.commit()
    t = (await _resumen(maker))["tarjetas"]
    assert t["vencidos"] == 2 and t["pendientes"] == 2          # 1 programado + 1 en cola


async def test_conteo_de_pendientes_antes_de_eliminar_un_numero(mundo):
    maker, _ = mundo
    sid, _i = await _preparar(maker, pendientes=3)
    async with maker() as db:                                     # los ya enviados no cuentan
        db.add(MensajeProgramado(id_usuario=U1, id_mensaje=uuid.uuid4(), id_sesion=sid, id_grupo="1@g.us", texto_mensaje="x",
                                 fecha_hora_disparo=datetime.now(timezone.utc), estado_envio="enviado"))
        await db.commit()
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=U1)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(f"/sesiones/{sid}/pendientes")
    assert r.status_code == 200 and r.json() == {"pendientes": 3}
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=uuid.uuid4())     # otro usuario
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            assert (await c.get(f"/sesiones/{sid}/pendientes")).status_code == 404
