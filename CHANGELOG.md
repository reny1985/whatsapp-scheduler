# Changelog

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
