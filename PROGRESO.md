# PROGRESO — WhatsApp Scheduler

Última actualización: 2026-10-15 (estado tras v1.0.8)

## Estado actual

**Production (`https://enviafast.net`) corre v1.0.8**, sobre `main`. 278 pruebas automáticas pasan.

- **v1.0.8** — envío escalonado y protección del número:
  - Un trabajador por sesión; pausas 20–45 s (individual) y 8–20 s (grupo), el doble en rojo; espera inicial 0–20 s descontando el ciclo; ciclo de 20 s; constantes en `app/envio_config.py`.
  - Retoma la cola tras reinicio (latido en `estado_programador`, `enviado_en`); nunca reenvía un `enviando` huérfano.
  - Aviso al programar, "En cola · sale aprox.", hora pasada en móvil, semáforo anti-spam, límites de peso por archivo y "escribiendo…" (`ENVIO_ESCRIBIENDO=1`, activo en production).

- **v1.0.7** — mensajes vencidos, dueño por usuario, avisos y calendario:
  - **Tolerancia de retraso** (`TOLERANCIA_RETRASO_MIN = 5`, un solo lugar, `app/scheduler.py`) medida al TOMAR el mensaje, en dos fases: tomar (`en_cola` o `vencido` con su motivo) y enviar (todo lo que entró a la cola a tiempo se envía siempre). Estados nuevos `en_cola` y `vencido`; los vencidos se reprograman desde Mensajes. "Enviar ahora" se guarda como "ahora" y una serie recurrente no se corta si un envío vence.
  - **Dueño por `id_usuario`** directo en `mensajes_programados` (migración idempotente, sin borrar datos; los sin sesión van al usuario piloto). Inicio, Mensajes, estadísticas y el programador filtran por dueño, no por la sesión.
  - **Reasignación al re-vincular el MISMO número** (cada mensaje guarda `numero_origen`): los programados sin sesión pasan a la sesión nueva; los vencidos no se reasignan solos. **Mensajes sin sesión visibles** (filtro, etiqueta, "Reasignar" y "Reasignar todos") y contados en el Inicio.
  - **Aviso de desconexión**: aviso en el Inicio y **correo** ("[Enviafast] Tu WhatsApp se desconectó", un correo por desconexión). Un chequeo de cada minuto (`services/vigilancia.py`) revisa el estado REAL en Evolution de todas las sesiones con pendientes y deja rastro en el log (`Vigilancia: … estado real | guardado | pendientes | acción`); el correo se confirma con Evolution antes de enviarse (si no responde, no se envía y se reintenta).
  - **Sesión**: botón "Desvincular" (antes "Eliminar") con advertencia y aviso de N pendientes, nota visible sobre desvincular, tarjeta del número rediseñada (foto, nombre y número completos; botones en fila aparte). Eliminar/desvincular a propósito no envía correo.
  - **Calendario flatpickr** (local en `/static/vendor`, español, tema oscuro, 12 h, "✓ Listo", sin fechas pasadas) en computador; selector nativo en móvil. **Nombre de perfil** desde `fetchInstances.profileName` (en cuentas personales `fetchProfile.name` trae el número). **Mensajes en tarjetas** en móvil. Archivos estáticos versionados (`?v=`) y `index` sin caché.

- **v1.0.6** — Inicio amigable, menú y perfil:
  - Menú lateral con texto (ícono + palabra en computador; palabra de 11 px bajo el ícono en móvil), sección activa resaltada y "Salir" siempre visible abajo.
  - Inicio nuevo alimentado por un solo endpoint, `GET /dashboard/resumen` (4 consultas agregadas, filtrado por usuario, "hoy" y "este mes" en America/Guayaquil): saludo con fecha y hora de Ecuador, botón "Programar mensaje", estado de WhatsApp, 4 tarjetas (Pendientes, Enviados hoy, Enviados este mes, Fallidos del mes) que filtran Mensajes, próximos 5 envíos con nombre de grupo, barras de 7 días y tasa de entrega, y estados vacíos.
  - El indicador de conexión de la cabecera muestra el estado real apenas se entra a la app (antes decía "Sin sesión" hasta ~30 s).
  - Nombre y foto de perfil de WhatsApp (Evolution `chat/fetchProfile`): se consultan al conectarse y al pulsar "Verificar", como máximo una vez al día; la foto se descarga y guarda en `<UPLOAD_DIR>/avatars/<id_sesion>.jpg` y se sirve con login por `GET /sesiones/{id}/foto`. Se muestra en el saludo del Inicio, la cabecera y la pantalla Sesión (con iniciales o ícono como respaldo).
  - `avatars/` está bloqueada en `/static/uploads` (404): las fotos solo salen por el endpoint con login.
  - Migración: columnas nuevas `perfil_nombre`, `perfil_foto_en` y `perfil_consultado_en` en `sesiones_whatsapp`.

- **v1.0.5** — contraseñas:
  - Crear cuenta tiene el campo "Repetir contraseña": si no coinciden muestra "Las contraseñas no coinciden" debajo y no envía el formulario; también se valida en el backend (`/auth/setup` responde 422 con `confirmar_password` distinta). Mínimo de 8 caracteres con su mensaje.
  - Botón de ojo independiente (mostrar/ocultar, con `aria-label`) en los 3 campos de contraseña: login, contraseña y repetir contraseña.

