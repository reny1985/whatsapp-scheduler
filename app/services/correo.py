"""
services/correo.py
Envío de correos genéricos por Resend (mismo remitente y clave que el correo de verificación).
Espera la respuesta de Resend; si falla lanza CorreoError (el motivo queda en el log, nunca la clave).
"""
import logging
import os

import httpx

logger = logging.getLogger("whatsapp_scheduler")

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM = os.getenv("RESEND_FROM", "Enviafast <no-reply@enviafast.net>")
RESEND_TIMEOUT = httpx.Timeout(10.0)


class CorreoError(Exception):
    """Resend rechazó el correo, no respondió o no está configurado."""


async def enviar(to: str, asunto: str, html: str, texto: str | None = None) -> str | None:
    """Envía un correo y devuelve el id de Resend."""
    if not RESEND_API_KEY:
        raise CorreoError("RESEND_API_KEY no configurada")
    cuerpo = {"from": RESEND_FROM, "to": [to], "subject": asunto, "html": html}
    if texto:
        cuerpo["text"] = texto
    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json=cuerpo,
            )
    except httpx.TimeoutException:
        raise CorreoError("timeout")
    except httpx.HTTPError as exc:
        raise CorreoError(f"red ({type(exc).__name__})")
    if r.status_code >= 400:
        raise CorreoError(f"HTTP {r.status_code} {r.text[:200]}")
    try:
        return r.json().get("id")
    except ValueError:
        return None
