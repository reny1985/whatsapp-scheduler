"""
routers/auth.py
Login, logout, perfil, setup inicial y verificación de correo.
"""
import logging
import os
import random
import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Response, status
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
from app.models import Usuario
from app.schemas import PILOTO_ID_USUARIO

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/auth", tags=["auth"])

RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")
RESEND_FROM    = os.getenv("RESEND_FROM", "WhatsApp Scheduler <onboarding@resend.dev>")
APP_NAME       = os.getenv("APP_NAME", "WhatsApp Scheduler")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _generar_codigo() -> str:
    return str(random.randint(100000, 999999))


async def _enviar_email(to: str, codigo: str) -> None:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY no configurada — código %s para %s (no enviado)", codigo, to)
        return

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
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                json={"from": RESEND_FROM, "to": [to], "subject": asunto, "html": cuerpo},
            )
            r.raise_for_status()
        logger.info("Código de verificación enviado a %s via Resend", to)
    except Exception as exc:
        logger.error("Error enviando email a %s: %s", to, exc)


# ── Schemas ───────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


class VerificarCodigoRequest(BaseModel):
    email: EmailStr
    codigo: str = Field(min_length=6, max_length=6)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/login")
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    user = res.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email o contraseña incorrectos",
        )

    if not user.email_verificado:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_no_verificado",
        )

    token = create_access_token(user.id_usuario, user.rol)
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
            email=payload.email,
            password_hash=hash_password(payload.password),
            rol="admin",
            estado_suscripcion="activa",
            email_verificado=False,
            codigo_verificacion=codigo,
            codigo_expira_en=expira,
        )
        db.add(user)

    await db.commit()

    # Enviar email en background (no bloquea la respuesta)
    asyncio.create_task(_enviar_email(payload.email, codigo))

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
    await db.commit()

    asyncio.create_task(_enviar_email(email, codigo))
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
        token = create_access_token(user.id_usuario, user.rol)
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

    token = create_access_token(user.id_usuario, user.rol)
    response.set_cookie(
        key="access_token", value=token, httponly=True, samesite="lax",
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
    )
    return {"ok": True, "rol": user.rol, "email": user.email}
