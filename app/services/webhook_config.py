"""
services/webhook_config.py
Webhook de Evolution con secreto por sesión: cada instancia envía sus eventos a /webhook/evolution/<secreto>.

Transición segura (v1.1.0): la ruta vieja /webhook/evolution sigue aceptando eventos de una sesión mientras no se
haya CONFIRMADO en Evolution (GET /webhook/find) que esa instancia ya usa la URL nueva. Al confirmarlo se guarda
webhook_confirmado_en y desde entonces la ruta vieja ignora esa instancia. Un job periódico migra las sesiones que
quedan pendientes (por ejemplo, si Evolution no respondía al desplegar). Nunca se escribe el secreto ni el token en el log.
"""
import logging
import os
import secrets
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, update

from app.database import AsyncSessionLocal
from app.models import SesionWhatsApp

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")
EVENTOS = ["GROUPS_UPSERT", "GROUP_UPDATE", "CONNECTION_UPDATE", "QRCODE_UPDATED"]


def nuevo_secreto() -> str:
    return secrets.token_hex(32)       # 64 caracteres hex


def url_webhook(secreto: str) -> str:
    base = os.getenv("BASE_PUBLIC_URL", "https://app.enviafast.net")
    return base.rstrip("/") + "/webhook/evolution/" + secreto


def _headers(token: str | None) -> dict:
    return {"apikey": token or EVOLUTION_KEY, "Content-Type": "application/json"}


def _url_configurada(data) -> str | None:
    """Evolution 1.8.x devuelve {enabled, url, ...}; se toleran variantes ({webhook: {url}} o lista)."""
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("webhook"), dict):
        data = data["webhook"]
    return data.get("url")


async def configurar_instancia(instancia: str, token: str | None, secreto: str | None,
                               con_ajustes: bool = True) -> bool:
    """Apunta el webhook de la instancia a la URL con secreto (y, si con_ajustes, aplica los settings de siempre).
    Devuelve True si Evolution confirma que la URL nueva quedó configurada. Nunca lanza."""
    if not secreto:
        logger.warning("Webhook de '%s': la sesión no tiene secreto; no se configura", instancia)
        return False
    destino = url_webhook(secreto)
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            r = await client.post(
                f"{EVOLUTION_URL}/webhook/set/{instancia}",
                headers=_headers(token),
                json={"enabled": True, "url": destino, "webhookByEvents": False,
                      "webhookBase64": False, "events": EVENTOS},
            )
            logger.info("Webhook (URL con secreto) configurado para '%s': HTTP %s", instancia, r.status_code)
        except Exception as exc:
            logger.warning("No se pudo configurar webhook para '%s': %s", instancia, type(exc).__name__)
            return False

        if con_ajustes:
            try:
                r = await client.post(
                    f"{EVOLUTION_URL}/settings/set/{instancia}",
                    headers=_headers(token),
                    json={
                        "reject_call": False,
                        "msg_call": "",
                        "groups_ignore": False,
                        "always_online": False,
                        "read_messages": False,
                        "read_status": False,
                        "sync_full_history": True,
                        "wavoipToken": "",
                    },
                )
                logger.info("Settings configurados para '%s': HTTP %s", instancia, r.status_code)
            except Exception as exc:
                logger.warning("No se pudo configurar settings para '%s': %s", instancia, type(exc).__name__)

        try:
            r = await client.get(f"{EVOLUTION_URL}/webhook/find/{instancia}", headers=_headers(token))
            actual = _url_configurada(r.json()) if r.status_code == 200 and r.text.strip() else None
        except Exception as exc:
            logger.warning("No se pudo comprobar el webhook de '%s': %s", instancia, type(exc).__name__)
            return False
    ok = actual == destino
    if not ok:
        logger.warning("Webhook de '%s': Evolution aún no muestra la URL con secreto (la ruta vieja sigue activa)", instancia)
    return ok


async def configurar_y_confirmar(id_sesion, con_ajustes: bool = True) -> bool:
    """Configura el webhook de una sesión y, si Evolution lo confirma, guarda webhook_confirmado_en. Nunca lanza."""
    try:
        async with AsyncSessionLocal() as db:
            sesion = await db.get(SesionWhatsApp, id_sesion)
            if not sesion:
                return False
            if not sesion.secreto_webhook:
                sesion.secreto_webhook = nuevo_secreto()
                await db.commit()
            instancia, token, secreto = sesion.instancia_evolution, sesion.token_autorizacion, sesion.secreto_webhook
        ok = await configurar_instancia(instancia, token, secreto, con_ajustes=con_ajustes)
        if ok:
            async with AsyncSessionLocal() as db:
                await db.execute(update(SesionWhatsApp).where(SesionWhatsApp.id_sesion == id_sesion)
                                 .values(webhook_confirmado_en=datetime.now(timezone.utc)))
                await db.commit()
            logger.info("Webhook de '%s' confirmado con la URL con secreto: la ruta vieja ya no acepta sus eventos", instancia)
        return ok
    except Exception as exc:
        logger.error("Webhook: error inesperado al configurar la sesión %s (%s)", id_sesion, type(exc).__name__)
        return False


async def migrar_pendientes() -> int:
    """Job periódico: pasa a la URL con secreto las sesiones vinculadas que aún no la tienen confirmada.
    Solo toca el webhook (no los settings). Devuelve cuántas quedaron confirmadas."""
    async with AsyncSessionLocal() as db:
        ids = (await db.execute(
            select(SesionWhatsApp.id_sesion).where(
                SesionWhatsApp.webhook_confirmado_en.is_(None),
                SesionWhatsApp.numero_telefono.is_not(None),
                SesionWhatsApp.token_autorizacion.is_not(None),
            )
        )).scalars().all()
    confirmadas = 0
    for id_sesion in ids:
        if await configurar_y_confirmar(id_sesion, con_ajustes=False):
            confirmadas += 1
    if ids:
        logger.info("Webhook: %d de %d sesión(es) pendientes quedaron con la URL con secreto", confirmadas, len(ids))
    return confirmadas
