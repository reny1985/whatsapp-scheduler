"""
v1.0.8 — envío escalonado, estimación, semáforo anti-spam y límites de peso.
Postgres desechable; Evolution simulado; las pausas NO se esperan (tests/conftest.py registra lo que se habría dormido).
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import text

from app import envio_config as cfg
from app import scheduler
from app.models import MensajeProgramado
from app.services import estimacion, semaforo
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)
from tests.test_sin_sesion import NUM, U1, _api, _m, _msg, _s, _sesion

pytestmark = pytest.mark.asyncio(loop_scope="function")

INDIV = "593900000001@s.whatsapp.net"
GRUPO = "120363000000000001@g.us"
VIEJA = lambda: datetime.now(timezone.utc) - timedelta(days=30)        # número vinculado hace tiempo
HACE = lambda s: datetime.now(timezone.utc) - timedelta(seconds=s)


async def _ciclo(enviar=None, esperar=True):
    enviar = enviar or AsyncMock(return_value={"key": {"id": "X"}})
    with patch.object(scheduler, "_enviar_mensaje", new=enviar):
        await scheduler._procesar_mensajes_pendientes(esperar=esperar)
    return enviar


async def _carga(maker, sid, n, destino_fmt="5939{:08d}@s.whatsapp.net", texto_fmt="texto {}", estado="enviado", usuario=U1):
    """Inserta n mensajes de HOY para la sesión (rápido, con generate_series)."""
    async with maker() as db:
        await db.execute(text(
            "INSERT INTO mensajes_programados (id_mensaje,id_usuario,id_sesion,id_grupo,texto_mensaje,fecha_hora_disparo,estado_envio,enviado_en) "
            "SELECT gen_random_uuid(), :u, :s, '5939' || lpad(g::text, 8, '0') || :suf, " + ("CAST(:t AS text) || g" if "{}" in texto_fmt else "CAST(:t AS text)") + ", now(), CAST(:e AS varchar), CASE WHEN :env THEN now() END "
            "FROM generate_series(1, :n) g"),
            {"u": usuario, "s": sid, "suf": "@g.us" if "g.us" in destino_fmt else "@s.whatsapp.net",
             "t": texto_fmt.replace("{}", ""), "e": estado, "env": estado == "enviado", "n": n})
        await db.commit()


# ══ 1) Colas por sesión ══════════════════════════════════════════════════════

async def test_las_pausas_estan_dentro_de_rango_segun_el_destino(Session, _programador_limpio):
    dormidas = _programador_limpio
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    destinos = [INDIV, GRUPO, INDIV, GRUPO, GRUPO, INDIV, INDIV, GRUPO]
    for i, d in enumerate(destinos):
        await _msg(Session, sid=sid, destino=d, disparo=HACE(100 - i))     # orden de disparo = orden de la lista
    await _ciclo()
    assert len(dormidas) == len(destinos) + 1                               # espera inicial + una pausa tras cada envío
    assert 0 <= dormidas[0] <= cfg.ESPERA_INICIAL_COLA[1]
    for d, pausa in zip(destinos, dormidas[1:]):
        lo, hi = cfg.PAUSA_GRUPO if d.endswith("@g.us") else cfg.PAUSA_INDIVIDUAL
        assert lo <= pausa <= hi, (d, pausa)


async def test_espera_inicial_solo_antes_del_primero_y_nunca_antes_de_la_hora(Session, _programador_limpio):
    dormidas = _programador_limpio
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    futuro = await _msg(Session, sid=sid, disparo=datetime.now(timezone.utc) + timedelta(seconds=30))
    pasado = await _msg(Session, sid=sid, disparo=HACE(5))
    enviar = await _ciclo()
    assert enviar.await_count == 1                                          # el futuro NO se toca (nunca antes de su hora)
    assert (await _m(Session, futuro)).estado_envio == "programado"
    enviado = await _m(Session, pasado)
    assert enviado.estado_envio == "enviado" and enviado.enviado_en >= enviado.fecha_hora_disparo
    assert 0 <= dormidas[0] <= 20 and len(dormidas) == 2                    # una espera inicial + una pausa


async def test_los_mensajes_de_una_sesion_salen_en_orden_de_fecha_hora_disparo(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    orden = []
    for seg, dest in ((10, "593900000003@s.whatsapp.net"), (90, "593900000001@s.whatsapp.net"), (50, "593900000002@s.whatsapp.net")):
        await _msg(Session, sid=sid, destino=dest, disparo=HACE(seg))       # se crean desordenados
    enviar = await _ciclo()
    orden = [c.kwargs["destinatario"] for c in enviar.await_args_list]
    assert orden == ["593900000001@s.whatsapp.net", "593900000002@s.whatsapp.net", "593900000003@s.whatsapp.net"]


async def test_dos_sesiones_envian_en_paralelo_sin_bloquearse(Session):
    a = await _sesion(Session, numero="593900000001", fecha_creacion=VIEJA())
    b = await _sesion(Session, numero="593900000002", fecha_creacion=VIEJA())
    for sid in (a, b):
        for i in range(2):
            await _msg(Session, sid=sid, disparo=HACE(60 - i), origen=None)
    barrera = asyncio.Barrier(2)          # solo se cruza si AMBAS sesiones están enviando a la vez
    cruzaron = []

    async def envio(**kw):
        if not cruzaron or len(cruzaron) < 2:
            await asyncio.wait_for(barrera.wait(), timeout=3)
            cruzaron.append(kw["instancia"])
        return {"key": {"id": "X"}}

    enviar = await _ciclo(AsyncMock(side_effect=envio))
    assert enviar.await_count == 4 and len(set(cruzaron)) == 2


async def test_un_ciclo_nuevo_no_lanza_una_segunda_cola_para_la_misma_sesion(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    ids = [await _msg(Session, sid=sid, disparo=HACE(50 - i)) for i in range(3)]
    liberar = asyncio.Event()
    llamadas = []

    async def envio(**kw):
        llamadas.append(kw["destinatario"])
        await liberar.wait()
        return {"key": {"id": "X"}}

    mock = AsyncMock(side_effect=envio)
    with patch.object(scheduler, "_enviar_mensaje", new=mock):
        await scheduler._procesar_mensajes_pendientes()                    # arranca la cola
        await asyncio.sleep(0.05)
        primera = scheduler._workers[sid]
        for _ in range(3):                                                  # tres ciclos más mientras envía
            await scheduler._procesar_mensajes_pendientes()
        assert len(scheduler._workers) == 1 and scheduler._workers[sid] is primera
        liberar.set()
        await scheduler._esperar_trabajadores()
    assert mock.await_count == 3                                            # cada mensaje se envió una sola vez
    assert [(await _m(Session, i)).estado_envio for i in ids] == ["enviado"] * 3


async def test_el_ciclo_del_programador_es_de_20_segundos():
    job = scheduler.crear_scheduler().get_job("enviar_mensajes")
    assert cfg.CICLO_SEG == 20 and job.trigger.interval.total_seconds() == 20


# ══ 2) Reinicio y caída larga ════════════════════════════════════════════════

async def _latido(maker, hace_min):
    async with maker() as db:
        await db.execute(text("INSERT INTO estado_programador (clave, valor) VALUES ('latido', :v) "
                              "ON CONFLICT (clave) DO UPDATE SET valor = EXCLUDED.valor"),
                         {"v": datetime.now(timezone.utc) - timedelta(minutes=hace_min)})
        await db.commit()


async def test_tras_un_reinicio_corto_lo_en_cola_se_retoma_sin_perderse_ni_duplicarse(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    en_cola = [await _msg(Session, sid=sid, estado="en_cola", disparo=HACE(120 - i), destino=f"59390000000{i}@s.whatsapp.net") for i in range(3)]
    ya_enviado = await _msg(Session, sid=sid, estado="enviado", disparo=HACE(300))
    a_medias = await _msg(Session, sid=sid, estado="enviando", disparo=HACE(200))
    await _latido(Session, hace_min=5)                                      # el servidor estuvo caído 5 min
    enviar = await _ciclo()
    destinos = [c.kwargs["destinatario"] for c in enviar.await_args_list]
    assert sorted(destinos) == sorted(f"59390000000{i}@s.whatsapp.net" for i in range(3))   # los 3, una vez cada uno
    assert [(await _m(Session, i)).estado_envio for i in en_cola] == ["enviado"] * 3
    assert (await _m(Session, ya_enviado)).estado_envio == "enviado"
    m = await _m(Session, a_medias)                                         # el que estaba enviándose NO se reenvía solo
    assert m.estado_envio == "fallido" and "reinicio" in m.error_envio
    await _ciclo(enviar)                                                    # otro ciclo: nada se vuelve a enviar
    assert enviar.await_count == 3


async def test_tras_una_caida_de_mas_de_60_min_lo_en_cola_pasa_a_vencido(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    normal = await _msg(Session, sid=sid, estado="en_cola", disparo=HACE(4000))
    recurrente = await _msg(Session, sid=sid, estado="en_cola", disparo=HACE(4000), destino=GRUPO)
    async with Session() as db:
        await db.execute(text("UPDATE mensajes_programados SET recurrencia='daily' WHERE id_mensaje=:i"), {"i": recurrente})
        await db.commit()
    await _latido(Session, hace_min=90)
    enviar = await _ciclo()
    enviar.assert_not_called()
    for i in (normal, recurrente):
        m = await _m(Session, i)
        assert m.estado_envio == "vencido" and "sin actividad" in m.error_envio and "90" in m.error_envio
    async with Session() as db:                                             # la serie recurrente no se corta
        hijos = (await db.execute(text("SELECT count(*) FROM mensajes_programados WHERE id_mensaje_origen=:i"), {"i": recurrente})).scalar_one()
    assert hijos == 1


async def test_el_latido_se_actualiza_en_cada_ciclo(Session):
    await _latido(Session, hace_min=3)
    await _ciclo()
    async with Session() as db:
        v = (await db.execute(text("SELECT valor FROM estado_programador WHERE clave='latido'"))).scalar_one()
    assert datetime.now(timezone.utc) - v < timedelta(seconds=30)


# ══ 3) Semáforo anti-spam ════════════════════════════════════════════════════

@pytest.mark.parametrize("n,color", [(0, "verde"), (99, "verde"), (100, "amarillo"), (300, "amarillo"), (301, "rojo")])
async def test_semaforo_cada_color_con_numero_antiguo(Session, n, color):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, n)
    async with Session() as db:
        r = await semaforo.calcular(db, sid, VIEJA())
    assert r["color"] == color and r["individuales"] == n and r["numero_nuevo"] is False
    assert r["pausas_duplicadas"] == (color == "rojo")
    assert r["umbral_amarillo"] == 100 and r["umbral_rojo"] == 300


@pytest.mark.parametrize("n,color", [(49, "verde"), (50, "amarillo"), (150, "amarillo"), (151, "rojo")])
async def test_semaforo_numero_nuevo_tiene_umbrales_a_la_mitad(Session, n, color):
    reciente = datetime.now(timezone.utc) - timedelta(days=2)
    sid = await _sesion(Session, fecha_creacion=reciente)
    await _carga(Session, sid, n)
    async with Session() as db:
        r = await semaforo.calcular(db, sid, reciente)
    assert r["color"] == color and r["numero_nuevo"] is True and r["umbral_amarillo"] == 50 and r["umbral_rojo"] == 150
    assert any("recién vinculado" in a for a in r["avisos"])


async def test_a_los_7_dias_el_numero_ya_no_es_nuevo(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    async with Session() as db:
        justo = await semaforo.calcular(db, sid, datetime.now(timezone.utc) - timedelta(days=7, minutes=1))
        antes = await semaforo.calcular(db, sid, datetime.now(timezone.utc) - timedelta(days=6, hours=23))
    assert justo["numero_nuevo"] is False and antes["numero_nuevo"] is True


async def test_los_grupos_cuentan_aparte_y_pesan_0_2(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, 495, destino_fmt="x@g.us")                  # 495 × 0,2 = 99
    async with Session() as db:
        r = await semaforo.calcular(db, sid, VIEJA())
    assert r["grupos"] == 495 and r["individuales"] == 0 and r["carga"] == 99.0 and r["color"] == "verde"
    await _carga(Session, sid, 5, destino_fmt="x@g.us", texto_fmt="otro")  # 500 grupos = 100
    async with Session() as db:
        r = await semaforo.calcular(db, sid, VIEJA())
    assert r["carga"] == 100.0 and r["color"] == "amarillo"


async def test_cuentan_enviados_y_programados_de_hoy_pero_no_otros_dias(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _msg(Session, sid=sid, estado="enviado", destino=INDIV, disparo=HACE(60))
    await _msg(Session, sid=sid, estado="programado", destino="593900000002@s.whatsapp.net",
               disparo=datetime.now(timezone.utc).astimezone(semaforo.EC).replace(hour=23, minute=59).astimezone(timezone.utc))
    await _msg(Session, sid=sid, estado="programado", destino="593900000003@s.whatsapp.net", disparo=datetime.now(timezone.utc) + timedelta(days=2))
    await _msg(Session, sid=sid, estado="enviado", destino="593900000004@s.whatsapp.net", disparo=datetime.now(timezone.utc) - timedelta(days=2))
    await _msg(Session, sid=sid, estado="cancelado", destino="593900000005@s.whatsapp.net", disparo=HACE(30))
    async with Session() as db:
        r = await semaforo.calcular(db, sid, VIEJA())
    assert r["individuales"] == 2          # el enviado de hoy + el programado de hoy (a las 23:59 de Ecuador)


async def test_aviso_si_el_mismo_texto_va_a_mas_de_20_numeros_individuales(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, 20, texto_fmt="Hola, promo del día")
    async with Session() as db:
        r20 = await semaforo.calcular(db, sid, VIEJA())
    assert r20["texto_repetido"] is False                                    # exactamente 20: todavía no
    await _msg(Session, sid=sid, destino="593911111111@s.whatsapp.net")      # (otro texto: no cuenta)
    await _carga(Session, sid, 21, destino_fmt="x@s.whatsapp.net", texto_fmt="Hola, promo del día")
    async with Session() as db:
        r = await semaforo.calcular(db, sid, VIEJA())
    assert r["texto_repetido"] is True and "Personaliza tus mensajes para evitar bloqueos." in r["avisos"]


async def test_texto_repetido_a_grupos_no_dispara_el_aviso(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, 30, destino_fmt="x@g.us", texto_fmt="Aviso para todos")
    async with Session() as db:
        assert (await semaforo.calcular(db, sid, VIEJA()))["texto_repetido"] is False


async def test_en_rojo_las_pausas_se_duplican_y_ningun_mensaje_se_cancela(Session, _programador_limpio):
    dormidas = _programador_limpio
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, 310)                                          # rojo: > 300 hoy
    pend = [await _msg(Session, sid=sid, destino=d, disparo=HACE(80 - i)) for i, d in enumerate([INDIV, GRUPO, INDIV])]
    await _ciclo()
    pausas = dormidas[1:]
    assert len(pausas) == 3
    assert 2 * cfg.PAUSA_INDIVIDUAL[0] <= pausas[0] <= 2 * cfg.PAUSA_INDIVIDUAL[1]      # 40–90 s
    assert 2 * cfg.PAUSA_GRUPO[0] <= pausas[1] <= 2 * cfg.PAUSA_GRUPO[1]                # 16–40 s
    assert 2 * cfg.PAUSA_INDIVIDUAL[0] <= pausas[2] <= 2 * cfg.PAUSA_INDIVIDUAL[1]
    assert [(await _m(Session, i)).estado_envio for i in pend] == ["enviado"] * 3        # se envían todos
    async with Session() as db:
        n = (await db.execute(text("SELECT count(*) FROM mensajes_programados WHERE estado_envio IN ('cancelado','vencido','fallido')"))).scalar_one()
    assert n == 0


async def test_en_verde_las_pausas_no_se_duplican(Session, _programador_limpio):
    dormidas = _programador_limpio
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _msg(Session, sid=sid, destino=INDIV, disparo=HACE(30))
    await _ciclo()
    assert cfg.PAUSA_INDIVIDUAL[0] <= dormidas[1] <= cfg.PAUSA_INDIVIDUAL[1]


async def test_el_resumen_del_inicio_trae_el_semaforo_de_cada_numero(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    await _carga(Session, sid, 120)
    j = (await _api(Session, "GET", "/dashboard/resumen")).json()
    sm = j["sesiones"][0]["semaforo"]
    assert sm["color"] == "amarillo" and sm["individuales"] == 120 and "ritmo" in sm["frase"].lower() or sm["frase"]
    assert sm["umbral_amarillo"] == 100


# ══ 4) Estimación al programar y "En cola · sale aprox." ═════════════════════

def _pend(minuto, destino=INDIV, n=1):
    base = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc) + timedelta(minutes=minuto)      # 10:00 en Ecuador
    return [SimpleNamespace(id_mensaje=uuid.uuid4(), fecha_hora_disparo=base, id_grupo=destino) for _ in range(n)]


async def test_estimacion_con_las_pausas_promedio():
    ahora = datetime(2026, 10, 14, 14, 50, tzinfo=timezone.utc)
    hora = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    r = estimacion.aviso_al_programar(_pend(0, n=3), hora, INDIV, ahora, rojo=False)
    # primero 10:00:10 (espera media 10 s); luego +32,5 s cada uno → el 4.º (nuevo) a las 10:01:47
    assert r["n"] == 3 and r["hora"] == "10:00" and r["desde"] == "10:00" and r["hasta"] == "10:01"
    assert r["mensaje"] == "Este número ya tiene 3 mensajes a las 10:00; se enviarán aproximadamente entre 10:00 y 10:01"


async def test_estimacion_singular_y_sin_mensajes_cercanos():
    ahora = datetime(2026, 10, 14, 14, 50, tzinfo=timezone.utc)
    hora = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    assert "ya tiene 1 mensaje a las" in estimacion.aviso_al_programar(_pend(0), hora, INDIV, ahora, False)["mensaje"]
    assert estimacion.aviso_al_programar([], hora, INDIV, ahora, False) == {"n": 0}
    lejos = _pend(60, n=5)                                                    # una hora después: fuera de la ventana cercana
    assert estimacion.aviso_al_programar(lejos, hora, INDIV, ahora, False) == {"n": 0}


async def test_estimacion_grupos_son_mas_rapidos_y_en_rojo_el_doble():
    ahora = datetime(2026, 10, 14, 14, 50, tzinfo=timezone.utc)
    hora = datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)
    items = lambda d: [(i.id_mensaje, i.fecha_hora_disparo, i.id_grupo) for i in _pend(0, destino=d, n=10)]
    ind = estimacion.simular(items(INDIV), ahora, rojo=False)
    grp = estimacion.simular(items(GRUPO), ahora, rojo=False)
    roj = estimacion.simular(items(INDIV), ahora, rojo=True)
    ult = lambda s: max(s.values())
    assert ult(grp) < ult(ind) < ult(roj)
    assert (ult(ind) - min(ind.values())).total_seconds() == pytest.approx(9 * 32.5)
    assert (ult(roj) - min(roj.values())).total_seconds() == pytest.approx(9 * 65.0)
    assert (ult(grp) - min(grp.values())).total_seconds() == pytest.approx(9 * 14.0)


async def test_endpoint_de_estimacion_usa_los_mensajes_de_esa_sesion(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    otra = await _sesion(Session, numero="593900000009", fecha_creacion=VIEJA())
    hora = datetime.now(timezone.utc) + timedelta(hours=2)
    for _ in range(4):
        await _msg(Session, sid=sid, disparo=hora)
    await _msg(Session, sid=otra, disparo=hora)
    q = f"/mensajes/estimacion?id_sesion={sid}&fecha_hora={hora.isoformat().replace('+', '%2B')}&grupo=false"
    r = (await _api(Session, "GET", q)).json()
    assert r["n"] == 4 and r["mensaje"].startswith("Este número ya tiene 4 mensajes a las ")
    vacio = (await _api(Session, "GET", f"/mensajes/estimacion?id_sesion={otra}&fecha_hora={(hora + timedelta(hours=5)).isoformat().replace('+', '%2B')}")).json()
    assert vacio == {"n": 0}
    ajena = await _api(Session, "GET", q, usuario=uuid.uuid4())
    assert ajena.status_code == 404


async def test_mensajes_en_cola_muestran_su_salida_estimada_en_orden(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    ids = [await _msg(Session, sid=sid, estado="en_cola", disparo=HACE(100 - i)) for i in range(3)]
    otro = await _msg(Session, sid=sid, estado="enviado", disparo=HACE(500))
    lista = {m["id_mensaje"]: m for m in (await _api(Session, "GET", "/mensajes/")).json()}
    salidas = [lista[str(i)]["salida_estimada"] for i in ids]
    assert all(salidas) and salidas == sorted(salidas) and len(set(salidas)) == 3
    assert lista[str(otro)]["salida_estimada"] is None


# ══ 5) "Escribiendo…" ════════════════════════════════════════════════════════

class _Cliente:
    posts: list = []
    respuesta = (201, {"key": {"id": "X"}})

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        _Cliente.posts.append((url, json))
        st, body = _Cliente.respuesta
        return httpx.Response(st, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def evo(monkeypatch):
    _Cliente.posts, _Cliente.respuesta = [], (201, {"key": {"id": "X"}})
    monkeypatch.setattr(scheduler.httpx, "AsyncClient", _Cliente)
    monkeypatch.setattr(scheduler, "EVOLUTION_URL", "http://evo")
    monkeypatch.setattr(scheduler, "BASE_PUBLIC_URL", "https://app.test")
    return _Cliente


async def _enviar(tipo, texto="hola"):
    url = {"image": "/static/uploads/a.jpg", "audio": "/static/uploads/a.ogg", None: None}[tipo]
    return await scheduler._enviar_mensaje("wa-1", INDIV, texto, tipo, url, "tok")


async def test_escribiendo_desactivado_no_agrega_options(evo, monkeypatch):
    monkeypatch.setattr(cfg, "ESCRIBIENDO_ACTIVO", False)
    for tipo in (None, "image", "audio"):
        await _enviar(tipo)
    texto, imagen, audio, texto_audio = evo.posts
    assert "options" not in texto[1] and "options" not in imagen[1]
    assert audio[1]["options"] == {"encoding": True}                          # el audio conserva solo su opción de siempre


async def test_escribiendo_activado_usa_delay_de_1_a_3_s_y_presencia_segun_el_tipo(evo, monkeypatch):
    monkeypatch.setattr(cfg, "ESCRIBIENDO_ACTIVO", True)
    for tipo in (None, "image", "audio"):
        await _enviar(tipo)
    texto, imagen, audio, _ = evo.posts
    assert texto[0].endswith("/sendText/wa-1") and texto[1]["options"]["presence"] == "composing"
    assert imagen[0].endswith("/sendMedia/wa-1") and imagen[1]["options"]["presence"] == "composing"
    assert audio[0].endswith("/sendWhatsAppAudio/wa-1")
    assert audio[1]["options"]["presence"] == "recording" and audio[1]["options"]["encoding"] is True
    for _, payload in (texto, imagen, audio):
        assert cfg.ESCRIBIENDO_MS[0] <= payload["options"]["delay"] <= cfg.ESCRIBIENDO_MS[1]


async def test_si_evolution_rechaza_las_options_se_desactivan_y_no_se_reintenta(evo, monkeypatch):
    monkeypatch.setattr(cfg, "ESCRIBIENDO_ACTIVO", True)
    evo.respuesta = (400, {"message": ["options.presence must be one of the following values"]})
    with pytest.raises(RuntimeError, match="Evolution API 400"):
        await _enviar(None)
    assert len(evo.posts) == 1                                                # NO se reintentó (pudo haber salido)
    assert scheduler._escribiendo_rechazado is True
    evo.respuesta = (201, {"key": {"id": "X"}})
    await _enviar(None)
    assert "options" not in evo.posts[-1][1]                                  # los siguientes ya van sin options


async def test_un_error_400_ajeno_a_las_options_no_las_desactiva(evo, monkeypatch):
    monkeypatch.setattr(cfg, "ESCRIBIENDO_ACTIVO", True)
    evo.respuesta = (400, {"message": "number does not exist"})
    with pytest.raises(RuntimeError):
        await _enviar(None)
    assert scheduler._escribiendo_rechazado is False


async def test_un_envio_que_falla_queda_fallido_y_no_se_reintenta(Session):
    sid = await _sesion(Session, fecha_creacion=VIEJA())
    mid = await _msg(Session, sid=sid, disparo=HACE(20))
    enviar = await _ciclo(AsyncMock(side_effect=RuntimeError("Evolution API 500: timeout")))
    assert enviar.await_count == 1
    assert (await _m(Session, mid)).estado_envio == "fallido"
    await _ciclo(enviar)
    assert enviar.await_count == 1


# ══ 6) Límites de peso ═══════════════════════════════════════════════════════

from httpx import ASGITransport, AsyncClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import media  # noqa: E402

MB = 1024 * 1024


@pytest.fixture
def subidas(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "UPLOAD_DIR", tmp_path)
    return tmp_path


async def _subir(nombre, datos, tipo):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/media/subir", files={"file": (nombre, datos, tipo)})


async def test_los_limites_estan_en_un_solo_lugar():
    assert cfg.LIMITE_MB == {"image": 5, "audio": 16, "video": 16, "document": 25}


async def test_imagen_hasta_5_mb_ok_y_mas_de_5_mb_se_rechaza_con_mensaje_claro(subidas):
    ok = await _subir("a.jpg", b"\xff\xd8\xff" + b"0" * (5 * MB - 3), "image/jpeg")
    assert ok.status_code == 201
    r = await _subir("b.jpg", b"\xff\xd8\xff" + b"0" * (5 * MB), "image/jpeg")
    assert r.status_code == 413
    assert r.json()["detail"].startswith("La imagen pesa 5,0 MB; el máximo permitido es 5 MB.")
    assert [p.name for p in subidas.iterdir() if p.name.endswith("b.jpg")] == []      # lo rechazado no queda guardado


async def test_documento_hasta_25_mb_ok_y_mas_se_rechaza(subidas):
    assert (await _subir("a.pdf", b"%PDF" + b"0" * (25 * MB - 4), "application/pdf")).status_code == 201
    r = await _subir("b.pdf", b"%PDF" + b"0" * (26 * MB), "application/pdf")
    assert r.status_code == 413 and "El documento pesa 26,0 MB; el máximo permitido es 25 MB." in r.json()["detail"]


async def test_tope_interno_de_50_mb_para_el_archivo_original(subidas):
    r = await _subir("c.pdf", b"%PDF" + b"0" * (51 * MB), "application/pdf")
    assert r.status_code == 413


@pytest.mark.parametrize("tipo,ct,args,nombre", [
    ("video", "video/mp4", ["-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=2", "-pix_fmt", "yuv420p", "-c:v", "libx264"], "v.mp4"),
    ("audio", "audio/mp4", ["-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "aac"], "a.m4a"),
])
async def test_audio_y_video_se_miden_despues_de_convertir(subidas, monkeypatch, tmp_path_factory, tipo, ct, args, nombre):
    import shutil, subprocess
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg no está instalado")
    origen = tmp_path_factory.mktemp("origen") / nombre
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args, str(origen)], check=True)
    # con el límite real el archivo chico pasa
    ok = await _subir(nombre, origen.read_bytes(), ct)
    assert ok.status_code == 201 and ok.json()["tipo_media"] == tipo
    # con un límite de 0 MB, el archivo CONVERTIDO lo supera: se rechaza, se informa y no queda guardado
    monkeypatch.setitem(cfg.LIMITE_MB, tipo, 0)
    antes = {p.name for p in subidas.iterdir()}
    r = await _subir(nombre, origen.read_bytes(), ct)
    assert r.status_code == 413
    assert "después de convertirlo; el máximo permitido es 0 MB" in r.json()["detail"]
    assert {p.name for p in subidas.iterdir()} == antes


async def test_el_formulario_muestra_los_nuevos_limites():
    html = open(media.__file__.replace("routers/media.py", "templates/index.html"), encoding="utf-8").read()
    assert "Máx. 50 MB" not in html
    assert "imágenes 5 MB" in html and "audio y video 16 MB" in html and "documentos 25 MB" in html
