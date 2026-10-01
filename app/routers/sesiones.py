"""
routers/sesiones.py
CRUD de SesionesWhatsApp + endpoints auxiliares para el frontend.
"""
import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, require_admin
from app.database import get_db, AsyncSessionLocal
from app.models import SesionWhatsApp, Usuario
from app.schemas import SesionWhatsAppCreate, SesionWhatsAppRead, PILOTO_ID_USUARIO

logger = logging.getLogger("whatsapp_scheduler")

EVOLUTION_URL = os.getenv("EVOLUTION_API_URL", "http://localhost:8080")
EVOLUTION_KEY = os.getenv("EVOLUTION_API_KEY", "")

router = APIRouter(prefix="/sesiones", tags=["sesiones"])


# ------------------------------------------------------------------
# Helpers Evolution API
# ------------------------------------------------------------------

def _evo_headers(token: str | None = None) -> dict:
    """
    Construye headers para Evolution API.
    - Para operaciones de admin (crear instancia): usa la clave global EVOLUTION_KEY
    - Para operaciones de instancia (QR, grupos, envío): usa el token propio de la instancia
    IMPORTANTE: Evolution API usa apikeys distintas por instancia.
    La clave global solo sirve para crear instancias, no para operarlas.
    """
    apikey = token or EVOLUTION_KEY
    return {"apikey": apikey, "Content-Type": "application/json"}


async def _evo_get(path: str, token: str | None = None, timeout: int = 15) -> Any:
    """GET a Evolution API. Lanza HTTPException en caso de error."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(f"{EVOLUTION_URL}{path}", headers=_evo_headers(token))
    logger.debug("EVO GET %s → %s | %s", path, r.status_code, r.text[:300])
    if r.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Evolution API error {r.status_code}: {r.text[:300]}",
        )
    # Evolution a veces devuelve body vacío (ej: lista de grupos cuando no hay ninguno)
    text = r.text.strip()
    if not text:
        return []
    try:
        return r.json()
    except Exception:
        logger.warning("EVO GET %s → respuesta no es JSON válido: %r", path, text[:200])
        return []


async def _evo_post(path: str, body: dict, token: str | None = None) -> Any:
    """POST a Evolution API. Lanza HTTPException en caso de error."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{EVOLUTION_URL}{path}",
            headers=_evo_headers(token),
            json=body,
        )
    logger.debug("EVO POST %s → %s | %s", path, r.status_code, r.text[:300])
    if r.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Evolution API error {r.status_code}: {r.text[:300]}",
        )
    return r.json()


# ------------------------------------------------------------------
# Helpers internos
# ------------------------------------------------------------------

def _extraer_numero(owner_jid: str) -> str:
    """Extrae el número limpio de un JID: '593969829845@s.whatsapp.net' → '593969829845'."""
    return owner_jid.split("@")[0] if "@" in owner_jid else owner_jid


