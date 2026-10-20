-- foto_migracion.sql — conteo y hash (md5) de cada tabla con SOLO las columnas que ya existían en v1.0.8.
-- Antes y después de migrar a v1.1.0 tiene que dar exactamente lo mismo: la migración no cambia ni pierde datos.
-- La usan scripts/ensayo_migracion.sh y tests/test_migracion_v110.py.
SELECT 'usuarios' AS tabla, count(*) AS filas,
       md5(coalesce(string_agg(concat_ws('|', id_usuario, email, password_hash, rol, estado_suscripcion,
                                         email_verificado, codigo_verificacion, codigo_expira_en, fecha_registro),
                               ',' ORDER BY id_usuario), '')) AS hash
  FROM usuarios
UNION ALL
SELECT 'sesiones_whatsapp', count(*),
       md5(coalesce(string_agg(concat_ws('|', id_sesion, id_usuario, nombre_sesion, numero_telefono, perfil_nombre,
                                         perfil_foto_en, perfil_consultado_en, aviso_desconexion_en, desconectada_en,
                                         instancia_evolution, token_autorizacion, estado_conexion, fecha_creacion,
                                         fecha_ultima_conexion),
                               ',' ORDER BY id_sesion), ''))
  FROM sesiones_whatsapp
UNION ALL
SELECT 'mensajes_programados', count(*),
       md5(coalesce(string_agg(concat_ws('|', id_mensaje, id_sesion, id_usuario, numero_origen, id_grupo,
                                         md5(coalesce(texto_mensaje, '')), tipo_media, url_media, fecha_hora_disparo,
                                         estado_envio, recurrencia, id_mensaje_origen, error_envio, tomado_en,
                                         enviado_en, fecha_creacion),
                               ',' ORDER BY id_mensaje), ''))
  FROM mensajes_programados
UNION ALL
SELECT 'plantillas', count(*),
       md5(coalesce(string_agg(concat_ws('|', id_plantilla, id_usuario, nombre, md5(coalesce(texto_mensaje, '')),
                                         tipo_media, url_media, fecha_creacion),
                               ',' ORDER BY id_plantilla), ''))
  FROM plantillas
UNION ALL
SELECT 'grupos_whatsapp', count(*),
       md5(coalesce(string_agg(concat_ws('|', id_grupo, id_sesion, subject, size, actualizado_en),
                               ',' ORDER BY id_grupo, id_sesion), ''))
  FROM grupos_whatsapp
ORDER BY tabla;
