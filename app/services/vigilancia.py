"""
services/vigilancia.py
Chequeo de cada minuto de las conexiones de WhatsApp (job del programador). Existe porque el aviso de
desconexión no puede depender solo del evento del webhook, que puede perderse:

 a) Para las sesiones marcadas 'conectado' con mensajes pendientes, pregunta a Evolution por el estado real
    (como máximo una vez cada 2 min por sesión). Si no está 'open', la marca desconectada.
 b) Para las sesiones desconectadas con mensajes pendientes y sin aviso previo (y desconectadas hace más de
    2 min, para no avisar por microcortes), envía el correo. Sigue siendo UN correo por desconexión.
Nunca lanza: un fallo aquí no debe afectar al envío de mensajes.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import exists, or_, select

from app.database import AsyncSessionLocal
from app.models import MensajeProgramado, SesionWhatsApp
from app.services import aviso_desconexion

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")

GRACIA = timedelta(minutes=2)             # desconectada al menos este tiempo antes de enviar el correo
VERIFICAR_CADA = timedelta(minutes=2)     # frecuencia máxima de consulta a Evolution por sesión
PENDIENTES = ("programado", "en_cola")
_ultima_verificacion: dict = {}


def _hay_pendientes():
    return exists().where(MensajeProgramado.id_sesion == SesionWhatsApp.id_sesion,
                          MensajeProgramado.estado_envio.in_(PENDIENTES))


async def consultar_estado(sesion) -> str | None:
    """Estado real en Evolution ('open' | 'close' | 'connecting') o None si no se pudo consultar."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            r = await client.get(f"{EVOLUTION_URL}/instance/connectionState/{sesion.instancia_evolution}",
                                 headers={"apikey": sesion.token_autorizacion or EVOLUTION_KEY})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        datos = r.json()
        return str((datos.get("instance") or datos).get("state") or "").lower() or None
    except Exception as exc:
        logger.info("Vigilancia: no se pudo consultar el estado de '%s' (%s: %s)",
                    sesion.instancia_evolution, type(exc).__name__, exc)
        return None


async def vigilar_conexiones() -> None:
    try:
        ahora = datetime.now(timezone.utc)

        # a) sesiones "conectadas" con pendientes: ¿siguen conectadas de verdad?
        async with AsyncSessionLocal() as db:
            conectadas = (await db.execute(
                select(SesionWhatsApp).where(
                    SesionWhatsApp.estado_conexion == "conectado",
                    SesionWhatsApp.numero_telefono.is_not(None),
                    _hay_pendientes(),
                )
            )).scalars().all()
            for s in conectadas:
                if ahora - _ultima_verificacion.get(s.id_sesion, datetime.min.replace(tzinfo=timezone.utc)) < VERIFICAR_CADA:
                    continue
                _ultima_verificacion[s.id_sesion] = ahora
                estado = await consultar_estado(s)
                if estado is None or estado == "open":
                    continue
                s.estado_conexion = "desconectado"
                s.desconectada_en = ahora
                await db.commit()
                logger.warning("Vigilancia: '%s' aparece '%s' en Evolution aunque estaba conectada; se marca desconectada "
                               "(no llegó el evento del webhook)", s.instancia_evolution, estado)

        # b) sesiones desconectadas con pendientes y sin aviso: correo (una vez por desconexión)
        async with AsyncSessionLocal() as db:
            ids = (await db.execute(
                select(SesionWhatsApp.id_sesion).where(
                    SesionWhatsApp.estado_conexion != "conectado",
                    SesionWhatsApp.numero_telefono.is_not(None),
                    SesionWhatsApp.aviso_desconexion_en.is_(None),
                    or_(SesionWhatsApp.desconectada_en.is_(None), SesionWhatsApp.desconectada_en <= ahora - GRACIA),
                    _hay_pendientes(),
                )
            )).scalars().all()
        for id_sesion in ids:
            await aviso_desconexion.avisar_desconexion(id_sesion)
    except Exception as exc:
        logger.error("Vigilancia de conexiones: error inesperado (%s: %s)", type(exc).__name__, exc)