async def _obtener_numero_instancia(instancia: str) -> str | None:
    """Consulta Evolution API para obtener el número del propietario de la instancia."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"{EVOLUTION_URL}/instance/fetchInstances?instanceName={instancia}",
                headers=_evo_headers(),
            )
        if r.status_code != 200:
            return None
        data = r.json()
        instances = data if isinstance(data, list) else [data]
        for inst in instances:
            info = inst.get("instance", inst)
            if info.get("instanceName") == instancia:
                owner = info.get("owner", "")
                if owner:
                    return _extraer_numero(owner)
    except Exception as exc:
        logger.warning("No se pudo obtener número para '%s': %s", instancia, exc)
    return None


async def _configurar_instancia_bg(instancia: str, token: str | None) -> None:
    """Configura webhook y settings correctos para la instancia (background)."""
    base_url = os.getenv("BASE_PUBLIC_URL", "https://app.enviafast.net")
    webhook_url = base_url.rstrip("/") + "/webhook/evolution"

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Webhook
        try:
            r = await client.post(
                f"{EVOLUTION_URL}/webhook/set/{instancia}",
                headers=_evo_headers(token),
                json={
                    "enabled": True,
                    "url": webhook_url,
                    "webhookByEvents": False,
                    "webhookBase64": False,
                    "events": ["GROUPS_UPSERT", "GROUP_UPDATE", "CONNECTION_UPDATE", "QRCODE_UPDATED"],
                },
            )
            logger.info("Webhook configurado para '%s': HTTP %s", instancia, r.status_code)
        except Exception as exc:
            logger.warning("No se pudo configurar webhook para '%s': %s", instancia, exc)

        # 2. Settings: grupos habilitados + sync completo
        try:
            r = await client.post(
                f"{EVOLUTION_URL}/settings/set/{instancia}",
                headers=_evo_headers(token),
                json={
                    "reject_call": False,
                    "msg_call": "",
                    "groups_ignore": False,
                    "always_online": False,
                    "read_messages": False,
                    "read_status": False,
                    "sync_full_history": True,
                    "wavoipToken": "",
                },
            )
            logger.info("Settings configurados para '%s': HTTP %s", instancia, r.status_code)
        except Exception as exc:
            logger.warning("No se pudo configurar settings para '%s': %s", instancia, exc)


# ------------------------------------------------------------------
# CRUD
# ------------------------------------------------------------------

@router.post("/", response_model=SesionWhatsAppRead, status_code=status.HTTP_201_CREATED)
async def crear_sesion(payload: SesionWhatsAppCreate, db: AsyncSession = Depends(get_db), _: Usuario = Depends(require_admin)):
    """
    Crea un registro de sesión en la BD y registra la instancia en Evolution API.
    Captura el apikey propio de la instancia y lo guarda en token_autorizacion.
    En modo piloto, crea automáticamente el usuario piloto si no existe.
    """
    # 1. Garantizar que el usuario piloto exista en la BD
    usuario = await db.get(Usuario, payload.id_usuario)
    if not usuario:
        usuario = Usuario(
            id_usuario=payload.id_usuario,
            email="piloto@whatsapp-scheduler.local",
            password_hash="piloto-no-auth",
            estado_suscripcion="activa",
        )
        db.add(usuario)
        await db.flush()  # inserta sin commit para que la FK funcione

    # 2. Validar límite de sesiones por usuario (máx. 2)
    count_res = await db.execute(
        select(func.count()).where(SesionWhatsApp.id_usuario == payload.id_usuario)
    )
    if (count_res.scalar() or 0) >= 2:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Límite alcanzado: máximo 2 cuentas de WhatsApp por usuario. Elimina una sesión existente para agregar otra.",
        )

    # 3. Crear la instancia en Evolution API
    token_instancia = None
    try:
        # Siempre pasar token explícito: Evolution v1.8.x falla al auto-generar
        # el token cuando ya existe alguna instancia ("Token already exists").
        token_generado = str(uuid.uuid4()).upper()
        resp_evo = await _evo_post(
            "/instance/create",
            {
                "instanceName": payload.instancia_evolution,
                "token": token_generado,
                "qrcode": True,
                "integration": "WHATSAPP-BAILEYS",
            },
        )
        logger.info(
            "Instancia '%s' creada en Evolution API. Respuesta: %s",
            payload.instancia_evolution, str(resp_evo)[:200],
        )
        token_instancia = (
            (resp_evo.get("hash") or {}).get("apikey")
            or (resp_evo.get("instance") or {}).get("apikey")
            or resp_evo.get("apikey")
        )
        if token_instancia:
            logger.info("✅ Token instancia '%s': %s...", payload.instancia_evolution, token_instancia[:8])
        else:
            logger.warning("⚠️ Sin token en respuesta para '%s': %s", payload.instancia_evolution, resp_evo)

    except HTTPException as exc:
        detail_str = str(exc.detail).lower()
        if "409" in str(exc.detail) or "already" in detail_str or "exists" in detail_str:
            # La instancia ya existe en Evolution — recuperar su token
            logger.info("Instancia '%s' ya existe, recuperando token.", payload.instancia_evolution)
            try:
                instances = await _evo_get(
                    f"/instance/fetchInstances?instanceName={payload.instancia_evolution}"
                )
                if isinstance(instances, list) and instances:
                    inst_data = instances[0]
                    token_instancia = (
                        (inst_data.get("instance") or {}).get("apikey")
                        or inst_data.get("apikey")
                        or (inst_data.get("hash") or {}).get("apikey")
                    )
                    if token_instancia:
                        logger.info("✅ Token recuperado para '%s'", payload.instancia_evolution)
            except Exception as e:
                logger.warning("No se pudo recuperar token de instancia existente: %s", e)
        else:
            # Error real de Evolution API → informar al usuario
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Evolution API no pudo crear la instancia: {exc.detail}",
            )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo conectar con Evolution API: {exc}. Verifica que Evolution esté corriendo.",
        )

    # 4. Guardar sesión en la BD con el token capturado
    data = payload.model_dump()
    # Sobreescribir token_autorizacion con el que capturamos de Evolution
    if token_instancia:
        data["token_autorizacion"] = token_instancia

    sesion = SesionWhatsApp(**data)
    db.add(sesion)
    await db.commit()
    await db.refresh(sesion)

    # Configurar webhook automáticamente (best-effort, no bloquea si falla)
    asyncio.create_task(_configurar_instancia_bg(sesion.instancia_evolution, token_instancia))

    return sesion


@router.post("/iniciar-pairing")
async def iniciar_pairing(
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """
    Flujo completo de vinculación por código (pairing code):
    1. Elimina instancias pendientes del usuario para evitar conflictos
    2. Crea la instancia Evolution con el número → Baileys inicia WS con phoneNumber
    3. Configura webhook + settings
    4. Espera 3s a que el WS establezca la sesión con WhatsApp
    5. Sondea GET /connect?number= hasta obtener pairingCode (máx 4 reintentos × 2s)
    6. Devuelve {id_sesion, pairing_code} o 503 con mensaje claro

    El número debe llegar normalizado (solo dígitos, con código de país).
    """
    numero: str = body.get("numero", "")
    digits = "".join(c for c in numero if c.isdigit())
    logger.info("iniciar-pairing recibido: numero=%r digits=%r len=%d", numero, digits, len(digits))
    if len(digits) < 8:
        raise HTTPException(status_code=400, detail="Número de teléfono inválido")

    id_usuario = PILOTO_ID_USUARIO

    # 1. Eliminar instancias pendientes del usuario para evitar conflictos con WhatsApp
    result = await db.execute(
        select(SesionWhatsApp).where(
            SesionWhatsApp.id_usuario == id_usuario,
            SesionWhatsApp.estado_conexion == "qr_pendiente",
        )
    )
    pendientes = result.scalars().all()
    for s in pendientes:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.delete(
                    f"{EVOLUTION_URL}/instance/delete/{s.instancia_evolution}",
                    headers=_evo_headers(s.token_autorizacion),
                )
        except Exception:
            pass
        await db.delete(s)
    if pendientes:
        await db.commit()
        logger.info("iniciar-pairing: eliminadas %d instancias pendientes", len(pendientes))

    # 2. Crear instancia en Evolution con el número para que Baileys fije phoneNumber
    instancia = "wa-" + str(uuid.uuid4())[:8]
    token_generado = str(uuid.uuid4()).upper()
    try:
        resp_evo = await _evo_post("/instance/create", {
            "instanceName": instancia,
            "token": token_generado,
            "integration": "WHATSAPP-BAILEYS",
            "number": digits,       # ← fija phoneNumber en Baileys desde el inicio
            "qrcode": False,        # no necesitamos QR
        })
    except HTTPException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Evolution API no pudo crear la instancia: {exc.detail}",
        )

    token = (
        (resp_evo.get("hash") or {}).get("apikey")
        or (resp_evo.get("instance") or {}).get("apikey")
        or token_generado
    )
    logger.info("iniciar-pairing: instancia '%s' creada, token=%s...", instancia, token[:8])

    # 3. Guardar en BD
    nueva = SesionWhatsApp(
        id_usuario=id_usuario,
        instancia_evolution=instancia,
        token_autorizacion=token,
        estado_conexion="qr_pendiente",
    )
    db.add(nueva)
    await db.commit()
    await db.refresh(nueva)

    # 4. Configurar webhook + settings (inline: necesitamos que termine antes de sondear)
    await _configurar_instancia_bg(instancia, token)

    # 5. Esperar a que Baileys establezca la sesión WS con WhatsApp
    await asyncio.sleep(3)

    # 6. Sondear pairingCode
    pairing_code = None
    path = f"/instance/connect/{instancia}?number={digits}"

    for attempt in range(4):
        if attempt > 0:
            await asyncio.sleep(2)
        try:
            data = await _evo_get(path, token=token)
        except Exception as exc:
            logger.warning("iniciar-pairing intento %d error HTTP: %s", attempt + 1, exc)
            continue

        pc = data.get("pairingCode") if isinstance(data, dict) else None
        count = data.get("count") if isinstance(data, dict) else None
        logger.info(
            "iniciar-pairing intento %d | instancia=%s | number=%s | pairingCode=%s | count=%s | raw=%s",
            attempt + 1, instancia, digits, pc, count, str(data)[:300],
        )
        if pc:
            pairing_code = pc
            break

    if not pairing_code:
        # Limpiar instancia fallida
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.delete(
                    f"{EVOLUTION_URL}/instance/delete/{instancia}",
                    headers=_evo_headers(token),
                )
        except Exception:
            pass
        await db.delete(nueva)
        await db.commit()

        # Obtener versión de Evolution para el log
        try:
            ver_resp = await _evo_get("/", token=token)
            evo_version = ver_resp.get("version", "desconocida") if isinstance(ver_resp, dict) else "desconocida"
        except Exception:
            evo_version = "desconocida"
        logger.error(
            "iniciar-pairing FALLÓ: pairingCode null tras 4 intentos. "
            "instancia=%s number=%s Evolution version=%s",
            instancia, digits, evo_version,
        )
        raise HTTPException(
            status_code=503,
            detail=(
                "No se pudo generar el código de vinculación. "
                "Posibles causas: número ya vinculado a otra sesión activa, "
                "o WhatsApp limitó temporalmente las solicitudes. "
                "Intenta con QR."
            ),
        )

    # Formatear XXXX-XXXX
    code = str(pairing_code)
    if len(code) == 8 and "-" not in code:
        code = f"{code[:4]}-{code[4:]}"

    return {"id_sesion": str(nueva.id_sesion), "pairing_code": code}


@router.get("/", response_model=list[SesionWhatsAppRead])
async def listar_sesiones(db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_user)):
    """Lista todas las sesiones (un usuario = 1 sesión en la fase piloto)."""
    result = await db.execute(
        select(SesionWhatsApp).order_by(
            # conectado primero, luego por fecha de creación
            SesionWhatsApp.estado_conexion.desc(),
            SesionWhatsApp.fecha_creacion.desc(),
        )
    )
    return result.scalars().all()


@router.get("/{id_sesion}", response_model=SesionWhatsAppRead)
async def obtener_sesion(id_sesion: uuid.UUID, db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_user)):
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    return sesion


@router.get("/{id_sesion}/pairing-code")
async def obtener_pairing_code(
    id_sesion: uuid.UUID,
    numero: str,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """
    Solicita un pairing code a Evolution API para vincular por número de teléfono.
    El número debe estar normalizado: solo dígitos con código de país (ej: 593969829845).
    Llama a GET /instance/connect/{instancia}?number={numero} y devuelve el pairingCode.
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    digits = "".join(c for c in numero if c.isdigit())
    if len(digits) < 8:
        raise HTTPException(status_code=400, detail="Número de teléfono inválido")

    # Primera llamada inicia la conexión WS de Baileys con el número.
    # El pairingCode se genera de forma asíncrona cuando el WS conecta (~2s),
    # así que esperamos antes del primer intento y reintentamos si sigue null.
    pairing_code = None
    path = f"/instance/connect/{sesion.instancia_evolution}?number={digits}"

    # Llamada 0: inicia la conexión WS (casi siempre devuelve null)
    await _evo_get(path, token=sesion.token_autorizacion)

    # Esperar a que Baileys establezca la sesión WS con WhatsApp
    await asyncio.sleep(3)

    # Reintentar hasta 4 veces con 2s de pausa
    for attempt in range(4):
        if attempt > 0:
            await asyncio.sleep(2)
        data = await _evo_get(path, token=sesion.token_autorizacion)
        pairing_code = data.get("pairingCode") if isinstance(data, dict) else None
        logger.info(
            "pairing-code (renovar) intento %d | instancia=%s | pairingCode=%s | raw=%s",
            attempt + 1, sesion.instancia_evolution, pairing_code, str(data)[:300],
        )
        if pairing_code:
            break

    if not pairing_code:
        raise HTTPException(
            status_code=503,
            detail=(
                "WhatsApp no generó un código de vinculación. "
                "Posibles causas: el número ya está vinculado a otra sesión activa, "
                "o WhatsApp limitó temporalmente las solicitudes. "
                "Intenta escanear el QR o usa otro número."
            ),
        )

    code = str(pairing_code)
    if len(code) == 8 and "-" not in code:
        code = f"{code[:4]}-{code[4:]}"

    return {"pairing_code": code}


