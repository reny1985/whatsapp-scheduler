-- foto_v1.1.1.sql — conteo y hash (md5) de cada tabla que ya existía en v1.1.0, con SOLO las columnas de v1.1.0.
-- Antes y después de migrar a v1.1.1 (y tras el rollback y la nueva subida) tiene que dar exactamente lo mismo:
-- la migración solo agrega (columna estado_antes_suspension, tablas auditoria_admin y periodos_cuenta, candados).
-- Cada fila se hashea completa (to_jsonb) y en orden por su texto: no imprime ningún dato, solo el md5.
-- La usa scripts/ensayo_v1.1.1.sh.
SELECT 'cuentas' AS tabla, count(*) AS filas,
       md5(coalesce(string_agg((to_jsonb(t) - 'estado_antes_suspension')::text, ','
                               ORDER BY (to_jsonb(t) - 'estado_antes_suspension')::text), '')) AS hash
  FROM cuentas t
UNION ALL SELECT 'usuarios', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM usuarios t
UNION ALL SELECT 'sesiones_whatsapp', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM sesiones_whatsapp t
UNION ALL SELECT 'mensajes_programados', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM mensajes_programados t
UNION ALL SELECT 'plantillas', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM plantillas t
UNION ALL SELECT 'grupos_whatsapp', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM grupos_whatsapp t
UNION ALL SELECT 'estado_programador', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM estado_programador t
UNION ALL SELECT 'intentos_acceso', count(*), md5(coalesce(string_agg(to_jsonb(t)::text, ',' ORDER BY to_jsonb(t)::text), ''))
  FROM intentos_acceso t;
