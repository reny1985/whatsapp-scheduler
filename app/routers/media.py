"""
routers/media.py
Subida de archivos multimedia (imágenes, audio, video, documentos).
"""
import logging
import mimetypes
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile, status

logger = logging.getLogger("whatsapp_scheduler")

_default = Path(__file__).resolve().parent.parent / "static" / "uploads"
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(_default)))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

MAX_BYTES = 50 * 1024 * 1024

MIME_TO_TIPO: dict[str, str] = {
    "image/jpeg": "image",
    "image/png": "image",
    "image/gif": "image",
    "image/webp": "image",
    "video/mp4": "video",
    "video/mpeg": "video",
    "video/webm": "video",
    "video/3gpp": "video",
    "audio/mpeg": "audio",
    "audio/ogg": "audio",
    "audio/wav": "audio",
    "audio/mp4": "audio",
    "audio/aac": "audio",
    "audio/webm": "audio",
    "application/pdf": "document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "document",
    "application/msword": "document",
    "text/plain": "document",
}

router = APIRouter(prefix="/media", tags=["media"])


@router.post("/subir", status_code=status.HTTP_201_CREATED)
async def subir_archivo(file: UploadFile = File(...)):
    content_type = file.content_type or ""
    if not content_type or content_type == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(file.filename or "")
        content_type = guessed or "application/octet-stream"

    tipo_media = MIME_TO_TIPO.get(content_type)
    if not tipo_media:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Tipo de archivo no soportado: {content_type}. "
                   "Use imagen (jpg/png/gif/webp), video (mp4/webm), "
                   "audio (mp3/ogg/wav/aac) o documento (pdf/docx/xlsx).",
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

    original_stem = Path(file.filename or "archivo").stem
    ext = Path(file.filename or "").suffix.lower() or mimetypes.guess_extension(content_type) or ""
    nombre_unico = f"{uuid.uuid4().hex}_{original_stem[:40]}{ext}"
    destino = UPLOAD_DIR / nombre_unico

    destino.write_bytes(contenido)
    logger.info("Archivo subido: %s (%s, %d bytes)", nombre_unico, content_type, len(contenido))

    return {
        "url": f"/static/uploads/{nombre_unico}",
        "tipo_media": tipo_media,
        "nombre_original": file.filename,
        "bytes": len(contenido),
        "content_type": content_type,
    }
