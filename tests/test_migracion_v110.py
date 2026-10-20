"""
v1.1.0 — Migración a cuentas, probada desde el esquema REAL de v1.0.8 (sus migraciones se leen de la etiqueta v1.0.8
en git) con datos como los de production. Comprueba: no cambia ni pierde datos (foto antes/después), es idempotente,
todo queda en la cuenta …0001 con superadministrador, y rollback_v1.1.0.sql deja arrancar v1.0.8 y volver a subir.
Se omite si no hay Postgres local o git con la etiqueta v1.0.8.
"""
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.migrate import run_migrations
from tests.test_sesiones_perdidas import ADMIN_URL

pytestmark = pytest.mark.asyncio(loop_scope="function")

RAIZ = Path(__file__).resolve().parent.parent
FOTO_SQL = (RAIZ / "scripts" / "foto_migracion.sql").read_text()
ROLLBACK_SQL = (RAIZ / "scripts" / "rollback_v1.1.0.sql").read_text()
CUENTA = U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
TABLAS = ("usuarios", "sesiones_whatsapp", "mensajes_programados", "plantillas")


def _pasos_v108() -> list[str]:
    try:
        fuente = subprocess.run(["git", "show", "v1.0.8:app/migrate.py"], cwd=RAIZ, check=True,
                                capture_output=True, text=True).stdout
    except Exception as exc:
        pytest.skip(f"Sin la etiqueta v1.0.8 en git: {exc}")
    espacio: dict = {}
    exec(compile(fuente, "migrate_v108.py", "exec"), espacio)
    return espacio["_STEPS"]


async def _correr_v108(conn: asyncpg.Connection) -> None:
    """Lo que hace v1.0.8 al arrancar (sus migraciones)."""
    for paso in _pasos_v108():
        await conn.execute(paso.strip())


