"""
routers/auth.py
Login, logout, perfil, setup inicial y verificación de correo.
"""
import logging
import os
import random
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    ACCESS_TOKEN_EXPIRE_HOURS,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.database import get_db
from app.models import CUENTA_INTERNA_ID, Usuario
from app.schemas import PILOTO_ID_USUARIO
from app.services import intentos

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/auth", tags=["auth"])

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "Enviafast <no-reply@enviafast.net>")
APP_NAME       = "Enviafast"   # nombre visible en asunto y cuerpo del correo
RESEND_TIMEOUT = httpx.Timeout(10.0)
MSG_ERROR_CORREO = "No pudimos enviar el correo, intenta de nuevo en unos minutos"
# Hash de una contraseña que nadie conoce: el login lo compara cuando el correo no existe (mismo tiempo de respuesta)
_HASH_FICTICIO = hash_password(os.urandom(16).hex())


class EnvioCorreoError(Exception):
    """Resend rechazó el correo, no respondió o no está configurado."""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generar_codigo() -> str:
    return str(random.randint(100000, 999999))


async def _enviar_email(to: str, codigo: str) -> str | None:
    """Envía el código por Resend y ESPERA la respuesta. Devuelve el id del correo.

    Lanza EnvioCorreoError si falla (el motivo queda en el log, nunca la clave).
    """
    if not RESEND_API_KEY:
        logger.error("Correo a %s no enviado: RESEND_API_KEY no configurada", to)
        raise EnvioCorreoError("RESEND_API_KEY no configurada")

    asunto = f"[{APP_NAME}] Tu código de verificación: {codigo}"
    cuerpo = f"""
    <div style="font-family:sans-serif;max-width:480px;margin:0 auto">
      <h2 style="color:#25d366">{APP_NAME}</h2>
      <p>Tu código de verificación es:</p>
      <div style="font-size:2.5rem;font-weight:700;letter-spacing:.3em;color:#111;
                  background:#f4f4f4;padding:20px;text-align:center;border-radius:8px">
        {codigo}
      </div>
      <p style="color:#888;font-size:.85rem;margin-top:16px">
        Válido por 30 minutos. Si no solicitaste este código, ignora este mensaje.
      </p>
    </div>
    """
    try:
        async with httpx.AsyncClient(timeout=RESEND_TIMEOUT) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": RESEND_FROM, "to": [to], "subject": asunto, "html": cuerpo},
            )
    except httpx.TimeoutException:
        logger.error("Resend no respondió a tiempo al enviar correo a %s (timeout %ss)", to, RESEND_TIMEOUT.read)
        raise EnvioCorreoError("timeout")
    except httpx.HTTPError as exc:
        logger.error("Error de red con Resend al enviar correo a %s: %s", to, type(exc).__name__)
        raise EnvioCorreoError("red")

    if r.status_code >= 400:
        logger.error("Resend rechazó el correo a %s: HTTP %s %s", to, r.status_code, r.text[:300])
        raise EnvioCorreoError(f"HTTP {r.status_code}")

    try:
        id_correo = r.json().get("id")
    except ValueError:
        id_correo = None
    logger.info("Código de verificación enviado a %s via Resend (id=%s)", to, id_correo)
    return id_correo


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    confirmar_password: str


class VerificarCodigoRequest(BaseModel):
    email: EmailStr
    codigo: str = Field(min_length=6, max_length=6)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    # Límite de intentos: por correo (adivinar una contraseña) y por IP (probar muchos correos)
    ip = intentos.ip_cliente(request)
    k_correo = intentos.clave("login", "correo", payload.email)
    k_ip = intentos.clave("login", "ip", ip)
    espera = max(
        await intentos.bloqueado(db, k_correo, intentos.LOGIN_MAX_FALLOS_CORREO, intentos.LOGIN_VENTANA),
        await intentos.bloqueado(db, k_ip, intentos.LOGIN_MAX_FALLOS_IP, intentos.LOGIN_VENTANA),
    )
    if espera:
        logger.warning("Login bloqueado por demasiados intentos (ip=%s, faltan %ss)", ip, espera)
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail=intentos.mensaje_espera(espera), headers={"Retry-After": str(espera)})

    res = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    user = res.scalar_one_or_none()

    # Si el correo no existe se compara igual contra un hash, para no revelar por el tiempo de respuesta si existe
    hash_a_comparar = user.password_hash if user else _HASH_FICTICIO
    if not verify_password(payload.password, hash_a_comparar) or not user:
        await intentos.registrar_fallo(db, k_correo, intentos.LOGIN_VENTANA)
        await intentos.registrar_fallo(db, k_ip, intentos.LOGIN_VENTANA)
        await db.commit()
        logger.info("Login fallido (ip=%s)", ip)      # nunca el correo ni la contraseña
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )

    await intentos.limpiar(db, k_correo)
    await db.commit()

    if not user.email_verificado:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_no_verificado",
        )

    token = create_access_token(user.id_usuario, user.rol, user.version_token)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return {"ok": True, "rol": user.rol, "email": user.email}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie("access_token", samesite="lax")
    return {"ok": True}


