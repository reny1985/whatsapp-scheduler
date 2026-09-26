import asyncio
import logging
import random
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import engine
from app.models import MensajeProgramado, SesionWhatsApp
from services.evolution_client import enviar_mensaje

logger = logging.getLogger("whatsapp_scheduler")
scheduler = AsyncIOScheduler(timezone="UTC")

AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def procesar_mensaje(mensaje_id, id_sesion, texto, tipo_media, url_media, id_grupo):
    retraso = random.uniform(1, 50)
    logger.info("⏳ [Thundering Herd] Mensaje %s esperando %.2f seg...", mensaje_id, retraso)
    await asyncio.sleep(retraso)

    logger.info("🚀 ENVIANDO MENSAJE %s | Texto: %s | Media: %s", mensaje_id, texto, tipo_media)

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(SesionWhatsApp).where(SesionWhatsApp.id_sesion == id_sesion))
        sesion = result.scalar_one_or_none()

    if sesion is None:
        logger.error("❌ Sesión %s no encontrada para mensaje %s", id_sesion, mensaje_id)
        nuevo_estado = "fallido"
    else:
        exito = await enviar_mensaje(
            instancia=sesion.instancia_evolution,
            token=sesion.token_autorizacion,
            numero_grupo=id_grupo,
            texto=texto,
            tipo_media=tipo_media,
            url_media=url_media,
        )
        nuevo_estado = "enviado" if exito else "fallido"

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(MensajeProgramado)
            .where(MensajeProgramado.id_mensaje == mensaje_id)
            .values(estado_envio=nuevo_estado)
        )
        await db.commit()
    logger.info("✅ Mensaje %s finalizado → estado=%s", mensaje_id, nuevo_estado)

async def check_mensajes_programados():
    logger.info("🔍 Orquestador buscando mensajes programados...")
    ahora = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(MensajeProgramado)
            .where(MensajeProgramado.estado_envio == "programado")
            .where(MensajeProgramado.fecha_hora_disparo <= ahora)
            .with_for_update(skip_locked=True)
        )
        mensajes = result.scalars().all()

    if not mensajes:
        logger.info("😴 No hay mensajes pendientes.")
        return

    logger.info("📦 %d mensajes encontrados. Iniciando dispersión...", len(mensajes))
    
    for msg in mensajes:
        async with AsyncSessionLocal() as db:
            await db.execute(
                update(MensajeProgramado)
                .where(MensajeProgramado.id_mensaje == msg.id_mensaje)
                .values(estado_envio="enviando")
            )
            await db.commit()

        asyncio.create_task(
            procesar_mensaje(
                mensaje_id=msg.id_mensaje,
                id_sesion=msg.id_sesion,
                texto=msg.texto_mensaje,
                tipo_media=msg.tipo_media,
                url_media=msg.url_media,
                id_grupo=msg.id_grupo,
            )
        )

def start_orchestrator(intervalo_segundos: int = 30):
    scheduler.add_job(
        check_mensajes_programados,
        "interval",
        seconds=intervalo_segundos,
        id="check_mensajes",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=60,
    )
    scheduler.start()
    logger.info("⏱️ Orquestador APScheduler iniciado (intervalo=%ds).", intervalo_segundos)

def stop_orchestrator():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("🛑 Orquestador APScheduler detenido.")
