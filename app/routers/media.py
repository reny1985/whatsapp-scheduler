"""
routers/media.py
Subida de archivos multimedia (imágenes, audio, video, documentos).
"""
import asyncio
import logging
import mimetypes
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app import envio_config as cfg
from app.auth import require_admin
from app.models import Usuario

logger = logging.getLogger("whatsapp_scheduler")

_default = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(_default)))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = cfg.MAX_ENTRADA_MB * 1024 * 1024     # tope del archivo ORIGINAL (el límite por tipo está en envio_config)
_NOMBRE_TIPO = {"image": "La imagen", "audio": "El audio", "video": "El video", "document": "El documento"}


def _mb(n: int) -> str:
    return f"{n / 1024 / 1024:.1f}".replace(".", ",")


def _error_peso(tipo: str, peso: int, convertido: bool = False) -> HTTPException:
    limite = cfg.LIMITE_MB[tipo]
    sugerencia = {"image": "Usa una imagen más liviana o reduce su resolución.",
                  "audio": "Graba un audio más corto.",
                  "video": "Prueba con un video más corto o de menor calidad.",
                  "document": "Comprime el documento o divídelo en partes."}[tipo]
    cuando = " después de convertirlo" if convertido else ""
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail=f"{_NOMBRE_TIPO[tipo]} pesa {_mb(peso)} MB{cuando}; el máximo permitido es {limite} MB. {sugerencia}",
    )

MIME_TO_TIPO: dict[str, str] = {
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video",
    "video/quicktime": "video",
    "video/mpeg": "video",
    "video/webm": "video",
    "video/3gpp": "video",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "audio/wav": "audio",
    "audio/mp4": "audio",
    "audio/aac": "audio",
    "audio/webm": "audio",
    "audio/x-m4a": "audio",
    "audio/3gpp": "audio",
    "application/pdf": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/msword": "document",
    "text/plain": "document",
    "application/vnd.ms-excel": "document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "document",
    "text/csv": "document",
}

# Extensión → mimetype de los documentos (también lo usa el scheduler al enviar)
DOC_EXT_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".doc": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".txt": "text/plain",
    ".csv": "text/csv",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

FFMPEG_TIMEOUT = {"audio": 120, "video": 600}

# MP4 H.264 + AAC, máx. 1280 px de ancho, dimensiones pares
FFMPEG_VIDEO_ARGS = [
    "-map", "0:v:0", "-map", "0:a:0?",
    "-vf", "scale=w='trunc(min(1280,iw)/2)*2':h=-2",
    "-c:v", "libx264", "-profile:v", "main", "-pix_fmt", "yuv420p",
    "-preset", "veryfast", "-crf", "26",
    "-c:a", "aac", "-b:a", "128k",
    "-movflags", "+faststart",
]

# OGG Opus mono 48 kHz (nota de voz)
FFMPEG_AUDIO_ARGS = [
    "-vn", "-map", "0:a:0",
    "-c:a", "libopus", "-b:a", "32k", "-ac", "1", "-ar", "48000",
    "-application", "voip",
]


def _error_conversion(tipo_media: str, detalle: str) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=f"No se pudo procesar el {tipo_media}: el archivo está dañado o tiene un "
               f"formato no soportado. {detalle}".strip(),
    )


async def _convertir_media(contenido: bytes, tipo_media: str, ext: str, unico: str, carpeta: Path = UPLOAD_DIR) -> str:
    """Convierte audio → .ogg (Opus) o video → .mp4 (H.264+AAC) dentro de `carpeta`. Devuelve el nombre final.

    Escribe el original en un temporal que siempre se borra. Si ffmpeg falla,
    no queda nada guardado y se lanza HTTPException.
    """
    salida_ext = ".ogg" if tipo_media == "audio" else ".mp4"
    entrada = carpeta / f".{unico}.orig{ext or '.bin'}"
    destino = carpeta / f"{unico}{salida_ext}"
    args = FFMPEG_AUDIO_ARGS if tipo_media == "audio" else FFMPEG_VIDEO_ARGS
    try:
        entrada.write_bytes(contenido)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-v", "error", "-i", str(entrada), *args, str(destino),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("ffmpeg no está instalado en el servidor")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"El servidor no puede convertir {tipo_media} en este momento (ffmpeg no disponible).",
            )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), FFMPEG_TIMEOUT[tipo_media])
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            logger.error("ffmpeg excedió el tiempo al convertir %s", unico)
            raise _error_conversion(tipo_media, "La conversión tardó demasiado; use un archivo más corto.")
        if proc.returncode != 0 or not destino.exists() or destino.stat().st_size == 0:
            logger.error("ffmpeg falló (rc=%s) en %s: %s", proc.returncode, unico,
                         stderr.decode(errors="replace")[-500:])
            raise _error_conversion(tipo_media, "")
        logger.info("%s convertido: %s", tipo_media.capitalize(), destino.name)
        return destino.name
    except BaseException:
        destino.unlink(missing_ok=True)
        raise
    finally:
        entrada.unlink(missing_ok=True)


