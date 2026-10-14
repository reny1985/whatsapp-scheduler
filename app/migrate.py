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
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email_verificado BOOLEAN NOT NULL DEFAULT FALSE",
    # Los usuarios existentes (que ya configuraron su cuenta manualmente) se marcan como verificados
    "UPDATE usuarios SET email_verificado = TRUE WHERE password_hash != 'piloto-no-auth' AND email_verificado = FALSE",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS codigo_verificacion VARCHAR(10)",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS codigo_expira_en TIMESTAMPTZ",

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
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS numero_telefono VARCHAR(30)",
    # Perfil de WhatsApp (nombre y foto; la foto se guarda en disco, aquí solo cuándo)
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS perfil_nombre VARCHAR(255)",
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS perfil_foto_en TIMESTAMPTZ",
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS perfil_consultado_en TIMESTAMPTZ",
    # Aviso por correo de desconexión: se envía a lo sumo uno por desconexión (se limpia al reconectar)
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS aviso_desconexion_en TIMESTAMPTZ",
    # Desde cuándo está desconectada (para esperar unos minutos antes de avisar y no avisar por microcortes)
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS desconectada_en TIMESTAMPTZ",

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
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS error_envio TEXT",
    # Cuándo lo tomó el programador (para medir el retraso). Los estados nuevos 'en_cola' y 'vencido'
    # no necesitan migración: estado_envio es VARCHAR sin restricción y los datos existentes no cambian.
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS tomado_en TIMESTAMPTZ",

    # ── Dueño del mensaje: id_usuario directo (ya no depende de la sesión) ───────────────────────────
    # Idempotente y sin borrar datos: (1) columnas nuevas, (2) se rellenan desde la sesión, (3) los que no
    # tienen sesión van al usuario piloto (hoy hay uno solo por base; si no existe el piloto, al más antiguo),
    # (4) NOT NULL solo cuando ya no queda ninguna fila sin dueño, (5) FK + índice.
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS id_usuario UUID",
    # Número con el que se programó: sirve para reasignar al re-vincular el MISMO número
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS numero_origen VARCHAR(30)",
    """
    UPDATE mensajes_programados m
       SET id_usuario = s.id_usuario
      FROM sesiones_whatsapp s
     WHERE m.id_sesion = s.id_sesion AND m.id_usuario IS NULL
    """,
    """
    UPDATE mensajes_programados m
       SET numero_origen = s.numero_telefono
      FROM sesiones_whatsapp s
     WHERE m.id_sesion = s.id_sesion AND m.numero_origen IS NULL AND s.numero_telefono IS NOT NULL
    """,
    """
    UPDATE mensajes_programados
       SET id_usuario = (
             SELECT id_usuario FROM usuarios
              ORDER BY (id_usuario = CAST('00000000-0000-0000-0000-000000000001' AS uuid)) DESC, fecha_registro ASC
              LIMIT 1)
     WHERE id_usuario IS NULL
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM mensajes_programados WHERE id_usuario IS NULL) THEN
            ALTER TABLE mensajes_programados ALTER COLUMN id_usuario SET NOT NULL;
        END IF;
    END $$
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'mensajes_programados_id_usuario_fkey') THEN
            ALTER TABLE mensajes_programados
                ADD CONSTRAINT mensajes_programados_id_usuario_fkey
                FOREIGN KEY (id_usuario) REFERENCES usuarios(id_usuario) ON DELETE CASCADE;
        END IF;
    END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_mensajes_usuario_estado ON mensajes_programados (id_usuario, estado_envio, fecha_hora_disparo)",
    # Borrar una sesión no debe borrar sus mensajes: FK CASCADE → SET NULL (sin tocar datos)
    "ALTER TABLE mensajes_programados ALTER COLUMN id_sesion DROP NOT NULL",
    "ALTER TABLE mensajes_programados DROP CONSTRAINT IF EXISTS mensajes_programados_id_sesion_fkey",
    """
    ALTER TABLE mensajes_programados
        ADD CONSTRAINT mensajes_programados_id_sesion_fkey
        FOREIGN KEY (id_sesion) REFERENCES sesiones_whatsapp(id_sesion) ON DELETE SET NULL
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
