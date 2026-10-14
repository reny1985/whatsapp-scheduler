"""
services/evolution_estado.py
Estado REAL de una instancia en Evolution (connectionState), en solo lectura y solo desde el servidor.
Lo usan el chequeo de cada minuto y el aviso de desconexión para no fiarse únicamente del estado guardado en la base.
"""
import logging
import os

import httpx

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")


async def consultar_estado(sesion) -> str | None:
    """'open' | 'close' | 'connecting' (según Evolution), o None si no se pudo consultar."""
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0)) as client:
            r = await client.get(f"{EVOLUTION_URL}/instance/connectionState/{sesion.instancia_evolution}",
                                 headers={"apikey": sesion.token_autorizacion or EVOLUTION_KEY})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        datos = r.json()
        return str((datos.get("instance") or datos).get("state") or "").lower() or None
    except Exception as exc:
        logger.info("Estado real de '%s': no se pudo consultar a Evolution (%s: %s)",
                    sesion.instancia_evolution, type(exc).__name__, exc)
        return None