router = APIRouter(prefix="/media", tags=["media"])


@router.post("/subir", status_code=status.HTTP_201_CREATED)
async def subir_archivo(file: UploadFile = File(...), usuario: Usuario = Depends(require_admin)):
    """Exige login. Cada cuenta guarda sus adjuntos en <UPLOAD_DIR>/<id_cuenta>/ (los anteriores a v1.1.0 siguen en
    la raíz y siguen funcionando). Un mensaje solo acepta adjuntos de su cuenta (ver aislamiento.validar_adjunto)."""
    content_type = file.content_type or ""
    if not content_type or content_type == "application/octet-stream":
        nombre_ext = Path(file.filename or "").suffix.lower()
        guessed = DOC_EXT_MIME.get(nombre_ext) or mimetypes.guess_type(file.filename or "")[0]
        content_type = guessed or "application/octet-stream"

    # Strip codecs parameter (e.g. "audio/webm;codecs=opus" → "audio/webm")
    base_ct = content_type.split(";")[0].strip()
    tipo_media = MIME_TO_TIPO.get(base_ct)
    if not tipo_media:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Tipo de archivo no soportado: {content_type}. "
                   "Use imagen (jpg/png/gif/webp), video (mp4/webm/mov/3gp), "
                   "audio (mp3/ogg/wav/aac/m4a) o documento (pdf/doc/docx/xls/xlsx/pptx/txt/csv).",
        )

    contenido = await file.read()
    if len(contenido) > MAX_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo supera el límite de {MAX_BYTES // (1024*1024)} MB",
        )
    if len(contenido) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo está vacío",
        )

    # Imágenes y documentos no se convierten: se mide el archivo tal cual. Audio y video se miden DESPUÉS de convertir.
    if tipo_media in ("image", "document") and len(contenido) > cfg.LIMITE_MB[tipo_media] * 1024 * 1024:
        raise _error_peso(tipo_media, len(contenido))

    original_stem = Path(file.filename or "archivo").stem
    ext = Path(file.filename or "").suffix.lower()
    if not ext and tipo_media == "document":
        ext = next((e for e, m in DOC_EXT_MIME.items() if m == base_ct), "")
    ext = ext or mimetypes.guess_extension(base_ct) or ""
    # Sin separadores de ruta en el nombre (el original lo eligió el cliente)
    original_stem = original_stem.replace("/", "_").replace("\\", "_")
    unico = f"{uuid.uuid4().hex}_{original_stem[:40]}"
    carpeta = UPLOAD_DIR / str(usuario.id_cuenta)
    carpeta.mkdir(parents=True, exist_ok=True)

    if tipo_media in ("audio", "video"):
        # Todo audio/video se recodifica; el original nunca se guarda.
        nombre_unico = await _convertir_media(contenido, tipo_media, ext, unico, carpeta)
        peso_final = (carpeta / nombre_unico).stat().st_size
        if peso_final > cfg.LIMITE_MB[tipo_media] * 1024 * 1024:
            (carpeta / nombre_unico).unlink(missing_ok=True)       # no queda guardado
            raise _error_peso(tipo_media, peso_final, convertido=True)
    else:
        nombre_unico = f"{unico}{ext}"
        (carpeta / nombre_unico).write_bytes(contenido)

    logger.info("Archivo subido: %s (%s, %d bytes)", nombre_unico, content_type, len(contenido))

    return {
        "url": f"/static/uploads/{usuario.id_cuenta}/{nombre_unico}",
        "tipo_media": tipo_media,
        "nombre_original": file.filename,
        "bytes": len(contenido),
        "content_type": content_type,
    }
