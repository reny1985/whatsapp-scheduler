"""
services/intentos.py
Límite de intentos (login en v1.1.0; registro, recuperación y canje después). Ventana fija por clave, guardada en
Postgres para que un deploy o un reinicio no la borre. Las claves nunca guardan el correo en claro (sha256).
"""
import hashlib
import ipaddress
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


def ip_cliente(request: Request) -> str | None:
    """IP real del cliente, o None si no se puede saber con seguridad.

    Se usa X-Real-IP: el proxy de Railway la escribe con la IP de quien se conecta y SOBRESCRIBE la que mande el
    cliente (comprobado en staging enviando X-Real-IP falsa: llegó la real). No se usa X-Forwarded-For: su último
    valor es el nodo de borde (CDN), que cambia en cada petición y lo comparten muchos usuarios; otros encabezados
    (X-Envoy-External-Address, Forwarded, CF-Connecting-IP, True-Client-IP...) llegan tal cual los inventa el
    cliente. client.host tampoco sirve: es el proxy interno de Railway (100.64.x.x).
    """
    valor = (request.headers.get("x-real-ip") or "").strip()
    try:
        return str(ipaddress.ip_address(valor))
    except ValueError:
        return None


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
