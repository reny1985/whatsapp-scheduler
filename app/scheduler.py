"""
scheduler.py
Orquestador APScheduler para el envío de mensajes programados.

Ejecuta un job cada 60 s que:
  1. Busca mensajes con estado='programado' cuya fecha_hora_disparo <= now() + jitter
  2. Los marca como 'enviando' (evita doble envío)
  3. Llama a Evolution API:
       - Texto solo → POST /message/sendText/{instancia}
       - Audio → POST /message/sendWhatsAppAudio/{instancia} (+ texto aparte)
       - Otro media (+caption opcional) → POST /message/sendMedia/{instancia}
  4. Marca como 'enviado' o 'fallido' según el resultado
"""
import logging
import os
import random
import re
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, unquote, urlparse

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal as AsyncSessionFactory
from app.models import MensajeProgramado, SesionWhatsApp, condicion_sesion_nunca_conectada
from app.routers.media import DOC_EXT_MIME

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")

# Servidor público para construir URL absoluta de archivos locales
BASE_PUBLIC_URL = os.getenv("BASE_PUBLIC_URL", "http://host.docker.internal:8000")

# Rango de jitter anti-thundering-herd (segundos)
JITTER_MIN = 1
JITTER_MAX = 60


def _siguiente_disparo(base: datetime, recurrencia: str) -> datetime | None:
    """Calcula el próximo disparo según la recurrencia. Retorna None si es 'none'."""
    if recurrencia == "daily":
        return base + timedelta(days=1)
    if recurrencia == "weekly":
        return base + timedelta(weeks=1)
    if recurrencia == "monthly":
        # Avanza un mes preservando día/hora; si el mes no tiene ese día, usa el último
        year, month = base.year, base.month + 1
        if month > 12:
            month, year = 1, year + 1
        import calendar
        max_day = calendar.monthrange(year, month)[1]
        day = min(base.day, max_day)
        return base.replace(year=year, month=month, day=day)
    return None


def _evo_headers(token: str | None = None) -> dict:
    apikey = token or EVOLUTION_KEY
    return {"apikey": apikey, "Content-Type": "application/json"}


def _make_absolute_url(url_media: str) -> str:
    if url_media.startswith("http://") or url_media.startswith("https://"):
        return url_media
    return BASE_PUBLIC_URL.rstrip("/") + quote(url_media)


def _nombre_mp4(url_media: str) -> str:
    """fileName con extensión .mp4 a partir de la URL del video."""
    base = unquote(urlparse(url_media).path.rsplit("/", 1)[-1]) or "video"
    return os.path.splitext(base)[0] + ".mp4"


def _datos_documento(url_media: str) -> tuple[str, str]:
    """(fileName, mimetype) de un documento: nombre original con extensión.

    La subida guarda `<32 hex>_<nombre><ext>`; se quita ese prefijo.
    """
    base = unquote(urlparse(url_media).path.rsplit("/", 1)[-1])
    base = re.sub(r"^[0-9a-fA-F]{32}_", "", base)
    nombre, ext = os.path.splitext(base)
    if not ext and nombre.startswith("."):  # ".pdf": splitext lo toma como nombre
        nombre, ext = "", nombre
    ext = ext.lower()
    if not nombre.strip():
        base = "documento" + ext
    return base, DOC_EXT_MIME.get(ext, "application/octet-stream")


async def _post_evolution(client: httpx.AsyncClient, endpoint: str, payload: dict, headers: dict) -> dict:
    r = await client.post(endpoint, json=payload, headers=headers)
    logger.info("📥 Evolution %s → %s: %s", endpoint.rsplit("/message/", 1)[-1], r.status_code, r.text[:500])
    if r.status_code >= 400:
        raise RuntimeError(f"Evolution API {r.status_code}: {r.text[:300]}")
    return r.json()


