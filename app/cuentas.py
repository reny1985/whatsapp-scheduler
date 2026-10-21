"""
app/cuentas.py
Estado EFECTIVO de una cuenta: el ÚNICO lugar donde se decide si una cuenta puede entrar, programar y enviar.

El estado guardado (cuentas.estado) es el último que se asignó (prueba, activa, cortesía o suspendida). "Vencida" se
CALCULA con las fechas en cada consulta (nunca hay una ventana en la que una cuenta vencida siga enviando porque un
proceso no pasó a marcarla):
  · la cuenta interna siempre está activa;
  · suspendida gana sobre todo;
  · prueba: hasta prueba_fin (sin fecha = vencida: ante un dato faltante se bloquea, no se regala);
  · activa (pagada): hasta periodo_fin; después DIAS_GRACIA_PAGADO días "en gracia" (sigue enviando); luego vencida;
  · cortesía: hasta periodo_fin, sin gracia.
"""
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Cuenta, Usuario
from app.planes import DIAS_GRACIA_PAGADO

CONTACTO_EMAIL = os.getenv("CONTACTO_EMAIL", "soporte@enviafast.net")

MSG_SUSPENDIDA = f"Tu cuenta está suspendida. Escríbenos a {CONTACTO_EMAIL} para más información."
MSG_NO_PROGRAMAR_VENCIDA = (f"Tu plan venció: no puedes programar mensajes. Escríbenos a {CONTACTO_EMAIL} "
                            f"para renovarlo.")
# Motivo que ve el cliente en un mensaje que no salió por el estado de su cuenta
MOTIVO_PLAN_VENCIDO = "Tu plan venció: el mensaje no se envió. Renueva tu plan y reprográmalo."
MOTIVO_SUSPENDIDA = f"Tu cuenta está suspendida: el mensaje no se envió. Escríbenos a {CONTACTO_EMAIL}."
# Encabezado de las respuestas 403 por cuenta suspendida: la app cierra la sesión y muestra el aviso
ENCABEZADO_SUSPENDIDA = {"X-Cuenta": "suspendida"}


def _aware(dt: datetime | None) -> datetime | None:
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class EstadoCuenta:
    estado: str                 # prueba | activa | cortesia | vencida | suspendida
    en_gracia: bool             # pagada con el período terminado, dentro de los días de gracia
    fin: datetime | None        # fin de la prueba o del período (None = sin vencimiento)
    corte: datetime | None      # cuándo deja de poder enviar (fin + gracia si es pagada)

    @property
    def puede_enviar(self) -> bool:
        return self.estado in ("prueba", "activa", "cortesia")

    puede_programar = puede_enviar


def estado_efectivo(cuenta: Cuenta, ahora: datetime | None = None) -> EstadoCuenta:
    ahora = ahora or datetime.now(timezone.utc)
    if cuenta.es_interna:
        return EstadoCuenta("activa", False, None, None)
    guardado = cuenta.estado
    if guardado == "suspendida":
        return EstadoCuenta("suspendida", False, _aware(cuenta.periodo_fin or cuenta.prueba_fin), None)
    if guardado == "prueba":
        fin = _aware(cuenta.prueba_fin)
        return EstadoCuenta("prueba" if fin and ahora <= fin else "vencida", False, fin, fin)
    if guardado == "activa":
        fin = _aware(cuenta.periodo_fin)
        if fin is None:                     # la BD no lo permite fuera de la cuenta interna: se bloquea
            return EstadoCuenta("vencida", False, None, None)
        corte = fin + timedelta(days=DIAS_GRACIA_PAGADO)
        if ahora <= fin:
            return EstadoCuenta("activa", False, fin, corte)
        if ahora <= corte:
            return EstadoCuenta("activa", True, fin, corte)
        return EstadoCuenta("vencida", False, fin, corte)
    if guardado == "cortesia":
        fin = _aware(cuenta.periodo_fin)
        return EstadoCuenta("cortesia" if fin and ahora <= fin else "vencida", False, fin, fin)
    fin = _aware(cuenta.periodo_fin or cuenta.prueba_fin)       # "vencida" guardada (o un valor desconocido)
    return EstadoCuenta("vencida", False, fin, fin)


def motivo_no_envio(est: EstadoCuenta) -> str:
    return MOTIVO_SUSPENDIDA if est.estado == "suspendida" else MOTIVO_PLAN_VENCIDO


async def exigir_puede_programar(db: AsyncSession, usuario: Usuario) -> None:
    """403 si la cuenta de quien pide no puede programar (vencida o suspendida)."""
    cuenta = await db.get(Cuenta, usuario.id_cuenta)
    est = estado_efectivo(cuenta) if cuenta else None
    if est and est.puede_programar:
        return
    if est and est.estado == "suspendida":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MSG_SUSPENDIDA,
                            headers=ENCABEZADO_SUSPENDIDA)
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=MSG_NO_PROGRAMAR_VENCIDA)
