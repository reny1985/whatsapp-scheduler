"""
Ajustes comunes: el programador usa trabajadores por sesión con pausas de segundos. Las pruebas no deben
esperar de verdad: _dormir se sustituye por una función que solo REGISTRA cuánto se habría dormido, y el
estado global del programador se reinicia en cada prueba.
"""
import pytest

from app import scheduler


@pytest.fixture(autouse=True)
def _programador_limpio(monkeypatch):
    scheduler._workers.clear()
    monkeypatch.setattr(scheduler, "_recuperado", False)
    monkeypatch.setattr(scheduler, "_escribiendo_rechazado", False)
    dormidas: list[float] = []

    async def _registrar(segundos: float) -> None:
        dormidas.append(segundos)

    monkeypatch.setattr(scheduler, "_dormir", _registrar)
    yield dormidas
    for t in list(scheduler._workers.values()):
        t.cancel()
    scheduler._workers.clear()
