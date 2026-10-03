"""
routers/webhook.py
Recibe eventos de Evolution API y actualiza la BD en tiempo real.

Eventos procesados:
  - GROUPS_UPSERT / groups.upsert  → inserta/actualiza grupos_whatsapp
  - GROUP_UPDATE  / group.update   → actualiza nombre/tamaño del grupo
  - CONNECTION_UPDATE              → sincroniza estado_conexion de la sesión
"""
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import select, text, update

from app.database import AsyncSessionLocal
from app.models import SesionWhatsApp

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")

logger = logging.getLogger("whatsapp_scheduler")

router = APIRouter(prefix="/webhook", tags=["webhook"])


def _normalizar_evento(raw: str) -> str:
    """Normaliza nombres de evento: 'groups.upsert' → 'GROUPS_UPSERT'."""
    return raw.upper().replace(".", "_")


async def _upsert_grupos(id_sesion: str, grupos: list[dict]) -> int:
    """Inserta o actualiza grupos en la caché. Retorna cantidad procesada."""
    if not grupos:
        return 0
    validos = [g for g in grupos if g.get("id") and "@g.us" in (g.get("id") or "")]
    if not validos:
        return 0

    async with AsyncSessionLocal() as db:
        for g in validos:
            await db.execute(text("""
                INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size, actualizado_en)
                VALUES (:id, :sid, :subject, :size, now())
                ON CONFLICT (id_grupo, id_sesion) DO UPDATE
                  SET subject = EXCLUDED.subject,
                      size    = EXCLUDED.size,
                      actualizado_en = now()
            """), {
                "id": g["id"],
                "sid": id_sesion,
                "subject": g.get("subject") or g.get("name") or "",
                "size": g.get("size") or 0,
            })
        await db.commit()
    return len(validos)


async def _fetch_instancia_info(instancia: str) -> dict:
    """Devuelve {apikey, owner, numero} de la instancia desde Evolution API."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{EVOLUTION_URL}/instance/fetchInstances?instanceName={instancia}",
                headers={"apikey": EVOLUTION_KEY},
            )
        if r.status_code != 200:
            return {}
        data = r.json()
        # Evolution devuelve objeto o lista según la versión
        inst_data = data if isinstance(data, dict) else (data[0] if data else {})
        info = inst_data.get("instance", inst_data)
        owner = info.get("owner", "")
        return {
            "apikey": info.get("apikey", ""),
            "owner": owner,
            "numero": owner.split("@")[0] if owner else "",
        }
    except Exception as exc:
        logger.warning("No se pudo obtener info de '%s': %s", instancia, exc)
    return {}


async def _sincronizar_conexion(instancia: str, estado_evo: str) -> None:
    """Actualiza estado_conexion en BD según el estado reportado por Evolution."""
    nuevo_estado = {
        "open": "conectado",
        "close": "desconectado",
        "closed": "desconectado",
        "connecting": "qr_pendiente",
    }.get(estado_evo.lower(), None)

    if not nuevo_estado:
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(SesionWhatsApp).where(
                SesionWhatsApp.instancia_evolution == instancia
            )
        )
        sesion = result.scalar_one_or_none()
        if not sesion:
            logger.warning("Webhook connection.update: instancia '%s' no encontrada en BD", instancia)
            return

        extra = {}
        if nuevo_estado == "conectado":
            info = await _fetch_instancia_info(instancia)
            if info.get("apikey"):
                extra["token_autorizacion"] = info["apikey"]
            if info.get("numero") and not sesion.numero_telefono:
                extra["numero_telefono"] = info["numero"]
                logger.info("Webhook: número obtenido para '%s': %s", instancia, info["numero"])

        await db.execute(
            update(SesionWhatsApp)
            .where(SesionWhatsApp.instancia_evolution == instancia)
            .values(
                estado_conexion=nuevo_estado,
                fecha_ultima_conexion=(
                    datetime.now(timezone.utc) if nuevo_estado == "conectado"
                    else sesion.fecha_ultima_conexion
                ),
                **extra,
            )
        )
        await db.commit()
        logger.info(
            "Webhook CONNECTION_UPDATE: instancia='%s' → estado='%s'",
            instancia, nuevo_estado,
        )


@router.post("/evolution")
async def recibir_evento_evolution(request: Request):
    """
    Endpoint que recibe todos los webhooks de Evolution API.
    No requiere autenticación — Evolution API envía desde la red interna.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "body no es JSON"}

    evento_raw = body.get("event") or body.get("type") or ""
    evento = _normalizar_evento(str(evento_raw))
    instancia = body.get("instance") or ""
    data = body.get("data") or {}

    logger.debug("Webhook Evolution: evento=%s instancia=%s", evento, instancia)

    # ── Grupos creados / actualizados ─────────────────────────────────────────
    if evento in ("GROUPS_UPSERT", "GROUP_UPDATE", "GROUP_UPSERT"):
        grupos_raw = data if isinstance(data, list) else ([data] if data else [])

        # Buscar id_sesion por instancia
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(SesionWhatsApp.id_sesion).where(
                    SesionWhatsApp.instancia_evolution == instancia
                )
            )
            row = result.scalar_one_or_none()

        if not row:
            logger.warning("Webhook grupos: instancia '%s' no encontrada en BD", instancia)
            return {"ok": True, "ignorado": "instancia desconocida"}

        n = await _upsert_grupos(str(row), grupos_raw)
        logger.info(
            "Webhook %s: instancia='%s' → %d grupo(s) actualizados",
            evento, instancia, n,
        )
        return {"ok": True, "grupos_actualizados": n}

    # ── Estado de conexión ────────────────────────────────────────────────────
    if evento == "CONNECTION_UPDATE":
        estado_evo = (
            (data.get("state") if isinstance(data, dict) else None)
            or body.get("state")
            or ""
        )
        if estado_evo:
            await _sincronizar_conexion(instancia, estado_evo)
        return {"ok": True}

    # Evento no manejado — ignorar silenciosamente
    return {"ok": True, "ignorado": evento}
