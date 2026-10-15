"""
Mensajes sin sesión y reasignación al re-vincular el mismo número, dueño por id_usuario, Inicio y Mensajes,
chequeo de cada minuto para el correo de desconexión, y migración de id_usuario. Postgres desechable.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text

from app import scheduler
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.main import app
from app.migrate import run_migrations
from app.models import MensajeProgramado, SesionWhatsApp
from app.routers import sesiones, webhook
from app.services import aviso_desconexion, correo, evolution_estado, reasignar, vigilancia
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")
U2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
NUM = "593961409996"
OTRO_NUM = "593900000000"


@pytest.fixture
def mundo(Session, monkeypatch):
    enviar = AsyncMock(return_value="id-correo")
    monkeypatch.setattr(correo, "enviar", enviar)
    for modulo in (aviso_desconexion, vigilancia, reasignar, webhook):
        monkeypatch.setattr(modulo, "AsyncSessionLocal", Session)
    monkeypatch.setattr(aviso_desconexion, "GRACIA_SEG", 0)
    monkeypatch.setattr(vigilancia, "_ultima_verificacion", {})
    # por defecto, Evolution confirma que la sesión NO está conectada (cada prueba lo cambia cuando le interesa)
    monkeypatch.setattr(evolution_estado, "consultar_estado", AsyncMock(return_value="close"))
    monkeypatch.setattr(webhook.perfil, "actualizar_perfil_si_corresponde", AsyncMock(return_value=True))
    yield Session, enviar
    app.dependency_overrides.clear()


async def _usuario2(maker):
    async with maker() as db:
        await db.execute(text("INSERT INTO usuarios (id_usuario, email, password_hash, rol, email_verificado) "
                              "VALUES (:u, 'otro@x.com', 'x', 'admin', true) ON CONFLICT DO NOTHING"), {"u": U2})
        await db.commit()


async def _verificar_correo_usuario(maker, email="cliente@ejemplo.com"):
    async with maker() as db:
        await db.execute(text("UPDATE usuarios SET email=:e, email_verificado=true WHERE id_usuario=:u"), {"e": email, "u": U1})
        await db.commit()


async def _sesion(maker, usuario=U1, estado="conectado", numero=NUM, **extra):
    sid = uuid.uuid4()
    async with maker() as db:
        db.add(SesionWhatsApp(id_sesion=sid, id_usuario=usuario, instancia_evolution=f"wa-{sid.hex[:8]}",
                              token_autorizacion="tok", estado_conexion=estado, numero_telefono=numero, **extra))
        await db.commit()
    return sid


async def _msg(maker, sid=None, usuario=U1, estado="programado", disparo=None, origen=NUM, destino="120363@g.us"):
    mid = uuid.uuid4()
    async with maker() as db:
        db.add(MensajeProgramado(id_mensaje=mid, id_usuario=usuario, id_sesion=sid, numero_origen=origen,
                                 id_grupo=destino, texto_mensaje="hola", estado_envio=estado,
                                 fecha_hora_disparo=disparo or datetime.now(timezone.utc) + timedelta(hours=1)))
        await db.commit()
    return mid


async def _m(maker, mid):
    async with maker() as db:
        m = await db.get(MensajeProgramado, mid)
        await db.refresh(m)
        return m


async def _s(maker, sid):
    async with maker() as db:
        s = await db.get(SesionWhatsApp, sid)
        await db.refresh(s)
        return s


# ── Re-vincular el MISMO número reasigna los programados ─────────────────────

async def _conectar_nueva_sesion(maker, monkeypatch, numero=NUM):
    sid = await _sesion(maker, estado="qr_pendiente", numero=None)
    inst = (await _s(maker, sid)).instancia_evolution
    monkeypatch.setattr(webhook, "_fetch_instancia_info", AsyncMock(return_value={"apikey": "k", "numero": numero}))
    await webhook._sincronizar_conexion(inst, "open")
    return sid


async def test_revincular_el_mismo_numero_reasigna_los_programados_sin_sesion(mundo, monkeypatch):
    maker, _ = mundo
    await _usuario2(maker)
    futuro = await _msg(maker)                                                          # programado, mismo número
    vencido = await _msg(maker, estado="vencido", disparo=datetime.now(timezone.utc) - timedelta(hours=2))
    otro_numero = await _msg(maker, origen=OTRO_NUM)
    sin_origen = await _msg(maker, origen=None)                                         # antiguo: sin número guardado
    de_otro_usuario = await _msg(maker, usuario=U2)
    nueva = await _conectar_nueva_sesion(maker, monkeypatch)
    assert (await _m(maker, futuro)).id_sesion == nueva                                 # reasignado
    assert (await _m(maker, vencido)).id_sesion is None                                 # los vencidos NO se reasignan solos
    assert (await _m(maker, vencido)).estado_envio == "vencido"
    assert (await _m(maker, otro_numero)).id_sesion is None                             # otro número: no
    assert (await _m(maker, sin_origen)).id_sesion is None                              # sin número guardado: manual
    assert (await _m(maker, de_otro_usuario)).id_sesion is None                         # de otro usuario: no


async def test_reasignado_antes_de_su_hora_se_envia(mundo, monkeypatch):
    maker, _ = mundo
    pendiente = await _msg(maker, disparo=datetime.now(timezone.utc) - timedelta(seconds=5))
    await _conectar_nueva_sesion(maker, monkeypatch)
    enviar = AsyncMock(return_value={"key": {"id": "X"}})
    with patch.object(scheduler, "_enviar_mensaje", new=enviar):
        await scheduler._procesar_mensajes_pendientes(esperar=True)
    assert enviar.await_count == 1 and (await _m(maker, pendiente)).estado_envio == "enviado"


async def test_sin_sesion_pasada_su_hora_mas_5_min_queda_vencido(mundo):
    maker, _ = mundo
    mid = await _msg(maker, disparo=datetime.now(timezone.utc) - timedelta(minutes=6))
    with patch.object(scheduler, "_enviar_mensaje", new=AsyncMock()) as enviar:
        await scheduler._procesar_mensajes_pendientes(esperar=True)
    enviar.assert_not_called()
    m = await _m(maker, mid)
    assert m.estado_envio == "vencido" and "eliminada" in m.error_envio


async def test_la_sesion_de_otro_usuario_no_envia_mensajes_ajenos(mundo):
    maker, _ = mundo
    await _usuario2(maker)
    ajena = await _sesion(maker, usuario=U2)
    mid = await _msg(maker, sid=ajena, usuario=U1, disparo=datetime.now(timezone.utc) - timedelta(seconds=30))
    with patch.object(scheduler, "_enviar_mensaje", new=AsyncMock()) as enviar:
        await scheduler._procesar_mensajes_pendientes(esperar=True)
    enviar.assert_not_called()
    assert (await _m(maker, mid)).estado_envio == "programado"        # espera (dentro de la tolerancia)


# ── Inicio y Mensajes cuentan y muestran los sin sesión ──────────────────────

async def _api(maker, metodo, ruta, usuario=U1, json=None):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=usuario)
    app.dependency_overrides[require_admin] = lambda: MagicMock(id_usuario=usuario)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            return await c.request(metodo, ruta, json=json)


async def test_inicio_cuenta_y_avisa_de_los_mensajes_sin_sesion(mundo):
    maker, _ = mundo
    sid = await _sesion(maker)
    async with maker() as db:
        await db.execute(text("INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size) VALUES ('120363@g.us', :s, 'Dolaritos', 5)"), {"s": sid})
        await db.commit()
    await _msg(maker, disparo=datetime.now(timezone.utc) + timedelta(hours=1))                      # sin sesión, programado
    await _msg(maker, estado="vencido", disparo=datetime.now(timezone.utc) - timedelta(hours=1))   # sin sesión, vencido
    await _msg(maker, sid=sid, disparo=datetime.now(timezone.utc) + timedelta(hours=2))            # con sesión
    j = (await _api(maker, "GET", "/dashboard/resumen")).json()
    t = j["tarjetas"]
    assert t["pendientes"] == 2 and t["vencidos"] == 1 and t["sin_sesion"] == 1
    assert j["aviso_sin_sesion"] == {"programados": 1} and j["tiene_mensajes"] is True
    prox = j["proximos"]
    assert len(prox) == 2 and [p["sin_sesion"] for p in prox] == [True, False]
    assert prox[0]["destino"] == "Dolaritos"                  # el nombre del grupo sale aunque no tenga sesión


async def test_inicio_sin_mensajes_sin_sesion_no_muestra_aviso(mundo):
    maker, _ = mundo
    sid = await _sesion(maker)
    await _msg(maker, sid=sid)
    j = (await _api(maker, "GET", "/dashboard/resumen")).json()
    assert j["aviso_sin_sesion"] is None and j["tarjetas"]["sin_sesion"] == 0


async def test_mensajes_lista_los_sin_sesion_y_filtra_por_usuario(mundo):
    maker, _ = mundo
    await _usuario2(maker)
    sid = await _sesion(maker)
    huerfano = await _msg(maker)
    vencido = await _msg(maker, estado="vencido", disparo=datetime.now(timezone.utc) - timedelta(hours=1))
    con_sesion = await _msg(maker, sid=sid)
    ajeno = await _msg(maker, usuario=U2)
    todos = (await _api(maker, "GET", "/mensajes/")).json()
    ids = {m["id_mensaje"] for m in todos}
    assert ids == {str(huerfano), str(vencido), str(con_sesion)} and str(ajeno) not in ids
    assert next(m for m in todos if m["id_mensaje"] == str(huerfano))["id_sesion"] is None
    solo = (await _api(maker, "GET", "/mensajes/?sin_sesion=true")).json()
    assert {m["id_mensaje"] for m in solo} == {str(huerfano), str(vencido)}
    assert {m["id_mensaje"] for m in (await _api(maker, "GET", "/mensajes/?estado=vencido")).json()} == {str(vencido)}
    assert (await _api(maker, "GET", "/mensajes/sin-sesion/conteo")).json() == {"programados": 1, "vencidos": 1, "total": 2}
    assert (await _api(maker, "GET", f"/mensajes/{ajeno}")).status_code == 404               # ajeno: no se ve
    assert (await _api(maker, "DELETE", f"/mensajes/{ajeno}")).status_code == 404
    stats = (await _api(maker, "GET", "/mensajes/stats")).json()
    assert stats["total"] == 3 and stats["vencidos"] == 1


async def test_reasignar_a_mano_uno_o_todos(mundo):
    maker, _ = mundo
    await _usuario2(maker)
    sid = await _sesion(maker, numero=OTRO_NUM)
    ajena = await _sesion(maker, usuario=U2)
    a = await _msg(maker); b = await _msg(maker, origen=None); v = await _msg(maker, estado="vencido")
    r = await _api(maker, "PATCH", f"/mensajes/{a}/reasignar", json={"id_sesion": str(sid)})
    assert r.status_code == 200 and r.json()["id_sesion"] == str(sid)
    assert (await _m(maker, a)).numero_origen == OTRO_NUM                       # ahora recuerda el número de su nueva sesión
    assert (await _api(maker, "PATCH", f"/mensajes/{v}/reasignar", json={"id_sesion": str(sid)})).status_code == 409   # vencido: se reprograma
    assert (await _api(maker, "PATCH", f"/mensajes/{b}/reasignar", json={"id_sesion": str(ajena)})).status_code == 404  # sesión ajena
    r = await _api(maker, "POST", "/mensajes/reasignar-sin-sesion", json={"id_sesion": str(sid)})
    assert r.json() == {"reasignados": 1}                                       # solo queda b (programado)
    assert (await _m(maker, b)).id_sesion == sid and (await _m(maker, v)).id_sesion is None


async def test_programar_guarda_dueno_y_numero_y_rechaza_sesion_ajena(mundo):
    maker, _ = mundo
    await _usuario2(maker)
    sid = await _sesion(maker)
    ajena = await _sesion(maker, usuario=U2)
    cuerpo = lambda s: {"id_sesion": str(s), "id_grupo": "120363@g.us", "texto_mensaje": "hola",
                        "fecha_hora_disparo": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    r = await _api(maker, "POST", "/mensajes/", json=cuerpo(sid))
    assert r.status_code == 201
    m = await _m(maker, uuid.UUID(r.json()["id_mensaje"]))
    assert m.id_usuario == U1 and m.numero_origen == NUM
    assert (await _api(maker, "POST", "/mensajes/", json=cuerpo(ajena))).status_code == 404


# ── Correo de desconexión: chequeo de cada minuto ────────────────────────────

async def test_correo_por_el_chequeo_aunque_el_webhook_no_haya_avisado_y_una_sola_vez(mundo):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado")          # el webhook nunca disparó el aviso
    await _msg(maker, sid=sid)
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1 and (await _s(maker, sid)).aviso_desconexion_en is not None
    await vigilancia.vigilar_conexiones()
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1                              # una sola vez por desconexión


async def test_evento_perdido_la_vigilancia_detecta_la_caida_real_y_luego_envia_el_correo(mundo, monkeypatch):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="conectado")             # en la BD sigue "conectado": el evento se perdió
    await _msg(maker, sid=sid)
    estado = AsyncMock(return_value="close")
    monkeypatch.setattr(evolution_estado, "consultar_estado", estado)
    await vigilancia.vigilar_conexiones()
    s = await _s(maker, sid)
    assert s.estado_conexion == "desconectado" and s.desconectada_en is not None
    enviar.assert_not_awaited()                                 # aún dentro de la gracia de 2 min (no avisa por microcortes)
    async with maker() as db:                                   # pasan 3 minutos
        (await db.get(SesionWhatsApp, sid)).desconectada_en = datetime.now(timezone.utc) - timedelta(minutes=3)
        await db.commit()
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1


async def test_la_vigilancia_no_consulta_a_evolution_mas_de_una_vez_cada_2_min_por_sesion(mundo, monkeypatch):
    maker, _ = mundo
    sid = await _sesion(maker)
    await _msg(maker, sid=sid)
    estado = AsyncMock(return_value="open")
    monkeypatch.setattr(evolution_estado, "consultar_estado", estado)
    await vigilancia.vigilar_conexiones(); await vigilancia.vigilar_conexiones(); await vigilancia.vigilar_conexiones()
    assert estado.await_count == 1
    assert (await _s(maker, sid)).estado_conexion == "conectado"


@pytest.mark.parametrize("estado_evolution", ["open", None])
async def test_conectada_de_verdad_o_evolution_sin_respuesta_no_cambia_nada(mundo, monkeypatch, estado_evolution):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker)
    await _msg(maker, sid=sid)
    monkeypatch.setattr(evolution_estado, "consultar_estado", AsyncMock(return_value=estado_evolution))
    await vigilancia.vigilar_conexiones()
    assert (await _s(maker, sid)).estado_conexion == "conectado"
    enviar.assert_not_awaited()


async def test_microcorte_no_envia_correo(mundo):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado", desconectada_en=datetime.now(timezone.utc) - timedelta(seconds=30))
    await _msg(maker, sid=sid)
    await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()


async def test_sin_pendientes_la_vigilancia_no_envia(mundo):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    await _sesion(maker, estado="desconectado")
    await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()


async def test_un_correo_por_desconexion_y_otro_en_la_siguiente(mundo, monkeypatch):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado")
    inst = (await _s(maker, sid)).instancia_evolution
    await _msg(maker, sid=sid)
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1
    monkeypatch.setattr(webhook, "_fetch_instancia_info", AsyncMock(return_value={"apikey": "k", "numero": NUM}))
    await webhook._sincronizar_conexion(inst, "open")                    # reconecta → se limpia la marca
    assert (await _s(maker, sid)).aviso_desconexion_en is None
    async with maker() as db:                                            # nueva desconexión, ya pasada la gracia
        s = await db.get(SesionWhatsApp, sid)
        s.estado_conexion, s.desconectada_en = "desconectado", datetime.now(timezone.utc) - timedelta(minutes=5)
        await db.commit()
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 2


# ── Borrar una sesión a propósito NO manda correo ────────────────────────────

async def test_eliminar_una_sesion_a_proposito_no_envia_correo(mundo, monkeypatch):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker)
    await _msg(maker, sid=sid)
    monkeypatch.setattr(sesiones.httpx, "AsyncClient", MagicMock(side_effect=RuntimeError("sin red")))   # el borrado en Evolution es opcional
    r = await _api(maker, "DELETE", f"/sesiones/{sid}")
    assert r.status_code == 204
    await vigilancia.vigilar_conexiones()
    await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()
    async with maker() as db:   # el mensaje queda sin sesión (sigue siendo del usuario)
        m = (await db.execute(text("SELECT id_usuario, id_sesion FROM mensajes_programados"))).one()
    assert m.id_usuario == U1 and m.id_sesion is None


async def test_si_se_elimina_la_sesion_durante_la_espera_del_aviso_no_se_envia(mundo, monkeypatch):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado")
    await _msg(maker, sid=sid)
    import asyncio
    tarea = asyncio.create_task(aviso_desconexion.avisar_desconexion(sid, espera=0.2))     # evento de desconexión + gracia
    await asyncio.sleep(0.05)
    monkeypatch.setattr(sesiones.httpx, "AsyncClient", MagicMock(side_effect=RuntimeError("sin red")))
    assert (await _api(maker, "DELETE", f"/sesiones/{sid}")).status_code == 204             # el usuario la elimina
    assert await tarea is False
    enviar.assert_not_awaited()


async def test_microcorte_que_se_recupera_durante_la_espera_no_envia(mundo):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="conectado")                    # ya se reconectó cuando termina la espera
    await _msg(maker, sid=sid)
    assert await aviso_desconexion.avisar_desconexion(sid, espera=0.05) is False
    enviar.assert_not_awaited()


# ── Migración de id_usuario ──────────────────────────────────────────────────

async def test_migracion_de_id_usuario_es_idempotente_y_no_borra_datos(Session):
    engine = Session.kw["bind"]
    sid = uuid.uuid4()
    async with Session() as db:
        await db.execute(text("INSERT INTO usuarios (id_usuario, email, password_hash, rol, email_verificado) VALUES (:u,'otro@x.com','x','admin',true)"), {"u": U2})
        await db.execute(text("INSERT INTO sesiones_whatsapp (id_sesion,id_usuario,instancia_evolution,estado_conexion,numero_telefono) VALUES (:s,:u,'wa-a','conectado',:n)"),
                         {"s": sid, "u": U2, "n": NUM})
        await db.commit()
    async with engine.begin() as conn:      # deja el esquema como estaba ANTES de esta versión
        await conn.execute(text("ALTER TABLE mensajes_programados DROP COLUMN id_usuario"))
        await conn.execute(text("ALTER TABLE mensajes_programados DROP COLUMN numero_origen"))
    ids = [uuid.uuid4() for _ in range(3)]
    async with Session() as db:
        for i, (m, ses) in enumerate(zip(ids, (sid, None, None))):
            await db.execute(text("INSERT INTO mensajes_programados (id_mensaje,id_sesion,id_grupo,texto_mensaje,fecha_hora_disparo,estado_envio) "
                                  "VALUES (:m,:s,'1@g.us','t',now(),'enviado')"), {"m": m, "s": ses})
        await db.commit()
    snap = "SELECT id_mensaje,id_sesion,id_grupo,texto_mensaje,fecha_hora_disparo,estado_envio FROM mensajes_programados ORDER BY id_mensaje"
    async with Session() as db:
        antes = (await db.execute(text(snap))).all()
    await run_migrations(engine)
    await run_migrations(engine)            # idempotente
    async with Session() as db:
        despues = (await db.execute(text(snap))).all()
        filas = (await db.execute(text("SELECT id_mensaje, id_usuario, numero_origen FROM mensajes_programados"))).all()
        nulos = (await db.execute(text("SELECT count(*) FROM mensajes_programados WHERE id_usuario IS NULL"))).scalar_one()
        notnull = (await db.execute(text("SELECT is_nullable FROM information_schema.columns WHERE table_name='mensajes_programados' AND column_name='id_usuario'"))).scalar_one()
    assert antes == despues and len(despues) == 3                         # ningún dato cambió ni se perdió
    por_id = {r.id_mensaje: r for r in filas}
    assert por_id[ids[0]].id_usuario == U2 and por_id[ids[0]].numero_origen == NUM      # con sesión: dueño y número de la sesión
    assert por_id[ids[1]].id_usuario == U1 and por_id[ids[2]].id_usuario == U1          # sin sesión: usuario piloto
    assert nulos == 0 and notnull == "NO"


# ── Versión de los archivos estáticos ────────────────────────────────────────

async def test_el_index_versiona_app_js_y_app_css_y_no_se_cachea():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
    import re
    assert re.search(r'/static/js/app\.js\?v=\d+"', r.text) and re.search(r'/static/css/app\.css\?v=\d+"', r.text)