@pytest_asyncio.fixture
async def base_v108():
    nombre = f"test_mig110_{uuid.uuid4().hex[:8]}"
    try:
        admin = await asyncpg.connect(ADMIN_URL, timeout=3)
    except Exception as exc:
        pytest.skip(f"Sin Postgres local para pruebas: {exc}")
    await admin.execute(f'CREATE DATABASE "{nombre}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{nombre}"
    conn = await asyncpg.connect(url)
    engine = create_async_engine(url.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        await _correr_v108(conn)
        yield conn, engine
    finally:
        await conn.close()
        await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
        await admin.close()


async def _datos_como_production(conn) -> dict:
    """1 usuario (el piloto ya configurado), 1 sesión conectada, mensajes de todos los estados (con y sin sesión,
    recurrente con su clon, con adjunto antiguo), 1 plantilla y grupos en caché."""
    s = uuid.uuid4()
    ahora = datetime.now(timezone.utc)
    await conn.execute("UPDATE usuarios SET email='dueno@enviafast.net', password_hash='$2b$12$hash', "
                       "email_verificado=TRUE, estado_suscripcion='activa' WHERE id_usuario=$1", U1)
    await conn.execute("INSERT INTO sesiones_whatsapp (id_sesion, id_usuario, instancia_evolution, token_autorizacion, "
                       "estado_conexion, numero_telefono, fecha_ultima_conexion) VALUES ($1,$2,'wa-prod','TOK',"
                       "'conectado','593969829845',$3)", s, U1, ahora)
    ids = {}
    for etiqueta, sesion, estado, extra in (
        ("enviado", s, "enviado", {}), ("programado", s, "programado", {"url": "/static/uploads/abc_foto.jpg"}),
        ("sin_sesion", None, "programado", {}), ("vencido", s, "vencido", {}), ("recurrente", s, "enviado", {}),
    ):
        mid = uuid.uuid4()
        ids[etiqueta] = mid
        await conn.execute(
            "INSERT INTO mensajes_programados (id_mensaje, id_sesion, id_usuario, numero_origen, id_grupo, "
            "texto_mensaje, tipo_media, url_media, fecha_hora_disparo, estado_envio, recurrencia, enviado_en) "
            "VALUES ($1,$2,$3,'593969829845','120363@g.us','hola',$4,$5,$6,$7,$8,$9)",
            mid, sesion, U1, "image" if extra.get("url") else None, extra.get("url"), ahora + timedelta(hours=1),
            estado, "daily" if etiqueta == "recurrente" else "none", ahora if estado == "enviado" else None)
    await conn.execute(
        "INSERT INTO mensajes_programados (id_sesion, id_usuario, id_grupo, texto_mensaje, fecha_hora_disparo, "
        "estado_envio, recurrencia, id_mensaje_origen) VALUES ($1,$2,'120363@g.us','hola',$3,'programado','daily',$4)",
        s, U1, ahora + timedelta(days=1), ids["recurrente"])
    await conn.execute("INSERT INTO plantillas (id_usuario, nombre, texto_mensaje) VALUES ($1, 'Saludo', 'hola')", U1)
    for i in range(3):
        await conn.execute("INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size) VALUES ($1,$2,$3,5)",
                           f"12036{i}@g.us", s, f"Grupo {i}")
    # Production ya arrancó con v1.0.8 después de crear estos datos (sus migraciones completan, p. ej., numero_origen)
    await _correr_v108(conn)
    return {"sesion": s, **ids}


async def _foto(conn) -> list:
    return [tuple(r) for r in await conn.fetch(FOTO_SQL)]


async def _verificar_v110(conn) -> None:
    for t in TABLAS:
        assert await conn.fetchval(f"SELECT count(*) FROM {t} WHERE id_cuenta IS NULL") == 0, t
        assert await conn.fetchval(f"SELECT count(*) FROM {t} WHERE id_cuenta <> $1", CUENTA) == 0, t
        assert await conn.fetchval("SELECT is_nullable FROM information_schema.columns "
                                   "WHERE table_name=$1 AND column_name='id_cuenta'", t) == "NO", t
    c = await conn.fetchrow("SELECT * FROM cuentas WHERE id_cuenta=$1", CUENTA)
    assert (c["plan"], c["estado"], c["periodo_fin"], c["es_interna"]) == ("enterprise", "activa", None, True)
    assert await conn.fetchval("SELECT count(*) FROM cuentas") == 1
    assert await conn.fetchval("SELECT es_superadmin FROM usuarios WHERE id_usuario=$1", U1) is True
    assert await conn.fetchval("SELECT count(*) FROM usuarios WHERE es_superadmin") == 1
    for nombre in ("fk_mensaje_sesion_misma_cuenta", "uq_sesion_cuenta", "usuarios_id_cuenta_fkey",
                   "mensajes_programados_id_cuenta_fkey"):
        assert await conn.fetchval("SELECT count(*) FROM pg_constraint WHERE conname=$1", nombre) == 1, nombre
    for trigger in ("trg_cuenta_sesiones", "trg_cuenta_mensajes", "trg_cuenta_plantillas"):
        assert await conn.fetchval("SELECT count(*) FROM pg_trigger WHERE tgname=$1", trigger) == 1, trigger
    assert await conn.fetchval("SELECT count(*) FROM sesiones_whatsapp WHERE length(secreto_webhook) <> 64 "
                               "OR secreto_webhook IS NULL") == 0
    assert await conn.fetchval("SELECT count(*) FROM sesiones_whatsapp WHERE webhook_confirmado_en IS NOT NULL") == 0


async def test_migracion_desde_v108_no_cambia_datos_es_idempotente_y_todo_queda_en_la_cuenta_interna(base_v108):
    conn, engine = base_v108
    await _datos_como_production(conn)
    antes = await _foto(conn)
    await run_migrations(engine)
    await run_migrations(engine)                    # idempotente
    assert await _foto(conn) == antes
    await _verificar_v110(conn)


async def test_rollback_deja_arrancar_v108_y_se_puede_volver_a_subir(base_v108):
    conn, engine = base_v108
    datos = await _datos_como_production(conn)
    antes = await _foto(conn)
    await run_migrations(engine)

    await conn.execute(ROLLBACK_SQL)
    await _correr_v108(conn)                         # v1.0.8 vuelve a arrancar sin errores
    # lo que v1.0.8 crea mientras tanto (sin saber de cuentas) queda en la cuenta de su usuario
    nueva = uuid.uuid4()
    await conn.execute("INSERT INTO sesiones_whatsapp (id_sesion, id_usuario, instancia_evolution) VALUES ($1,$2,'wa-x')",
                       nueva, U1)
    await conn.execute("INSERT INTO mensajes_programados (id_sesion, id_usuario, id_grupo, texto_mensaje, "
                       "fecha_hora_disparo) VALUES ($1,$2,'1@g.us','x',now())", nueva, U1)
    assert await conn.fetchval("SELECT id_cuenta FROM sesiones_whatsapp WHERE id_sesion=$1", nueva) == CUENTA

    await run_migrations(engine)                     # volver a subir a v1.1.0
    await _verificar_v110(conn)
    await conn.execute("DELETE FROM mensajes_programados WHERE id_sesion=$1", nueva)
    await conn.execute("DELETE FROM sesiones_whatsapp WHERE id_sesion=$1", nueva)
    assert await _foto(conn) == antes                # los datos originales siguen idénticos
    assert await conn.fetchval("SELECT count(*) FROM mensajes_programados WHERE id_sesion=$1", datos["sesion"]) == 5