async def _enviar_mensaje(
    instancia: str,
    destinatario: str,
    texto: str | None,
    tipo_media: str | None,
    url_media: str | None,
    token: str | None = None,
) -> dict:
    headers = _evo_headers(token)

    async with httpx.AsyncClient(timeout=30) as client:

        if tipo_media == "audio" and url_media:
            # Nota de voz; si hay texto va como mensaje aparte después del audio.
            url_abs = _make_absolute_url(url_media)
            logger.debug("📤 sendWhatsAppAudio → %s | destinatario=%s | url=%s", instancia, destinatario, url_abs)
            resultado = await _post_evolution(
                client,
                f"{EVOLUTION_URL}/message/sendWhatsAppAudio/{instancia}",
                {
                    "number": destinatario,
                    "options": {"encoding": True},
                    "audioMessage": {"audio": url_abs},
                },
                headers,
            )
            if texto and texto.strip():
                try:
                    await _post_evolution(
                        client,
                        f"{EVOLUTION_URL}/message/sendText/{instancia}",
                        {"number": destinatario, "textMessage": {"text": texto}},
                        headers,
                    )
                except Exception as exc:
                    raise RuntimeError(f"Audio enviado pero el texto falló: {exc}") from exc
            return resultado

        if tipo_media and url_media:
            url_abs = _make_absolute_url(url_media)
            media_message = {
                "mediatype": tipo_media,
                "media": url_abs,
                "caption": texto or "",
            }
            if tipo_media == "video":
                media_message["fileName"] = _nombre_mp4(url_media)
            elif tipo_media == "document":
                media_message["fileName"], media_message["mimetype"] = _datos_documento(url_media)
            payload = {"number": destinatario, "mediaMessage": media_message}
            endpoint = f"{EVOLUTION_URL}/message/sendMedia/{instancia}"
            logger.debug(
                "📤 sendMedia → %s | destinatario=%s | tipo=%s | url=%s",
                instancia, destinatario, tipo_media, url_abs
            )
        else:
            payload = {
                "number": destinatario,
                "textMessage": {"text": texto or ""},
            }
            endpoint = f"{EVOLUTION_URL}/message/sendText/{instancia}"
            logger.debug(
                "📤 sendText → %s | destinatario=%s | texto=%s",
                instancia, destinatario, (texto or "")[:60]
            )

        return await _post_evolution(client, endpoint, payload, headers)


