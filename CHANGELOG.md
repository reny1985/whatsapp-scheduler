# Changelog

## v1.0.5 — 2026-10-11 — Confirmar contraseña y ver contraseña

- **Campo "Repetir contraseña"** en Crear cuenta: si no coincide muestra "Las contraseñas no coinciden" en rojo debajo del campo y no envía el formulario
- **Validación también en el backend**: `/auth/setup` recibe `confirmar_password` y responde 422 "Las contraseñas no coinciden" sin tocar la base de datos ni enviar correo; las contraseñas nunca se escriben en el log
- **Mínimo 8 caracteres** con su mensaje claro en el navegador ("La contraseña debe tener al menos 8 caracteres")
- **Botón de ojo** independiente en los 3 campos de contraseña (login, contraseña y repetir contraseña): oculta por defecto, área táctil ≥ 40 px, no envía el formulario, `aria-label` "Mostrar contraseña" / "Ocultar contraseña"
- Pruebas automáticas: 93

## v1.0.4 — 2026-10-11 — Ajustes de móvil y grupos

- **Salir visible en móvil**: en pantallas angostas (≤ 640 px) el correo de la cabecera se reduce a un ícono y el título se acorta con "…", así el botón Salir ya no queda fuera de la pantalla
- **Campos con botones a ancho completo**: número personal, grupo, enlace/JID, agenda y edición — la casilla ocupa todo el ancho y los botones pasan a la línea de abajo, uno al lado del otro
- **Verificar grupo** (Programar mensaje y edición): muestra el nombre del grupo y el número de participantes, o "El grupo ya no existe o tu número ya no pertenece a él"; si WhatsApp no responde, un error claro. Nuevo `GET /sesiones/{id}/verificar-grupo` (Evolution `group/findGroupInfos`); la clave de Evolution no sale del servidor
- **Recargar grupos sincroniza desde WhatsApp** (`?forzar=true`) con indicador de carga y mensaje si falla; si la lista está vacía al abrir Programar se sincroniza sola; un **candado por sesión** en el backend y el botón deshabilitado evitan sincronizaciones dobles
- Antes, con la caché vacía la primera carga tardaba 30–45 s sin indicador y, si Evolution fallaba, devolvía una lista vacía sin avisar
- Pruebas automáticas: 87

## v1.0.3 — 2026-10-11 — Correo de verificación

- El correo de verificación sale de **Enviafast <no-reply@enviafast.net>**, con el dominio verificado en Resend; el asunto y el cuerpo dicen "Enviafast"
- La app **espera la respuesta de Resend** (timeout 10 s) en `/auth/setup` y `/auth/reenviar-codigo`; si falla, devuelve un error claro ("No pudimos enviar el correo, intenta de nuevo en unos minutos"), que se muestra en las pantallas de registro y verificación, y registra el motivo en el log sin exponer la clave
- Si el envío falla no se guarda ningún cambio, para poder reintentar el registro
- Sin `RESEND_API_KEY` el envío cuenta como fallo y ya no se escribe el código en el log
- Pruebas automáticas: 69

## v1.0.2 — 2026-10-11 — Forma: selector de países y cámara

- **Selector de países** legible: panel desplegable con el tema oscuro de la app (fondo oscuro, texto claro, código en gris claro, resaltado al pasar/seleccionar, buscador incluido), en celular y escritorio; `app.css` se carga después de intl-tel-input
- **Cámara nativa**: botones "Tomar foto" y "Grabar video" (`capture="environment"`, cámara trasera por defecto) que usan el mismo flujo de subida y conversión
- Grabación de video en la página renombrada "Webcam" (solo equipos con puntero preciso) con botón para alternar cámara trasera/frontal (trasera por defecto)
- Grabación de audio sin cambios
- CLAUDE.md: versión de Evolution API corregida a 1.8.6

## v1.0.1 — 2026-10-11 — Audio, video y documentos

- **Audio** convertido al subir a OGG Opus y enviado con `sendWhatsAppAudio` (el texto va como mensaje aparte)
- **Video** (webm, mp4, mov, 3gp) convertido al subir a MP4 H.264 + AAC, máx. 1280 px de ancho; `fileName` `.mp4`
- Si ffmpeg falla al convertir: error 422 claro y no se guarda el original
- **Documentos**: `fileName` con el nombre original y extensión (ya no llegan como "BIN") y `mimetype` según la extensión
- Se aceptan también `.xls`, `.pptx` y `.csv`; el archivo conserva su extensión aunque el navegador envíe `application/octet-stream`
- **`error_envio`**: columna nueva en `mensajes_programados`; si Evolution falla, el mensaje queda `fallido` con el motivo
- **Corrección de la pérdida de sesiones y mensajes**:
  - La limpieza automática solo borra sesiones que nunca se conectaron (sin número y sin última conexión) y con más de 10 minutos; nunca una que alguna vez tuvo número
  - `connecting` en una sesión que ya tiene número queda `desconectado`, no `qr_pendiente` (webhook y `/verificar`)
  - `iniciar-pairing` ya no borra sesiones que alguna vez conectaron
  - FK `mensajes_programados.id_sesion` pasa de `ON DELETE CASCADE` a `ON DELETE SET NULL` (columna nullable): borrar una sesión ya no borra sus mensajes; el scheduler ignora los que quedan sin sesión
- Pruebas automáticas: 59

## v1.0.0 — 2026-10-02 — Primera versión estable

### Funcionalidades incluidas

- **Programación de mensajes**: texto, imagen, audio, video y documento con fecha/hora en zona Ecuador (UTC-5)
- **Recurrencia**: diaria, semanal, mensual o sin recurrencia
- **Plantillas**: creación y reutilización de mensajes frecuentes
- **Audio desde navegador**: grabación con MediaRecorder; conversión automática webm → ogg (opus) para compatibilidad con WhatsApp
- **Video y archivos**: subida hasta 50 MB; envío como mediaMessage vía Evolution API
- **Vinculación por QR**: escaneo del código QR en WhatsApp
- **Vinculación por código (pairing code)**: formato XXXX-XXXX, sin necesidad de QR
- **Selector de países**: intl-tel-input v23 con ~240 países en español, banderas, búsqueda y detección automática del país
- **Normalización de número**: acepta 0969829845, 969829845, +593 0969829845, etc.
- **Caché de grupos**: fetch sincrónico si caché vacía, refresco en background si hay datos
- **Scheduler APScheduler**: envío cada 60 s con jitter; limpieza de sesiones huérfanas cada 5 min
- **Autenticación**: login con email + verificación por código (Resend API); roles admin/usuario
- **Dominio**: https://enviafast.net (Cloudflare → Railway)
- **26 pruebas automáticas**: normalización, mensajes, grupos, pairing code
