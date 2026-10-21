"""
routers/admin.py
Panel de superadministrador (v1.1.1). SOLO para el superadministrador: para cualquier otro (o sin login) todo /admin
responde 404, igual que una ruta inexistente. Dos candados: el middleware de main.py (antes del enrutador, así ni un
405 ni un 422 delatan que la ruta existe) y la dependencia require_superadmin de este router.

· Nunca devuelve contenido de los mensajes del cliente (texto, destino, adjuntos): solo conteos.
· Cada acción se simula primero (confirmar=false → antes/después sin guardar nada) y se ejecuta con confirmar=true
  en UNA transacción (cuenta bloqueada con FOR UPDATE + período + auditoría). La `clave` (UUID que genera el
  navegador al abrir el diálogo) es única en la auditoría: un doble toque aplica la acción una sola vez.
· La cuenta interna es de solo lectura (y la BD tampoco permite cambiarle el estado ni ponerle vencimiento).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, not_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_superadmin
from app.cuentas import EstadoCuenta, estado_efectivo
from app.database import get_db
from app.models import (
    AuditoriaAdmin,
    Cuenta,
    MensajeProgramado,
    PeriodoCuenta,
    SesionWhatsApp,
    Usuario,
    condicion_sesion_nunca_conectada,
)
from app.planes import DIAS_PERIODO_PAGADO, ESTADOS_CUENTA, PLANES
from app.services import intentos

logger = logging.getLogger("whatsapp_scheduler")
router = APIRouter(prefix="/admin", dependencies=[Depends(require_superadmin)], include_in_schema=False)

PLANTILLAS = Path(__file__).resolve().parent.parent / "templates"
ZONA = ZoneInfo("America/Guayaquil")
MSG_INTERNA = "La cuenta interna no se puede modificar desde el panel."
PENDIENTES = ("programado", "en_cola")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _inicio_mes(ahora: datetime) -> datetime:
    local = ahora.astimezone(ZONA)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()


def _mascara(numero: str | None) -> str:
    return f"…{numero[-4:]}" if numero else "—"


def _plural(n: int, singular: str, plural: str) -> str:
    return f"{n} {singular if n == 1 else plural}"


# ── Páginas (también pasan por los dos candados) ─────────────────────────────

def _version() -> str:
    archivos = [PLANTILLAS / "admin.js", PLANTILLAS.parent / "static" / "css" / "app.css"]
    return str(int(max((p.stat().st_mtime for p in archivos if p.exists()), default=0)))


@router.get("", response_class=HTMLResponse)
async def pagina_admin():
    html = (PLANTILLAS / "admin.html").read_text(encoding="utf-8").replace("__V__", _version())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@router.get("/admin.js")
async def script_admin():
    return FileResponse(PLANTILLAS / "admin.js", media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})


# ── Datos ────────────────────────────────────────────────────────────────────

async def _duenos(db: AsyncSession) -> dict[uuid.UUID, list[str]]:
    """Correos de cada cuenta; el primero es el dueño (el usuario más antiguo)."""
    filas = (await db.execute(
        select(Usuario.id_cuenta, Usuario.email).order_by(Usuario.fecha_registro, Usuario.email)
    )).all()
    correos: dict[uuid.UUID, list[str]] = {}
    for id_cuenta, email in filas:
        correos.setdefault(id_cuenta, []).append(email)
    return correos


def _cond_numero_vinculado():
    """Sesión que tiene (o tuvo) número: ocupa un lugar del plan aunque esté desconectada."""
    return not_(condicion_sesion_nunca_conectada())


async def _conteo_por_cuenta(db: AsyncSession, consulta) -> dict[uuid.UUID, int]:
    return {c: n for c, n in (await db.execute(consulta)).all()}


def _fila(cuenta: Cuenta, est: EstadoCuenta, correos: list[str], numeros: int, enviados_mes: int) -> dict:
    plan = PLANES.get(cuenta.plan or "")
    return {
        "id_cuenta": str(cuenta.id_cuenta),
        "email": correos[0] if correos else None,
        "nombre": cuenta.nombre,
        "plan": cuenta.plan,
        "plan_nombre": plan["nombre"] if plan else None,
        "estado": est.estado,
        "estado_guardado": cuenta.estado,
        "en_gracia": est.en_gracia,
        "fin": _iso(est.fin),
        "corte": _iso(est.corte),
        "numeros": numeros,
        "numeros_max": plan["numeros"] if plan else None,
        "enviados_mes": enviados_mes,
        "creada_en": _iso(cuenta.creada_en),
        "es_interna": cuenta.es_interna,
    }


async def _todas(db: AsyncSession, ahora: datetime) -> list[tuple[Cuenta, EstadoCuenta, dict]]:
    cuentas = (await db.execute(select(Cuenta).order_by(Cuenta.creada_en.desc()))).scalars().all()
    correos = await _duenos(db)
    numeros = await _conteo_por_cuenta(db, select(SesionWhatsApp.id_cuenta, func.count())
                                       .where(_cond_numero_vinculado()).group_by(SesionWhatsApp.id_cuenta))
    # "Este mes" igual que el Inicio: por la hora programada, en hora de Ecuador
    enviados = await _conteo_por_cuenta(db, select(MensajeProgramado.id_cuenta, func.count())
                                        .where(MensajeProgramado.estado_envio == "enviado",
                                               MensajeProgramado.fecha_hora_disparo >= _inicio_mes(ahora))
                                        .group_by(MensajeProgramado.id_cuenta))
    salida = []
    for c in cuentas:
        est = estado_efectivo(c, ahora)
        salida.append((c, est, _fila(c, est, correos.get(c.id_cuenta, []), numeros.get(c.id_cuenta, 0),
                                     enviados.get(c.id_cuenta, 0))))
    return salida


@router.get("/api/resumen")
async def resumen(db: AsyncSession = Depends(get_db)):
    """Cuentas por estado, ingreso mensual estimado y las que vencen en 7 días (sin la cuenta interna)."""
    ahora = _ahora()
    limite = ahora + timedelta(days=7)
    conteos = {e: 0 for e in ESTADOS_CUENTA}
    en_gracia = 0
    ingreso, ingreso_gracia = Decimal("0"), Decimal("0")
    vencen = []
    for cuenta, est, fila in await _todas(db, ahora):
        if cuenta.es_interna:
            continue
        conteos[est.estado] = conteos.get(est.estado, 0) + 1
        precio = Decimal(str(PLANES[cuenta.plan]["precio_usd"])) if cuenta.plan in PLANES else Decimal("0")
        if est.estado == "activa" and est.en_gracia:
            en_gracia += 1
            ingreso_gracia += precio
            if est.corte and ahora < est.corte <= limite:
                vencen.append({**fila, "tipo": "gracia", "fecha": _iso(est.corte)})
        elif est.estado == "activa":
            ingreso += precio
        if est.estado in ("prueba", "activa", "cortesia") and not est.en_gracia and est.fin and ahora < est.fin <= limite:
            tipo = {"prueba": "prueba", "activa": "pagado", "cortesia": "cortesia"}[est.estado]
            vencen.append({**fila, "tipo": tipo, "fecha": _iso(est.fin)})
    vencen.sort(key=lambda v: v["fecha"])
    return {"conteos": conteos, "en_gracia": en_gracia, "ingreso_mensual": f"{ingreso:.2f}",
            "ingreso_en_gracia": f"{ingreso_gracia:.2f}", "vencen_7_dias": vencen}


@router.get("/api/cuentas")
async def listar_cuentas(
    q: str | None = Query(default=None, max_length=255),
    estado: str | None = Query(default=None),
    plan: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    ahora = _ahora()
    correos = await _duenos(db)
    texto = (q or "").strip().lower()
    salida = []
    for cuenta, est, fila in await _todas(db, ahora):
        if texto and not any(texto in e.lower() for e in correos.get(cuenta.id_cuenta, [])):
            continue
        if estado and est.estado != estado:
            continue
        if plan and (cuenta.plan or "sin_plan") != plan:
            continue
        salida.append(fila)
    return salida


async def _cuenta_o_404(db: AsyncSession, id_cuenta: uuid.UUID) -> Cuenta:
    cuenta = await db.get(Cuenta, id_cuenta)
    if cuenta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cuenta no encontrada")
    return cuenta


async def _sesiones_con_numero(db: AsyncSession, id_cuenta: uuid.UUID) -> list[SesionWhatsApp]:
    return list((await db.execute(
        select(SesionWhatsApp).where(SesionWhatsApp.id_cuenta == id_cuenta, _cond_numero_vinculado())
        .order_by(SesionWhatsApp.fecha_creacion)
    )).scalars().all())


def _acciones_permitidas(cuenta: Cuenta) -> list[str]:
    if cuenta.es_interna:
        return []
    if cuenta.estado == "suspendida":
        return ["reactivar"]
    if cuenta.estado == "prueba":
        return ["activar", "extender_prueba", "suspender"]
    return ["activar", "cambiar_plan", "suspender"]


@router.get("/api/cuentas/{id_cuenta}")
async def detalle_cuenta(id_cuenta: uuid.UUID, db: AsyncSession = Depends(get_db)):
    ahora = _ahora()
    cuenta = await _cuenta_o_404(db, id_cuenta)
    est = estado_efectivo(cuenta, ahora)
    correos = (await _duenos(db)).get(cuenta.id_cuenta, [])
    sesiones = await _sesiones_con_numero(db, cuenta.id_cuenta)
    mes_ini = _inicio_mes(ahora)

    def _cuenta_mensajes(*condiciones):
        return select(func.count()).select_from(MensajeProgramado).where(
            MensajeProgramado.id_cuenta == cuenta.id_cuenta, *condiciones)

    conteos = {
        "pendientes": (await db.execute(_cuenta_mensajes(MensajeProgramado.estado_envio.in_(PENDIENTES)))).scalar_one(),
    }
    for nombre, estado_envio in (("enviados_mes", "enviado"), ("fallidos_mes", "fallido"), ("vencidos_mes", "vencido")):
        conteos[nombre] = (await db.execute(_cuenta_mensajes(
            MensajeProgramado.estado_envio == estado_envio, MensajeProgramado.fecha_hora_disparo >= mes_ini))).scalar_one()
    conteos["mensajes_prueba_usados"] = cuenta.mensajes_prueba_usados

    fila = _fila(cuenta, est, correos, len(sesiones), conteos["enviados_mes"])
    return {
        **fila,
        "usuarios": correos,
        "prueba_fin": _iso(cuenta.prueba_fin),
        "periodo_fin": _iso(cuenta.periodo_fin),
        "motivo_suspension": cuenta.motivo_suspension,
        "numeros_lista": [{"id_sesion": str(s.id_sesion), "numero": _mascara(s.numero_telefono),
                           "estado_conexion": s.estado_conexion} for s in sesiones],
        "conteos": conteos,
        "acciones": _acciones_permitidas(cuenta),
        "planes": {k: {"nombre": v["nombre"], "numeros": v["numeros"], "precio_usd": v["precio_usd"]}
                   for k, v in PLANES.items()},
        "dias_periodo": DIAS_PERIODO_PAGADO,
    }


@router.get("/api/cuentas/{id_cuenta}/historial")
async def historial_cuenta(id_cuenta: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await _cuenta_o_404(db, id_cuenta)
    periodos = (await db.execute(select(PeriodoCuenta).where(PeriodoCuenta.id_cuenta == id_cuenta))).scalars().all()
    acciones = (await db.execute(select(AuditoriaAdmin).where(AuditoriaAdmin.id_cuenta == id_cuenta))).scalars().all()
    eventos = [{"tipo": "periodo", "fecha": _iso(p.creado_en), "periodo_tipo": p.tipo, "plan": p.plan,
                "inicio": _iso(p.inicio), "fin": _iso(p.fin),
                "precio_usd": f"{p.precio_usd:.2f}" if p.precio_usd is not None else None, "nota": p.nota}
               for p in periodos]
    eventos += [{"tipo": "accion", "fecha": _iso(a.creada_en), "accion": a.accion, "antes": a.antes,
                 "despues": a.despues, "nota": a.nota, "email_admin": a.email_admin} for a in acciones]
    eventos.sort(key=lambda e: e["fecha"] or "", reverse=True)
    return eventos


@router.post("/api/cuentas/{id_cuenta}/numeros/{id_sesion}/mostrar")
async def mostrar_numero(
    id_cuenta: uuid.UUID,
    id_sesion: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: Usuario = Depends(require_superadmin),
):
    """Número completo de una sesión de la cuenta. Cada vez deja una fila en la auditoría."""
    await _cuenta_o_404(db, id_cuenta)
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if sesion is None or sesion.id_cuenta != id_cuenta:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Número no encontrado")
    db.add(AuditoriaAdmin(id_admin=admin.id_usuario, email_admin=admin.email, id_cuenta=id_cuenta,
                          accion="mostrar_numero", antes=None,
                          despues={"id_sesion": str(id_sesion), "numero": _mascara(sesion.numero_telefono)},
                          ip=intentos.ip_cliente(request)))
    await db.commit()
    return {"numero": sesion.numero_telefono}


# ── Acciones ─────────────────────────────────────────────────────────────────

class _Accion(BaseModel):
    confirmar: bool = False
    clave: uuid.UUID | None = None


class CambiarPlan(_Accion):
    plan: str


class ExtenderPrueba(_Accion):
    dias: int = Field(ge=1, le=30)


class Activar(_Accion):
    plan: str | None = None
    nota: str | None = Field(default=None, max_length=200)


class Suspender(_Accion):
    motivo: str = Field(min_length=3, max_length=300)


def _foto(cuenta: Cuenta) -> dict:
    return {"plan": cuenta.plan, "estado": cuenta.estado, "prueba_fin": _iso(cuenta.prueba_fin),
            "periodo_fin": _iso(cuenta.periodo_fin), "motivo_suspension": cuenta.motivo_suspension,
            "estado_antes_suspension": cuenta.estado_antes_suspension}


def _cambios(antes: dict, despues: dict) -> list[dict]:
    return [{"campo": k, "antes": antes.get(k), "despues": despues.get(k)}
            for k in despues if antes.get(k) != despues.get(k)]


def _plan_valido(plan: str) -> str:
    if plan not in PLANES:
        raise HTTPException(status_code=422, detail=f"Plan desconocido. Opciones: {', '.join(PLANES)}.")
    return plan


async def _verificar_cabe_en_plan(db: AsyncSession, cuenta: Cuenta, plan: str) -> None:
    """No se puede bajar a un plan con menos números (o usuarios) de los que la cuenta ya tiene."""
    limites = PLANES[plan]
    n_numeros = len(await _sesiones_con_numero(db, cuenta.id_cuenta))
    if n_numeros > limites["numeros"]:
        sobran = n_numeros - limites["numeros"]
        raise HTTPException(
            status_code=409,
            detail=(f"La cuenta tiene {_plural(n_numeros, 'número vinculado', 'números vinculados')} y el plan "
                    f"{limites['nombre']} permite {limites['numeros']}. Pide al cliente que desvincule "
                    f"{sobran} antes de bajar de plan."))
    n_usuarios = (await db.execute(select(func.count()).select_from(Usuario)
                                   .where(Usuario.id_cuenta == cuenta.id_cuenta))).scalar_one()
    if n_usuarios > limites["usuarios"]:
        raise HTTPException(
            status_code=409,
            detail=(f"La cuenta tiene {_plural(n_usuarios, 'usuario', 'usuarios')} y el plan {limites['nombre']} "
                    f"permite {limites['usuarios']}. Quita usuarios antes de bajar de plan."))


async def _ejecutar(db: AsyncSession, request: Request, admin: Usuario, id_cuenta: uuid.UUID, accion: str,
                    datos: _Accion, aplicar) -> dict:
    """Simula (confirmar=false) o ejecuta (confirmar=true) una acción. `aplicar(db, cuenta, ahora)` hace primero
    todas sus consultas y comprobaciones, luego cambia la cuenta, y devuelve {"periodo", "nota", "al_confirmar"}."""
    if datos.confirmar and datos.clave is None:
        raise HTTPException(status_code=422, detail="Falta la clave de la acción.")
    if datos.confirmar:
        cuenta = (await db.execute(select(Cuenta).where(Cuenta.id_cuenta == id_cuenta).with_for_update())
                  ).scalar_one_or_none()
    else:
        cuenta = await db.get(Cuenta, id_cuenta)
    if cuenta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Cuenta no encontrada")

    if datos.confirmar:     # misma clave = la misma acción ya aplicada (doble toque o reenvío)
        previa = (await db.execute(select(AuditoriaAdmin).where(AuditoriaAdmin.clave == datos.clave))
                  ).scalar_one_or_none()
        if previa is not None:
            misma = previa.id_cuenta == id_cuenta and previa.accion == accion
            antes_previa, despues_previa = previa.antes, previa.despues
            await db.rollback()
            if not misma:
                raise HTTPException(status_code=409, detail="Esa clave ya se usó en otra acción.")
            return {"ok": True, "repetida": True, "accion": accion, "antes": antes_previa, "despues": despues_previa,
                    "cambios": _cambios(antes_previa or {}, despues_previa or {})}

    if cuenta.es_interna:
        raise HTTPException(status_code=409, detail=MSG_INTERNA)

    ahora = _ahora()
    antes = _foto(cuenta)
    resultado = await aplicar(db, cuenta, ahora)
    despues = _foto(cuenta)
    cambios = _cambios(antes, despues)

    if not datos.confirmar:
        await db.rollback()             # la simulación no guarda nada
        return {"simulacion": True, "accion": accion, "antes": antes, "despues": despues, "cambios": cambios}

    auditoria = AuditoriaAdmin(id_auditoria=uuid.uuid4(), id_admin=admin.id_usuario, email_admin=admin.email,
                               id_cuenta=cuenta.id_cuenta, accion=accion, antes=antes, despues=despues,
                               nota=resultado.get("nota"), ip=intentos.ip_cliente(request), clave=datos.clave)
    db.add(auditoria)
    await db.flush()
    if resultado.get("periodo"):
        db.add(PeriodoCuenta(id_cuenta=cuenta.id_cuenta, id_auditoria=auditoria.id_auditoria, **resultado["periodo"]))
    for paso in resultado.get("al_confirmar", []):
        await paso()
    try:
        await db.commit()
    except IntegrityError:
        # Otra petición con la misma clave ganó la carrera: la acción ya quedó aplicada una vez
        await db.rollback()
        previa = (await db.execute(select(AuditoriaAdmin).where(AuditoriaAdmin.clave == datos.clave))
                  ).scalar_one_or_none()
        if previa is None:
            raise
        return {"ok": True, "repetida": True, "accion": accion, "antes": previa.antes, "despues": previa.despues,
                "cambios": _cambios(previa.antes or {}, previa.despues or {})}
    logger.info("Admin: %s en la cuenta %s (auditoría %s)", accion, cuenta.id_cuenta, auditoria.id_auditoria)
    return {"ok": True, "accion": accion, "antes": antes, "despues": despues, "cambios": cambios}


@router.post("/api/cuentas/{id_cuenta}/plan")
async def cambiar_plan(id_cuenta: uuid.UUID, datos: CambiarPlan, request: Request,
                       db: AsyncSession = Depends(get_db), admin: Usuario = Depends(require_superadmin)):
    async def aplicar(db, cuenta, ahora):
        plan = _plan_valido(datos.plan)
        if cuenta.estado == "prueba":
            raise HTTPException(status_code=409, detail="La cuenta está en prueba: el plan se elige al activar el "
                                                        "período pagado.")
        if cuenta.estado == "suspendida":
            raise HTTPException(status_code=409, detail="Reactiva la cuenta antes de cambiar el plan.")
        if cuenta.plan == plan:
            raise HTTPException(status_code=409, detail=f"La cuenta ya tiene el plan {PLANES[plan]['nombre']}.")
        await _verificar_cabe_en_plan(db, cuenta, plan)
        cuenta.plan = plan
        return {}
    return await _ejecutar(db, request, admin, id_cuenta, "cambiar_plan", datos, aplicar)


@router.post("/api/cuentas/{id_cuenta}/extender-prueba")
async def extender_prueba(id_cuenta: uuid.UUID, datos: ExtenderPrueba, request: Request,
                          db: AsyncSession = Depends(get_db), admin: Usuario = Depends(require_superadmin)):
    async def aplicar(db, cuenta, ahora):
        if cuenta.estado != "prueba":
            raise HTTPException(status_code=409, detail="Solo se puede extender la prueba de una cuenta en prueba.")
        fin_actual = cuenta.prueba_fin
        base = max(fin_actual, ahora) if fin_actual else ahora       # si ya venció, cuenta desde hoy
        cuenta.prueba_fin = base + timedelta(days=datos.dias)
        return {"periodo": {"tipo": "extension_prueba", "plan": None, "inicio": base, "fin": cuenta.prueba_fin},
                "nota": _plural(datos.dias, "día", "días")}
    return await _ejecutar(db, request, admin, id_cuenta, "extender_prueba", datos, aplicar)


@router.post("/api/cuentas/{id_cuenta}/activar")
async def activar_periodo(id_cuenta: uuid.UUID, datos: Activar, request: Request,
                          db: AsyncSession = Depends(get_db), admin: Usuario = Depends(require_superadmin)):
    """Período pagado de DIAS_PERIODO_PAGADO días. Se suma al vigente (también si está en los días de gracia: los
    días de gracia no se regalan); si ya venció, o viene de la prueba, empieza hoy."""
    async def aplicar(db, cuenta, ahora):
        if cuenta.estado == "suspendida":
            raise HTTPException(status_code=409, detail="Reactiva la cuenta antes de activar un período.")
        plan = _plan_valido(datos.plan) if datos.plan else cuenta.plan
        if (cuenta.estado == "prueba" and not datos.plan) or not plan:
            raise HTTPException(status_code=422, detail="Elige el plan para activar la cuenta.")
        if plan != cuenta.plan:
            await _verificar_cabe_en_plan(db, cuenta, plan)
        est = estado_efectivo(cuenta, ahora)
        fin_actual = est.fin
        if cuenta.estado in ("activa", "cortesia") and fin_actual and est.estado != "vencida":
            inicio = fin_actual                 # vigente (o en gracia): se suma al fin anterior
        else:
            inicio = ahora
        cuenta.estado = "activa"
        cuenta.plan = plan
        cuenta.periodo_fin = inicio + timedelta(days=DIAS_PERIODO_PAGADO)
        nota = (datos.nota or "").strip() or None
        return {"periodo": {"tipo": "pagado", "plan": plan, "inicio": inicio, "fin": cuenta.periodo_fin,
                            "precio_usd": Decimal(str(PLANES[plan]["precio_usd"])), "nota": nota},
                "nota": nota}
    return await _ejecutar(db, request, admin, id_cuenta, "activar_periodo", datos, aplicar)


@router.post("/api/cuentas/{id_cuenta}/suspender")
async def suspender_cuenta(id_cuenta: uuid.UUID, datos: Suspender, request: Request,
                           db: AsyncSession = Depends(get_db), admin: Usuario = Depends(require_superadmin)):
    async def aplicar(db, cuenta, ahora):
        if cuenta.estado == "suspendida":
            raise HTTPException(status_code=409, detail="La cuenta ya está suspendida.")
        motivo = datos.motivo.strip()
        if len(motivo) < 3:
            raise HTTPException(status_code=422, detail="Escribe el motivo de la suspensión.")
        cuenta.estado_antes_suspension = cuenta.estado
        cuenta.estado = "suspendida"
        cuenta.motivo_suspension = motivo

        async def cerrar_sesiones():    # sus sesiones abiertas se cierran (además del 403 en cada petición)
            await db.execute(update(Usuario).where(Usuario.id_cuenta == cuenta.id_cuenta)
                             .values(version_token=Usuario.version_token + 1))
        return {"nota": motivo, "al_confirmar": [cerrar_sesiones]}
    return await _ejecutar(db, request, admin, id_cuenta, "suspender", datos, aplicar)


@router.post("/api/cuentas/{id_cuenta}/reactivar")
async def reactivar_cuenta(id_cuenta: uuid.UUID, datos: _Accion, request: Request,
                           db: AsyncSession = Depends(get_db), admin: Usuario = Depends(require_superadmin)):
    """Vuelve al estado que tenía antes de suspenderla (si su fecha ya pasó, queda vencida)."""
    async def aplicar(db, cuenta, ahora):
        if cuenta.estado != "suspendida":
            raise HTTPException(status_code=409, detail="La cuenta no está suspendida.")
        previo = cuenta.estado_antes_suspension
        if previo not in ("prueba", "activa", "cortesia", "vencida"):
            previo = "activa" if cuenta.periodo_fin else "prueba"
        if previo in ("activa", "cortesia") and cuenta.periodo_fin is None:
            previo = "prueba"
        cuenta.estado = previo
        cuenta.motivo_suspension = None
        cuenta.estado_antes_suspension = None
        return {}
    return await _ejecutar(db, request, admin, id_cuenta, "reactivar", datos, aplicar)
