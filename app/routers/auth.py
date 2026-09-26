"""
routers/auth.py
Login, logout, perfil y setup inicial del primer admin.
"""
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

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class SetupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)


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

    if user:
        user.email = payload.email
        user.password_hash = hash_password(payload.password)
        user.rol = "admin"
        user.estado_suscripcion = "activa"
    else:
        user = Usuario(
            id_usuario=PILOTO_ID_USUARIO,
            email=payload.email,
            password_hash=hash_password(payload.password),
            rol="admin",
            estado_suscripcion="activa",
        )
        db.add(user)

    await db.commit()
    return {"ok": True, "email": payload.email}
