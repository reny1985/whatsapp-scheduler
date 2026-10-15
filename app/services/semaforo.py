"""
services/semaforo.py
Semáforo anti-spam por sesión (base). Se calcula al vuelo con una consulta; no se guarda nada ni se cancela
o borra ningún mensaje: en rojo solo se duplican las pausas entre envíos.

  carga = mensajes a números individuales del día (enviados + programados para hoy)
          + PESO_GRUPO × mensajes a grupos
  verde < amarillo · amarillo ≤ rojo · rojo > rojo   (umbrales a la mitad si el número se vinculó hace < 7 días)
Todas las constantes están en app/envio_config.py.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import envio_config as cfg

EC = timezone(timedelta(hours=-5))      # Ecuador no tiene horario de verano

FRASES = {
    "verde": "Todo en orden: tus envíos de hoy van a un ritmo seguro.",
    "amarillo": "Vas con bastantes envíos hoy. Conviene espaciarlos o dejar algunos para mañana.",
    "rojo": "Tus envíos se espaciaron automáticamente para proteger tu número.",
}
AVISO_NUEVO = "Número recién vinculado: por seguridad los límites son la mitad durante sus primeros {dias} días."
AVISO_TEXTO_REPETIDO = "Personaliza tus mensajes para evitar bloqueos."


def limites_del_dia(ahora: datetime) -> tuple[datetime, datetime]:
    """Inicio y fin (UTC) del día de HOY en Ecuador."""
    local = ahora.astimezone(EC)
    ini = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return ini.astimezone(timezone.utc), (ini + timedelta(days=1)).astimezone(timezone.utc)


def color_para(carga: float, numero_nuevo: bool) -> tuple[str, float, float]:
    """(color, umbral_amarillo, umbral_rojo) para una carga dada."""
    f = 0.5 if numero_nuevo else 1.0
    amarillo, rojo = cfg.SEMAFORO_AMARILLO * f, cfg.SEMAFORO_ROJO * f
    if carga > rojo:
        return "rojo", amarillo, rojo
    if carga >= amarillo:
        return "amarillo", amarillo, rojo
    return "verde", amarillo, rojo


_SQL_CARGA = text("""
    SELECT
        count(*) FILTER (WHERE id_grupo LIKE '%@s.whatsapp.net') AS individuales,
        count(*) FILTER (WHERE id_grupo LIKE '%@g.us')           AS grupos
    FROM mensajes_programados
    WHERE id_sesion = :sid
      AND estado_envio IN ('enviado', 'enviando', 'en_cola', 'programado')
      AND COALESCE(enviado_en, fecha_hora_disparo) >= :ini
      AND COALESCE(enviado_en, fecha_hora_disparo) <  :fin
""")

_SQL_REPETIDO = text("""
    SELECT max(n) AS maximo FROM (
        SELECT count(DISTINCT id_grupo) AS n
        FROM mensajes_programados
        WHERE id_sesion = :sid
          AND id_grupo LIKE '%@s.whatsapp.net'
          AND texto_mensaje IS NOT NULL AND texto_mensaje <> ''
          AND estado_envio IN ('enviado', 'enviando', 'en_cola', 'programado')
          AND COALESCE(enviado_en, fecha_hora_disparo) >= :ini
          AND COALESCE(enviado_en, fecha_hora_disparo) <  :fin
        GROUP BY texto_mensaje
    ) t
""")


async def calcular(db: AsyncSession, id_sesion, fecha_creacion: datetime | None, ahora: datetime | None = None) -> dict:
    ahora = ahora or datetime.now(timezone.utc)
    ini, fin = limites_del_dia(ahora)
    fila = (await db.execute(_SQL_CARGA, {"sid": id_sesion, "ini": ini, "fin": fin})).one()
    maximo = (await db.execute(_SQL_REPETIDO, {"sid": id_sesion, "ini": ini, "fin": fin})).scalar_one() or 0

    individuales, grupos = fila.individuales, fila.grupos
    carga = individuales + cfg.PESO_GRUPO * grupos
    fc = fecha_creacion if (fecha_creacion is None or fecha_creacion.tzinfo) else fecha_creacion.replace(tzinfo=timezone.utc)
    nuevo = bool(fc and ahora - fc < timedelta(days=cfg.NUMERO_NUEVO_DIAS))
    color, amarillo, rojo = color_para(carga, nuevo)
    repetido = maximo > cfg.TEXTO_REPETIDO_MAX

    avisos = []
    if repetido:
        avisos.append(AVISO_TEXTO_REPETIDO)
    if nuevo:
        avisos.append(AVISO_NUEVO.format(dias=cfg.NUMERO_NUEVO_DIAS))
    return {
        "color": color,
        "carga": round(carga, 1),
        "individuales": individuales,
        "grupos": grupos,
        "umbral_amarillo": amarillo,
        "umbral_rojo": rojo,
        "numero_nuevo": nuevo,
        "texto_repetido": repetido,
        "pausas_duplicadas": color == "rojo",
        "frase": FRASES[color],
        "avisos": avisos,
    }


async def es_rojo(db: AsyncSession, id_sesion, fecha_creacion: datetime | None, ahora: datetime | None = None) -> bool:
    return (await calcular(db, id_sesion, fecha_creacion, ahora))["color"] == "rojo"
