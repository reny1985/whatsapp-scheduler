"""
app/migrate.py
Migraciones idempotentes — se ejecutan en el lifespan de FastAPI.
Usa CREATE TABLE IF NOT EXISTS y ALTER TABLE ... ADD COLUMN IF NOT EXISTS
para que el deploy en Railway arranque sin intervención manual.
"""
import logging
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger("whatsapp_scheduler")

# Cada ítem se ejecuta en orden; todos son idempotentes.
_STEPS = [
    # Extensión UUID
    'CREATE EXTENSION IF NOT EXISTS "uuid-ossp"',

    # ── usuarios ──────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        id_usuario        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        email             VARCHAR(255) UNIQUE NOT NULL,
        password_hash     VARCHAR(255) NOT NULL,
        estado_suscripcion VARCHAR(50) NOT NULL DEFAULT 'prueba',
        fecha_registro    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS rol VARCHAR(20) NOT NULL DEFAULT 'admin'",

    # ── sesiones_whatsapp ─────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS sesiones_whatsapp (
        id_sesion             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        id_usuario            UUID NOT NULL REFERENCES usuarios(id_usuario) ON DELETE CASCADE,
        nombre_sesion         VARCHAR(100),
        instancia_evolution   VARCHAR(100) UNIQUE NOT NULL,
        token_autorizacion    VARCHAR(255),
        estado_conexion       VARCHAR(50) NOT NULL DEFAULT 'qr_pendiente',
        fecha_creacion        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        fecha_ultima_conexion TIMESTAMP WITH TIME ZONE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_sesiones_usuario ON sesiones_whatsapp (id_usuario)",

    # ── mensajes_programados ──────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS mensajes_programados (
        id_mensaje          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        id_sesion           UUID NOT NULL REFERENCES sesiones_whatsapp(id_sesion) ON DELETE CASCADE,
        id_grupo            VARCHAR(255) NOT NULL,
        texto_mensaje       TEXT,
        tipo_media          VARCHAR(50),
        url_media           TEXT,
        fecha_hora_disparo  TIMESTAMP WITH TIME ZONE NOT NULL,
        estado_envio        VARCHAR(50) NOT NULL DEFAULT 'programado',
        fecha_creacion      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """,
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS recurrencia VARCHAR(20) NOT NULL DEFAULT 'none'",
    """
    ALTER TABLE mensajes_programados
        ADD COLUMN IF NOT EXISTS id_mensaje_origen UUID
        REFERENCES mensajes_programados(id_mensaje) ON DELETE SET NULL
    """,
    "CREATE INDEX IF NOT EXISTS idx_mensajes_fecha_estado ON mensajes_programados (fecha_hora_disparo, estado_envio)",

    # ── grupos_whatsapp (caché) ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS grupos_whatsapp (
        id_grupo      VARCHAR(255) NOT NULL,
        id_sesion     UUID        NOT NULL REFERENCES sesiones_whatsapp(id_sesion) ON DELETE CASCADE,
        subject       VARCHAR(255),
        size          INTEGER,
        actualizado_en TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (id_grupo, id_sesion)
    )
    """,

    # ── plantillas ────────────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS plantillas (
        id_plantilla  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        id_usuario    UUID NOT NULL REFERENCES usuarios(id_usuario) ON DELETE CASCADE,
        nombre        VARCHAR(100) NOT NULL,
        texto_mensaje TEXT,
        tipo_media    VARCHAR(50),
        url_media     TEXT,
        fecha_creacion TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
    )
    """,

    # ── usuario piloto (solo si la tabla está vacía) ──────────────────────
    """
    INSERT INTO usuarios (id_usuario, email, password_hash, rol)
    VALUES (
        '00000000-0000-0000-0000-000000000001',
        'admin@localhost',
        'piloto-no-auth',
        'admin'
    )
    ON CONFLICT (id_usuario) DO NOTHING
    """,
]


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for step in _STEPS:
            await conn.execute(text(step.strip()))
    logger.info("Migraciones aplicadas correctamente")
