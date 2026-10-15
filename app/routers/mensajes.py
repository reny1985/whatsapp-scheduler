import uuid
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select, func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import MensajeProgramado, SesionWhatsApp, Usuario
from app import envio_config as cfg
from app.services import estimacion, semaforo
from app.schemas import MensajeProgramadoCreate, MensajeProgramadoRead, MensajeProgramadoUpdate

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/mensajes", tags=["mensajes"])


class ReasignarRequest(BaseModel):
    id_sesion: uuid.UUID


async def _sesion_del_usuario(db: AsyncSession, id_sesion: uuid.UUID, usuario: Usuario) -> SesionWhatsApp:
    res = await db.execute(
        select(SesionWhatsApp).where(SesionWhatsApp.id_sesion == id_sesion,
                                     SesionWhatsApp.id_usuario == usuario.id_usuario)
    )
    sesion = res.scalar_one_or_none()
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return sesion


async def _mensaje_del_usuario(db: AsyncSession, id_mensaje: uuid.UUID, usuario: Usuario) -> MensajeProgramado:
    res = await db.execute(
        select(MensajeProgramado).where(MensajeProgramado.id_mensaje == id_mensaje,
                                        MensajeProgramado.id_usuario == usuario.id_usuario)
    )
    mensaje = res.scalar_one_or_none()
    if not mensaje:
        raise HTTPException(status_code=404, detail="Mensaje no encontrado")
    return mensaje


