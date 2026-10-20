"""
services/intentos.py
Límite de intentos (login en v1.1.0; registro, recuperación y canje después). Ventana fija por clave, guardada en
Postgres para que un deploy o un reinicio no la borre. Las claves nunca guardan el correo en claro (sha256).
"""
import hashlib
from datetime import datetime, timedelta, timezone

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

LOGIN_VENTANA = timedelta(minutes=15)
LOGIN_MAX_FALLOS_CORREO = 5
LOGIN_MAX_FALLOS_IP = 30


def clave(accion: str, tipo: str, valor: str) -> str:
    """'login', 'correo', 'Ana@X.com' → 'login:correo:<sha256 de ana@x.com>' (cabe en VARCHAR(120))."""
    h = hashlib.sha256(valor.strip().lower().encode()).hexdigest()[:64]
    return f"{accion}:{tipo}:{h}"


def ip_cliente(request: Request) -> str:
    """IP del cliente. Railway agrega la IP real al FINAL de X-Forwarded-For; lo que viene antes lo pudo escribir
    el propio cliente, así que se toma el último valor (no el primero)."""
    xff = request.headers.get("x-forwarded-for", "")
    partes = [p.strip() for p in xff.split(",") if p.strip()]
    if partes:
        return partes[-1]
    return request.client.host if request.client else "desconocida"


# DIAGNÓSTICO TEMPORAL (solo para averiguar qué encabezado trae la IP real detrás de Railway; se quita después)
_ENCABEZADOS_PROXY = ("x-forwarded-for", "x-real-ip", "x-envoy-external-address", "forwarded",
                      "cf-connecting-ip", "true-client-ip", "fastly-client-ip", "x-client-ip")


def diagnostico_encabezados(request: Request) -> str:
    vistos = {h: request.headers.get(h) for h in _ENCABEZADOS_PROXY if request.headers.get(h) is not None}
    return f"client.host={request.client.host if request.client else None} {vistos}"


async def bloqueado(db: AsyncSession, k: str, maximo: int, ventana: timedelta) -> int:
    """Segundos que faltan para poder intentar de nuevo (0 = no está bloqueado)."""
    fila = (await db.execute(
        text("SELECT conteo, ventana_inicio FROM intentos_acceso WHERE clave = :k"), {"k": k}
    )).first()
    if not fila:
        return 0
    fin = fila.ventana_inicio + ventana
    ahora = datetime.now(timezone.utc)
    if fila.conteo >= maximo and fin > ahora:
        return max(1, int((fin - ahora).total_seconds()))
    return 0


async def registrar_fallo(db: AsyncSession, k: str, ventana: timedelta) -> None:
    """Suma un fallo (atómico). Si la ventana anterior ya terminó, empieza una nueva."""
    await db.execute(text("""
        INSERT INTO intentos_acceso (clave, ventana_inicio, conteo) VALUES (:k, now(), 1)
        ON CONFLICT (clave) DO UPDATE SET
            conteo = CASE WHEN intentos_acceso.ventana_inicio < now() - make_interval(secs => :s)
                          THEN 1 ELSE intentos_acceso.conteo + 1 END,
            ventana_inicio = CASE WHEN intentos_acceso.ventana_inicio < now() - make_interval(secs => :s)
                                  THEN now() ELSE intentos_acceso.ventana_inicio END
    """), {"k": k, "s": ventana.total_seconds()})


async def limpiar(db: AsyncSession, k: str) -> None:
    await db.execute(text("DELETE FROM intentos_acceso WHERE clave = :k"), {"k": k})


def mensaje_espera(segundos: int) -> str:
    minutos = max(1, -(-segundos // 60))
    return f"Demasiados intentos fallidos. Espera {minutos} min y vuelve a intentarlo."
