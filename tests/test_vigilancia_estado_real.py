"""
Chequeo de cada minuto basado en el estado REAL de Evolution: confirmar antes de enviar el correo, revisar todas
las sesiones con pendientes y dejar rastro en el log de cada revisión. Postgres desechable; Evolution simulado.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models import MensajeProgramado, SesionWhatsApp
from app.routers import sesiones
from app.services import aviso_desconexion, evolution_estado, vigilancia
from tests.test_sesiones_perdidas import Session  # noqa: F401  (fixture: BD temporal con migraciones)
from tests.test_sin_sesion import (  # noqa: F401  (fixture `mundo` y ayudantes)
    NUM, U1, _api, _m, _msg, _s, _sesion, _verificar_correo_usuario, mundo,
)

pytestmark = pytest.mark.asyncio(loop_scope="function")

VIEJA = lambda: datetime.now(timezone.utc) - timedelta(minutes=10)      # desconectada hace rato (pasada la gracia)


def _real(monkeypatch, por_instancia: dict | str | None):
    """Evolution simulado: un estado fijo para todas las instancias o uno por instancia."""
    async def consulta(sesion):
        return por_instancia.get(sesion.instancia_evolution) if isinstance(por_instancia, dict) else por_instancia
    mock = AsyncMock(side_effect=consulta)
    monkeypatch.setattr(evolution_estado, "consultar_estado", mock)
    return mock


# ── 1) Confirmar con Evolution antes de enviar el correo ─────────────────────

async def test_no_envia_si_evolution_dice_que_esta_conectada_y_corrige_la_base(mundo, monkeypatch, caplog):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())      # la base quedó vieja
    await _msg(maker, sid=sid)
    _real(monkeypatch, "open")
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()
    s = await _s(maker, sid)
    assert s.estado_conexion == "conectado" and s.desconectada_en is None and s.aviso_desconexion_en is None


async def test_el_aviso_del_webhook_tambien_confirma_con_evolution(mundo, monkeypatch, caplog):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado")
    await _msg(maker, sid=sid)
    _real(monkeypatch, "open")                                   # microcorte: ya volvió
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        assert await aviso_desconexion.avisar_desconexion(sid) is False
    enviar.assert_not_awaited()
    assert (await _s(maker, sid)).estado_conexion == "conectado"
    assert "Evolution dice que" in caplog.text and "se corrige y no se envía" in caplog.text


async def test_si_evolution_no_responde_no_envia_y_reintenta_al_minuto_siguiente(mundo, monkeypatch, caplog):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())
    await _msg(maker, sid=sid)
    _real(monkeypatch, None)                                     # Evolution sin respuesta
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()
    assert (await _s(maker, sid)).aviso_desconexion_en is None   # no se reclamó: se podrá reintentar
    assert "no se pudo confirmar con Evolution" in caplog.text
    _real(monkeypatch, "close")                                  # Evolution vuelve y confirma la caída
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1                               # y sigue siendo uno solo


@pytest.mark.parametrize("real", ["close", "connecting"])
async def test_si_evolution_confirma_que_no_esta_abierta_se_envia(mundo, monkeypatch, real):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())
    await _msg(maker, sid=sid)
    _real(monkeypatch, real)
    await vigilancia.vigilar_conexiones()
    assert enviar.await_count == 1


# ── 2) Revisar el estado real de TODAS las sesiones con pendientes ───────────

async def test_revisa_todas_las_sesiones_con_pendientes_en_ambos_sentidos(mundo, monkeypatch):
    maker, enviar = mundo
    a = await _sesion(maker, estado="desconectado", numero="593900000001", desconectada_en=VIEJA())   # base: caída, real: abierta
    b = await _sesion(maker, estado="conectado", numero="593900000002")                                # base: abierta, real: caída
    c = await _sesion(maker, estado="conectado", numero="593900000003")                                # sin pendientes
    d = await _sesion(maker, estado="qr_pendiente", numero="593900000004")                             # vinculación en curso
    for sid in (a, b, d):
        await _msg(maker, sid=sid)
    inst = {x: (await _s(maker, x)).instancia_evolution for x in (a, b, c, d)}
    mock = _real(monkeypatch, {inst[a]: "open", inst[b]: "close", inst[c]: "close", inst[d]: "close"})
    await vigilancia.vigilar_conexiones()
    consultadas = {llamada.args[0].instancia_evolution for llamada in mock.await_args_list}
    assert inst[a] in consultadas and inst[b] in consultadas                       # las que tienen pendientes
    assert inst[c] not in consultadas                                              # sin pendientes: no se consulta
    assert (await _s(maker, a)).estado_conexion == "conectado"                     # corregida (caída → abierta)
    s_b = await _s(maker, b)
    assert s_b.estado_conexion == "desconectado" and s_b.desconectada_en is not None   # corregida (abierta → caída)
    assert (await _s(maker, c)).estado_conexion == "conectado"
    assert (await _s(maker, d)).estado_conexion == "qr_pendiente"                  # no se toca una vinculación en curso


async def test_volver_a_estar_conectada_reasigna_los_mensajes_sin_sesion_del_mismo_numero(mundo, monkeypatch):
    maker, _ = mundo
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())
    await _msg(maker, sid=sid)                                                     # da "pendientes" a la sesión
    huerfano = await _msg(maker, origen=NUM)                                       # programado, sin sesión, mismo número
    _real(monkeypatch, "open")
    await vigilancia.vigilar_conexiones()
    assert (await _m(maker, huerfano)).id_sesion == sid


async def test_no_consulta_a_evolution_mas_de_una_vez_cada_2_min_por_sesion(mundo, monkeypatch):
    maker, _ = mundo
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())
    await _msg(maker, sid=sid)
    mock = _real(monkeypatch, "open")
    await vigilancia.vigilar_conexiones()
    await vigilancia.vigilar_conexiones()
    await vigilancia.vigilar_conexiones()
    assert mock.await_count == 1


# ── 3) Rastro en el log de cada revisión ─────────────────────────────────────

async def test_cada_revision_deja_una_linea_con_estado_real_guardado_y_pendientes(mundo, monkeypatch, caplog):
    maker, _ = mundo
    sin_cambios = await _sesion(maker, estado="conectado", numero="593900000001")
    corregida = await _sesion(maker, estado="desconectado", numero="593900000002", desconectada_en=VIEJA())
    caida = await _sesion(maker, estado="conectado", numero="593900000003")
    muda = await _sesion(maker, estado="conectado", numero="593900000004")
    for sid in (sin_cambios, corregida, caida, muda):
        await _msg(maker, sid=sid); await _msg(maker, sid=sid)
    inst = {x: (await _s(maker, x)).instancia_evolution for x in (sin_cambios, corregida, caida, muda)}
    _real(monkeypatch, {inst[sin_cambios]: "open", inst[corregida]: "open", inst[caida]: "close", inst[muda]: None})
    with caplog.at_level(logging.INFO, logger="whatsapp_scheduler"):
        await vigilancia.vigilar_conexiones()
    log = caplog.text
    assert f"Vigilancia: '{inst[sin_cambios]}' → estado real: open | guardado: conectado | pendientes: 2 | acción: sin cambios" in log
    assert f"Vigilancia: '{inst[corregida]}' → estado real: open | guardado: desconectado | pendientes: 2 | acción: corregida a conectado" in log
    assert f"Vigilancia: '{inst[caida]}' → estado real: close | guardado: conectado | pendientes: 2 | acción: marcada desconectada" in log
    assert f"Vigilancia: '{inst[muda]}' → estado real: sin respuesta de Evolution | guardado: conectado | pendientes: 2 | acción: ninguna" in log


async def test_el_log_no_contiene_claves_ni_correos(mundo, monkeypatch, caplog):
    maker, _ = mundo
    await _verificar_correo_usuario(maker, email="privado@ejemplo.com")
    sid = await _sesion(maker, estado="desconectado", desconectada_en=VIEJA())
    await _msg(maker, sid=sid)
    _real(monkeypatch, "close")
    with caplog.at_level(logging.DEBUG, logger="whatsapp_scheduler"):
        await vigilancia.vigilar_conexiones()
    assert "privado@ejemplo.com" not in caplog.text and "tok" not in caplog.text.replace("token", "")


# ── 4) Eliminar una sesión a propósito: sin correo, y sin consultas inútiles ──

async def test_eliminar_sesion_no_envia_correo_ni_se_consulta_a_evolution_por_ella(mundo, monkeypatch):
    maker, enviar = mundo
    await _verificar_correo_usuario(maker)
    sid = await _sesion(maker)
    await _msg(maker, sid=sid)
    monkeypatch.setattr(sesiones.httpx, "AsyncClient", MagicMock(side_effect=RuntimeError("sin red")))
    mock = _real(monkeypatch, "close")                            # Evolution diría "cerrada" si se le preguntara
    assert (await _api(maker, "DELETE", f"/sesiones/{sid}")).status_code == 204
    await vigilancia.vigilar_conexiones()
    await vigilancia.vigilar_conexiones()
    enviar.assert_not_awaited()
    assert mock.await_count == 0                                  # ya no hay sesión que vigilar
