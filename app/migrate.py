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
    # Cuándo salió realmente cada mensaje (semáforo y estimaciones). NULL en los anteriores a esta versión.
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS enviado_en TIMESTAMPTZ",
    # Latido del programador: permite saber, tras un reinicio, cuánto estuvo caído el servidor
    """
    CREATE TABLE IF NOT EXISTS estado_programador (
        clave VARCHAR(50) PRIMARY KEY,
        valor TIMESTAMPTZ NOT NULL
    )
    """,
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

    # ── v1.1.0: cuentas (el cliente) ──────────────────────────────────────
    # Todo lo existente pasa a la cuenta interna …0001 (Enterprise, sin vencimiento) sin borrar ni cambiar datos.
    # Va ANTES del usuario piloto: ese INSERT ya tiene que llevar su cuenta (NOT NULL se valida antes del ON CONFLICT).
    """
    CREATE TABLE IF NOT EXISTS cuentas (
        id_cuenta              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        nombre                 VARCHAR(150),
        plan                   VARCHAR(20),
        estado                 VARCHAR(20) NOT NULL DEFAULT 'prueba',
        prueba_fin             TIMESTAMPTZ,
        periodo_fin            TIMESTAMPTZ,
        mensajes_prueba_usados INTEGER NOT NULL DEFAULT 0,
        es_interna             BOOLEAN NOT NULL DEFAULT FALSE,
        motivo_suspension      TEXT,
        creada_en              TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    INSERT INTO cuentas (id_cuenta, nombre, plan, estado, periodo_fin, es_interna)
    VALUES ('00000000-0000-0000-0000-000000000001', 'Enviafast', 'enterprise', 'activa', NULL, TRUE)
    ON CONFLICT (id_cuenta) DO NOTHING
    """,
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS id_cuenta UUID",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS es_superadmin BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS version_token INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS id_cuenta UUID",
    "ALTER TABLE mensajes_programados ADD COLUMN IF NOT EXISTS id_cuenta UUID",
    "ALTER TABLE plantillas ADD COLUMN IF NOT EXISTS id_cuenta UUID",
    # Webhook con secreto por sesión (la URL vieja sigue sirviendo hasta confirmar la nueva en Evolution)
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS secreto_webhook VARCHAR(64)",
    "ALTER TABLE sesiones_whatsapp ADD COLUMN IF NOT EXISTS webhook_confirmado_en TIMESTAMPTZ",

    # ── usuario piloto (solo si la tabla está vacía) ──────────────────────
    """
    INSERT INTO usuarios (id_usuario, email, password_hash, rol, id_cuenta)
    VALUES (
        '00000000-0000-0000-0000-000000000001',
        'admin@localhost',
        'piloto-no-auth',
        'admin',
        '00000000-0000-0000-0000-000000000001'
    )
    ON CONFLICT (id_usuario) DO NOTHING
    """,

    # ── v1.1.0: cada fila a su cuenta ─────────────────────────────────────
    # (1) usuarios sin cuenta → cuenta interna; (2) sesiones, mensajes y plantillas → la cuenta de su usuario;
    # (3) NOT NULL solo si no quedó ninguna fila sin cuenta; (4) FK, índices y candados de la BD.
    "UPDATE usuarios SET id_cuenta = '00000000-0000-0000-0000-000000000001' WHERE id_cuenta IS NULL",
    # Superadministrador: SOLO el usuario …0001 (por id, nunca por correo)
    "UPDATE usuarios SET es_superadmin = TRUE WHERE id_usuario = '00000000-0000-0000-0000-000000000001' AND NOT es_superadmin",
    """
    UPDATE sesiones_whatsapp s SET id_cuenta = u.id_cuenta
      FROM usuarios u WHERE s.id_usuario = u.id_usuario AND s.id_cuenta IS NULL
    """,
    """
    UPDATE mensajes_programados m SET id_cuenta = u.id_cuenta
      FROM usuarios u WHERE m.id_usuario = u.id_usuario AND m.id_cuenta IS NULL
    """,
    """
    UPDATE plantillas p SET id_cuenta = u.id_cuenta
      FROM usuarios u WHERE p.id_usuario = u.id_usuario AND p.id_cuenta IS NULL
    """,
    """
    DO $$
    DECLARE t text;
    BEGIN
        FOREACH t IN ARRAY ARRAY['usuarios', 'sesiones_whatsapp', 'mensajes_programados', 'plantillas'] LOOP
            EXECUTE format('SELECT 1 FROM %I WHERE id_cuenta IS NULL LIMIT 1', t);
            IF NOT FOUND THEN
                EXECUTE format('ALTER TABLE %I ALTER COLUMN id_cuenta SET NOT NULL', t);
            ELSE
                RAISE WARNING 'v1.1.0: % tiene filas sin id_cuenta; NOT NULL no se aplicó', t;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = t || '_id_cuenta_fkey') THEN
                EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I FOREIGN KEY (id_cuenta) REFERENCES cuentas(id_cuenta)',
                               t, t || '_id_cuenta_fkey');
            END IF;
        END LOOP;
    END $$
    """,
    "CREATE INDEX IF NOT EXISTS idx_usuarios_cuenta ON usuarios (id_cuenta)",
    "CREATE INDEX IF NOT EXISTS idx_sesiones_cuenta ON sesiones_whatsapp (id_cuenta)",
    "CREATE INDEX IF NOT EXISTS idx_mensajes_cuenta_estado ON mensajes_programados (id_cuenta, estado_envio, fecha_hora_disparo)",
    "CREATE INDEX IF NOT EXISTS idx_plantillas_cuenta ON plantillas (id_cuenta)",
    # Candado 1: la cuenta de una fila se deduce de su usuario y tiene que coincidir con ella. Si el código olvida
    # poner id_cuenta, la BD la completa desde el usuario (nunca desde una cuenta por defecto); si no coincide, error.
    # to_jsonb(NEW) permite que funcione aunque la tabla aún no tenga alguna de las columnas.
    """
    CREATE OR REPLACE FUNCTION fn_cuenta_del_usuario() RETURNS trigger AS $$
    DECLARE
        fila jsonb := to_jsonb(NEW);
        cuenta_usuario uuid;
    BEGIN
        IF NOT (fila ? 'id_cuenta') OR fila->>'id_usuario' IS NULL THEN
            RETURN NEW;
        END IF;
        SELECT id_cuenta INTO cuenta_usuario FROM usuarios WHERE id_usuario = CAST(fila->>'id_usuario' AS uuid);
        IF NEW.id_cuenta IS NULL THEN
            NEW.id_cuenta := cuenta_usuario;
        ELSIF cuenta_usuario IS NOT NULL AND NEW.id_cuenta <> cuenta_usuario THEN
            RAISE EXCEPTION 'id_cuenta (%) no coincide con la cuenta del usuario (%) en %',
                NEW.id_cuenta, cuenta_usuario, TG_TABLE_NAME USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
    END $$ LANGUAGE plpgsql
    """,
    "CREATE OR REPLACE TRIGGER trg_cuenta_sesiones BEFORE INSERT OR UPDATE ON sesiones_whatsapp "
    "FOR EACH ROW EXECUTE FUNCTION fn_cuenta_del_usuario()",
    "CREATE OR REPLACE TRIGGER trg_cuenta_mensajes BEFORE INSERT OR UPDATE ON mensajes_programados "
    "FOR EACH ROW EXECUTE FUNCTION fn_cuenta_del_usuario()",
    "CREATE OR REPLACE TRIGGER trg_cuenta_plantillas BEFORE INSERT OR UPDATE ON plantillas "
    "FOR EACH ROW EXECUTE FUNCTION fn_cuenta_del_usuario()",
    # Candado 2: un mensaje solo puede apuntar a una sesión de SU cuenta. Si la sesión se borra, solo id_sesion
    # pasa a NULL (la cuenta del mensaje no se toca). Se agrega solo si los datos ya lo cumplen.
    """
    DO $$
    BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'uq_sesion_cuenta') THEN
            ALTER TABLE sesiones_whatsapp ADD CONSTRAINT uq_sesion_cuenta UNIQUE (id_sesion, id_cuenta);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_mensaje_sesion_misma_cuenta') THEN
            IF EXISTS (SELECT 1 FROM mensajes_programados m JOIN sesiones_whatsapp s ON s.id_sesion = m.id_sesion
                        WHERE s.id_cuenta IS DISTINCT FROM m.id_cuenta) THEN
                RAISE WARNING 'v1.1.0: hay mensajes con sesión de otra cuenta; fk_mensaje_sesion_misma_cuenta no se aplicó';
            ELSE
                ALTER TABLE mensajes_programados ADD CONSTRAINT fk_mensaje_sesion_misma_cuenta
                    FOREIGN KEY (id_sesion, id_cuenta) REFERENCES sesiones_whatsapp (id_sesion, id_cuenta)
                    ON DELETE SET NULL (id_sesion);
            END IF;
        END IF;
    END $$
    """,
    # Secreto del webhook para las sesiones que aún no lo tienen (gen_random_uuid usa el generador seguro de Postgres)
    """
    UPDATE sesiones_whatsapp
       SET secreto_webhook = replace(gen_random_uuid()::text, '-', '') || replace(gen_random_uuid()::text, '-', '')
     WHERE secreto_webhook IS NULL
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_sesiones_secreto_webhook ON sesiones_whatsapp (secreto_webhook)",

    # Límite de intentos (login; luego registro, recuperación y canje). En la BD para que un deploy no lo reinicie.
    """
    CREATE TABLE IF NOT EXISTS intentos_acceso (
        clave          VARCHAR(120) PRIMARY KEY,
        ventana_inicio TIMESTAMPTZ NOT NULL,
        conteo         INTEGER NOT NULL
    )
    """,
]


async def run_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for step in _STEPS:
            await conn.execute(text(step.strip()))
    logger.info("Migraciones aplicadas correctamente")
