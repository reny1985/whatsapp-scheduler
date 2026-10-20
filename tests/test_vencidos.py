"""
Tolerancia de retraso del programador y mensajes vencidos. Postgres desechable (se omiten si no hay uno local).

Regla: el retraso se mide al TOMAR el mensaje (pasa a 'en_cola'), no cuando sale. Lo que entró a la cola a
tiempo se envía siempre; solo se marca 'vencido' lo que no pudo tomarse a tiempo (con su motivo).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import func, select, text

from app import scheduler
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.migrate import run_migrations
from app.models import MensajeProgramado, SesionWhatsApp
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _hace(**kw):
    return datetime.now(timezone.utc) - timedelta(**kw)


async def _sesion(maker, estado="conectado", token="tok"):
    sid = uuid.uuid4()
    async with maker() as db:
        db.add(SesionWhatsApp(id_sesion=sid, id_usuario=U1, instancia_evolution=f"wa-{sid.hex[:8]}",
                              token_autorizacion=token, estado_conexion=estado, numero_telefono="593961409996"))
        await db.commit()
    return sid


async def _msg(maker, sid, disparo, estado="programado", rec="none", destino="120363@g.us"):
    mid = uuid.uuid4()
    async with maker() as db:
        db.add(MensajeProgramado(id_usuario=U1, id_mensaje=mid, id_sesion=sid, id_grupo=destino, texto_mensaje="hola",
                                 fecha_hora_disparo=disparo, estado_envio=estado, recurrencia=rec))
        await db.commit()
    return mid


async def _estado(maker, mid):
    async with maker() as db:
        m = await db.get(MensajeProgramado, mid)
        await db.refresh(m)
        return m


async def _tick(enviar=None):
    enviar = enviar or AsyncMock(return_value={"key": {"id": "X"}})
    with patch.object(scheduler, "_enviar_mensaje", new=enviar):
        await scheduler._procesar_mensajes_pendientes(esperar=True)
    return enviar


async def test_la_tolerancia_es_una_sola_constante_de_5_minutos():
    assert scheduler.TOLERANCIA_RETRASO_MIN == 5


# ── Cola: lo que entró a tiempo se envía siempre ─────────────────────────────

async def test_30_mensajes_a_la_misma_hora_ninguno_vence_aunque_la_cola_dure_mas_de_5_min(Session, monkeypatch):
    sid = await _sesion(Session)
    hora = _hace(seconds=20)                      # todos a la misma hora, tomados con 20 s de retraso
    ids = [await _msg(Session, sid, hora) for _ in range(30)]
    reloj = {"simulado": 0}

    class RelojAdelantado(datetime):               # el reloj del programador avanza mientras envía
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(seconds=reloj["simulado"])

    monkeypatch.setattr(scheduler, "datetime", RelojAdelantado)

    async def envio_lento(**kw):                   # cada envío "tarda" 20 s simulados → la cola dura 10 min
        reloj["simulado"] += 20
        return {"key": {"id": "X"}}

    enviar = await _tick(AsyncMock(side_effect=envio_lento))
    assert enviar.await_count == 30 and reloj["simulado"] == 600       # la cola duró 10 min > tolerancia
    assert [(await _estado(Session, i)).estado_envio for i in ids] == ["enviado"] * 30
    async with Session() as db:
        n = (await db.execute(select(func.count()).select_from(MensajeProgramado)
                              .where(MensajeProgramado.estado_envio == "vencido"))).scalar_one()
    assert n == 0


async def test_lo_que_quedo_en_cola_de_un_ciclo_anterior_se_envia_aunque_haya_pasado_mucho(Session):
    """El servidor cayó con mensajes ya tomados (en_cola); al volver se envían, no se marcan vencidos."""
    sid = await _sesion(Session)
    ids = [await _msg(Session, sid, _hace(minutes=15), estado="en_cola") for _ in range(5)]
    await _tick()
    assert [(await _estado(Session, i)).estado_envio for i in ids] == ["enviado"] * 5


async def test_se_registra_cuando_se_tomo_el_mensaje(Session):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, _hace(seconds=30))
    enviar = AsyncMock(return_value={})
    with patch.object(scheduler, "_enviar_mensaje", new=enviar), \
         patch.object(scheduler, "_asegurar_trabajadores", new=AsyncMock()):          # solo la fase de toma
        await scheduler._procesar_mensajes_pendientes(esperar=True)
    m = await _estado(Session, mid)
    assert m.estado_envio == "en_cola" and m.tomado_en is not None


# ── Vencidos ─────────────────────────────────────────────────────────────────

async def test_tomado_a_los_4_minutos_se_envia(Session):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, _hace(minutes=4))
    enviar = await _tick()
    assert enviar.await_count == 1 and (await _estado(Session, mid)).estado_envio == "enviado"


async def test_tomado_con_6_minutos_por_desconexion_queda_vencido_con_motivo(Session):
    sid = await _sesion(Session, estado="desconectado")
    mid = await _msg(Session, sid, _hace(minutes=6))
    enviar = await _tick()
    m = await _estado(Session, mid)
    enviar.assert_not_called()
    assert m.estado_envio == "vencido"
    assert "desconectado" in m.error_envio and "6 min" in m.error_envio


async def test_desconectado_pero_dentro_de_la_tolerancia_espera_y_se_envia_al_reconectar(Session):
    sid = await _sesion(Session, estado="desconectado")
    mid = await _msg(Session, sid, _hace(minutes=4))
    enviar = await _tick()
    assert enviar.await_count == 0 and (await _estado(Session, mid)).estado_envio == "programado"   # espera
    async with Session() as db:                                                                     # reconecta
        (await db.get(SesionWhatsApp, sid)).estado_conexion = "conectado"
        await db.commit()
    await _tick(enviar)
    assert (await _estado(Session, mid)).estado_envio == "enviado"


async def test_sesion_eliminada_pasada_la_tolerancia_queda_vencido(Session):
    mid = await _msg(Session, None, _hace(minutes=7))
    enviar = await _tick()
    m = await _estado(Session, mid)
    enviar.assert_not_called()
    assert m.estado_envio == "vencido" and "eliminada" in m.error_envio


async def test_servidor_caido_con_sesion_conectada_pasada_la_tolerancia_queda_vencido(Session):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, _hace(hours=3))          # el servidor estuvo apagado 3 horas
    enviar = await _tick()
    m = await _estado(Session, mid)
    enviar.assert_not_called()
    assert m.estado_envio == "vencido" and "servidor" in m.error_envio


async def test_la_tolerancia_se_cambia_en_un_solo_lugar(Session, monkeypatch):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, _hace(minutes=6))
    monkeypatch.setattr(scheduler, "TOLERANCIA_RETRASO_MIN", 10)
    await _tick()
    assert (await _estado(Session, mid)).estado_envio == "enviado"     # con 10 min, 6 min de retraso aún se envía


async def test_mensaje_futuro_no_se_toca(Session):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, datetime.now(timezone.utc) + timedelta(hours=2))
    enviar = await _tick()
    enviar.assert_not_called()
    assert (await _estado(Session, mid)).estado_envio == "programado"


async def test_recurrente_vencido_no_corta_la_serie(Session):
    sid = await _sesion(Session, estado="desconectado")
    mid = await _msg(Session, sid, _hace(days=3, hours=1), rec="daily")     # perdió 3 días de envíos
    await _tick()
    assert (await _estado(Session, mid)).estado_envio == "vencido"
    async with Session() as db:
        hijos = (await db.execute(select(MensajeProgramado).where(MensajeProgramado.id_mensaje_origen == mid))).scalars().all()
    assert len(hijos) == 1 and hijos[0].estado_envio == "programado"
    assert hijos[0].fecha_hora_disparo > datetime.now(timezone.utc)          # la próxima ocurrencia FUTURA


async def test_fallo_de_evolution_sigue_siendo_fallido_no_vencido(Session):
    sid = await _sesion(Session)
    mid = await _msg(Session, sid, _hace(seconds=10))
    await _tick(AsyncMock(side_effect=RuntimeError("Evolution API 500")))
    m = await _estado(Session, mid)
    assert m.estado_envio == "fallido" and "Evolution" in m.error_envio


# ── "Enviar ahora" (forzar) ──────────────────────────────────────────────────

async def test_enviar_ahora_no_se_marca_vencido(Session):
    sid = await _sesion(Session)
    app.dependency_overrides[require_admin] = lambda: MagicMock(id_usuario=U1, id_cuenta=U1)
    async with Session() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/mensajes/", json={
                "id_sesion": str(sid), "id_grupo": "120363@g.us", "texto_mensaje": "ya",
                "fecha_hora_disparo": (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(), "forzar": True})
    app.dependency_overrides.clear()
    assert r.status_code == 201
    mid = uuid.UUID(r.json()["id_mensaje"])
    disparo = datetime.fromisoformat(r.json()["fecha_hora_disparo"])
    assert abs((datetime.now(timezone.utc) - disparo).total_seconds()) < 30          # quedó "ahora", no hace 20 min
    enviar = await _tick()
    assert enviar.await_count == 1 and (await _estado(Session, mid)).estado_envio == "enviado"


# ── Cancelar / eliminar con los estados nuevos ───────────────────────────────

async def test_no_se_cancela_en_cola_pero_si_se_elimina_un_vencido(Session):
    sid = await _sesion(Session)
    en_cola = await _msg(Session, sid, _hace(minutes=1), estado="en_cola")
    vencido = await _msg(Session, sid, _hace(hours=1), estado="vencido")
    app.dependency_overrides[require_admin] = lambda: MagicMock(id_usuario=U1, id_cuenta=U1)
    async with Session() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r1 = await c.patch(f"/mensajes/{en_cola}/cancelar")
            r2 = await c.delete(f"/mensajes/{vencido}")
            r3 = await c.delete(f"/mensajes/{en_cola}")
    app.dependency_overrides.clear()
    assert r1.status_code == 409 and r3.status_code == 409 and r2.status_code == 204


# ── Migración ────────────────────────────────────────────────────────────────

async def test_migracion_agrega_columnas_sin_tocar_datos_y_acepta_los_estados_nuevos(Session):
    sid = await _sesion(Session)
    viejos = [await _msg(Session, sid, _hace(days=i), estado=e)
              for i, e in enumerate(["programado", "enviado", "fallido", "cancelado"])]
    engine = Session.kw["bind"]
    async with engine.begin() as conn:      # deja la BD como estaba ANTES de esta versión
        await conn.execute(text("ALTER TABLE mensajes_programados DROP COLUMN tomado_en"))
        await conn.execute(text("ALTER TABLE sesiones_whatsapp DROP COLUMN aviso_desconexion_en"))
    async with Session() as db:
        antes = (await db.execute(text(
            "SELECT id_mensaje, estado_envio, fecha_hora_disparo, texto_mensaje, error_envio FROM mensajes_programados ORDER BY id_mensaje"))).all()
    await run_migrations(engine)
    await run_migrations(engine)            # idempotente
    async with Session() as db:
        despues = (await db.execute(text(
            "SELECT id_mensaje, estado_envio, fecha_hora_disparo, texto_mensaje, error_envio FROM mensajes_programados ORDER BY id_mensaje"))).all()
        tomado = (await db.execute(text("SELECT tomado_en FROM mensajes_programados"))).scalars().all()
        aviso = (await db.execute(text("SELECT aviso_desconexion_en FROM sesiones_whatsapp"))).scalars().all()
    assert antes == despues and len(despues) == 4                     # datos intactos
    assert all(t is None for t in tomado) and all(a is None for a in aviso)
    nuevo = await _msg(Session, sid, _hace(minutes=1), estado="en_cola")
    otro = await _msg(Session, sid, _hace(minutes=1), estado="vencido")
    assert (await _estado(Session, nuevo)).estado_envio == "en_cola" and (await _estado(Session, otro)).estado_envio == "vencido"
