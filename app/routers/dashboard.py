"""
routers/dashboard.py
Resumen de la pantalla de Inicio en una sola respuesta.

Cuatro consultas agregadas (no una por mensaje), todas filtradas por mensajes_programados.id_usuario (el dueño
del mensaje, que no depende de la sesión), y con "hoy" / "este mes" calculados en America/Guayaquil dentro de
Postgres. Los mensajes sin sesión (su número se eliminó) SÍ se cuentan y se señalan aparte.
"""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models import Usuario

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

ZONA = "America/Guayaquil"
PROXIMOS_LIMITE = 5

_SQL_TARJETAS = text("""
    WITH t AS (
        SELECT
            now() AT TIME ZONE CAST(:tz AS text)                                                     AS ahora,
            (date_trunc('day',   now() AT TIME ZONE CAST(:tz AS text)) AT TIME ZONE CAST(:tz AS text)) AS hoy_ini,
            ((date_trunc('day',  now() AT TIME ZONE CAST(:tz AS text)) + interval '1 day')
                                                           AT TIME ZONE CAST(:tz AS text))           AS hoy_fin,
            (date_trunc('month', now() AT TIME ZONE CAST(:tz AS text)) AT TIME ZONE CAST(:tz AS text)) AS mes_ini,
            ((date_trunc('month', now() AT TIME ZONE CAST(:tz AS text)) + interval '1 month')
                                                           AT TIME ZONE CAST(:tz AS text))           AS mes_fin
    )
    SELECT
        to_char(t.ahora, 'YYYY-MM-DD"T"HH24:MI:SS') AS ahora,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio IN ('programado', 'en_cola', 'enviando')) AS pendientes,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio IN ('programado', 'en_cola', 'enviando')
                                      AND m.estado_sesion <> 'conectado')                AS pendientes_desconectado,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio = 'vencido')                    AS vencidos,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio = 'programado'
                                      AND m.id_sesion IS NULL)                           AS sin_sesion,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio = 'enviado'
                                      AND m.fecha_hora_disparo >= t.hoy_ini
                                      AND m.fecha_hora_disparo <  t.hoy_fin)             AS enviados_hoy,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio = 'enviado'
                                      AND m.fecha_hora_disparo >= t.mes_ini
                                      AND m.fecha_hora_disparo <  t.mes_fin)             AS enviados_mes,
        count(m.id_mensaje) FILTER (WHERE m.estado_envio = 'fallido'
                                      AND m.fecha_hora_disparo >= t.mes_ini
                                      AND m.fecha_hora_disparo <  t.mes_fin)             AS fallidos_mes,
        count(m.id_mensaje)                                                              AS total
    FROM t
    LEFT JOIN (
        SELECT m.*, s.estado_conexion AS estado_sesion
        FROM mensajes_programados m
        LEFT JOIN sesiones_whatsapp s ON s.id_sesion = m.id_sesion
        WHERE m.id_usuario = :uid
    ) m ON TRUE
    GROUP BY t.ahora
""")

_SQL_SIETE_DIAS = text("""
    WITH h AS (SELECT (now() AT TIME ZONE CAST(:tz AS text))::date AS hoy),
    dias AS (SELECT (h.hoy - g) AS fecha FROM h, generate_series(0, 6) AS g),
    msgs AS (
        SELECT (m.fecha_hora_disparo AT TIME ZONE CAST(:tz AS text))::date AS fecha, m.estado_envio
        FROM mensajes_programados m
        WHERE m.id_usuario = :uid
          AND m.estado_envio IN ('enviado', 'fallido')
          AND m.fecha_hora_disparo >= (((SELECT hoy FROM h) - 6)::timestamp AT TIME ZONE CAST(:tz AS text))
    )
    SELECT
        to_char(d.fecha, 'YYYY-MM-DD') AS fecha,
        count(x.estado_envio) FILTER (WHERE x.estado_envio = 'enviado') AS enviados,
        count(x.estado_envio) FILTER (WHERE x.estado_envio = 'fallido') AS fallidos
    FROM dias d
    LEFT JOIN msgs x ON x.fecha = d.fecha
    GROUP BY d.fecha
    ORDER BY d.fecha
""")

