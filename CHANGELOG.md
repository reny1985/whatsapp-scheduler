# Changelog

## v1.0.8 — 2026-10-15 — Envío escalonado y protección del número

- **Envío escalonado por número**: un trabajador por sesión (las sesiones corren en paralelo, nunca dos colas de la misma sesión). Pausa aleatoria entre envíos: 20–45 s a números individuales y 8–20 s a grupos (el doble en semáforo rojo). Los mensajes de una sesión salen en orden de `fecha_hora_disparo`
- **Espera inicial 0–20 s** antes del primero de cada cola, siempre después de su hora y descontando lo que ya esperó por el ciclo: el primero sale entre 0 y ~23 s después de su hora (incluido el envío)
- **Ciclo del programador de 20 s** (`CICLO_SEG`); constantes en un solo lugar (`app/envio_config.py`)
- **Retomar la cola tras un reinicio**: latido en `estado_programador`; los `en_cola` se retoman sin perderse ni duplicarse, un `enviando` huérfano pasa a `fallido` (nunca se reenvía) y tras más de 60 min sin actividad los pendientes quedan `vencido` por "servidor sin actividad"
- **Aviso al programar** ("Tu mensaje saldría aproximadamente a las HH:MM", o cuántos mensajes tiene ya ese número) que habla siempre de ESTE mensaje y respeta la cola y el último envío; en Mensajes, los de la cola muestran "En cola · sale aprox. HH:MM"
- **Hora pasada**: el selector no permite horas pasadas en móvil (`min`), aviso amarillo "Esa hora ya pasó: el mensaje se enviará de inmediato" y el backend rechaza una hora pasada por más de 2 minutos salvo "Enviar ahora"
- **Semáforo anti-spam** por número (verde < 100, amarillo 100–300, rojo > 300 mensajes ponderados al día; grupo = 0,2; umbrales a la mitad si el número tiene < 7 días; aviso si el mismo texto va a más de 20 números individuales). Nunca cancela mensajes: solo avisa y alarga pausas
- **"Escribiendo…"** antes de enviar texto, imagen y audio (`options.delay/presence` de Evolution), activable con `ENVIO_ESCRIBIENDO=1`; se desactiva solo si Evolution lo rechaza
- **Límites de peso por archivo**: imagen 5 MB, audio 16 MB, video 16 MB (tras conversión), documento 25 MB
- Base de datos (migración idempotente): `mensajes_programados.enviado_en`, tabla `estado_programador` (latido)
- Pruebas automáticas: 278

## v1.0.7 — 2026-10-14 — Mensajes vencidos, avisos de desconexión y calendario

- **Tolerancia de retraso de 5 minutos** (`TOLERANCIA_RETRASO_MIN`, un solo lugar), medida al TOMAR el mensaje y no cuando sale. Dos fases: tomar (queda `en_cola` si entró a tiempo, o `vencido` con su motivo: sesión eliminada, WhatsApp desconectado, sin credenciales o servidor sin actividad) y enviar (lo que entró a la cola a tiempo se envía siempre, aunque la cola dure más). Se registra cuándo se tomó (`tomado_en`)
- **Mensajes vencidos**: estado propio con color y motivo, filtro "Vencidos", botón "Reprogramar" que abre Programar con los datos cargados, y tarjeta "Vencidos" en el Inicio. Una serie recurrente no se corta si un envío vence (se crea la próxima ocurrencia futura). "Enviar ahora" se guarda como "ahora" para no vencerse
- **Dueño del mensaje por `id_usuario`**: columna nueva en `mensajes_programados`, migración idempotente y sin borrar datos (se rellena desde la sesión; los sin sesión van al usuario piloto). Inicio, Mensajes, estadísticas, historial y el programador filtran por dueño, no por la sesión; el programador comprueba que la sesión sea del mismo usuario
- **Reasignación al re-vincular el MISMO número**: cada mensaje guarda el número con el que se programó (`numero_origen`); al conectarse una sesión con ese número, sus mensajes programados sin sesión pasan a ella. Los vencidos no se reasignan solos
- **Mensajes sin sesión visibles**: filtro "Sin sesión", etiqueta, "Reasignar" por mensaje y "Reasignar todos", aviso en Mensajes y en el Inicio (cuentan en Pendientes/Vencidos)
- **Aviso de desconexión**: aviso en el Inicio ("⚠️ Tu WhatsApp está desconectado: N mensajes pendientes no se enviarán…") y **correo** "[Enviafast] Tu WhatsApp se desconectó" (número, pendientes, hora del próximo, botón "Volver a vincular"), un correo por desconexión. Un chequeo de cada minuto revisa el estado REAL en Evolution de todas las sesiones con pendientes y deja una línea en el log por revisión; el correo se confirma con Evolution antes de enviarse. Eliminar/desvincular a propósito no envía correo
- **Sesión**: botón "Desvincular" (antes "Eliminar") con diálogo que avisa de los N mensajes pendientes y que quedarán en pausa; nota visible sobre salir de Enviafast vs. desvincular; tarjeta de cada número rediseñada (foto, nombre y número completos arriba, botones en una fila aparte)
- **Calendario flatpickr** en computador (copia local en `/static/vendor`, español, tema oscuro, 12 h con a. m./p. m., botón "✓ Listo", sin fechas pasadas), también en la edición; en móvil se mantiene el selector nativo
- **Nombre de perfil de WhatsApp** desde `fetchInstances.profileName` (en cuentas personales `fetchProfile.name` trae el número, que se descarta); candado por sesión contra consultas dobles
- **Mensajes en pantallas angostas**: cada fila se apila como tarjeta, con motivo y botones a la vista
- Archivos estáticos versionados (`?v=`) y `index.html` sin caché; hojas externas con `crossorigin` (evita un error de flatpickr)
- Base de datos (migraciones idempotentes): `mensajes_programados.tomado_en`, `.id_usuario` (NOT NULL, FK, índice), `.numero_origen`; `sesiones_whatsapp.aviso_desconexion_en`, `.desconectada_en`
- Pruebas automáticas: 205

