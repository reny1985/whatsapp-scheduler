"""
services/perfil.py
Nombre y foto de perfil de WhatsApp de cada sesión.

- Se consulta a Evolution (chat/fetchProfile) con el token de la instancia, como máximo una vez al día
  por sesión (al conectarse y al pulsar "Verificar").
- La foto se DESCARGA y se guarda en <UPLOAD_DIR>/avatars/<id_sesion>.jpg: el enlace de WhatsApp caduca,
  por eso nunca se guarda como referencia. Se sirve por GET /sesiones/{id}/foto (con login); la
  clave y la URL de Evolution nunca llegan al navegador.
- Si algo falla no se rompe nada: se registra el motivo (sin claves) y se conserva lo que ya había.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.database import AsyncSessionLocal
from app.models import SesionWhatsApp
from app.routers import media
from app.services.evolution_client import EVOLUTION_API_KEY, EVOLUTION_API_URL

logger = logging.getLogger("whatsapp_scheduler")

PERFIL_CADA = timedelta(hours=24)
REINTENTO_TRAS_ERROR = timedelta(hours=1)
MAX_FOTO_BYTES = 2 * 1024 * 1024
TIMEOUT = httpx.Timeout(10.0)


def dir_avatars() -> Path:
    return Path(media.UPLOAD_DIR) / "avatars"


def ruta_foto(id_sesion) -> Path:
    return dir_avatars() / f"{id_sesion}.jpg"


def tipo_imagen(datos: bytes) -> str | None:
    """Tipo MIME por los primeros bytes (el archivo se llama .jpg pero puede ser png/webp)."""
    if datos[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if datos[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "image/webp"
    return None


def debe_actualizarse(sesion, ahora: datetime | None = None) -> bool:
    ahora = ahora or datetime.now(timezone.utc)
    if sesion.estado_conexion != "conectado" or not sesion.numero_telefono:
        return False
    return sesion.perfil_consultado_en is None or ahora - sesion.perfil_consultado_en >= PERFIL_CADA


def _limpiar_nombre(nombre) -> str | None:
    """Nombre válido para mostrar, o None. Descarta lo que es solo un número de teléfono: en cuentas
    personales Evolution devuelve en fetchProfile.name el número formateado ("+593 96 982 9845")."""
    if not isinstance(nombre, str):
        return None
    limpio = "".join(c for c in nombre if c.isprintable()).strip()
    if not limpio or not any(c.isalpha() for c in limpio):
        return None
    return limpio[:100]


def elegir_nombre(*candidatos) -> str | None:
    """El primer candidato que sea un nombre de verdad (en el orden dado)."""
    return next((n for n in map(_limpiar_nombre, candidatos) if n), None)


async def _nombre_de_fetch_instances(instancia: str) -> str | None:
    """Nombre de perfil de WhatsApp (pushName) que Evolution expone como profileName en fetchInstances.
    Ese endpoint solo acepta la clave global (con el token de la instancia responde 401); se usa solo
    en el servidor. Si falla, se ignora: el nombre es opcional."""
    if not EVOLUTION_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(f"{EVOLUTION_API_URL}/instance/fetchInstances",
                                 params={"instanceName": instancia}, headers={"apikey": EVOLUTION_API_KEY})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        datos = r.json()
        datos = datos[0] if isinstance(datos, list) and datos else datos
        datos = datos.get("instance", datos) if isinstance(datos, dict) else {}
        return elegir_nombre(datos.get("profileName"), datos.get("pushName"))
    except Exception as exc:
        logger.info("Perfil de '%s': no se pudo leer profileName de fetchInstances (%s: %s)",
                    instancia, type(exc).__name__, exc)
        return None


async def _consultar_evolution(sesion) -> dict:
    """Devuelve {'nombre': str|None, 'foto_url': str|None}. Lanza si Evolution falla."""
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        r = await client.post(
            f"{EVOLUTION_API_URL}/chat/fetchProfile/{sesion.instancia_evolution}",
            headers={"apikey": sesion.token_autorizacion or EVOLUTION_API_KEY},
            json={"number": sesion.numero_telefono},
        )
    if r.status_code != 200:
        raise RuntimeError(f"Evolution respondió HTTP {r.status_code}")
    datos = r.json()
    if not isinstance(datos, dict):
        raise RuntimeError("respuesta de Evolution inesperada")
    foto = datos.get("picture") or datos.get("profilePictureUrl")
    # Orden: nombre de perfil de WhatsApp (fetchInstances.profileName) → fetchProfile.name/pushName
    nombre = elegir_nombre(await _nombre_de_fetch_instances(sesion.instancia_evolution),
                           datos.get("name"), datos.get("pushName"), datos.get("verifiedName"))
    return {
        "nombre": nombre,
        "foto_url": foto if isinstance(foto, str) and foto.startswith("http") else None,
    }


async def _descargar_foto(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=True) as client:
        r = await client.get(url)
    if r.status_code != 200:
        raise RuntimeError(f"la foto respondió HTTP {r.status_code}")
    if len(r.content) > MAX_FOTO_BYTES:
        raise RuntimeError("la foto es demasiado grande")
    if tipo_imagen(r.content) is None:
        raise RuntimeError("el archivo descargado no es una imagen")
    return r.content


def _guardar_foto(id_sesion, datos: bytes) -> None:
    destino = ruta_foto(id_sesion)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(".tmp")
    tmp.write_bytes(datos)
    os.replace(tmp, destino)      # escritura atómica: nunca queda una foto a medias


_locks: dict = {}


async def actualizar_perfil_si_corresponde(id_sesion, forzar: bool = False) -> bool:
    """Actualiza nombre y foto si pasó un día desde la última consulta. Nunca lanza.

    Un candado por sesión evita consultas dobles (p. ej. dos eventos de conexión casi simultáneos).
    Devuelve True si se consultó a Evolution con éxito."""
    async with _locks.setdefault(id_sesion, asyncio.Lock()):
        return await _actualizar(id_sesion, forzar)


async def _actualizar(id_sesion, forzar: bool) -> bool:
    ahora = datetime.now(timezone.utc)
    try:
        async with AsyncSessionLocal() as db:
            sesion = await db.get(SesionWhatsApp, id_sesion)
            if not sesion:
                return False
            if not forzar and not debe_actualizarse(sesion, ahora):
                return False
            if sesion.estado_conexion != "conectado" or not sesion.numero_telefono:
                logger.info("Perfil de '%s': sesión no conectada o sin número, se omite", sesion.instancia_evolution)
                return False

            instancia = sesion.instancia_evolution
            try:
                perfil = await _consultar_evolution(sesion)
            except Exception as exc:
                logger.warning("Perfil de '%s': no se pudo consultar a Evolution (%s: %s)",
                               instancia, type(exc).__name__, exc)
                # reintento en ~1 h en vez de esperar un día
                sesion.perfil_consultado_en = ahora - (PERFIL_CADA - REINTENTO_TRAS_ERROR)
                await db.commit()
                return False

            sesion.perfil_nombre = perfil["nombre"] or sesion.perfil_nombre
            # sin nombre todavía (recién vinculada, WhatsApp aún sincroniza): se reintenta en ~1 h
            ok = bool(sesion.perfil_nombre)
            if perfil["foto_url"]:
                try:
                    _guardar_foto(id_sesion, await _descargar_foto(perfil["foto_url"]))
                    sesion.perfil_foto_en = ahora
                except Exception as exc:
                    ok = False
                    logger.warning("Perfil de '%s': no se pudo descargar la foto (%s: %s)",
                                   instancia, type(exc).__name__, exc)
            else:
                # sin foto (privacidad o no tiene): se borra la anterior para no mostrar una desactualizada
                ruta_foto(id_sesion).unlink(missing_ok=True)
                sesion.perfil_foto_en = None
                logger.info("Perfil de '%s': sin foto de perfil disponible", instancia)

            sesion.perfil_consultado_en = ahora if ok else ahora - (PERFIL_CADA - REINTENTO_TRAS_ERROR)
            await db.commit()
            logger.info("Perfil de '%s' actualizado (nombre: %s, foto: %s)", instancia,
                        "sí" if sesion.perfil_nombre else "no", "sí" if sesion.perfil_foto_en else "no")
            return ok
    except Exception as exc:    # nunca debe romper el flujo que lo llamó
        logger.error("Perfil: error inesperado (%s: %s)", type(exc).__name__, exc)
        return False
