"""
Pruebas de GET /dashboard/resumen contra un Postgres desechable (se omiten si no hay uno local).
Cubren: tarjetas, "hoy"/"este mes" en America/Guayaquil, filtro por usuario, próximos envíos con
nombre de grupo, últimos 7 días, tasa de entrega, estado vacío y número de consultas constante.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import event, text

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models import MensajeProgramado, SesionWhatsApp
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

U1 = uuid.UUID("00000000-0000-0000-0000-000000000001")  # usuario piloto (lo crea la migración)
U2 = uuid.UUID("00000000-0000-0000-0000-000000000002")
EC = timezone(timedelta(hours=-5))  # Ecuador no tiene horario de verano


def _ec_ahora():
    return datetime.now(timezone.utc).astimezone(EC)


def _inicio_dia_ec(dt_ec):
    return dt_ec.replace(hour=0, minute=0, second=0, microsecond=0)


def _hoy_seguro():
    """Un instante que siempre cae dentro de HOY en Ecuador (a mitad de camino entre la medianoche y ahora),
    para que las pruebas no fallen si se ejecutan justo después de las 00:00."""
    ahora = _ec_ahora()
    inicio = _inicio_dia_ec(ahora)
    return inicio + (ahora - inicio) / 2


@pytest_asyncio.fixture
async def datos(Session):
    async with Session() as db:
        await db.execute(text(
            "INSERT INTO usuarios (id_usuario, email, password_hash, rol) "
            "VALUES (:u, 'otro@x.com', 'x', 'admin')"), {"u": U2})
        await db.commit()
    return Session


async def _sesion(maker, usuario, numero, estado="conectado"):
    sid = uuid.uuid4()
    async with maker() as db:
        db.add(SesionWhatsApp(id_sesion=sid, id_usuario=usuario, instancia_evolution=f"wa-{sid.hex[:8]}",
                              token_autorizacion="t", estado_conexion=estado, numero_telefono=numero))
        await db.commit()
    return sid


async def _msg(maker, sid, cuando, estado="enviado", destino="120363@g.us", tipo=None, rec="none"):
    async with maker() as db:
        duenio = (await db.get(SesionWhatsApp, sid)).id_usuario if sid else U1
        db.add(MensajeProgramado(id_usuario=duenio, id_mensaje=uuid.uuid4(), id_sesion=sid, id_grupo=destino, texto_mensaje="x",
                                 tipo_media=tipo, fecha_hora_disparo=cuando, estado_envio=estado, recurrencia=rec))
        await db.commit()


async def _resumen(maker, usuario=U1):
    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=usuario)
    async with maker() as db:
        app.dependency_overrides[get_db] = lambda: db
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/dashboard/resumen")
    app.dependency_overrides.clear()
    return r


async def test_estado_vacio(datos):
    r = await _resumen(datos)
    assert r.status_code == 200
    j = r.json()
    assert j["tiene_mensajes"] is False and j["sesiones"] == [] and j["proximos"] == []
    assert j["tarjetas"] == {"pendientes": 0, "enviados_hoy": 0, "enviados_mes": 0, "fallidos_mes": 0, "vencidos": 0, "sin_sesion": 0}
    assert j["aviso_desconexion"] is None
    assert len(j["ultimos_7_dias"]) == 7 and all(d["enviados"] == 0 for d in j["ultimos_7_dias"])
    assert j["tasa_entrega"] is None and j["zona"] == "America/Guayaquil"


async def test_tarjetas_hoy_y_mes_en_hora_de_ecuador(datos):
    sid = await _sesion(datos, U1, "593961409996")
    ahora = _ec_ahora()
    hoy_ini = _inicio_dia_ec(ahora)
    mes_ini = hoy_ini.replace(day=1)
    # enviados: uno justo al empezar hoy (cuenta hoy), uno 1 min antes (ayer en Ecuador)
    await _msg(datos, sid, hoy_ini + timedelta(minutes=1))
    await _msg(datos, sid, hoy_ini - timedelta(minutes=1))
    # enviado al empezar el mes y otro justo antes (mes anterior)
    await _msg(datos, sid, mes_ini + timedelta(minutes=1))
    await _msg(datos, sid, mes_ini - timedelta(minutes=1))
    await _msg(datos, sid, hoy_ini + timedelta(minutes=2), estado="fallido")
    await _msg(datos, sid, ahora + timedelta(days=1), estado="programado")
    await _msg(datos, sid, ahora + timedelta(days=2), estado="cancelado")

    j = (await _resumen(datos)).json()
    esperado_mes = sum(1 for t in (hoy_ini + timedelta(minutes=1), hoy_ini - timedelta(minutes=1),
                                   mes_ini + timedelta(minutes=1)) if t >= mes_ini)
    assert j["tarjetas"]["enviados_hoy"] == 1
    assert j["tarjetas"]["enviados_mes"] == esperado_mes
    assert j["tarjetas"]["fallidos_mes"] == 1
    assert j["tarjetas"]["pendientes"] == 1        # los cancelados no son pendientes
    assert j["tiene_mensajes"] is True
    assert j["ahora"][:10] == ahora.strftime("%Y-%m-%d")   # fecha de Ecuador, no UTC


async def test_filtra_siempre_por_usuario(datos):
    s1 = await _sesion(datos, U1, "593961409996")
    s2 = await _sesion(datos, U2, "593900000000")
    ahora = _ec_ahora()
    await _msg(datos, s1, _hoy_seguro())
    for _ in range(3):
        await _msg(datos, s2, _hoy_seguro())
        await _msg(datos, s2, ahora + timedelta(hours=1), estado="programado", destino="593911111111@s.whatsapp.net")

    j1 = (await _resumen(datos, U1)).json()
    j2 = (await _resumen(datos, U2)).json()
    assert j1["tarjetas"]["enviados_hoy"] == 1 and j1["tarjetas"]["pendientes"] == 0 and j1["proximos"] == []
    assert [s["numero"] for s in j1["sesiones"]] == ["593961409996"]
    assert j2["tarjetas"]["enviados_hoy"] == 3 and j2["tarjetas"]["pendientes"] == 3
    assert [s["numero"] for s in j2["sesiones"]] == ["593900000000"]
    assert sum(d["enviados"] for d in j1["ultimos_7_dias"]) == 1


async def test_mensajes_sin_sesion_si_se_cuentan_porque_el_dueño_es_el_usuario(datos):
    sid = await _sesion(datos, U1, "593961409996")
    ahora = _ec_ahora()
    await _msg(datos, sid, _hoy_seguro())
    async with datos() as db:     # mensaje huérfano (su sesión fue borrada): sigue siendo del usuario
        db.add(MensajeProgramado(id_usuario=U1, id_mensaje=uuid.uuid4(), id_sesion=None, id_grupo="1@g.us", texto_mensaje="x",
                                 fecha_hora_disparo=_hoy_seguro(), estado_envio="enviado"))
        await db.commit()
    j = (await _resumen(datos)).json()
    assert j["tarjetas"]["enviados_hoy"] == 2


async def test_proximos_5_ordenados_con_nombre_de_grupo_y_numero(datos):
    sid = await _sesion(datos, U1, "593961409996")
    ahora = _ec_ahora()
    async with datos() as db:
        await db.execute(text("INSERT INTO grupos_whatsapp (id_grupo, id_sesion, subject, size) "
                              "VALUES ('120363@g.us', :s, 'Dolaritos', 50)"), {"s": sid})
        await db.commit()
    await _msg(datos, sid, ahora + timedelta(hours=7), estado="programado", destino="593999@s.whatsapp.net", tipo="audio")
    await _msg(datos, sid, ahora + timedelta(hours=1), estado="programado", tipo="image", rec="daily")
    for h in range(2, 7):
        await _msg(datos, sid, ahora + timedelta(hours=h), estado="programado")
    await _msg(datos, sid, ahora - timedelta(hours=1), estado="enviado")
    p = (await _resumen(datos)).json()["proximos"]
    assert len(p) == 5
    horas = [x["fecha_hora_disparo"] for x in p]
    assert horas == sorted(horas)
    assert p[0]["destino"] == "Dolaritos" and p[0]["es_grupo"] is True
    assert p[0]["tipo_media"] == "image" and p[0]["recurrente"] is True and p[0]["estado"] == "programado"
    j = (await _resumen(datos)).json()
    assert j["tarjetas"]["pendientes"] == 7            # el límite de 5 solo aplica a la lista


async def test_destino_personal_se_muestra_como_numero(datos):
    sid = await _sesion(datos, U1, "593961409996")
    await _msg(datos, sid, _ec_ahora() + timedelta(hours=1), estado="programado", destino="593961409996@s.whatsapp.net")
    p = (await _resumen(datos)).json()["proximos"][0]
    assert p["destino"] == "+593961409996" and p["es_grupo"] is False


async def test_ultimos_7_dias_rellena_dias_vacios_y_calcula_tasa(datos):
    sid = await _sesion(datos, U1, "593961409996")
    hoy_ini = _inicio_dia_ec(_ec_ahora())
    for _ in range(3):
        await _msg(datos, sid, hoy_ini + timedelta(hours=1))                      # hoy
    await _msg(datos, sid, hoy_ini - timedelta(days=2) + timedelta(hours=3))       # hace 2 días
    await _msg(datos, sid, hoy_ini - timedelta(days=2) + timedelta(hours=4), estado="fallido")
    await _msg(datos, sid, hoy_ini - timedelta(days=9), estado="enviado")          # fuera de la ventana
    j = (await _resumen(datos)).json()
    d = j["ultimos_7_dias"]
    assert len(d) == 7 and d[-1]["fecha"] == hoy_ini.strftime("%Y-%m-%d")
    fechas = [x["fecha"] for x in d]
    assert fechas == sorted(fechas) and len(set(fechas)) == 7
    assert d[-1]["enviados"] == 3 and d[-3]["enviados"] == 1 and d[-3]["fallidos"] == 1
    assert sum(x["enviados"] for x in d) == 4
    assert j["tasa_entrega"] == 80.0               # 4 enviados de 5 terminados


async def test_sesiones_con_estado(datos):
    await _sesion(datos, U1, "593969829845", "conectado")
    await _sesion(datos, U1, "593961409996", "desconectado")
    s = (await _resumen(datos)).json()["sesiones"]
    assert {(x["numero"], x["conectado"]) for x in s} == {("593969829845", True), ("593961409996", False)}


async def test_numero_de_consultas_no_depende_de_la_cantidad_de_mensajes(datos):
    sid = await _sesion(datos, U1, "593961409996")
    ahora = _ec_ahora()
    contador = {"n": 0}

    def _cuenta(conn, cursor, statement, parameters, context, executemany):
        contador["n"] += 1

    app.dependency_overrides[get_current_user] = lambda: MagicMock(id_usuario=U1)

    async def medir():
        async with datos() as db:
            event.listen(db.bind.sync_engine, "before_cursor_execute", _cuenta)
            contador["n"] = 0
            app.dependency_overrides[get_db] = lambda: db
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/dashboard/resumen")
            event.remove(db.bind.sync_engine, "before_cursor_execute", _cuenta)
            assert r.status_code == 200
            return contador["n"]

    await _msg(datos, sid, _hoy_seguro())
    pocas = await medir()
    for _ in range(40):
        await _msg(datos, sid, _hoy_seguro())
        await _msg(datos, sid, ahora + timedelta(hours=2), estado="programado")
    muchas = await medir()
    app.dependency_overrides.clear()
    assert pocas == muchas <= 4


async def test_requiere_login():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard/resumen")
    assert r.status_code == 401
