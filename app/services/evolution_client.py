import logging
import os
import httpx

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_API_URL: str = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_API_KEY: str = os.getenv("EVOLUTION_API_KEY", "")

# URL base que Evolution API (Docker) usará para descargar archivos media del servidor FastAPI.
# Desde dentro de Docker, "localhost" apunta al contenedor, no al Mac.
# "host.docker.internal" apunta al Mac host.
MEDIA_BASE_URL: str = os.getenv(
    "MEDIA_BASE_URL",
    "http://host.docker.internal:8000"
)


def _build_media_url(url_media: str) -> str:
    """
    Convierte una ruta relativa como /static/uploads/archivo.jpg
    en una URL absoluta accesible desde Docker.
    Si ya es una URL absoluta (http/https), la devuelve tal cual.
    """
    if url_media.startswith("http://") or url_media.startswith("https://"):
        return url_media
    # Asegurar que empiece con /
    path = url_media if url_media.startswith("/") else "/" + url_media
    return MEDIA_BASE_URL.rstrip("/") + path


async def enviar_mensaje(
    instancia: str,
    token: str | None,
    numero_grupo: str,
    texto: str,
    tipo_media: str | None = None,
    url_media: str | None = None,
) -> bool:
    api_key = token or EVOLUTION_API_KEY
    headers = {
        "Content-Type": "application/json",
        "apikey": api_key,
    }

    if not tipo_media:
        url = f"{EVOLUTION_API_URL}/message/sendText/{instancia}"
        payload = {"number": numero_grupo, "textMessage": {"text": texto}}
    else:
        endpoint_map = {
            "image":    "sendMedia",
            "video":    "sendMedia",
            "audio":    "sendWhatsAppAudio",
            "document": "sendMedia",
        }
        endpoint = endpoint_map.get(tipo_media, "sendMedia")
        url = f"{EVOLUTION_API_URL}/message/{endpoint}/{instancia}"

        # Construir URL absoluta para que Docker pueda descargar el archivo
        media_url_absoluta = _build_media_url(url_media) if url_media else ""
        logger.info("📎 Media URL enviada a Evolution: %s", media_url_absoluta)

        # Evolution API v2 requiere los campos de media bajo la clave "mediaMessage"
        payload = {
            "number": numero_grupo,
            "mediaMessage": {
                "mediatype": tipo_media,
                "media": media_url_absoluta,
                "caption": texto or "",
            },
        }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code in (200, 201):
            logger.info("✅ Evolution OK → instancia=%s grupo=%s status=%s", instancia, numero_grupo, response.status_code)
            return True
        logger.warning("⚠️ Evolution respondió %s → instancia=%s body=%s", response.status_code, instancia, response.text[:300])
        return False
    except httpx.TimeoutException:
        logger.error("⏱️ Timeout al llamar Evolution API → instancia=%s", instancia)
        return False
    except httpx.RequestError as exc:
        logger.error("🔌 Error de red → Evolution API: %s", exc)
        return False