@router.delete("/{id_sesion}", status_code=status.HTTP_204_NO_CONTENT)
async def eliminar_sesion(
    id_sesion: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")
    # Eliminar instancia de Evolution API (best-effort)
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            await client.delete(
                f"{EVOLUTION_URL}/instance/delete/{sesion.instancia_evolution}",
                headers=_evo_headers(sesion.token_autorizacion),
            )
    except Exception as exc:
        logger.warning("No se pudo eliminar instancia '%s' de Evolution: %s", sesion.instancia_evolution, exc)
    await db.delete(sesion)
    await db.commit()


@router.patch("/{id_sesion}/estado")
async def actualizar_estado(
    id_sesion: uuid.UUID,
    estado: str,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """Actualiza estado_conexion manualmente (qr_pendiente / conectado / desconectado)."""
    allowed = {"qr_pendiente", "conectado", "desconectado"}
    if estado not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Estado inválido. Usar uno de: {allowed}"
        )
    stmt = (
        update(SesionWhatsApp)
        .where(SesionWhatsApp.id_sesion == id_sesion)
        .values(
            estado_conexion=estado,
            fecha_ultima_conexion=datetime.now(timezone.utc) if estado == "conectado" else None,
        )
    )
    await db.execute(stmt)
    await db.commit()
    return {"ok": True, "estado": estado}


# ------------------------------------------------------------------
# Endpoints auxiliares (delegados a Evolution API)
# ------------------------------------------------------------------

@router.get("/{id_sesion}/qr")
async def obtener_qr_por_id(id_sesion: uuid.UUID, db: AsyncSession = Depends(get_db), _: Usuario = Depends(require_admin)):
    """
    Obtiene QR fresco de Evolution API para la sesión identificada por UUID.
    Usa el token propio de la instancia (token_autorizacion en BD).
    Normaliza la respuesta para que el frontend siempre reciba { base64, code }.
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    instancia = sesion.instancia_evolution
    token = sesion.token_autorizacion

    if not token:
        logger.warning(
            "Sesión '%s' no tiene token_autorizacion. "
            "Intentando con clave global (puede fallar). "
            "Ejecuta fix_tokens.sh para corregir.",
            instancia
        )

    try:
        # Evolution v2: GET /instance/connect/{instanceName}
        # Retorna el QR si la instancia está desconectada,
        # o { instance: { state: "open" } } si ya está conectada
        data = await _evo_get(f"/instance/connect/{instancia}", token=token)

        # Evolution v2 puede devolver distintas estructuras:
        # { base64: "data:image/png;base64,..." }  → imagen QR
        # { code: "2@xxx..." }                     → texto QR
        # { qrcode: { base64: "...", code: "..." } }
        # { instance: { state: "open" } }          → ya conectado
        # Detectar si ya está conectada: {"instance": {"state": "open"}}
        state = (data.get("instance") or {}).get("state", "")
        if state == "open":
            # Sincronizar BD automáticamente
            await db.execute(
                update(SesionWhatsApp)
                .where(SesionWhatsApp.id_sesion == id_sesion)
                .values(
                    estado_conexion="conectado",
                    fecha_ultima_conexion=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            logger.info("Sesión '%s' ya conectada — BD sincronizada.", instancia)
            return {"base64": None, "code": None, "estado": "conectado", "raw": data}

        b64 = (
            data.get("base64")
            or (data.get("qrcode") or {}).get("base64")
        )
        code = (
            data.get("code")
            or (data.get("qrcode") or {}).get("code")
        )

        if not b64 and not code:
            logger.warning(
                "QR sin contenido esperado para '%s': %s",
                instancia, data
            )

        return {"base64": b64, "code": code, "raw": data}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error obteniendo QR para '%s': %s", instancia, exc)
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/{id_sesion}/verificar")
async def verificar_estado(id_sesion: uuid.UUID, db: AsyncSession = Depends(get_db), _: Usuario = Depends(require_admin)):
    """
    Consulta el estado de la sesión en Evolution API y sincroniza la BD.
    Retorna el estado actual de Evolution.
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    try:
        # Usa el token propio de la instancia
        data = await _evo_get(
            f"/instance/connectionState/{sesion.instancia_evolution}",
            token=sesion.token_autorizacion,
        )
        # Evolution devuelve: {"instance": {"instanceName": "...", "state": "open"}}
        estado_evo = (data.get("instance") or data).get("state", "unknown")
    except HTTPException as exc:
        raise exc
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    # Mapear estado Evolution → BD
    nuevo_estado = {
        "open": "conectado",
        "close": "desconectado",
        "connecting": "qr_pendiente",
    }.get(estado_evo, "desconectado")

    extra = {}
    if nuevo_estado == "conectado":
        numero = await _obtener_numero_instancia(sesion.instancia_evolution)
        if numero:
            extra["numero_telefono"] = numero

    stmt = update(SesionWhatsApp).where(
        SesionWhatsApp.id_sesion == id_sesion
    ).values(
        estado_conexion=nuevo_estado,
        fecha_ultima_conexion=(
            datetime.now(timezone.utc)
            if nuevo_estado == "conectado"
            else sesion.fecha_ultima_conexion
        ),
        **extra,
    )
    await db.execute(stmt)
    await db.commit()

    return {"estado_evolution": estado_evo, "estado_bd": nuevo_estado, **extra}


async def _refrescar_grupos_bg(id_sesion: uuid.UUID, instancia: str, token: str | None) -> None:
    """Refresca la caché de grupos_whatsapp en background (no bloquea la respuesta)."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(
                f"{EVOLUTION_URL}/group/fetchAllGroups/{instancia}?getParticipants=false",
                headers=_evo_headers(token),
            )
        if r.status_code != 200:
            logger.warning("Refresco grupos '%s' → HTTP %s", instancia, r.status_code)
            return
        raw = r.json() if r.text.strip() else []
        if not isinstance(raw, list) or not raw:
            return
        async with AsyncSessionLocal() as db:
            await db.execute(text("""
                INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size, actualizado_en)
                SELECT g->>'id', :sid, g->>'subject', (g->>'size')::int, now()
                FROM json_array_elements(CAST(:data AS json)) AS g
                WHERE g->>'id' IS NOT NULL AND g->>'id' != ''
                ON CONFLICT (id_grupo, id_sesion) DO UPDATE
                  SET subject = EXCLUDED.subject,
                      size    = EXCLUDED.size,
                      actualizado_en = now()
            """), {"sid": str(id_sesion), "data": r.text})
            await db.commit()
        logger.info("Caché grupos actualizada para '%s': %d grupos", instancia, len(raw))
    except Exception as exc:
        logger.warning("Error refrescando grupos en bg para '%s': %s", instancia, exc)


@router.get("/{id_sesion}/grupos")
async def listar_grupos(id_sesion: uuid.UUID, db: AsyncSession = Depends(get_db), _: Usuario = Depends(get_current_user)):
    """
    Devuelve grupos desde caché en BD (respuesta inmediata).
    Dispara un refresco en background para mantener la caché actualizada.
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    # Leer caché de BD
    result = await db.execute(
        select(text("id_grupo, subject, size"))
        .select_from(text("grupos_whatsapp"))
        .where(text("id_sesion = :sid"))
        .order_by(text("lower(subject)"))
        .params(sid=str(id_sesion))
    )
    grupos = [
        {"id": row[0], "subject": row[1], "size": row[2] or 0}
        for row in result.fetchall()
    ]

    # Refresco en background (no bloquea)
    asyncio.create_task(
        _refrescar_grupos_bg(id_sesion, sesion.instancia_evolution, sesion.token_autorizacion)
    )

    logger.info("Grupos para '%s': %d (caché BD)", sesion.instancia_evolution, len(grupos))
    return grupos


