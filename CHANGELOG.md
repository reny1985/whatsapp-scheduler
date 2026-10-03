# Changelog

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
