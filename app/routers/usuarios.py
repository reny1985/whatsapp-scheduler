import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Usuario
from app.schemas import UsuarioCreate, UsuarioRead
from app.security import hash_password

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/usuarios", tags=["usuarios"])


@router.post("/", response_model=UsuarioRead, status_code=status.HTTP_201_CREATED)
async def crear_usuario(payload: UsuarioCreate, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Usuario).where(Usuario.email == payload.email))
    if res.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="El email ya está registrado")
    usuario = Usuario(email=payload.email, password_hash=hash_password(payload.password))
    db.add(usuario)
    await db.commit()
    await db.refresh(usuario)
    return usuario


@router.get("/{id_usuario}", response_model=UsuarioRead)
async def obtener_usuario(id_usuario: uuid.UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Usuario).where(Usuario.id_usuario == id_usuario))
    usuario = res.scalar_one_or_none()
    if not usuario:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return usuario