@router.get("/{id_sesion}/resolver-enlace")
async def resolver_enlace_grupo(
    id_sesion: uuid.UUID,
    codigo: str,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """
    Convierte un código de invitación de WhatsApp en el JID del grupo.

    Evolution API: GET /group/inviteInfo/{instanceName}?inviteCode=XXXX
    Responde: { groupJid: "120363xxx@g.us", subject: "Nombre grupo", ... }

    El frontend extrae el código de la URL (chat.whatsapp.com/<codigo>)
    y lo pasa aquí como query param ?codigo=XXXX.

    Retorna: { jid: "120363xxx@g.us", subject: "Nombre del grupo" }
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    instancia = sesion.instancia_evolution
    token = sesion.token_autorizacion

    if not codigo or len(codigo) < 5:
        raise HTTPException(status_code=400, detail="Código de invitación inválido")

    try:
        data = await _evo_get(
            f"/group/inviteInfo/{instancia}?inviteCode={codigo}",
            token=token,
        )
        logger.info(
            "inviteInfo para código '%s' en '%s': %s",
            codigo[:8], instancia, str(data)[:200]
        )

        # Evolution puede devolver estructuras distintas según la versión:
        # { groupJid: "...", subject: "..." }
        # { id: "...", subject: "..." }
        jid = (
            data.get("groupJid")
            or data.get("id")
            or data.get("jid")
        )
        subject = data.get("subject") or data.get("name") or ""

        if not jid:
            logger.warning(
                "inviteInfo no devolvió JID para código '%s'. Respuesta: %s",
                codigo, data
            )
            raise HTTPException(
                status_code=422,
                detail=(
                    "No se pudo obtener el JID del grupo. "
                    "Verifica que el enlace sea válido y que la sesión esté conectada. "
                    f"Respuesta Evolution: {str(data)[:200]}"
                ),
            )

        # Asegurar que el JID termine en @g.us (formato grupo)
        if "@" not in jid:
            jid = jid + "@g.us"

        return {"jid": jid, "subject": subject}

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Error resolviendo enlace código='%s' instancia='%s': %s",
            codigo, instancia, exc
        )
        raise HTTPException(status_code=502, detail=str(exc))


@router.get("/{id_sesion}/contactos")
async def listar_contactos(
    id_sesion: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(get_current_user),
):
    """
    Devuelve la agenda de contactos desde Evolution API.
    Evolution v1.x: GET /chat/findContacts/{instance}?where={"key":{"remoteJid":""}}
    Solo retorna contactos con JID @s.whatsapp.net (números personales).
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    try:
        data = await _evo_post(
            f"/chat/findContacts/{sesion.instancia_evolution}",
            {"where": {}},
            token=sesion.token_autorizacion,
        )
    except HTTPException as exc:
        raise exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    contactos = []
    raw = data if isinstance(data, list) else (data.get("contacts") or data.get("data") or [])
    for c in raw:
        jid = c.get("id") or c.get("jid") or ""
        if not jid or "@s.whatsapp.net" not in jid:
            continue
        nombre = c.get("name") or c.get("pushName") or c.get("notify") or ""
        numero = jid.replace("@s.whatsapp.net", "")
        contactos.append({"jid": jid, "nombre": nombre, "numero": numero})

    contactos.sort(key=lambda x: (x["nombre"].lower() if x["nombre"] else "zzz" + x["numero"]))
    logger.info("Contactos para '%s': %d", sesion.instancia_evolution, len(contactos))
    return contactos


@router.post("/{id_sesion}/sincronizar")
async def sincronizar_sesion(
    id_sesion: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """
    Sincroniza token y grupos desde Evolution API.
    Útil cuando el token en BD quedó desactualizado tras reconexión.
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    # 1. Obtener token correcto desde Evolution (usa global key)
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{EVOLUTION_URL}/instance/fetchInstances?instanceName={sesion.instancia_evolution}",
            headers={"apikey": EVOLUTION_KEY},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail="No se pudo consultar Evolution API")

    data = r.json()
    instances = data if isinstance(data, list) else [data]
    token_correcto = None
    for inst in instances:
        info = inst.get("instance", inst)
        if info.get("instanceName") == sesion.instancia_evolution:
            token_correcto = info.get("apikey")
            break

    if not token_correcto:
        raise HTTPException(status_code=404, detail="Instancia no encontrada en Evolution")

    # 2. Actualizar token en BD si cambió
    if token_correcto != sesion.token_autorizacion:
        await db.execute(
            update(SesionWhatsApp)
            .where(SesionWhatsApp.id_sesion == id_sesion)
            .values(token_autorizacion=token_correcto)
        )
        await db.commit()
        logger.info("Token actualizado para '%s'", sesion.instancia_evolution)

    # 3. Forzar refresco de grupos con token correcto
    asyncio.create_task(
        _refrescar_grupos_bg(id_sesion, sesion.instancia_evolution, token_correcto)
    )

    return {"ok": True, "token_actualizado": token_correcto != sesion.token_autorizacion}


@router.post("/{id_sesion}/configurar-webhook")
async def configurar_webhook(
    id_sesion: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """Configura webhook y settings correctos para la sesión."""
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    await _configurar_instancia_bg(sesion.instancia_evolution, sesion.token_autorizacion)
    return {"ok": True}


@router.post("/{id_sesion}/verificar-numero")
async def verificar_numero_whatsapp(
    id_sesion: uuid.UUID,
    body: dict = Body(...),
    db: AsyncSession = Depends(get_db),
    _: Usuario = Depends(require_admin),
):
    """
    Verifica si un número de teléfono tiene cuenta de WhatsApp activa.
    Llama a Evolution API /chat/whatsappNumbers/{instance}.
    Retorna: { jid, existe, nombre }
    """
    sesion = await db.get(SesionWhatsApp, id_sesion)
    if not sesion:
        raise HTTPException(status_code=404, detail="Sesión no encontrada")

    numero_raw = str(body.get("numero", "")).strip()
    digits = "".join(c for c in numero_raw if c.isdigit())
    if not digits:
        raise HTTPException(status_code=400, detail="Número inválido")

    try:
        result = await _evo_post(
            f"/chat/whatsappNumbers/{sesion.instancia_evolution}",
            {"numbers": [digits]},
            token=sesion.token_autorizacion,
        )
        if isinstance(result, list) and result:
            item = result[0]
            existe = bool(item.get("exists"))
            jid = item.get("jid") or f"{digits}@s.whatsapp.net"
            nombre = item.get("name") or item.get("pushName") or ""
            return {"jid": jid, "existe": existe, "nombre": nombre}
    except HTTPException as exc:
        logger.warning("verificar-numero '%s': %s", digits, exc.detail)

    return {"jid": f"{digits}@s.whatsapp.net", "existe": None, "nombre": ""}