_SQL_PROXIMOS = text("""
    SELECT m.id_mensaje, m.id_grupo, m.tipo_media, m.texto_mensaje, m.fecha_hora_disparo,
           m.estado_envio, m.recurrencia, m.id_sesion IS NULL AS sin_sesion, g.subject AS grupo_nombre
    FROM mensajes_programados m
    LEFT JOIN LATERAL (
        SELECT gg.subject
        FROM grupos_whatsapp gg
        JOIN sesiones_whatsapp ss ON ss.id_sesion = gg.id_sesion
        WHERE ss.id_usuario = m.id_usuario AND gg.id_grupo = m.id_grupo
        LIMIT 1
    ) g ON TRUE
    WHERE m.id_usuario = :uid
      AND m.estado_envio IN ('programado', 'en_cola', 'enviando')
    ORDER BY m.fecha_hora_disparo ASC
    LIMIT :limite
""")

_SQL_SESIONES = text("""
    SELECT id_sesion, nombre_sesion, numero_telefono, estado_conexion, perfil_nombre, perfil_foto_en
    FROM sesiones_whatsapp
    WHERE id_usuario = :uid
    ORDER BY fecha_creacion
""")


def _destino(id_grupo: str, nombre: str | None) -> dict:
    es_grupo = id_grupo.endswith("@g.us")
    numero = id_grupo.split("@")[0]
    if es_grupo:
        return {"destino": nombre or "Grupo de WhatsApp", "es_grupo": True}
    return {"destino": f"+{numero}" if numero.isdigit() else numero, "es_grupo": False}


@router.get("/resumen")
async def resumen(
    db: AsyncSession = Depends(get_db),
    usuario: Usuario = Depends(get_current_user),
):
    uid = usuario.id_usuario
    tz = ZONA

    t = (await db.execute(_SQL_TARJETAS, {"tz": tz, "uid": uid})).mappings().one()
    dias = (await db.execute(_SQL_SIETE_DIAS, {"tz": tz, "uid": uid})).mappings().all()
    proximos = (await db.execute(_SQL_PROXIMOS, {"uid": uid, "limite": PROXIMOS_LIMITE})).mappings().all()
    sesiones = (await db.execute(_SQL_SESIONES, {"uid": uid})).mappings().all()

    enviados_7 = sum(d["enviados"] for d in dias)
    fallidos_7 = sum(d["fallidos"] for d in dias)
    terminados = enviados_7 + fallidos_7

    # Perfil para el saludo: la primera sesión conectada (o, si no hay, la primera)
    principal = next((s for s in sesiones if s["estado_conexion"] == "conectado"), sesiones[0] if sesiones else None)

    return {
        "zona": tz,
        "ahora": t["ahora"],
        "tiene_mensajes": t["total"] > 0,
        "perfil": {
            "id_sesion": str(principal["id_sesion"]) if principal else None,
            "nombre": principal["perfil_nombre"] if principal else None,
            "foto_v": principal["perfil_foto_en"].isoformat() if principal and principal["perfil_foto_en"] else None,
        },
        "sesiones": [
            {
                "id_sesion": str(s["id_sesion"]),
                "numero": s["numero_telefono"] or "",
                "estado": s["estado_conexion"],
                "conectado": s["estado_conexion"] == "conectado",
                "nombre": s["perfil_nombre"],
                "foto_v": s["perfil_foto_en"].isoformat() if s["perfil_foto_en"] else None,
            }
            for s in sesiones
        ],
        "tarjetas": {
            "pendientes": t["pendientes"],
            "enviados_hoy": t["enviados_hoy"],
            "enviados_mes": t["enviados_mes"],
            "fallidos_mes": t["fallidos_mes"],
            "vencidos": t["vencidos"],
            "sin_sesion": t["sin_sesion"],
        },
        # Mensajes programados cuyo número se eliminó: esperan; al re-vincular el mismo número se reasignan solos
        "aviso_sin_sesion": {"programados": t["sin_sesion"]} if t["sin_sesion"] > 0 else None,
        # Aviso del Inicio: hay mensajes esperando y su WhatsApp está desconectado
        "aviso_desconexion": (
            {"pendientes": t["pendientes_desconectado"]} if t["pendientes_desconectado"] > 0 else None
        ),
        "proximos": [
            {
                "id_mensaje": str(m["id_mensaje"]),
                **_destino(m["id_grupo"], m["grupo_nombre"]),
                "tipo_media": m["tipo_media"],
                "fecha_hora_disparo": m["fecha_hora_disparo"].isoformat(),
                "estado": m["estado_envio"],
                "recurrente": bool(m["recurrencia"] and m["recurrencia"] != "none"),
                "sin_sesion": m["sin_sesion"],
            }
            for m in proximos
        ],
        "ultimos_7_dias": [
            {"fecha": d["fecha"], "enviados": d["enviados"], "fallidos": d["fallidos"]} for d in dias
        ],
        "tasa_entrega": round(enviados_7 / terminados * 100, 1) if terminados else None,
    }
