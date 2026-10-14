"""
Pruebas contra un Postgres real y desechable (base temporal que se crea y se borra):
limpieza de sesiones huérfanas, estado 'connecting' y FK mensajes → SET NULL.
Se omiten si no hay un Postgres local accesible. Nunca tocan la base de desarrollo.
Variable opcional: TEST_PG_ADMIN_URL (por defecto postgresql://macbook@localhost:5432/postgres).
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import scheduler
from app.migrate import run_migrations
from app.models import MensajeProgramado, SesionWhatsApp
from app.routers import sesiones, webhook

pytestmark = pytest.mark.asyncio(loop_scope="function")

ADMIN_URL = os.getenv("TEST_PG_ADMIN_URL", "postgresql://macbook@localhost:5432/postgres")
USUARIO = uuid.UUID("00000000-0000-0000-0000-000000000001")  # lo crea la migración


@pytest_asyncio.fixture
async def Session(monkeypatch):
    nombre = f"test_sesiones_{uuid.uuid4().hex[:8]}"
    try:
        admin = await asyncpg.connect(ADMIN_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"Sin Postgres local para pruebas: {exc}")
    await admin.execute(f'CREATE DATABASE "{nombre}"')
    base = ADMIN_URL.rsplit("/", 1)[0]
    engine = create_async_engine(base.replace("postgresql://", "postgresql+asyncpg://") + f"/{nombre}")
    try:
        await run_migrations(engine)
        await run_migrations(engine)  # idempotente
        maker = async_sessionmaker(engine, expire_on_commit=False)
        monkeypatch.setattr(scheduler, "AsyncSessionFactory", maker)
        monkeypatch.setattr(webhook, "AsyncSessionLocal", maker)
        yield maker
    finally:
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
        await admin.close()


async def _crear_sesion(maker, *, numero=None, conexion=None, creada_hace=timedelta(minutes=30),
                        estado="qr_pendiente"):
    sid = uuid.uuid4()
    async with maker() as db:
        db.add(SesionWhatsApp(
            id_sesion=sid, id_usuario=USUARIO, instancia_evolution=f"wa-{sid.hex[:8]}",
            token_autorizacion="tok", estado_conexion=estado, numero_telefono=numero,
            fecha_ultima_conexion=conexion,
            fecha_creacion=datetime.now(timezone.utc) - creada_hace,
        ))
        await db.commit()
    return sid


async def _existe(maker, sid) -> bool:
    async with maker() as db:
        return await db.get(SesionWhatsApp, sid) is not None


@pytest.fixture
def sin_evolution():
    with patch.object(scheduler.httpx, "AsyncClient") as c:
        c.return_value.__aenter__.return_value.delete = AsyncMock()
        yield


async def test_sesion_conectada_con_connecting_no_se_borra(Session, sin_evolution):
    sid = await _crear_sesion(Session, numero="593969829845", estado="conectado",
                              conexion=datetime.now(timezone.utc) - timedelta(hours=2))
    await webhook._sincronizar_conexion(
        (await _instancia(Session, sid)), "connecting")
    async with Session() as db:
        s = await db.get(SesionWhatsApp, sid)
        assert s.estado_conexion == "desconectado"  # nunca qr_pendiente
    await scheduler._limpiar_sesiones_huerfanas()
    assert await _existe(Session, sid)


async def _instancia(maker, sid):
    async with maker() as db:
        return (await db.get(SesionWhatsApp, sid)).instancia_evolution


async def test_sesion_con_numero_pero_sin_ultima_conexion_no_se_borra(Session, sin_evolution):
    sid = await _crear_sesion(Session, numero="593969829845")
    await scheduler._limpiar_sesiones_huerfanas()
    assert await _existe(Session, sid)


async def test_sesion_con_ultima_conexion_pero_sin_numero_no_se_borra(Session, sin_evolution):
    sid = await _crear_sesion(Session, conexion=datetime.now(timezone.utc) - timedelta(days=1))
    await scheduler._limpiar_sesiones_huerfanas()
    assert await _existe(Session, sid)


async def test_sesion_nueva_sin_numero_de_hace_mas_de_10_min_se_borra(Session, sin_evolution):
    vieja = await _crear_sesion(Session, creada_hace=timedelta(minutes=11))
    reciente = await _crear_sesion(Session, creada_hace=timedelta(minutes=3))
    await scheduler._limpiar_sesiones_huerfanas()
    assert not await _existe(Session, vieja)
    assert await _existe(Session, reciente)


async def test_borrar_sesion_deja_sus_mensajes(Session):
    sid = await _crear_sesion(Session, numero="593969829845", estado="conectado")
    mid = uuid.uuid4()
    async with Session() as db:
        db.add(MensajeProgramado(
            id_usuario=USUARIO, id_mensaje=mid, id_sesion=sid, id_grupo="120363@g.us", texto_mensaje="hola",
            fecha_hora_disparo=datetime.now(timezone.utc) + timedelta(days=1)))
        await db.commit()
    async with Session() as db:  # igual que DELETE /sesiones/{id}
        await db.delete(await db.get(SesionWhatsApp, sid))
        await db.commit()
    async with Session() as db:
        m = await db.get(MensajeProgramado, mid)
        assert m is not None and m.id_sesion is None and m.estado_envio == "programado"


async def test_scheduler_ignora_mensajes_sin_sesion(Session):
    mid = uuid.uuid4()
    async with Session() as db:
        db.add(MensajeProgramado(
            id_usuario=USUARIO, id_mensaje=mid, id_sesion=None, id_grupo="120363@g.us", texto_mensaje="huérfano",
            fecha_hora_disparo=datetime.now(timezone.utc) - timedelta(minutes=1)))   # dentro de la tolerancia
        await db.commit()
    with patch.object(scheduler, "_enviar_mensaje", new=AsyncMock()) as enviar:
        await scheduler._procesar_mensajes_pendientes()
    enviar.assert_not_called()
    async with Session() as db:
        assert (await db.get(MensajeProgramado, mid)).estado_envio == "programado"


async def test_migracion_deja_fk_en_set_null_y_columna_nullable(Session):
    async with Session() as db:
        fk = (await db.execute(text(
            "SELECT confdeltype FROM pg_constraint WHERE conname='mensajes_programados_id_sesion_fkey'"
        ))).scalar_one()
        nulo = (await db.execute(text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name='mensajes_programados' AND column_name='id_sesion'"
        ))).scalar_one()
    assert fk in ("n", b"n") and nulo == "YES"
