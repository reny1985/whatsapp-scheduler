"""
services/vigilancia.py
Chequeo de cada minuto de las conexiones de WhatsApp (job del programador). Existe porque el aviso de
desconexión no puede depender solo del evento del webhook, que puede perderse, ni del estado guardado en la base:

 a) REVISIÓN DEL ESTADO REAL: para TODAS las sesiones con mensajes pendientes, pregunta a Evolution por el estado
    real (como máximo una vez cada 2 min por sesión) y corrige la base si difiere (conectada↔desconectada). Cada
    revisión deja una línea en el log con el estado real, el guardado y los pendientes.
 b) AVISO: para las sesiones desconectadas con pendientes y sin aviso previo (y desconectadas hace más de 2 min,
    para no avisar por microcortes) envía el correo. Antes de enviarlo, avisar_desconexion confirma con Evolution
    que sigue desconectada. Sigue siendo UN correo por desconexión.
Nunca lanza: un fallo aquí no debe afectar al envío de mensajes.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, func, or_, select

from app.database import AsyncSessionLocal
from app.models import MensajeProgramado, SesionWhatsApp
from app.services import aviso_desconexion, evolution_estado, reasignar

logger = logging.getLogger("whatsapp_scheduler")

GRACIA = timedelta(minutes=2)             # desconectada al menos este tiempo antes de enviar el correo
VERIFICAR_CADA = timedelta(minutes=2)     # frecuencia máxima de consulta a Evolution por sesión
PENDIENTES = ("programado", "en_cola")
_ultima_verificacion: dict = {}


def _hay_pendientes():
    return exists().where(MensajeProgramado.id_sesion == SesionWhatsApp.id_sesion,
                          MensajeProgramado.estado_envio.in_(PENDIENTES))


async def _revisar_estado_real(ahora: datetime) -> None:
    """a) Compara el estado guardado con el real de Evolution, para todas las sesiones con pendientes."""
    reconectadas = []
    async with AsyncSessionLocal() as db:
        sesiones = (await db.execute(
            select(SesionWhatsApp).where(
                SesionWhatsApp.numero_telefono.is_not(None),
                SesionWhatsApp.estado_conexion.in_(("conectado", "desconectado")),   # no se toca una vinculación en curso (QR)
                _hay_pendientes(),
            )
        )).scalars().all()
        for s in sesiones:
            if ahora - _ultima_verificacion.get(s.id_sesion, datetime.min.replace(tzinfo=timezone.utc)) < VERIFICAR_CADA:
                continue
            _ultima_verificacion[s.id_sesion] = ahora
            pendientes = (await db.execute(
                select(func.count()).select_from(MensajeProgramado)
                .where(MensajeProgramado.id_sesion == s.id_sesion, MensajeProgramado.estado_envio.in_(PENDIENTES))
            )).scalar_one()
            real = await evolution_estado.consultar_estado(s)
            guardado = s.estado_conexion

            if real is None:
                logger.info("Vigilancia: '%s' → estado real: sin respuesta de Evolution | guardado: %s | pendientes: %d | acción: ninguna",
                            s.instancia_evolution, guardado, pendientes)
                continue
            abierta = real == "open"
            if abierta and guardado != "conectado":
                s.estado_conexion, s.desconectada_en, s.aviso_desconexion_en = "conectado", None, None
                await db.commit()
                reconectadas.append(s.id_sesion)
                accion = "corregida a conectado"
            elif not abierta and guardado == "conectado":
                s.estado_conexion, s.desconectada_en = "desconectado", ahora
                await db.commit()
                accion = "marcada desconectada (no llegó el evento del webhook)"
            else:
                accion = "sin cambios"
            logger.info("Vigilancia: '%s' → estado real: %s | guardado: %s | pendientes: %d | acción: %s",
                        s.instancia_evolution, real, guardado, pendientes, accion)
    for id_sesion in reconectadas:      # volvió a estar conectada: los mensajes sin sesión del mismo número pasan a ella
        await reasignar.reasignar_huerfanos(id_sesion)


async def vigilar_conexiones() -> None:
    try:
        ahora = datetime.now(timezone.utc)
        await _revisar_estado_real(ahora)

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
