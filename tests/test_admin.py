"""
v1.1.1 — Panel de superadministrador y efectos de los estados de cuenta (Postgres real y desechable; se omiten si no
hay Postgres local).

· Acceso: sin login, un usuario normal, un "superadmin" de una cuenta que no es la interna o un token viejo reciben en
  TODA ruta de /admin (también con método equivocado o cuerpo roto) exactamente la misma respuesta que una ruta
  inexistente, y no se escribe nada. /admin no aparece en OpenAPI.
· Cada acción deja UNA fila de auditoría con antes y después; simular no escribe; la misma clave aplica una sola vez.
· No se puede bajar de plan con más números; la cuenta interna está protegida (endpoint y BD); la auditoría no se
  modifica ni se borra; las respuestas del panel nunca traen contenido de mensajes.
· Efectos: suspendida no entra ni envía (tampoco lo que está en cola) ni recibe avisos; vencida no programa y sus
  mensajes pasan a vencido con su motivo, sin clonar la serie; en gracia sigue enviando.
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app import scheduler
from app.auth import hash_password
from app.cuentas import MOTIVO_PLAN_VENCIDO, MOTIVO_SUSPENDIDA, estado_efectivo
from app.database import get_db
from app.main import app
from app.models import AuditoriaAdmin, Cuenta, MensajeProgramado, PeriodoCuenta, SesionWhatsApp
from app.routers.admin import MSG_INTERNA
from app.services import aviso_desconexion, correo, evolution_estado
from tests.test_aislamiento import ADMIN, _cliente, _rutas_app
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)

pytestmark = pytest.mark.asyncio(loop_scope="function")

AHORA = datetime.now(timezone.utc)
CA = UA = uuid.UUID("00000000-0000-0000-0000-000000000001")      # interna; su usuario es el superadministrador
CP, UP = uuid.UUID("00000000-0000-0000-0000-0000000000c1"), uuid.UUID("00000000-0000-0000-0000-0000000000d1")
CT, UT = uuid.UUID("00000000-0000-0000-0000-0000000000c2"), uuid.UUID("00000000-0000-0000-0000-0000000000d2")
CN, UN = uuid.UUID("00000000-0000-0000-0000-0000000000c3"), uuid.UUID("00000000-0000-0000-0000-0000000000d3")
CG, UG = uuid.UUID("00000000-0000-0000-0000-0000000000c4"), uuid.UUID("00000000-0000-0000-0000-0000000000d4")
CV, UV = uuid.UUID("00000000-0000-0000-0000-0000000000c5"), uuid.UUID("00000000-0000-0000-0000-0000000000d5")
ST = [uuid.uuid4() for _ in range(3)]          # 3 números de la cuenta Team
SG, SV, SA = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
CLAVE = "Clave-de-prueba-123"
# Si alguna de estas cadenas aparece en una respuesta del panel, se filtró contenido de un mensaje
CONTENIDO = ["TEXTO-SECRETO-CLIENTE", "999888777@g.us", "adjunto-secreto"]

CUENTAS = [
    # id, usuario, correo, estado, plan, prueba_fin, periodo_fin
    (CP, UP, "prueba@x.com", "prueba", None, AHORA + timedelta(days=2), None),
    (CT, UT, "team@x.com", "activa", "team", None, AHORA + timedelta(days=10)),
    (CN, UN, "falso-admin@x.com", "activa", "pro", None, AHORA + timedelta(days=20)),
    (CG, UG, "gracia@x.com", "activa", "pro", None, AHORA - timedelta(days=1)),
    (CV, UV, "vencida@x.com", "activa", "pro", None, AHORA - timedelta(days=10)),
]


@pytest_asyncio.fixture
async def panel(Session, monkeypatch):
    maker = Session
    async with maker() as db:
        await db.execute(text("UPDATE usuarios SET email='duena@x.com', password_hash=:h, email_verificado=TRUE "
                              "WHERE id_usuario=:u"), {"u": UA, "h": hash_password(CLAVE)})
        for c, u, email, estado, plan, prueba_fin, periodo_fin in CUENTAS:
            await db.execute(text("INSERT INTO cuentas (id_cuenta, nombre, estado, plan, prueba_fin, periodo_fin) "
                                  "VALUES (:c, :n, :e, :p, :pf, :per)"),
                             {"c": c, "n": email, "e": estado, "p": plan, "pf": prueba_fin, "per": periodo_fin})
            await db.execute(text("INSERT INTO usuarios (id_usuario, id_cuenta, email, password_hash, rol, "
                                  "email_verificado) VALUES (:u, :c, :e, :h, 'admin', TRUE)"),
                             {"u": u, "c": c, "e": email, "h": hash_password(CLAVE)})
        # Un usuario con es_superadmin en una cuenta que NO es la interna: tampoco entra
        await db.execute(text("UPDATE usuarios SET es_superadmin = TRUE WHERE id_usuario = :u"), {"u": UN})
        for i, sid in enumerate(ST):
            db.add(SesionWhatsApp(id_sesion=sid, id_usuario=UT, instancia_evolution=f"wa-team-{i}",
                                  token_autorizacion="tok", estado_conexion="conectado",
                                  numero_telefono=f"59390000000{i}", fecha_ultima_conexion=AHORA))
        db.add_all([
            SesionWhatsApp(id_sesion=SG, id_usuario=UG, instancia_evolution="wa-gracia", token_autorizacion="tok",
                           estado_conexion="conectado", numero_telefono="593911111111", fecha_ultima_conexion=AHORA),
            SesionWhatsApp(id_sesion=SV, id_usuario=UV, instancia_evolution="wa-vencida", token_autorizacion="tok",
                           estado_conexion="conectado", numero_telefono="593922222222", fecha_ultima_conexion=AHORA),
            SesionWhatsApp(id_sesion=SA, id_usuario=UA, instancia_evolution="wa-interna", token_autorizacion="tok",
                           estado_conexion="conectado", numero_telefono="593933333333", fecha_ultima_conexion=AHORA),
        ])
        await db.flush()
        db.add(MensajeProgramado(id_usuario=UT, id_sesion=ST[0], id_grupo="999888777@g.us",
                                 texto_mensaje="TEXTO-SECRETO-CLIENTE", tipo_media="image",
                                 url_media="/static/uploads/adjunto-secreto.jpg", estado_envio="enviado",
                                 fecha_hora_disparo=AHORA - timedelta(minutes=5), enviado_en=AHORA))
        await db.commit()

    async def _db():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_db] = _db
    yield maker
    app.dependency_overrides.clear()


async def _auditorias(maker, id_cuenta=None) -> list[AuditoriaAdmin]:
    async with maker() as db:
        q = select(AuditoriaAdmin).order_by(AuditoriaAdmin.creada_en)
        if id_cuenta:
            q = q.where(AuditoriaAdmin.id_cuenta == id_cuenta)
        return list((await db.execute(q)).scalars().all())


async def _cuenta(maker, id_cuenta) -> Cuenta:
    async with maker() as db:
        return await db.get(Cuenta, id_cuenta)


def _url(ruta: str) -> str:
    return ruta.replace("{id_cuenta}", str(CT)).replace("{id_sesion}", str(ST[0]))


async def _accion(c, ruta: str, cuerpo: dict, confirmar: bool, clave: uuid.UUID | None = None):
    datos = {**cuerpo, "confirmar": confirmar}
    if confirmar:
        datos["clave"] = str(clave or uuid.uuid4())
    return await c.post(ruta, json=datos)


# ── Acceso ───────────────────────────────────────────────────────────────────

async def test_todo_admin_responde_como_ruta_inexistente_para_cualquier_otro(panel):
    async with _cliente(None) as c:
        inexistente = await c.get("/no-existe-esta-ruta")
    assert inexistente.status_code == 404
    clientes = {"sin login": _cliente(None), "usuario normal": _cliente(UT),
                "superadmin de otra cuenta": _cliente(UN), "token viejo del superadmin": _cliente(UA, version=7)}
    errores = []
    for nombre, cliente in clientes.items():
        async with cliente as c:
            for metodo, ruta in sorted(ADMIN):
                url = _url(ruta)
                intentos = [
                    (metodo, url, {"json": {"plan": "pro", "dias": 5, "motivo": "xx x", "confirmar": True,
                                            "clave": str(uuid.uuid4())}}),
                    (metodo, url, {"content": b"{roto", "headers": {"Content-Type": "application/json"}}),
                    ("DELETE", url, {}),                                       # método que no existe
                    ("GET" if metodo == "POST" else "POST", url, {"json": {}}),
                ]
                for m, u, extra in intentos:
                    r = await c.request(m, u, **extra)
                    if r.status_code != 404 or r.json() != inexistente.json() or "x-cuenta" in r.headers:
                        errores.append(f"{nombre}: {m} {u} → {r.status_code} {r.text[:80]}")
    assert not errores, "\n".join(errores)
    assert await _auditorias(panel) == [], "un rechazado llegó a escribir en la auditoría"
    assert (await _cuenta(panel, CT)).plan == "team"


async def test_el_superadmin_entra_y_admin_no_aparece_en_openapi(panel):
    async with _cliente(UA) as c:
        pagina = await c.get("/admin")
        script = await c.get("/admin/admin.js")
        resumen = await c.get("/admin/api/resumen")
        yo = (await c.get("/auth/me")).json()
        esquema = (await c.get("/api/openapi.json")).json()
    assert pagina.status_code == 200 and "Administración" in pagina.text and "/admin/admin.js?v=" in pagina.text
    assert script.status_code == 200 and "javascript" in script.headers["content-type"]
    assert resumen.status_code == 200
    assert yo["es_superadmin"] is True
    assert not [p for p in esquema["paths"] if p.startswith("/admin")]
    async with _cliente(UT) as c:
        assert (await c.get("/auth/me")).json()["es_superadmin"] is False


# ── Resumen, lista y detalle ─────────────────────────────────────────────────

async def test_resumen_cuenta_por_estado_ingreso_y_vencimientos(panel):
    async with _cliente(UA) as c:
        r = (await c.get("/admin/api/resumen")).json()
    # sin la interna: CP prueba; CT, CN y CG activas (CG en gracia); CV vencida tras la gracia
    assert r["conteos"] == {"prueba": 1, "activa": 3, "cortesia": 0, "vencida": 1, "suspendida": 0}
    assert r["en_gracia"] == 1
    assert Decimal(r["ingreso_mensual"]) == Decimal("29.99") + Decimal("9.99")       # Team + Pro al día
    assert Decimal(r["ingreso_en_gracia"]) == Decimal("9.99")
    vencen = {(v["id_cuenta"], v["tipo"]) for v in r["vencen_7_dias"]}
    assert vencen == {(str(CP), "prueba"), (str(CG), "gracia")}


async def test_lista_busca_filtra_y_nunca_trae_contenido_de_mensajes(panel):
    async with _cliente(UA) as c:
        todas = (await c.get("/admin/api/cuentas")).json()
        por_correo = (await c.get("/admin/api/cuentas", params={"q": "TEAM@"})).json()
        vencidas = (await c.get("/admin/api/cuentas", params={"estado": "vencida"})).json()
        sin_plan = (await c.get("/admin/api/cuentas", params={"plan": "sin_plan"})).json()
        respuestas = [await c.get(u) for u in ("/admin/api/resumen", "/admin/api/cuentas", f"/admin/api/cuentas/{CT}",
                                               f"/admin/api/cuentas/{CT}/historial")]
    assert len(todas) == 6
    assert [f["email"] for f in por_correo] == ["team@x.com"]
    t = por_correo[0]
    assert (t["plan"], t["estado"], t["numeros"], t["numeros_max"], t["enviados_mes"]) == ("team", "activa", 3, 4, 1)
    assert [f["id_cuenta"] for f in vencidas] == [str(CV)]
    assert [f["id_cuenta"] for f in sin_plan] == [str(CP)]
    for r in respuestas:
        assert r.status_code == 200
        assert not [m for m in CONTENIDO if m in r.text], f"{r.request.url} trajo contenido de un mensaje"


async def test_detalle_enmascara_los_numeros_y_mostrar_deja_auditoria(panel):
    async with _cliente(UA) as c:
        d = (await c.get(f"/admin/api/cuentas/{CT}")).json()
        assert [n["numero"] for n in d["numeros_lista"]] == ["…0000", "…0001", "…0002"]
        assert "593900000000" not in str(d)
        assert d["conteos"]["enviados_mes"] == 1 and d["acciones"] == ["activar", "cambiar_plan", "suspender"]
        r = await c.post(f"/admin/api/cuentas/{CT}/numeros/{ST[0]}/mostrar")
        assert r.json() == {"numero": "593900000000"}
        ajeno = await c.post(f"/admin/api/cuentas/{CT}/numeros/{SG}/mostrar")      # número de otra cuenta
        assert ajeno.status_code == 404
        interna = (await c.get(f"/admin/api/cuentas/{CA}")).json()
        assert interna["acciones"] == [] and interna["es_interna"] is True
    filas = await _auditorias(panel, CT)
    assert [(a.accion, a.despues) for a in filas] == [("mostrar_numero", {"id_sesion": str(ST[0]), "numero": "…0000"})]


# ── Acciones y auditoría ─────────────────────────────────────────────────────

async def test_simular_no_escribe_y_confirmar_deja_una_auditoria_con_antes_y_despues(panel):
    async with _cliente(UA) as c:
        sim = await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "enterprise"}, confirmar=False)
        assert sim.status_code == 200 and sim.json()["simulacion"] is True
        assert sim.json()["cambios"] == [{"campo": "plan", "antes": "team", "despues": "enterprise"}]
        assert await _auditorias(panel) == [] and (await _cuenta(panel, CT)).plan == "team"

        clave = uuid.uuid4()
        r1 = await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "enterprise"}, True, clave)
        r2 = await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "enterprise"}, True, clave)   # doble toque
    assert r1.status_code == 200 and r2.status_code == 200 and r2.json()["repetida"] is True
    filas = await _auditorias(panel, CT)
    assert len(filas) == 1
    a = filas[0]
    assert (a.accion, a.id_admin, a.email_admin, a.clave) == ("cambiar_plan", UA, "duena@x.com", clave)
    assert a.antes["plan"] == "team" and a.despues["plan"] == "enterprise" and a.creada_en is not None
    assert (await _cuenta(panel, CT)).plan == "enterprise"


async def test_cada_accion_queda_en_la_auditoria(panel):
    async with _cliente(UA) as c:
        pasos = [
            (CP, "extender-prueba", {"dias": 3}, "extender_prueba"),
            (CP, "activar", {"plan": "pro", "nota": "transferencia 21-oct"}, "activar_periodo"),
            (CT, "plan", {"plan": "enterprise"}, "cambiar_plan"),
            (CT, "suspender", {"motivo": "Pago rechazado"}, "suspender"),
            (CT, "reactivar", {}, "reactivar"),
        ]
        for cuenta, ruta, cuerpo, accion in pasos:
            r = await _accion(c, f"/admin/api/cuentas/{cuenta}/{ruta}", cuerpo, confirmar=True)
            assert r.status_code == 200, f"{ruta}: {r.text}"
            ultima = (await _auditorias(panel, cuenta))[-1]
            assert ultima.accion == accion and ultima.antes != ultima.despues
    assert len(await _auditorias(panel)) == len(pasos)


async def test_no_se_puede_bajar_de_plan_con_mas_numeros(panel):
    async with _cliente(UA) as c:
        for confirmar in (False, True):
            r = await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "pro"}, confirmar)
            assert r.status_code == 409
            assert r.json()["detail"] == ("La cuenta tiene 3 números vinculados y el plan Pro permite 1. "
                                          "Pide al cliente que desvincule 2 antes de bajar de plan.")
        # activar con un plan menor tampoco
        r = await _accion(c, f"/admin/api/cuentas/{CT}/activar", {"plan": "pro"}, True)
        assert r.status_code == 409
        # sin números de más sí se puede bajar
        async with panel() as db:
            await db.execute(text("DELETE FROM sesiones_whatsapp WHERE id_sesion IN (:a, :b)"), {"a": ST[1], "b": ST[2]})
            await db.commit()
        r = await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "pro"}, True)
        assert r.status_code == 200, r.text
    assert [a.accion for a in await _auditorias(panel, CT)] == ["cambiar_plan"]
    assert (await _cuenta(panel, CT)).plan == "pro"


async def test_activar_suma_al_vigente_y_a_la_gracia_y_empieza_hoy_si_vencio_o_viene_de_prueba(panel):
    antes = {c: await _cuenta(panel, c) for c in (CT, CG, CV, CP)}
    async with _cliente(UA) as c:
        sin_plan = await _accion(c, f"/admin/api/cuentas/{CP}/activar", {}, True)
        assert sin_plan.status_code == 422 and "Elige el plan" in sin_plan.json()["detail"]
        for cuenta, cuerpo in ((CT, {}), (CG, {}), (CV, {}), (CP, {"plan": "team", "nota": "transferencia 21-oct"})):
            r = await _accion(c, f"/admin/api/cuentas/{cuenta}/activar", cuerpo, True)
            assert r.status_code == 200, r.text
    ahora = datetime.now(timezone.utc)
    d30 = timedelta(days=30)
    assert (await _cuenta(panel, CT)).periodo_fin == antes[CT].periodo_fin + d30              # vigente: se suma
    assert (await _cuenta(panel, CG)).periodo_fin == antes[CG].periodo_fin + d30              # en gracia: también
    for cuenta in (CV, CP):                                                                    # vencida / prueba: hoy
        fin = (await _cuenta(panel, cuenta)).periodo_fin
        assert abs(fin - (ahora + d30)) < timedelta(minutes=1)
    p = await _cuenta(panel, CP)
    assert (p.estado, p.plan) == ("activa", "team")
    async with panel() as db:
        periodo = (await db.execute(select(PeriodoCuenta).where(PeriodoCuenta.id_cuenta == CP))).scalar_one()
    assert (periodo.tipo, periodo.plan, periodo.precio_usd, periodo.nota) == ("pagado", "team", Decimal("29.99"),
                                                                             "transferencia 21-oct")
    assert periodo.id_auditoria == (await _auditorias(panel, CP))[0].id_auditoria


async def test_extender_prueba_vigente_y_vencida(panel):
    async with panel() as db:
        await db.execute(text("UPDATE cuentas SET estado='prueba', plan=NULL, periodo_fin=NULL, "
                              "prueba_fin = now() - interval '3 days' WHERE id_cuenta=:c"), {"c": CV})
        await db.commit()
    fin_cp = (await _cuenta(panel, CP)).prueba_fin
    async with _cliente(UA) as c:
        assert (await _accion(c, f"/admin/api/cuentas/{CP}/extender-prueba", {"dias": 5}, True)).status_code == 200
        assert (await _accion(c, f"/admin/api/cuentas/{CV}/extender-prueba", {"dias": 5}, True)).status_code == 200
        fuera = await _accion(c, f"/admin/api/cuentas/{CP}/extender-prueba", {"dias": 31}, False)
        no_prueba = await _accion(c, f"/admin/api/cuentas/{CT}/extender-prueba", {"dias": 5}, True)
    assert fuera.status_code == 422 and no_prueba.status_code == 409
    assert (await _cuenta(panel, CP)).prueba_fin == fin_cp + timedelta(days=5)
    assert abs((await _cuenta(panel, CV)).prueba_fin - (datetime.now(timezone.utc) + timedelta(days=5))) < timedelta(minutes=1)


async def test_suspender_cierra_sesiones_bloquea_el_login_y_reactivar_vuelve_al_estado_anterior(panel):
    async with _cliente(UA) as c:
        sin_motivo = await _accion(c, f"/admin/api/cuentas/{CT}/suspender", {"motivo": "  "}, True)
        assert sin_motivo.status_code == 422
        r = await _accion(c, f"/admin/api/cuentas/{CT}/suspender", {"motivo": "Pago rechazado"}, True)
        assert r.status_code == 200
    t = await _cuenta(panel, CT)
    assert (t.estado, t.motivo_suspension, t.estado_antes_suspension) == ("suspendida", "Pago rechazado", "activa")

    async with _cliente(UT) as c:                       # su token de antes ya no sirve
        yo = await c.get("/auth/me")
        assert yo.status_code in (401, 403)
    async with _cliente(UT, version=1) as c:            # ni uno nuevo: la cuenta está suspendida
        yo = await c.get("/auth/me")
        assert yo.status_code == 403 and yo.headers["x-cuenta"] == "suspendida"
        assert "suspendida" in yo.json()["detail"]
    async with _cliente(None) as c:
        mala = await c.post("/auth/login", json={"email": "team@x.com", "password": "otra-clave-xx"})
        buena = await c.post("/auth/login", json={"email": "team@x.com", "password": CLAVE})
    assert mala.status_code == 401                       # con la contraseña mala no se revela la suspensión
    assert buena.status_code == 403 and buena.headers["x-cuenta"] == "suspendida" and "access_token" not in buena.cookies

    async with _cliente(UA) as c:
        assert (await _accion(c, f"/admin/api/cuentas/{CT}/reactivar", {}, True)).status_code == 200
    t = await _cuenta(panel, CT)
    assert (t.estado, t.motivo_suspension, t.estado_antes_suspension) == ("activa", None, None)
    async with _cliente(None) as c:
        assert (await c.post("/auth/login", json={"email": "team@x.com", "password": CLAVE})).status_code == 200


async def test_la_cuenta_interna_es_de_solo_lectura_en_el_panel_y_en_la_bd(panel):
    async with _cliente(UA) as c:
        for ruta, cuerpo in (("plan", {"plan": "pro"}), ("extender-prueba", {"dias": 3}), ("activar", {"plan": "pro"}),
                             ("suspender", {"motivo": "prueba"}), ("reactivar", {})):
            for confirmar in (False, True):
                r = await _accion(c, f"/admin/api/cuentas/{CA}/{ruta}", cuerpo, confirmar)
                assert r.status_code == 409 and r.json()["detail"] == MSG_INTERNA, f"{ruta}: {r.text}"
    assert await _auditorias(panel) == []
    for sql in ("UPDATE cuentas SET estado = 'suspendida' WHERE es_interna",
                "UPDATE cuentas SET periodo_fin = now() WHERE es_interna"):
        async with panel() as db:
            with pytest.raises(IntegrityError):
                await db.execute(text(sql))
    i = await _cuenta(panel, CA)
    assert (i.estado, i.plan, i.periodo_fin) == ("activa", "enterprise", None)


async def test_la_bd_no_deja_activa_sin_fecha_ni_estados_o_planes_desconocidos(panel):
    for sql in ("UPDATE cuentas SET periodo_fin = NULL WHERE id_cuenta = :c",
                "UPDATE cuentas SET estado = 'regalada' WHERE id_cuenta = :c",
                "UPDATE cuentas SET plan = 'gratis' WHERE id_cuenta = :c"):
        async with panel() as db:
            with pytest.raises(IntegrityError):
                await db.execute(text(sql), {"c": CT})


async def test_la_auditoria_no_se_puede_modificar_ni_borrar(panel):
    async with _cliente(UA) as c:
        await _accion(c, f"/admin/api/cuentas/{CT}/plan", {"plan": "enterprise"}, True)
    for sql in ("UPDATE auditoria_admin SET accion = 'otra'", "DELETE FROM auditoria_admin",
                "TRUNCATE auditoria_admin CASCADE"):
        async with panel() as db:
            with pytest.raises(DBAPIError):
                await db.execute(text(sql))
    assert len(await _auditorias(panel)) == 1


# ── Efectos de los estados ───────────────────────────────────────────────────

async def test_estado_efectivo_en_los_bordes():
    t = datetime(2026, 10, 21, 12, tzinfo=timezone.utc)
    fin = t - timedelta(days=3)
    assert estado_efectivo(Cuenta(estado="activa", periodo_fin=t), t).en_gracia is False            # el último día
    assert estado_efectivo(Cuenta(estado="activa", periodo_fin=fin), t).en_gracia is True            # justo 3 días
    assert estado_efectivo(Cuenta(estado="activa", periodo_fin=fin - timedelta(seconds=1)), t).estado == "vencida"
    assert estado_efectivo(Cuenta(estado="cortesia", periodo_fin=t - timedelta(seconds=1)), t).estado == "vencida"
    assert estado_efectivo(Cuenta(estado="prueba", prueba_fin=None), t).estado == "vencida"           # sin fecha: bloquea
    assert estado_efectivo(Cuenta(estado="suspendida", periodo_fin=t + timedelta(days=9)), t).puede_enviar is False
    interna = Cuenta(estado="activa", periodo_fin=None, es_interna=True)
    assert estado_efectivo(interna, t).estado == "activa" and estado_efectivo(interna, t).puede_enviar


async def _mensaje(maker, usuario, sesion, *, estado="programado", hace=timedelta(minutes=1), recurrencia="none"):
    mid = uuid.uuid4()
    async with maker() as db:
        db.add(MensajeProgramado(id_mensaje=mid, id_usuario=usuario, id_sesion=sesion, id_grupo="1@g.us",
                                 texto_mensaje="hola", estado_envio=estado, recurrencia=recurrencia,
                                 fecha_hora_disparo=datetime.now(timezone.utc) - hace,
                                 tomado_en=datetime.now(timezone.utc) if estado == "en_cola" else None))
        await db.commit()
    return mid


async def _estado_mensaje(maker, mid):
    async with maker() as db:
        m = await db.get(MensajeProgramado, mid)
        return m.estado_envio, m.error_envio


async def test_al_tomar_vencida_y_suspendida_pasan_a_vencido_sin_clonar_y_gracia_e_interna_salen(panel):
    async with panel() as db:
        await db.execute(text("UPDATE cuentas SET estado='suspendida', estado_antes_suspension='activa', "
                              "motivo_suspension='x' WHERE id_cuenta=:c"), {"c": CT})
        await db.commit()
    vencida = await _mensaje(panel, UV, SV, recurrencia="daily")
    suspendida = await _mensaje(panel, UT, ST[1], recurrencia="daily")
    gracia = await _mensaje(panel, UG, SG)
    interna = await _mensaje(panel, UA, SA)
    async with panel() as db:
        await scheduler._tomar_mensajes(db, datetime.now(timezone.utc))
    assert await _estado_mensaje(panel, vencida) == ("vencido", MOTIVO_PLAN_VENCIDO)
    assert await _estado_mensaje(panel, suspendida) == ("vencido", MOTIVO_SUSPENDIDA)
    assert (await _estado_mensaje(panel, gracia))[0] == "en_cola"
    assert (await _estado_mensaje(panel, interna))[0] == "en_cola"
    async with panel() as db:
        clones = (await db.execute(text("SELECT count(*) FROM mensajes_programados WHERE id_mensaje_origen IN (:a, :b)"),
                                   {"a": vencida, "b": suspendida})).scalar_one()
    assert clones == 0, "la serie de una cuenta vencida o suspendida no se clona"


async def test_suspender_frena_tambien_lo_que_ya_estaba_en_cola(panel, monkeypatch):
    enviar = AsyncMock(return_value={})
    monkeypatch.setattr(scheduler, "_enviar_mensaje", enviar)
    monkeypatch.setattr(scheduler, "_dormir", AsyncMock())
    en_cola = await _mensaje(panel, UT, ST[2], estado="en_cola", hace=timedelta(seconds=10))
    async with _cliente(UA) as c:
        assert (await _accion(c, f"/admin/api/cuentas/{CT}/suspender", {"motivo": "Pago rechazado"}, True)).status_code == 200
    await scheduler._trabajador_sesion(ST[2])
    assert await _estado_mensaje(panel, en_cola) == ("vencido", MOTIVO_SUSPENDIDA)
    enviar.assert_not_awaited()


async def test_cuenta_vencida_no_puede_programar_ni_editar_y_en_gracia_si(panel):
    futuro = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    cuerpo = {"id_grupo": "1@g.us", "texto_mensaje": "hola", "fecha_hora_disparo": futuro}
    pendiente = await _mensaje(panel, UV, SV, hace=-timedelta(hours=3))
    async with _cliente(UV) as c:
        r = await c.post("/mensajes/", json={**cuerpo, "id_sesion": str(SV)})
        e = await c.patch(f"/mensajes/{pendiente}", json={"texto_mensaje": "cambio"})
        yo = await c.get("/auth/me")
    assert r.status_code == 403 and "Tu plan venció" in r.json()["detail"]
    assert e.status_code == 403
    assert yo.status_code == 200                                   # vencida SÍ entra (para ver y renovar)
    async with _cliente(UG) as c:
        assert (await c.post("/mensajes/", json={**cuerpo, "id_sesion": str(SG)})).status_code == 201


async def test_cuenta_suspendida_no_recibe_aviso_de_desconexion(panel, monkeypatch):
    monkeypatch.setattr(aviso_desconexion, "AsyncSessionLocal", panel)
    consultar = AsyncMock(return_value="close")
    enviar = AsyncMock()
    monkeypatch.setattr(evolution_estado, "consultar_estado", consultar)
    monkeypatch.setattr(correo, "enviar", enviar)
    async with panel() as db:
        await db.execute(text("UPDATE sesiones_whatsapp SET estado_conexion='desconectado' WHERE id_sesion=:s"), {"s": ST[0]})
        await db.execute(text("UPDATE cuentas SET estado='suspendida', estado_antes_suspension='activa' "
                              "WHERE id_cuenta=:c"), {"c": CT})
        await db.commit()
    await _mensaje(panel, UT, ST[0], hace=-timedelta(hours=1))
    assert await aviso_desconexion.avisar_desconexion(ST[0]) is False
    consultar.assert_not_awaited()
    enviar.assert_not_awaited()