@router.get("/me")
async def me(user: Usuario = Depends(get_current_user)):
    return {"id": str(user.id_usuario), "email": user.email, "rol": user.rol}


@router.post("/setup", status_code=status.HTTP_201_CREATED)
async def setup(payload: SetupRequest, db: AsyncSession = Depends(get_db)):
    """
    Configura el primer admin. Solo funciona mientras el usuario piloto
    tenga la contraseña placeholder 'piloto-no-auth'.
    Después del registro envía un código de verificación por email.
    """
    # Se valida primero y sin tocar la BD ni enviar correo. Nunca se registran contraseñas en el log.
    if payload.password != payload.confirmar_password:
        raise HTTPException(
            status_code=422,
            detail="Las contraseñas no coinciden",
        )

    user = await db.get(Usuario, PILOTO_ID_USUARIO)

    if user and user.password_hash != "piloto-no-auth":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El sistema ya está configurado. Usa el login normal.",
        )

    # Verificar que el email no esté tomado por otro usuario
    res = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    existing = res.scalar_one_or_none()
    if existing and existing.id_usuario != PILOTO_ID_USUARIO:
        raise HTTPException(status_code=400, detail="Email ya en uso")

    codigo = _generar_codigo()
    expira = datetime.now(timezone.utc) + timedelta(minutes=30)

    if user:
        user.email                = payload.email
        user.password_hash        = hash_password(payload.password)
        user.rol                  = "admin"
        user.estado_suscripcion   = "activa"
        user.email_verificado     = False
        user.codigo_verificacion  = codigo
        user.codigo_expira_en     = expira
    else:
        user = Usuario(
            id_usuario=PILOTO_ID_USUARIO,
            id_cuenta=CUENTA_INTERNA_ID,
            email=payload.email,
            password_hash=hash_password(payload.password),
            rol="admin",
            estado_suscripcion="activa",
            email_verificado=False,
            codigo_verificacion=codigo,
            codigo_expira_en=expira,
        )
        db.add(user)

    # Se envía ANTES de guardar: si Resend falla, se revierte y el registro se puede reintentar
    # (después de guardar, /auth/setup respondería 409 "ya configurado").
    try:
        await _enviar_email(payload.email, codigo)
    except EnvioCorreoError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=MSG_ERROR_CORREO)

    await db.commit()

    return {"ok": True, "email": payload.email, "verificacion_requerida": True}


@router.post("/enviar-codigo")
async def enviar_codigo(
    payload: BaseModel,
    db: AsyncSession = Depends(get_db),
):
    """Reenvía el código de verificación. Body: { email }"""
    # Usamos un modelo simple inline
    raise HTTPException(status_code=400, detail="Usa /auth/reenviar-codigo con {email}")


@router.post("/reenviar-codigo")
async def reenviar_codigo(
    body: dict,
    db: AsyncSession = Depends(get_db),
):
    email = str(body.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email requerido")

    res = await db.execute(select(Usuario).where(Usuario.email == email))
    user = res.scalar_one_or_none()
    if not user:
        # No revelar si el email existe o no
        return {"ok": True}

    if user.email_verificado:
        raise HTTPException(status_code=400, detail="El correo ya está verificado")

    codigo = _generar_codigo()
    user.codigo_verificacion = codigo
    user.codigo_expira_en    = datetime.now(timezone.utc) + timedelta(minutes=30)

    # Si Resend falla se revierte: el código anterior sigue siendo válido
    try:
        await _enviar_email(email, codigo)
    except EnvioCorreoError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=MSG_ERROR_CORREO)

    await db.commit()
    return {"ok": True}


@router.post("/verificar-codigo")
async def verificar_codigo(
    payload: VerificarCodigoRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    """
    Verifica el código de 6 dígitos enviado por email.
    Si es correcto, marca el correo como verificado y hace login automático.
    """
    res = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    user = res.scalar_one_or_none()

    if not user:
        raise HTTPException(status_code=400, detail="Código incorrecto o expirado")

    if user.email_verificado:
        # Ya verificado, hacer login directo
        token = create_access_token(user.id_usuario, user.rol, user.version_token)
        response.set_cookie(
            key="access_token", value=token, httponly=True, samesite="lax",
            max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        )
        return {"ok": True, "rol": user.rol, "email": user.email}

    ahora = datetime.now(timezone.utc)
    expira = user.codigo_expira_en
    if expira and expira.tzinfo is None:
        expira = expira.replace(tzinfo=timezone.utc)

    if (
        user.codigo_verificacion != payload.codigo
        or not expira
        or ahora > expira
    ):
        raise HTTPException(status_code=400, detail="Código incorrecto o expirado")

    user.email_verificado    = True
    user.codigo_verificacion = None
    user.codigo_expira_en    = None
    await db.commit()

    token = create_access_token(user.id_usuario, user.rol, user.version_token)
    response.set_cookie(
        key="access_token", value=token, httponly=True, samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return {"ok": True, "rol": user.rol, "email": user.email}
