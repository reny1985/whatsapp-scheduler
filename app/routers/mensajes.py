import uuid
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, func, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import MensajeProgramado, SesionWhatsApp, Usuario
from app.schemas import MensajeProgramadoCreate, MensajeProgramadoRead, MensajeProgramadoUpdate

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/mensajes", tags=["mensajes"])


@router.post("/", response_model=MensajeProgramadoRead, status_code=status.HTTP_201_CREATED)
async def programar_mensaje(
    payload: MensajeProgramadoCreate,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    res = await db.execute(
        select(SesionWhatsApp).where(SesionWhatsApp.id_sesion == payload.id_sesion)
    )
    if not res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    mensaje = MensajeProgramado(**payload.model_dump())
    db.add(mensaje)
    await db.commit()
    await db.refresh(mensaje)
    logger.info("Mensaje programado → id=%s sesion=%s disparo=%s",
                mensaje.id_mensaje, mensaje.id_sesion, mensaje.fecha_hora_disparo)
    return mensaje


@router.get("/stats")
async def stats_mensajes(
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    res = await db.execute(select(MensajeProgramado.estado_envio, func.count())
                           .group_by(MensajeProgramado.estado_envio))
    conteos = dict(res.all())
    enviados = conteos.get("enviado", 0)
    fallidos = conteos.get("fallido", 0)
    terminados = enviados + fallidos
    return {
        "programados":   conteos.get("programado", 0),
        "enviados":      enviados,
        "fallidos":      fallidos,
        "cancelados":    conteos.get("cancelado", 0),
        "total":         sum(conteos.values()),
        "tasa_exito":    round(enviados / terminados * 100, 1) if terminados else None,
    }


@router.get("/historial")
async def historial_mensajes(
    dias: int = Query(default=14, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    """Agrega mensajes enviados/fallidos por día para el gráfico de actividad."""
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    res = await db.execute(
        text("""
            SELECT
                DATE(fecha_hora_disparo AT TIME ZONE 'UTC' AT TIME ZONE 'America/Guayaquil') AS fecha,
                COUNT(*) FILTER (WHERE estado_envio = 'enviado')  AS enviados,
                COUNT(*) FILTER (WHERE estado_envio = 'fallido')  AS fallidos
            FROM mensajes_programados
            WHERE fecha_hora_disparo >= :desde
              AND estado_envio IN ('enviado', 'fallido')
            GROUP BY fecha
            ORDER BY fecha ASC
        """),
        {"desde": desde},
    )
    return [{"fecha": str(r.fecha), "enviados": r.enviados, "fallidos": r.fallidos}
            for r in res.fetchall()]


@router.get("/proximos")
async def proximos_mensajes(
    limite: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    """Próximos mensajes programados (para el panel del dashboard)."""
    res = await db.execute(
        select(MensajeProgramado)
        .where(MensajeProgramado.estado_envio == "programado")
        .order_by(MensajeProgramado.fecha_hora_disparo.asc())
        .limit(limite)
    )
    return res.scalars().all()


@router.get("/recientes")
async def mensajes_recientes(
    limite: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    """Últimos mensajes enviados o fallidos (para el panel del dashboard)."""
    res = await db.execute(
        select(MensajeProgramado)
        .where(MensajeProgramado.estado_envio.in_(["enviado", "fallido"]))
        .order_by(MensajeProgramado.fecha_hora_disparo.desc())
        .limit(limite)
    )
    return res.scalars().all()


@router.get("/", response_model=list[MensajeProgramadoRead])
async def listar_mensajes(
    estado: str | None = None,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    q = select(MensajeProgramado).order_by(MensajeProgramado.fecha_hora_disparo.desc())
    if estado:
        q = q.where(MensajeProgramado.estado_envio == estado)
    q = q.limit(limit).offset(offset)
    res = await db.execute(q)
    return res.scalars().all()


@router.patch("/{id_mensaje}/cancelar", response_model=MensajeProgramadoRead)
async def cancelar_mensaje(
    id_mensaje: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if mensaje.estado_envio in ("enviado", "enviando"):
        raise HTTPException(status_code=409,
                            detail=f"No se puede cancelar un mensaje en estado '{mensaje.estado_envio}'")
    mensaje.estado_envio = "cancelado"
    await db.commit()
    await db.refresh(mensaje)
    return mensaje


@router.patch("/{id_mensaje}", response_model=MensajeProgramadoRead)
async def editar_mensaje(
    id_mensaje: uuid.UUID,
    payload: MensajeProgramadoUpdate,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if mensaje.estado_envio != "programado":
        raise HTTPException(status_code=409,
                            detail=f"Solo se pueden editar mensajes con estado 'programado'")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(mensaje, key, value)
    await db.commit()
    await db.refresh(mensaje)
    return mensaje


@router.delete("/{id_mensaje}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_mensaje(
    id_mensaje: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    if mensaje.estado_envio == "enviando":
        raise HTTPException(status_code=409,
                            detail="No se puede eliminar un mensaje en estado 'enviando'")
    await db.delete(mensaje)
    await db.commit()


@router.get("/{id_mensaje}", response_model=MensajeProgramadoRead)
async def obtener_mensaje(
    id_mensaje: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    return mensaje
