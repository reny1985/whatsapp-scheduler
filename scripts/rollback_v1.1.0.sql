-- rollback_v1.1.0.sql
-- Deja la base lista para volver a desplegar v1.0.8 SIN BORRAR NINGÚN DATO.
--
-- Por qué hace falta: v1.0.8 al arrancar ejecuta
--   INSERT INTO usuarios (...) ON CONFLICT DO NOTHING
-- y Postgres valida NOT NULL antes del ON CONFLICT: con usuarios.id_cuenta NOT NULL ese arranque fallaría.
--
-- Qué NO toca: las tablas y columnas nuevas (cuentas, id_cuenta, secreto_webhook, intentos_acceso...) se quedan con
-- sus datos; v1.0.8 simplemente las ignora. Los triggers que completan id_cuenta desde el usuario se quedan: así lo
-- que v1.0.8 cree mientras tanto queda en la cuenta correcta y volver a subir a v1.1.0 no necesita nada más.
--
-- Pasos para volver atrás:
--   1. psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/rollback_v1.1.0.sql
--   2. Desplegar la etiqueta v1.0.8.
--   3. Webhook: v1.0.8 solo tiene la ruta /webhook/evolution. Las instancias ya pasadas a la URL con secreto
--      deben volver a la ruta vieja: con sesión iniciada, POST /sesiones/<id_sesion>/configurar-webhook por cada
--      sesión (v1.0.8 la configura con la URL vieja). Hasta hacerlo se pierden eventos de grupos/conexión (los
--      mensajes se siguen enviando; la vigilancia de cada minuto corrige el estado de conexión).
BEGIN;
ALTER TABLE usuarios             ALTER COLUMN id_cuenta DROP NOT NULL;
ALTER TABLE sesiones_whatsapp    ALTER COLUMN id_cuenta DROP NOT NULL;
ALTER TABLE mensajes_programados ALTER COLUMN id_cuenta DROP NOT NULL;
ALTER TABLE plantillas           ALTER COLUMN id_cuenta DROP NOT NULL;
ALTER TABLE mensajes_programados DROP CONSTRAINT IF EXISTS fk_mensaje_sesion_misma_cuenta;
COMMIT;
