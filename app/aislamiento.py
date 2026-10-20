"""
app/aislamiento.py
Aislamiento entre cuentas: todo id que llega del cliente (por URL o en el cuerpo) se busca SIEMPRE junto con la
cuenta de quien pide. Si es de otra cuenta se responde 404 (igual que si no existiera: no se confirma que exista).
"""
import uuid
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from fastapi import HTTPException, status
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MensajeProgramado, Plantilla, SesionWhatsApp, Usuario

PREFIJO_UPLOADS = "/static/uploads/"
MSG_ADJUNTO_AJENO = "El adjunto no es válido: vuelve a subir el archivo."


async def sesion_de_la_cuenta(db: AsyncSession, id_sesion: uuid.UUID, usuario: Usuario,
                              detalle: str = "Sesión no encontrada") -> SesionWhatsApp:
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion or sesion.id_cuenta != usuario.id_cuenta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detalle)
    return sesion


async def mensaje_de_la_cuenta(db: AsyncSession, id_mensaje: uuid.UUID, usuario: Usuario) -> MensajeProgramado:
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje,
                                        MensajeProgramado.id_cuenta == usuario.id_cuenta)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mensaje no encontrado")
    return mensaje


def _ruta_en_uploads(url: str) -> str | None:
    """Parte de la ruta que sigue a /static/uploads/ (None si la URL no apunta a los adjuntos)."""
    ruta = unquote(urlparse(url).path if "://" in url else url.split("?", 1)[0])
    i = ruta.find(PREFIJO_UPLOADS)
    if i < 0:
        return None
    return ruta[i + len(PREFIJO_UPLOADS):]


async def validar_adjunto(db: AsyncSession, url_media: str | None, usuario: Usuario) -> None:
    """Un mensaje o plantilla solo puede usar adjuntos de SU cuenta.

    · Nuevos (v1.1.0): /static/uploads/<id_cuenta>/<archivo> → solo si <id_cuenta> es la propia.
    · Antiguos (antes de v1.1.0, en la raíz de uploads): solo si un mensaje o plantilla de la misma cuenta ya lo
      usa (así siguen funcionando los programados existentes y su edición, sin mover archivos).
    · URLs externas (fuera de /static/uploads/) no son de ninguna cuenta: se aceptan como antes.
    """
    if not url_media:
        return
    resto = _ruta_en_uploads(url_media)
    if resto is None:
        return
    partes = PurePosixPath(resto).parts
    if not partes or ".." in partes or "\\" in resto or partes[0] == "avatars":
        raise HTTPException(status_code=422, detail=MSG_ADJUNTO_AJENO)
    if len(partes) == 2 and partes[0] == str(usuario.id_cuenta):
        return
    if len(partes) == 1:
        ya_usado = (await db.execute(select(or_(
            exists().where(MensajeProgramado.id_cuenta == usuario.id_cuenta, MensajeProgramado.url_media == url_media),
            exists().where(Plantilla.id_cuenta == usuario.id_cuenta, Plantilla.url_media == url_media),
        )))).scalar()
        if ya_usado:
            return
    raise HTTPException(status_code=422, detail=MSG_ADJUNTO_AJENO)
