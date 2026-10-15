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
    """Aviso para un mensaje nuevo programado a `fecha_hora`, siempre hablando de ESTE mensaje.

    · Sin mensajes cerca → {'n': 0} (no se muestra aviso).
    · Si sale ANTES que los otros: "Tu mensaje saldría aproximadamente a las HH:MM (antes que los N mensajes de las HH:MM)."
    · Si sale después: "Este número ya tiene N mensajes a las HH:MM. Tu mensaje saldría aproximadamente a las HH:MM."
      (si los vecinos tienen horas distintas: "…N mensajes cerca de esa hora…")."""
    pasada = fecha_hora < ahora
    ventana = timedelta(minutes=cfg.VENTANA_AVISO_MIN)
    cercanos = [m for m in pendientes if abs(m.fecha_hora_disparo - fecha_hora) <= ventana]
    if not cercanos:
        return {"n": 0, "hora_pasada": pasada} if pasada else {"n": 0}
    items = [(m.id_mensaje, m.fecha_hora_disparo, m.id_grupo) for m in pendientes] + [("nuevo", fecha_hora, destino)]
    salidas = simular(items, ahora, rojo, ultimo_envio)
    mi_salida = salidas["nuevo"]
    mi_hora = hhmm(mi_salida)

    n = len(cercanos)
    horas_vecinas = sorted({hhmm(m.fecha_hora_disparo) for m in cercanos})
    sale_antes = all(salidas[m.id_mensaje] > mi_salida for m in cercanos)
    if sale_antes:
        ref = f"de las {horas_vecinas[0]}" if len(horas_vecinas) == 1 else f"de entre las {horas_vecinas[0]} y las {horas_vecinas[-1]}"
        otros = "el mensaje" if n == 1 else f"los {n} mensajes"
        mensaje = f"Tu mensaje saldría aproximadamente a las {mi_hora} (antes que {otros} {ref})."
    else:
        plural = "mensajes" if n != 1 else "mensaje"
        donde = f"a las {horas_vecinas[0]}" if len(horas_vecinas) == 1 else "cerca de esa hora"
        mensaje = f"Este número ya tiene {n} {plural} {donde}. Tu mensaje saldría aproximadamente a las {mi_hora}."
    return {
        "n": n,
        "hora": hhmm(fecha_hora),
        "salida": mi_hora,
        "antes_que_los_otros": sale_antes,
        "hora_pasada": pasada,
        "duplicadas": rojo,
        "mensaje": mensaje,
    }