- **v1.0.4** — ajustes de móvil y grupos:
  - En pantallas angostas (≤ 640 px) el botón "Salir" siempre se ve (el correo se reduce a un ícono y el título se acorta).
  - Las filas "campo + botones" (número personal, grupo, enlace/JID, agenda) usan todo el ancho y los botones pasan a la línea de abajo.
  - Botón "Verificar" de grupos (Programar y edición): muestra nombre y participantes, o avisa si el grupo ya no existe o el número ya no pertenece a él (`GET /sesiones/{id}/verificar-grupo`, usa `group/findGroupInfos` de Evolution 1.8.6).
  - "Recargar" grupos sincroniza desde WhatsApp (`?forzar=true`) con indicador de carga y mensaje si falla; un candado por sesión en el backend evita sincronizaciones dobles.

- **v1.0.3** — correo de verificación:
  - Se envía desde `Enviafast <no-reply@enviafast.net>` (dominio verificado en Resend, región São Paulo); asunto y cuerpo dicen "Enviafast".
  - La app espera la respuesta de Resend; si falla (rechazo, timeout o error de red) muestra "No pudimos enviar el correo, intenta de nuevo en unos minutos" en el registro y en la verificación, y registra el motivo en el log (sin la clave). Si falla, `/auth/setup` y `/auth/reenviar-codigo` no guardan cambios.
  - `RESEND_FROM` ya no es obligatoria: el valor por defecto del código es el remitente de enviafast.net. En staging está definida igual.

- **v1.0.2** — forma:
  - Selector de países (intl-tel-input) con el tema oscuro de la app, legible en celular y escritorio.
  - Cámara nativa del celular en el formulario de mensaje: "Tomar foto" y "Grabar video" (cámara trasera por defecto), con el mismo flujo de subida y conversión.
  - Grabación de video en la página renombrada "Webcam" (solo computador), con botón para alternar cámara trasera/frontal.
  - CLAUDE.md corregido: Evolution API es la 1.8.6.
- **v1.0.1** — audio, video y documentos:
  - Audio convertido a OGG Opus y enviado con `sendWhatsAppAudio`; video convertido a MP4 H.264 + AAC.
  - Documentos llegan con su nombre original, extensión y `mimetype` correctos (ya no como "BIN"); se aceptan también `.xls`, `.pptx` y `.csv`.
  - `error_envio`: si Evolution falla, el mensaje queda `fallido` con el motivo.
  - Corrección de la pérdida de sesiones y mensajes: la limpieza automática solo borra sesiones nunca conectadas; `connecting` en una sesión con número queda `desconectado`; la FK de mensajes pasó a `ON DELETE SET NULL`, así que borrar una sesión ya no borra sus mensajes.
- 205 pruebas automáticas pasan.

## Decisiones de producto
- **Planes:** el plan **Profesional ($9) incluye 1 solo número de WhatsApp** (ya no 2). Los planes **Equipo** y **Empresarial** están por definir.

## Números vinculados
- **Production** (`enviafast.net`) usa el **0969829845** (593969829845).
- **Staging** usa el **0961409996** (593961409996).
- **Regla: nunca el mismo número en staging y production.**

## Entornos
- Production: `https://enviafast.net`. Staging: `https://thorough-adventure-staging.up.railway.app`.
- Todo despliegue va a staging; a production solo con "aprobado para producción", tras respaldo `pg_dump` fuera del proyecto, merge a `main`, etiqueta y push (ver CLAUDE.md).
- Respaldos de production en `~/Desktop/Respaldos_Enviafast/` (se usa `pg_dump` 18 de Homebrew).

## Pendientes futuros (en orden de prioridad)
1. ✅ **Hecho (v1.0.8): envío escalonado.** (Pendiente de ese ítem: límites por hora y por día según el plan.) Hoy los mensajes se envían en serie y sin pausa (~1,1–1,7 s cada uno), lo que se parece a un bot y arriesga el número. Agregar: (a) pausa aleatoria entre envíos a números individuales; (b) aviso "se enviarán entre HH:MM y HH:MM" al programar varios mensajes a la misma hora; (c) límites por hora y por día según el plan.
2. **Seguridad — Evolution en un subdominio con HTTPS** (no la IP por HTTP): hoy se llama a `http://159.69.248.218`, y hay redes corporativas que la bloquean ("Web Page Blocked"). Incluye verificar la clave global.
3. **Logo de Enviafast en los correos:** subir el archivo a `/static` y definir `EMAIL_LOGO_URL` (el lugar ya está preparado en el correo).
4. Separar los datos por cliente (multiusuario), imprescindible antes de vender. (Avance en v1.0.7: los mensajes ya tienen `id_usuario`; faltan sesiones/plantillas por cliente y el registro de usuarios nuevos.)
5. **Seguridad:** los adjuntos en `/static/uploads` son públicos con nombre aleatorio. Antes de vender, usar enlaces firmados que caduquen.
6. Botón "Desvincular": si Evolution no confirma el borrado de la instancia, avisar al usuario en vez de borrar solo en la BD.
7. Ondas en notas de voz: requieren Evolution v2 (PR #2346); probar antes en un servidor aparte.
8. Agregar los botones de cámara nativa al formulario de edición de mensajes.
9. Error de consola "Unexpected token 'export'": viene de `utils.js` de intl-tel-input, que se carga como script clásico (es un módulo). Investigar si afecta la validación/formato de números.
10. ✅ **Hecho (v1.0.7):** mensajes sin sesión visibles y contados, con reasignación al re-vincular el mismo número.
11. ✅ **Hecho (v1.0.3):** verificar `enviafast.net` en Resend y configurar el remitente.
