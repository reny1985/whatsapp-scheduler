# PROGRESO — rama `fix-audio-video`

Última actualización: 2026-10-11

## Qué se hizo hoy
- **Staging creado** en Railway: `https://thorough-adventure-staging.up.railway.app` (entorno `staging`, servicio `thorough-adventure` vinculado). Production (`https://enviafast.net`) no se tocó.
- **Reglas en CLAUDE.md** (en `main`): entornos staging/production, deploy solo a staging, `aprobado para producción`, respaldo `pg_dump` + merge + etiqueta antes de producción, ramas desde la última etiqueta estable, adjuntos en `/data` (`UPLOAD_DIR=/data/uploads`), nunca borrar datos de production.
- **Conversión de audio y video al subir** (`app/routers/media.py`):
  - Audio → OGG Opus (`libopus`, 32k, mono, 48 kHz, `voip`).
  - Video (webm, mp4, mov, 3gp) → MP4 H.264 main + AAC, máx. 1280 px de ancho, dimensiones pares, `+faststart`.
  - Si ffmpeg falla: error 422 claro y no se guarda el original.
- **Envío** (`app/scheduler.py`): audio por `sendWhatsAppAudio` (texto como mensaje aparte), video con `fileName` `.mp4`, respuesta de Evolution registrada hasta 500 caracteres.
- **`error_envio`**: columna nueva `mensajes_programados.error_envio TEXT` (`ADD COLUMN IF NOT EXISTS`); si Evolution falla, el mensaje queda `fallido` con el motivo.
- **Pruebas**: 40 pasan (`tests/test_media.py` nuevo, verificación con ffprobe).
- Desplegado a staging (deploy `75bf46c7`), `/health` ok.
- **Corrección de pérdida de sesiones** (commit `3ccf9b3`): limpieza solo de sesiones nunca conectadas, `connecting` → `desconectado` si hay número, FK de mensajes a `SET NULL`. 59 pruebas. Reconexión probada en staging (deploy `595a4dd2`).

## Pruebas en staging (2026-10-11)
Aprobadas: audio grabado, video grabado, video de galería, imágenes y documentos (pdf, docx, xlsx con nombre y extensión correctos).

## Release v1.0.1 (2026-10-11)
Aprobado para producción (incluye la corrección de sesiones). Pasos: respaldo `pg_dump` → merge a `main` → etiqueta `v1.0.1` → push → deploy a production → verificación de `/health` y de la columna `error_envio`.

## Pendientes futuros
- Verificar el dominio en Resend y configurar `RESEND_FROM`.
- Mostrar un error al usuario cuando el correo de verificación no se envía.
- Separar los datos por cliente.
