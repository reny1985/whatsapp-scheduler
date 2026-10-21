-- rollback_v1.1.1.sql
-- Deja la base lista para volver a desplegar v1.1.0 SIN BORRAR NINGÚN DATO.
--
-- Qué hace: quita los 5 candados CHECK de cuentas que agregó v1.1.1 (ck_cuentas_*), para que la base quede con las
-- mismas reglas que tenía v1.1.0. Nada más.
--
-- Qué NO toca (v1.1.0 simplemente los ignora):
--   · tablas auditoria_admin y periodos_cuenta, con sus datos (el historial de acciones y de pagos se conserva);
--   · su trigger de inmutabilidad (la auditoría sigue sin poder modificarse);
--   · la columna cuentas.estado_antes_suspension.
-- Volver a subir a v1.1.1 no necesita nada: sus migraciones son idempotentes y vuelven a poner los candados.
--
-- ⚠️  IMPORTANTE: v1.1.0 NO aplica los estados de cuenta. Mientras corra v1.1.0, las cuentas suspendidas o vencidas
--    vuelven a poder entrar, programar y enviar. Al final se listan para que sepas cuáles son.
--
-- Pasos para volver atrás:
--   1. psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/rollback_v1.1.1.sql
--   2. Desplegar la etiqueta v1.1.0.
BEGIN;
ALTER TABLE cuentas DROP CONSTRAINT IF EXISTS ck_cuentas_estado;
ALTER TABLE cuentas DROP CONSTRAINT IF EXISTS ck_cuentas_plan;
ALTER TABLE cuentas DROP CONSTRAINT IF EXISTS ck_cuentas_estado_antes;
ALTER TABLE cuentas DROP CONSTRAINT IF EXISTS ck_cuentas_interna;
ALTER TABLE cuentas DROP CONSTRAINT IF EXISTS ck_cuentas_con_vencimiento;
COMMIT;

SELECT 'Cuentas que con v1.1.0 recuperan el acceso: ' || count(*) AS aviso
  FROM cuentas
 WHERE NOT es_interna
   AND (estado = 'suspendida'
        OR (estado = 'prueba' AND (prueba_fin IS NULL OR prueba_fin < now()))
        OR (estado = 'activa' AND periodo_fin < now() - interval '3 days')
        OR (estado = 'cortesia' AND periodo_fin < now()));