@router.post("/", response_model=MensajeProgramadoRead, status_code=status.HTTP_201_CREATED)
async def programar_mensaje(
    payload: MensajeProgramadoCreate,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    ahora = datetime.now(timezone.utc)
    pasada_min = (ahora - payload.fecha_hora_disparo).total_seconds() / 60
    if not payload.forzar and pasada_min > cfg.HORA_PASADA_TOLERANCIA_MIN:
        raise HTTPException(
            status_code=422,
            detail=f"Esa hora ya pasó hace {int(pasada_min)} min. Elige una hora futura o confirma «Enviar ahora» "
                   f"para que salga de inmediato.",
        )
    sesion = await _sesion_del_usuario(db, payload.id_sesion, usuario)

    datos = payload.model_dump(exclude={'forzar'})
    if datos["fecha_hora_disparo"] <= ahora:
        # "Enviar ahora" (o una hora pasada hace menos de 2 min): se programa para este instante; si conservara la
        # hora pasada, el programador la vería fuera de la tolerancia de retraso y la marcaría "vencida".
        datos["fecha_hora_disparo"] = ahora
    # El mensaje es del USUARIO (no de la sesión) y recuerda con qué número se programó: si la sesión se
    # borra y se vuelve a vincular el mismo número, se reasigna solo.
    mensaje = MensajeProgramado(**datos, id_usuario=usuario.id_usuario, numero_origen=sesion.numero_telefono)
    db.add(mensaje)
    await db.commit()
    await db.refresh(mensaje)
    logger.info("Mensaje programado → id=%s sesion=%s disparo=%s",
                mensaje.id_mensaje, mensaje.id_sesion, mensaje.fecha_hora_disparo)
    return mensaje


@router.get("/stats")
async def stats_mensajes(
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    res = await db.execute(select(MensajeProgramado.estado_envio, func.count())
                           .where(MensajeProgramado.id_usuario == usuario.id_usuario)
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
        "vencidos":      conteos.get("vencido", 0),
        "total":         sum(conteos.values()),
        "tasa_exito":    round(enviados / terminados * 100, 1) if terminados else None,
    }


@router.get("/historial")
async def historial_mensajes(
    dias: int = Query(default=14, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
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
              AND id_usuario = :uid
              AND estado_envio IN ('enviado', 'fallido')
            GROUP BY fecha
            ORDER BY fecha ASC
        """),
        {"desde": desde, "uid": usuario.id_usuario},
    )
    return [{"fecha": str(r.fecha), "enviados": r.enviados, "fallidos": r.fallidos}
            for r in res.fetchall()]


@router.get("/proximos")
async def proximos_mensajes(
    limite: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Próximos mensajes programados (para el panel del dashboard)."""
    res = await db.execute(
        select(MensajeProgramado)
        .where(MensajeProgramado.id_usuario == usuario.id_usuario,
               MensajeProgramado.estado_envio == "programado")
        .order_by(MensajeProgramado.fecha_hora_disparo.asc())
        .limit(limite)
    )
    return res.scalars().all()


@router.get("/recientes")
async def mensajes_recientes(
    limite: int = Query(default=5, ge=1, le=20),
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Últimos mensajes enviados o fallidos (para el panel del dashboard)."""
    res = await db.execute(
        select(MensajeProgramado)
        .where(MensajeProgramado.id_usuario == usuario.id_usuario,
               MensajeProgramado.estado_envio.in_(["enviado", "fallido"]))
        .order_by(MensajeProgramado.fecha_hora_disparo.desc())
        .limit(limite)
    )
    return res.scalars().all()


@router.get("/sin-sesion/conteo")
async def conteo_sin_sesion(
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Mensajes del usuario que no tienen sesión (su número se eliminó). Los 'programados' esperan:
    si se vuelve a vincular el mismo número se reasignan solos; si no, pasan a vencidos."""
    res = await db.execute(
        select(MensajeProgramado.estado_envio, func.count())
        .where(MensajeProgramado.id_usuario == usuario.id_usuario, MensajeProgramado.id_sesion.is_(None))
        .group_by(MensajeProgramado.estado_envio)
    )
    c = dict(res.all())
    return {"programados": c.get("programado", 0), "vencidos": c.get("vencido", 0), "total": sum(c.values())}


@router.post("/reasignar-sin-sesion")
async def reasignar_todos_sin_sesion(
    payload: ReasignarRequest,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    """Asigna a una sesión todos los mensajes programados que quedaron sin sesión (conservan su hora)."""
    sesion = await _sesion_del_usuario(db, payload.id_sesion, usuario)
    res = await db.execute(
        update(MensajeProgramado)
        .where(MensajeProgramado.id_usuario == usuario.id_usuario,
               MensajeProgramado.id_sesion.is_(None),
               MensajeProgramado.estado_envio == "programado")
        .values(id_sesion=sesion.id_sesion, numero_origen=sesion.numero_telefono)
    )
    await db.commit()
    logger.info("Reasignados %d mensajes sin sesión a '%s'", res.rowcount, sesion.instancia_evolution)
    return {"reasignados": res.rowcount}


@router.get("/estimacion")
async def estimacion_al_programar(
    id_sesion: uuid.UUID,
    fecha_hora: datetime,
    grupo: bool = False,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    """Aviso al programar: si la sesión ya tiene mensajes en una ventana cercana a `fecha_hora`, cuántos son y entre
    qué horas saldrían aproximadamente (simulación con las pausas promedio; el doble si el semáforo está en rojo)."""
    sesion = await _sesion_del_usuario(db, id_sesion, usuario)
    if fecha_hora.tzinfo is None:
        fecha_hora = fecha_hora.replace(tzinfo=timezone(timedelta(hours=-5)))   # hora de Ecuador
    ahora = datetime.now(timezone.utc)
    pendientes = await estimacion.pendientes_de_sesion(db, id_sesion)
    rojo = await semaforo.es_rojo(db, id_sesion, sesion.fecha_creacion)
    ultimo = await estimacion.ultimo_envio_de_sesion(db, id_sesion, ahora)
    destino = "destino@g.us" if grupo else "destino@s.whatsapp.net"
    return estimacion.aviso_al_programar(pendientes, fecha_hora, destino, ahora, rojo, ultimo)


@router.get("/", response_model=list[MensajeProgramadoRead])
async def listar_mensajes(
    estado: str | None = None,
    sin_sesion: bool = False,
    limit: int = 20,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    q = (select(MensajeProgramado)
         .where(MensajeProgramado.id_usuario == usuario.id_usuario)
         .order_by(MensajeProgramado.fecha_hora_disparo.desc()))
    if estado:
        q = q.where(MensajeProgramado.estado_envio == estado)
    if sin_sesion:
        q = q.where(MensajeProgramado.id_sesion.is_(None))
    q = q.limit(limit).offset(offset)
    res = await db.execute(q)
    lista = res.scalars().all()

    # "En cola · sale aprox. HH:MM": misma simulación que el aviso al programar, por sesión
    ahora = datetime.now(timezone.utc)
    for id_sesion in {m.id_sesion for m in lista if m.estado_envio == "en_cola" and m.id_sesion}:
        sesion = await db.get(SesionWhatsApp, id_sesion)
        pendientes = await estimacion.pendientes_de_sesion(db, id_sesion)
        rojo = await semaforo.es_rojo(db, id_sesion, sesion.fecha_creacion if sesion else None)
        ultimo = await estimacion.ultimo_envio_de_sesion(db, id_sesion, ahora)
        salidas = estimacion.simular([(p.id_mensaje, p.fecha_hora_disparo, p.id_grupo) for p in pendientes], ahora, rojo, ultimo)
        for m in lista:
            if m.id_mensaje in salidas and m.estado_envio == "en_cola":
                m.salida_estimada = salidas[m.id_mensaje]
    return lista


@router.patch("/{id_mensaje}/cancelar", response_model=MensajeProgramadoRead)
async def cancelar_mensaje(
    id_mensaje: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    mensaje = await _mensaje_del_usuario(db, id_mensaje, usuario)
    if mensaje.estado_envio in ("enviado", "enviando", "en_cola"):
        raise HTTPException(status_code=409,
                            detail=f"No se puede cancelar un mensaje en estado '{mensaje.estado_envio}'")
    mensaje.estado_envio = "cancelado"
    await db.commit()
    await db.refresh(mensaje)
    return mensaje


@router.patch("/{id_mensaje}/reasignar", response_model=MensajeProgramadoRead)
async def reasignar_mensaje(
    id_mensaje: uuid.UUID,
    payload: ReasignarRequest,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    """Asigna una sesión a un mensaje programado que quedó sin sesión (conserva su hora)."""
    mensaje = await _mensaje_del_usuario(db, id_mensaje, usuario)
    if mensaje.estado_envio != "programado":
        raise HTTPException(status_code=409, detail="Solo se pueden reasignar mensajes programados; "
                                                    "los vencidos se reprograman")
    sesion = await _sesion_del_usuario(db, payload.id_sesion, usuario)
    mensaje.id_sesion = sesion.id_sesion
    mensaje.numero_origen = sesion.numero_telefono
    await db.commit()
    await db.refresh(mensaje)
    return mensaje


@router.patch("/{id_mensaje}", response_model=MensajeProgramadoRead)
async def editar_mensaje(
    id_mensaje: uuid.UUID,
    payload: MensajeProgramadoUpdate,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(require_admin),
):
    mensaje = await _mensaje_del_usuario(db, id_mensaje, usuario)
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
    usuario: Usuario = Depends(require_admin),
):
    mensaje = await _mensaje_del_usuario(db, id_mensaje, usuario)
    if mensaje.estado_envio in ("enviando", "en_cola"):
        raise HTTPException(status_code=409,
                            detail=f"No se puede eliminar un mensaje en estado '{mensaje.estado_envio}'")
    await db.delete(mensaje)
    await db.commit()


@router.get("/{id_mensaje}", response_model=MensajeProgramadoRead)
async def obtener_mensaje(
    id_mensaje: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    return await _mensaje_del_usuario(db, id_mensaje, usuario)