## v1.0.6 — 2026-10-12 — Inicio amigable, menú con texto y perfil de WhatsApp

- **Menú lateral con texto**: Inicio, Programar, Mensajes, Sesión y, abajo, Salir con su ícono. Computador: ícono + palabra; móvil vertical: palabra de 11 px bajo el ícono. Sección activa resaltada; el menú queda fijo a la altura de la pantalla, así Salir siempre se ve (se quitó el botón Salir de la cabecera)
- **Inicio sencillo**, de arriba a abajo: saludo con fecha y hora de Ecuador; botón grande "＋ Programar mensaje"; tarjeta de estado de WhatsApp (número(s) con punto verde "Conectado" o rojo "Desconectado" y botón "Ir a Sesión"); 4 tarjetas con número grande (Pendientes, Enviados hoy, Enviados este mes, Fallidos del mes) que llevan a Mensajes con su filtro; próximos 5 envíos con nombre de grupo o número, fecha y hora, ícono del tipo y punto de estado; barras de los últimos 7 días con tasa de entrega; estados vacíos amables. En móvil las tarjetas pasan a 2 columnas, sin scroll horizontal
- **`GET /dashboard/resumen`**: un solo endpoint con todo lo anterior, con 4 consultas agregadas (el número de consultas no depende de la cantidad de mensajes), "hoy" y "este mes" en America/Guayaquil y siempre filtrado por el usuario actual. Los mensajes sin sesión no se cuentan
- **Indicador de conexión de la cabecera al instante**: antes se consultaba al cargar la página, sin login, y mostraba "Sin sesión" hasta ~30 s después aunque hubiera una sesión conectada; ahora consulta apenas se entra a la app y distingue "sin login" de "sin sesión"
- **Nombre y foto de perfil de WhatsApp**: se obtienen de Evolution (`chat/fetchProfile`, con el token de la instancia) al conectarse y al pulsar "Verificar", como máximo una vez al día (reintento en 1 h si algo falla). La foto se descarga y se guarda en `<UPLOAD_DIR>/avatars/<id_sesion>.jpg` (el enlace de WhatsApp caduca y no se guarda) y se sirve con login por `GET /sesiones/{id}/foto`. Se muestra en el saludo del Inicio ("Hola, <nombre> 👋"), en la cabecera y en Sesión; sin foto, iniciales o ícono de usuario. Si algo falla no se rompe nada y el motivo queda en el log sin claves ni URLs firmadas
- **Seguridad**: la carpeta `avatars/` está bloqueada en `/static/uploads` (404), de modo que las fotos de perfil solo salen por el endpoint con login
- **Base de datos**: columnas nuevas `perfil_nombre`, `perfil_foto_en` y `perfil_consultado_en` en `sesiones_whatsapp` (migración idempotente, sin tocar datos)
- Pruebas automáticas: 128

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
