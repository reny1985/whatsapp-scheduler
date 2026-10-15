"""
services/estimacion.py
Estimación de cuándo saldrá cada mensaje de una sesión, simulando su cola con las pausas PROMEDIO
(app/envio_config.py). Se usa para el aviso al programar y para "En cola · sale aprox. HH:MM" en Mensajes.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import envio_config as cfg
from app.models import MensajeProgramado

EC = timezone(timedelta(hours=-5))
PENDIENTES = ("programado", "en_cola")


def pausa_media(destino: str, rojo: bool) -> float:
    rango = cfg.PAUSA_GRUPO if destino.endswith("@g.us") else cfg.PAUSA_INDIVIDUAL
    return cfg.promedio(rango) * (cfg.MULT_PAUSA_ROJO if rojo else 1)


def simular(items: list[tuple], ahora: datetime, rojo: bool) -> dict:
    """items = [(clave, fecha_hora_disparo, destino)]. Devuelve {clave: hora estimada de salida}.

    El primero sale a max(su hora, ahora) + espera inicial media; cada siguiente, a
    max(su hora, salida anterior + pausa media según su destino). Orden: fecha_hora_disparo."""
    salidas, anterior = {}, None
    for clave, disparo, destino in sorted(items, key=lambda x: x[1]):
        if anterior is None:
            t = max(disparo, ahora) + timedelta(seconds=cfg.promedio(cfg.ESPERA_INICIAL_COLA))
        else:
            t = max(disparo, anterior + timedelta(seconds=pausa_media(destino, rojo)))
        salidas[clave] = anterior = t
    return salidas


def hhmm(dt: datetime) -> str:
    return dt.astimezone(EC).strftime("%H:%M")


async def pendientes_de_sesion(db: AsyncSession, id_sesion) -> list[MensajeProgramado]:
    return (await db.execute(
        select(MensajeProgramado)
        .where(MensajeProgramado.id_sesion == id_sesion, MensajeProgramado.estado_envio.in_(PENDIENTES))
        .order_by(MensajeProgramado.fecha_hora_disparo, MensajeProgramado.fecha_creacion)
    )).scalars().all()


def aviso_al_programar(pendientes: list, fecha_hora: datetime, destino: str, ahora: datetime, rojo: bool) -> dict:
    """Aviso para un mensaje nuevo a `fecha_hora`. {'n': 0} si la sesión no tiene nada cerca."""
    ventana = timedelta(minutes=cfg.VENTANA_AVISO_MIN)
    cercanos = [m for m in pendientes if abs(m.fecha_hora_disparo - fecha_hora) <= ventana]
    if not cercanos:
        return {"n": 0}
    items = [(m.id_mensaje, m.fecha_hora_disparo, m.id_grupo) for m in pendientes] + [("nuevo", fecha_hora, destino)]
    salidas = simular(items, ahora, rojo)
    claves = [m.id_mensaje for m in cercanos] + ["nuevo"]
    desde, hasta = min(salidas[k] for k in claves), max(salidas[k] for k in claves)
    n = len(cercanos)
    plural = "mensajes" if n != 1 else "mensaje"
    return {
        "n": n,
        "hora": hhmm(fecha_hora), "desde": hhmm(desde), "hasta": hhmm(hasta),
        "duplicadas": rojo,
        "mensaje": f"Este número ya tiene {n} {plural} a las {hhmm(fecha_hora)}; "
                   f"se enviarán aproximadamente entre {hhmm(desde)} y {hhmm(hasta)}",
    }