async def _procesar_mensajes_pendientes() -> None:
    ahora = datetime.now(timezone.utc)
    ventana = ahora + timedelta(seconds=JITTER_MAX)

    async with AsyncSessionFactory() as db:
        stmt = (
            select(MensajeProgramado)
            .join(SesionWhatsApp, MensajeProgramado.id_sesion == SesionWhatsApp.id_sesion)
            .where(
                MensajeProgramado.id_sesion.is_not(None),  # sesión borrada → se ignora
                MensajeProgramado.estado_envio == "programado",
                MensajeProgramado.fecha_hora_disparo <= ventana,
            )
        )
        result = await db.execute(stmt)
        mensajes = result.scalars().all()

        if not mensajes:
            return

        logger.info("🗓  %d mensaje(s) a procesar", len(mensajes))

        for msg in mensajes:
            sesion = await db.get(SesionWhatsApp, msg.id_sesion)
            if not sesion:
                logger.warning(
                    "Sesión %s no encontrada para mensaje %s",
                    msg.id_sesion, msg.id_mensaje
                )
                continue

            if sesion.estado_conexion != "conectado":
                logger.warning(
                    "Sesión %s no está conectada (estado: '%s'), omitiendo mensaje %s.",
                    sesion.instancia_evolution,
                    sesion.estado_conexion,
                    msg.id_mensaje,
                )
                continue

            if not sesion.token_autorizacion:
                logger.error(
                    "Sesión %s no tiene token_autorizacion en BD.",
                    sesion.instancia_evolution,
                )
                continue

            jitter = random.uniform(JITTER_MIN, JITTER_MAX)
            disparo = msg.fecha_hora_disparo
            if disparo.tzinfo is None:
                disparo = disparo.replace(tzinfo=timezone.utc)
            if disparo > ahora:
                restante = (disparo - ahora).total_seconds()
                if restante > jitter:
                    continue

            await db.execute(
                update(MensajeProgramado)
                .where(
                    MensajeProgramado.id_mensaje == msg.id_mensaje,
                    MensajeProgramado.estado_envio == "programado",
                )
                .values(estado_envio="enviando")
            )
            await db.commit()

            try:
                resultado = await _enviar_mensaje(
                    instancia=sesion.instancia_evolution,
                    destinatario=msg.id_grupo,
                    texto=msg.texto_mensaje,
                    tipo_media=msg.tipo_media,
                    url_media=msg.url_media,
                    token=sesion.token_autorizacion,
                )
                logger.info(
                    "✅ Enviado %s → %s (sesión %s) resultado: %s",
                    msg.id_mensaje, msg.id_grupo, sesion.instancia_evolution,
                    str(resultado)[:120],
                )
                nuevo_estado = "enviado"
                error_envio = None
            except Exception as exc:
                logger.error(
                    "❌ Error enviando %s → %s: %s",
                    msg.id_mensaje, msg.id_grupo, exc,
                )
                nuevo_estado = "fallido"
                error_envio = str(exc)[:1000]

            await db.execute(
                update(MensajeProgramado)
                .where(MensajeProgramado.id_mensaje == msg.id_mensaje)
                .values(estado_envio=nuevo_estado, error_envio=error_envio)
            )

            # Si se envió correctamente y tiene recurrencia, clonar el próximo
            if nuevo_estado == "enviado" and msg.recurrencia != "none":
                siguiente = _siguiente_disparo(msg.fecha_hora_disparo, msg.recurrencia)
                if siguiente:
                    proximo = MensajeProgramado(
                        id_mensaje=uuid.uuid4(),
                        id_sesion=msg.id_sesion,
                        id_grupo=msg.id_grupo,
                        texto_mensaje=msg.texto_mensaje,
                        tipo_media=msg.tipo_media,
                        url_media=msg.url_media,
                        fecha_hora_disparo=siguiente,
                        estado_envio="programado",
                        recurrencia=msg.recurrencia,
                        id_mensaje_origen=msg.id_mensaje,
                    )
                    db.add(proximo)
                    logger.info(
                        "🔁 Próxima ocurrencia programada: %s → %s (%s)",
                        msg.id_mensaje, proximo.id_mensaje, siguiente.isoformat(),
                    )

            await db.commit()


async def _limpiar_sesiones_huerfanas() -> None:
    """
    Elimina solo sesiones que nunca se conectaron (sin número y sin última conexión)
    creadas hace más de 10 minutos: QR abandonado o pairing code no completado.
    """
    limite = datetime.now(timezone.utc) - timedelta(minutes=10)
    async with AsyncSessionFactory() as db:
        result = await db.execute(
            select(SesionWhatsApp).where(
                condicion_sesion_nunca_conectada(),
                SesionWhatsApp.fecha_creacion < limite,
            )
        )
        sesiones = result.scalars().all()

    for sesion in sesiones:
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                await client.delete(
                    f"{EVOLUTION_URL}/instance/delete/{sesion.instancia_evolution}",
                    headers={"apikey": sesion.token_autorizacion or EVOLUTION_KEY},
                )
        except Exception as exc:
            logger.warning(
                "Limpieza: no se pudo eliminar instancia '%s' de Evolution: %s",
                sesion.instancia_evolution, exc,
            )
        async with AsyncSessionFactory() as db:
            s = await db.get(SesionWhatsApp, sesion.id_sesion)
            # Re-verifica: si conectó mientras se llamaba a Evolution, no se borra
            if s and not s.numero_telefono and s.fecha_ultima_conexion is None:
                await db.delete(s)
                await db.commit()
        logger.info(
            "Sesión huérfana eliminada: instancia=%s id=%s",
            sesion.instancia_evolution, sesion.id_sesion,
        )


def crear_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="America/Guayaquil")
    scheduler.add_job(
        _procesar_mensajes_pendientes,
        trigger="interval",
        seconds=60,
        id="enviar_mensajes",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _limpiar_sesiones_huerfanas,
        trigger="interval",
        minutes=5,
        id="limpiar_huerfanas",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    return scheduler
