"""
routers/plantillas.py
CRUD de plantillas de mensajes.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Plantilla, Usuario
from app.schemas import PlantillaCreate, PlantillaRead

router = APIRouter(prefix="/plantillas", tags=["plantillas"])


@router.get("/", response_model=list[PlantillaRead])
async def listar_plantillas(
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    res = await db.execute(
        select(Plantilla)
        .where(Plantilla.id_usuario == usuario.id_usuario)
        .order_by(Plantilla.nombre)
    )
    return res.scalars().all()


@router.post("/", response_model=PlantillaRead, status_code=status.HTTP_201_CREATED)
async def crear_plantilla(
    payload: PlantillaCreate,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    plantilla = Plantilla(**payload.model_dump(), id_usuario=usuario.id_usuario)
    db.add(plantilla)
    await db.commit()
    await db.refresh(plantilla)
    return plantilla


@router.delete("/{id_plantilla}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_plantilla(
    id_plantilla: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    res = await db.execute(
        select(Plantilla).where(
            Plantilla.id_plantilla == id_plantilla,
            Plantilla.id_usuario == usuario.id_usuario,
        )
    )
    plantilla = res.scalar_one_or_none()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    await db.delete(plantilla)
    await db.commit()
