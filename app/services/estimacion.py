"""
services/estimacion.py
Estimación de cuándo saldrá cada mensaje de una sesión, simulando su cola con las pausas PROMEDIO
(app/envio_config.py). Se usa para el aviso al programar y para "En cola · sale aprox. HH:MM" en Mensajes.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import envio_config as cfg
from app.models import MensajeProgramado

EC = timezone(timedelta(hours=-5))
PENDIENTES = ("programado", "en_cola")


def pausa_media(destino: str, rojo: bool) -> float:
    rango = cfg.PAUSA_GRUPO if destino.endswith("@g.us") else cfg.PAUSA_INDIVIDUAL
    return cfg.promedio(rango) * (cfg.MULT_PAUSA_ROJO if rojo else 1)


def simular(items: list[tuple], ahora: datetime, rojo: bool, ultimo_envio: datetime | None = None) -> dict:
    """items = [(clave, fecha_hora_disparo, destino)]. Devuelve {clave: hora estimada de salida}.

    La cola sale en orden de fecha_hora_disparo, de a uno y con pausa. El primero sale a
    max(su hora, ahora) + espera inicial media; si la sesión envió hace un instante (cola ya corriendo), no antes
    de ese envío + la pausa media. Cada siguiente sale a max(su hora, salida anterior + pausa media según su
    destino). Una salida NUNCA es anterior a la hora programada del mensaje."""
    salidas, anterior = {}, None
    for clave, disparo, destino in sorted(items, key=lambda x: x[1]):
        if anterior is None:
            pausa = timedelta(seconds=pausa_media(destino, rojo))
            if ultimo_envio is not None and ahora - ultimo_envio < pausa:
                t = max(disparo, ultimo_envio + pausa)               # la cola ya está corriendo: respeta la pausa
            else:
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


async def ultimo_envio_de_sesion(db: AsyncSession, id_sesion, ahora: datetime) -> datetime | None:
    """Cuándo salió el último mensaje de la sesión (o ahora, si hay uno enviándose en este instante)."""
    enviando = (await db.execute(
        select(func.count()).select_from(MensajeProgramado)
        .where(MensajeProgramado.id_sesion == id_sesion, MensajeProgramado.estado_envio == "enviando")
    )).scalar_one()
    if enviando:
        return ahora
    return (await db.execute(
        select(func.max(MensajeProgramado.enviado_en)).where(MensajeProgramado.id_sesion == id_sesion)
    )).scalar_one_or_none()


def aviso_al_programar(pendientes: list, fecha_hora: datetime, destino: str, ahora: datetime, rojo: bool,
                       ultimo_envio: datetime | None = None) -> dict:
    """Aviso para un mensaje nuevo programado a `fecha_hora`: cuántos mensajes tiene la sesión cerca de esa hora y
    cuándo saldría ESTE. {'n': 0} si no hay ninguno cerca."""
    ventana = timedelta(minutes=cfg.VENTANA_AVISO_MIN)
    cercanos = [m for m in pendientes if abs(m.fecha_hora_disparo - fecha_hora) <= ventana]
    if not cercanos:
        return {"n": 0}
    items = [(m.id_mensaje, m.fecha_hora_disparo, m.id_grupo) for m in pendientes] + [("nuevo", fecha_hora, destino)]
    mi_salida = simular(items, ahora, rojo, ultimo_envio)["nuevo"]

    n = len(cercanos)
    horas_vecinas = {hhmm(m.fecha_hora_disparo) for m in cercanos}
    plural = "mensajes" if n != 1 else "mensaje"
    # Si todos los vecinos están en el MISMO minuto se dice cuál; si sus horas son distintas, "cerca de esa hora"
    donde = f"a las {next(iter(horas_vecinas))}" if len(horas_vecinas) == 1 else "cerca de esa hora"
    return {
        "n": n,
        "hora": hhmm(fecha_hora),
        "salida": hhmm(mi_salida),
        "duplicadas": rojo,
        "mensaje": f"Este número ya tiene {n} {plural} {donde}. Tu mensaje saldría aproximadamente a las {hhmm(mi_salida)}.",
    }
