# PROGRESO — WhatsApp Scheduler

Última actualización: 2026-10-11 (estado tras v1.0.3)

## Estado actual

**Production (`https://enviafast.net`) corre v1.0.3**, sobre `main`.

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
- 69 pruebas automáticas pasan.

## Números vinculados
- **Production** (`enviafast.net`) usa el **0969829845** (593969829845).
- **Staging** usa el **0961409996** (593961409996).
- **Regla: nunca el mismo número en staging y production.**

## Entornos
- Production: `https://enviafast.net`. Staging: `https://thorough-adventure-staging.up.railway.app`.
- Todo despliegue va a staging; a production solo con "aprobado para producción", tras respaldo `pg_dump` fuera del proyecto, merge a `main`, etiqueta y push (ver CLAUDE.md).
- Respaldos de production en `~/Desktop/Respaldos_Enviafast/` (se usa `pg_dump` 18 de Homebrew).

## Pendientes futuros (en orden de prioridad)
1. ✅ **Hecho (v1.0.3):** verificar `enviafast.net` en Resend y configurar el remitente.
2. Botón "Eliminar" sesión: si Evolution no confirma el borrado de la instancia, avisar al usuario en vez de borrar solo en la BD. (Revisado el 2026-10-11: `wa-b71909aa` ya no existe en Evolution, así que el borrado sí se había completado aunque el log dijo "No se pudo eliminar"; también se borraron las instancias de prueba `reny-1`, `Braulio-test-4` y `reny-02`. En Evolution quedan solo `wa-aba7dfdc` (production) y `wa-f823c4f4` (staging). Falta únicamente el aviso al usuario.)
3. Mensajes sin sesión (`id_sesion` nulo): mostrarlos como "sin sesión" y permitir reasignarlos a otra sesión.
4. Separar los datos por cliente (multiusuario), imprescindible antes de vender.
5. Seguridad de Evolution: verificar la clave global y poner HTTPS.
6. Ondas en notas de voz: requieren Evolution v2 (PR #2346); probar antes en un servidor aparte.
7. Agregar los botones de cámara nativa al formulario de edición de mensajes.
