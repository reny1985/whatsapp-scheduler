"""
services/aviso_desconexion.py
Correo de aviso cuando una sesión de WhatsApp pasa de conectada a desconectada y tiene mensajes
pendientes. UN solo correo por desconexión: se marca aviso_desconexion_en y no se vuelve a enviar
hasta que la sesión se reconecte (al reconectar se limpia). Si el correo falla, no rompe nada: queda en
el log y se libera la marca para poder reintentar en el siguiente evento.
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from html import escape

from sqlalchemy import func, select, update

from app.database import AsyncSessionLocal
from app.models import MensajeProgramado, SesionWhatsApp, Usuario
from app.services import correo

logger = logging.getLogger("whatsapp_scheduler")

URL_APP = os.getenv("APP_URL", "https://enviafast.net")
# Logo alojado en /static (PENDIENTE: subir el archivo y definir EMAIL_LOGO_URL, p. ej. https://enviafast.net/static/logo.png)
LOGO_URL = os.getenv("EMAIL_LOGO_URL", "")
VERDE = "#25D366"
EC = timezone(timedelta(hours=-5))      # Ecuador no tiene horario de verano
PENDIENTES = ("programado", "en_cola")


def _hora_ecuador(dt: datetime) -> str:
    dt = (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(EC)
    hora12 = dt.strftime("%I:%M").lstrip("0")
    return f"{dt.strftime('%d/%m/%Y')} a las {hora12} {'a. m.' if dt.hour < 12 else 'p. m.'}"


def construir_correo(numero: str, pendientes: int, proximo: datetime | None) -> tuple[str, str, str]:
    """(asunto, html, texto) del aviso."""
    asunto = "[Enviafast] Tu WhatsApp se desconectó"
    plural = "mensajes pendientes" if pendientes != 1 else "mensaje pendiente"
    linea_proximo = f"El próximo está programado para el {_hora_ecuador(proximo)} (hora de Ecuador)." if proximo else ""
    num = f"+{numero}" if numero else "tu número de WhatsApp"
    logo = (f'<img src="{escape(LOGO_URL)}" alt="Enviafast" height="36" style="display:block;margin:0 auto 6px">'
            if LOGO_URL else "<!-- LOGO: poner aquí un <img> alojado en /static cuando esté listo (EMAIL_LOGO_URL) -->")
    html = f"""
    <div style="font-family:Arial,sans-serif;max-width:520px;margin:0 auto;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
      <div style="background:{VERDE};padding:22px 20px;text-align:center">
        {logo}
        <div style="color:#073b1c;font-size:26px;font-weight:700;letter-spacing:.5px">Enviafast</div>
      </div>
      <div style="padding:26px 24px;color:#111827">
        <h2 style="margin:0 0 12px;font-size:20px">Tu WhatsApp se desconectó</h2>
        <p style="margin:0 0 10px;line-height:1.5">El número <strong>{escape(num)}</strong> ya no está vinculado a Enviafast.</p>
        <p style="margin:0 0 10px;line-height:1.5">Tienes <strong>{pendientes} {plural}</strong> que no se enviarán hasta que lo vuelvas a vincular. {escape(linea_proximo)}</p>
        <p style="margin:0 0 22px;line-height:1.5;color:#6b7280;font-size:14px">Si pasan más de unos minutos de la hora programada, el mensaje se marcará como vencido y tendrás que reprogramarlo.</p>
        <div style="text-align:center;margin:8px 0 6px">
          <a href="{escape(URL_APP)}" style="background:{VERDE};color:#073b1c;text-decoration:none;font-weight:700;padding:13px 28px;border-radius:8px;display:inline-block">Volver a vincular</a>
        </div>
      </div>
    </div>"""
    texto = (f"Tu WhatsApp se desconectó.\n\nEl número {num} ya no está vinculado a Enviafast. "
             f"Tienes {pendientes} {plural} que no se enviarán hasta que lo vuelvas a vincular. {linea_proximo}\n\n"
             f"Volver a vincular: {URL_APP}\n")
    return asunto, html, texto


async def avisar_desconexion(id_sesion) -> bool:
    """Envía el aviso si corresponde. Nunca lanza. Devuelve True si se envió."""
    reclamado = False
    try:
        async with AsyncSessionLocal() as db:
            sesion = await db.get(SesionWhatsApp, id_sesion)
            if not sesion or sesion.estado_conexion == "conectado" or sesion.aviso_desconexion_en is not None:
                return False
            usuario = await db.get(Usuario, sesion.id_usuario)
            if not usuario or not usuario.email_verificado or "@" not in usuario.email or usuario.email.endswith("@localhost"):
                logger.info("Aviso de desconexión: el usuario no tiene un correo verificado, se omite")
                return False
            n, proximo = (await db.execute(
                select(func.count(), func.min(MensajeProgramado.fecha_hora_disparo))
                .where(MensajeProgramado.id_sesion == id_sesion, MensajeProgramado.estado_envio.in_(PENDIENTES))
            )).one()
            if not n:
                return False

            # Reclama el aviso (un solo correo por desconexión, aunque lleguen varios eventos a la vez)
            res = await db.execute(
                update(SesionWhatsApp)
                .where(SesionWhatsApp.id_sesion == id_sesion, SesionWhatsApp.aviso_desconexion_en.is_(None))
                .values(aviso_desconexion_en=datetime.now(timezone.utc))
            )
            await db.commit()
            if not res.rowcount:
                return False
            reclamado = True

            asunto, html, texto = construir_correo(sesion.numero_telefono or "", n, proximo)
            id_correo = await correo.enviar(usuario.email, asunto, html, texto)
            logger.info("Aviso de desconexión enviado (sesión '%s', %d pendientes, id=%s)", sesion.instancia_evolution, n, id_correo)
            return True
    except Exception as exc:
        logger.error("Aviso de desconexión: no se pudo enviar el correo (%s: %s)", type(exc).__name__, exc)
        if reclamado:    # se libera para poder reintentar en un próximo evento
            try:
                async with AsyncSessionLocal() as db:
                    await db.execute(update(SesionWhatsApp).where(SesionWhatsApp.id_sesion == id_sesion)
                                     .values(aviso_desconexion_en=None))
                    await db.commit()
            except Exception as exc2:
                logger.error("Aviso de desconexión: no se pudo liberar la marca (%s)", exc2)
        return False
