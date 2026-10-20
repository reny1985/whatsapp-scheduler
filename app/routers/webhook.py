"""
routers/webhook.py
Recibe eventos de Evolution API y actualiza la BD en tiempo real.
Desde v1.1.0 cada sesión tiene su URL con secreto (/webhook/evolution/<secreto>); la ruta vieja solo atiende
sesiones cuyo webhook nuevo aún no está confirmado (transición).

Eventos procesados:
  - GROUPS_UPSERT / groups.upsert  → inserta/actualiza grupos_whatsapp
  - GROUP_UPDATE  / group.update   → actualiza nombre/tamaño del grupo
  - CONNECTION_UPDATE              → sincroniza estado_conexion de la sesión
"""
import asyncio
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select, text, update

from app.database import AsyncSessionLocal
from app.models import SesionWhatsApp
from app.services import aviso_desconexion, perfil, reasignar

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

        # connecting en una sesión que ya tuvo número no es un QR pendiente: está desconectada
        if estado_evo.lower() == "connecting" and sesion.numero_telefono:
            nuevo_estado = "desconectado"

        estado_previo = sesion.estado_conexion
        extra = {}
        if nuevo_estado == "conectado":
            extra["aviso_desconexion_en"] = None    # reconectó: el próximo corte podrá avisar de nuevo
            extra["desconectada_en"] = None
            info = await _fetch_instancia_info(instancia)
            if info.get("apikey"):
                extra["token_autorizacion"] = info["apikey"]
            if info.get("numero") and not sesion.numero_telefono:
                extra["numero_telefono"] = info["numero"]
                logger.info("Webhook: número obtenido para '%s': %s", instancia, info["numero"])
        elif estado_previo == "conectado":
            extra["desconectada_en"] = datetime.now(timezone.utc)   # desde cuándo está desconectada

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

    if nuevo_estado == "conectado":
        # nombre y foto de perfil (como máximo una vez al día); en segundo plano y sin afectar al webhook
        asyncio.create_task(perfil.actualizar_perfil_si_corresponde(sesion.id_sesion))
        # si es el MISMO número que antes, los mensajes programados que quedaron sin sesión pasan a esta
        await reasignar.reasignar_huerfanos(sesion.id_sesion)
    elif nuevo_estado == "desconectado" and estado_previo == "conectado":
        # pasó de conectada a desconectada: si hay mensajes pendientes, un correo de aviso (uno por corte),
        # tras una espera corta por si se trata de un microcorte
        logger.info("Desconexión detectada en '%s' (webhook): se avisará por correo en %ds si sigue desconectada y hay pendientes",
                    instancia, aviso_desconexion.GRACIA_SEG)
        asyncio.create_task(aviso_desconexion.avisar_desconexion(sesion.id_sesion, espera=aviso_desconexion.GRACIA_SEG))


async def _sesion_por_instancia(instancia: str) -> SesionWhatsApp | None:
    async with AsyncSessionLocal() as db:
        return (await db.execute(
            select(SesionWhatsApp).where(SesionWhatsApp.instancia_evolution == instancia)
        )).scalar_one_or_none()


@router.post("/evolution/{secreto}")
async def recibir_evento_evolution_con_secreto(secreto: str, request: Request):
    """
    Webhook de Evolution con secreto por sesión (v1.1.0). Solo se procesa si el secreto existe Y el evento es de la
    MISMA instancia dueña de ese secreto: un evento con el secreto de una sesión no puede tocar otra sesión ni cuenta.
    El primer evento válido confirma que la URL nueva funciona (desde entonces la ruta vieja ignora esa instancia).
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "body no es JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body no es un objeto"}
    if not secreto or len(secreto) > 64:
        raise HTTPException(status_code=404, detail="No encontrado")
    async with AsyncSessionLocal() as db:
        sesion = (await db.execute(
            select(SesionWhatsApp).where(SesionWhatsApp.secreto_webhook == secreto)
        )).scalar_one_or_none()
        if not sesion:
            raise HTTPException(status_code=404, detail="No encontrado")
        instancia = body.get("instance") or ""
        if str(instancia) != sesion.instancia_evolution:
            logger.warning("Webhook: evento de la instancia '%s' con el secreto de otra sesión; se ignora", instancia)
            return {"ok": True, "ignorado": "instancia no coincide"}
        if sesion.webhook_confirmado_en is None:
            await db.execute(update(SesionWhatsApp).where(SesionWhatsApp.id_sesion == sesion.id_sesion)
                             .values(webhook_confirmado_en=datetime.now(timezone.utc)))
            await db.commit()
            logger.info("Webhook de '%s' confirmado por su primer evento en la URL con secreto", instancia)
    return await _procesar_evento(body)


@router.post("/evolution")
async def recibir_evento_evolution(request: Request):
    """
    Ruta VIEJA (sin secreto), solo para la transición de v1.1.0: acepta eventos de una instancia mientras su
    webhook con secreto no esté confirmado en Evolution. Una vez confirmado, sus eventos aquí se ignoran.
    Instancias desconocidas se ignoran como antes.
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "body no es JSON"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "body no es un objeto"}
    instancia = body.get("instance") or ""
    sesion = await _sesion_por_instancia(instancia)
    if sesion and sesion.webhook_confirmado_en is not None:
        logger.warning("Webhook (ruta vieja): '%s' ya usa la URL con secreto; el evento se ignora", instancia)
        return {"ok": True, "ignorado": "usa la ruta con secreto"}
    return await _procesar_evento(body)


async def _procesar_evento(body: dict):
    """Procesa un evento ya autorizado (por secreto o, en la transición, por la ruta vieja)."""
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
