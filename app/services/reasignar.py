"""
services/reasignar.py
Cuando se vincula de nuevo el MISMO número, los mensajes programados que quedaron sin sesión (porque se
eliminó la anterior) pasan a la sesión nueva y se envían a su hora. Solo se reasignan los 'programado' del
misma cuenta y cuyo numero_origen coincide con el número de la sesión que acaba de conectarse. Los vencidos
no se reasignan solos (se reprograman) y los de otro número quedan sin sesión (reasignación manual).
"""
import logging

from sqlalchemy import update

from app.database import AsyncSessionLocal
from app.models import MensajeProgramado, SesionWhatsApp

logger = logging.getLogger("whatsapp_scheduler")


async def reasignar_huerfanos(id_sesion) -> int:
    """Devuelve cuántos mensajes se reasignaron. Nunca lanza."""
    try:
        async with AsyncSessionLocal() as db:
            sesion = await db.get(SesionWhatsApp, id_sesion)
            if not sesion or sesion.estado_conexion != "conectado" or not sesion.numero_telefono:
                return 0
            res = await db.execute(
                update(MensajeProgramado)
                .where(
                    MensajeProgramado.id_cuenta == sesion.id_cuenta,
                    MensajeProgramado.id_sesion.is_(None),
                    MensajeProgramado.estado_envio == "programado",
                    MensajeProgramado.numero_origen == sesion.numero_telefono,
                )
                .values(id_sesion=sesion.id_sesion)
            )
            await db.commit()
            if res.rowcount:
                logger.info("Re-vinculado el mismo número (%s): %d mensaje(s) sin sesión reasignados a '%s'",
                            sesion.numero_telefono, res.rowcount, sesion.instancia_evolution)
            return res.rowcount or 0
    except Exception as exc:
        logger.error("Reasignar mensajes sin sesión: error inesperado (%s: %s)", type(exc).__name__, exc)
        return 0
