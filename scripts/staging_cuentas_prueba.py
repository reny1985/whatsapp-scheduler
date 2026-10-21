"""
staging_cuentas_prueba.py — crea (o deja como nuevas) cuentas de prueba para probar el panel de superadministrador.
SOLO STAGING. Se corre desde la Mac, con las variables del servicio Postgres de staging (sin railway ssh):

    railway run --environment staging --service Postgres -- venv/bin/python scripts/staging_cuentas_prueba.py

Candados (si falla cualquiera, no escribe nada):
  1. RAILWAY_ENVIRONMENT_NAME y RAILWAY_ENVIRONMENT tienen que ser "staging" (los pone Railway en `railway run`).
  2. Si existe ENTORNO, también tiene que ser "staging".
  3. La base NO puede tener una sesión con el número de production (593969829845).
  4. La base ya tiene que tener v1.1.1 (tabla auditoria_admin): primero se despliega, después se corre esto.

Qué crea (ids fijos, correos @enviafast.test —dominio reservado, ningún correo llega a nadie— y nombre "[PRUEBA] …"):
una cuenta por situación, con sus usuarios, números falsos (instancias inexistentes, desconectados, sin token) y
mensajes ya enviados para los conteos. Ningún mensaje pendiente va a una sesión, así nada intenta enviar ni
consultar Evolution ni mandar correos. Correrlo otra vez devuelve las cuentas a su estado inicial (sus filas de
auditoría quedan: la auditoría no se borra).
La contraseña de los usuarios de prueba se pide al correrlo (no queda en el código).
"""
import asyncio
import getpass
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import bcrypt

NUMERO_PRODUCTION = "593969829845"
PREFIJO = "e57a0000-0000-4000-8000-"          # ids fijos de las cuentas de prueba


def _id(tipo: str, n: int) -> uuid.UUID:
    return uuid.UUID(f"{PREFIJO}{tipo}{n:011d}")


def _abortar(motivo: str) -> None:
    print(f"❌ No se hace nada: {motivo}")
    sys.exit(1)


def _verificar_entorno() -> str:
    for var in ("RAILWAY_ENVIRONMENT_NAME", "RAILWAY_ENVIRONMENT"):
        if os.getenv(var) != "staging":
            _abortar(f"{var} = {os.getenv(var)!r} (tiene que ser 'staging'). Córrelo con "
                     f"`railway run --environment staging --service Postgres -- …`.")
    if os.getenv("ENTORNO") not in (None, "staging"):
        _abortar(f"ENTORNO = {os.getenv('ENTORNO')!r}")
    url = os.getenv("DATABASE_PUBLIC_URL") or os.getenv("DATABASE_URL") or ""
    if not url:
        _abortar("no hay DATABASE_PUBLIC_URL (usa --service Postgres)")
    if ".railway.internal" in url:
        _abortar("la URL de la base es privada (railway.internal); usa --service Postgres para tener DATABASE_PUBLIC_URL")
    return url


AHORA = datetime.now(timezone.utc)
D = timedelta(days=1)
# (n, correo, estado, plan, prueba_fin, periodo_fin, números, enviados este mes, extra)
CUENTAS = [
    (1, "prueba-vence", "prueba", None, AHORA + 2 * D, None, 1, 12, {}),
    (2, "prueba-vencida", "prueba", None, AHORA - 1 * D, None, 1, 50, {"pendiente_sin_sesion": True}),
    (3, "pro-vence", "activa", "pro", None, AHORA + 5 * D, 1, 140, {}),
    (4, "team-gracia", "activa", "team", None, AHORA - 1 * D, 2, 310, {}),
    (5, "team-vencida", "activa", "team", None, AHORA - 5 * D, 2, 0, {}),
    (6, "cortesia", "cortesia", "enterprise", None, AHORA + 20 * D, 1, 75, {}),
    (7, "suspendida", "suspendida", "pro", None, AHORA + 10 * D, 1, 20,
     {"estado_antes": "activa", "motivo": "Prueba de staging: pago rechazado"}),
    (8, "team-3numeros", "activa", "team", None, AHORA + 15 * D, 3, 220, {}),
]


async def main() -> None:
    url = _verificar_entorno()
    conn = await asyncpg.connect(url, timeout=10)
    try:
        if await conn.fetchval("SELECT EXISTS (SELECT 1 FROM sesiones_whatsapp WHERE numero_telefono = $1)",
                               NUMERO_PRODUCTION):
            _abortar(f"la base tiene una sesión con el número de production ({NUMERO_PRODUCTION}): no es staging")
        if not await conn.fetchval("SELECT to_regclass('auditoria_admin') IS NOT NULL"):
            _abortar("la base no tiene v1.1.1 (falta auditoria_admin): despliega primero a staging")

        clave = os.getenv("CLAVE_PRUEBA") or getpass.getpass("Contraseña para los usuarios de prueba (mín. 10): ")
        if len(clave) < 10:
            _abortar("la contraseña debe tener al menos 10 caracteres")
        hash_ = bcrypt.hashpw(clave.encode(), bcrypt.gensalt()).decode()

        async with conn.transaction():
            for n, nombre, estado, plan, prueba_fin, periodo_fin, numeros, enviados, extra in CUENTAS:
                cuenta, usuario, email = _id("c", n), _id("a", n), f"{nombre}@enviafast.test"
                await conn.execute("""
                    INSERT INTO cuentas (id_cuenta, nombre, plan, estado, prueba_fin, periodo_fin, motivo_suspension,
                                         estado_antes_suspension, creada_en)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    ON CONFLICT (id_cuenta) DO UPDATE SET nombre = EXCLUDED.nombre, plan = EXCLUDED.plan,
                        estado = EXCLUDED.estado, prueba_fin = EXCLUDED.prueba_fin, periodo_fin = EXCLUDED.periodo_fin,
                        motivo_suspension = EXCLUDED.motivo_suspension,
                        estado_antes_suspension = EXCLUDED.estado_antes_suspension""",
                    cuenta, f"[PRUEBA] {nombre}", plan, estado, prueba_fin, periodo_fin, extra.get("motivo"),
                    extra.get("estado_antes"), AHORA - (20 - n) * D)
                await conn.execute("""
                    INSERT INTO usuarios (id_usuario, id_cuenta, email, password_hash, rol, email_verificado)
                    VALUES ($1, $2, $3, $4, 'admin', TRUE)
                    ON CONFLICT (id_usuario) DO UPDATE SET password_hash = EXCLUDED.password_hash,
                        email_verificado = TRUE, version_token = usuarios.version_token + 1""",
                    usuario, cuenta, email, hash_)
                # Se rehacen sus números y mensajes (los mensajes primero: la FK de sesión los apuntaría a NULL)
                await conn.execute("DELETE FROM mensajes_programados WHERE id_cuenta = $1", cuenta)
                await conn.execute("DELETE FROM sesiones_whatsapp WHERE id_cuenta = $1", cuenta)
                sesiones = []
                for i in range(numeros):
                    sid = _id("5", n * 10 + i)
                    sesiones.append(sid)
                    await conn.execute("""
                        INSERT INTO sesiones_whatsapp (id_sesion, id_usuario, id_cuenta, nombre_sesion, numero_telefono,
                            instancia_evolution, token_autorizacion, estado_conexion, fecha_ultima_conexion, desconectada_en,
                            aviso_desconexion_en)
                        VALUES ($1, $2, $3, '[PRUEBA]', $4, $5, NULL, 'desconectado', $6, $6, $6)""",
                        sid, usuario, cuenta, f"5939000{n:02d}{i:03d}", f"prueba-staging-{n}-{i}", AHORA - D)
                inicio_mes = AHORA.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
                for k in range(enviados):
                    cuando = min(inicio_mes + timedelta(minutes=17 * k), AHORA - timedelta(minutes=5))
                    await conn.execute("""
                        INSERT INTO mensajes_programados (id_usuario, id_cuenta, id_sesion, id_grupo, texto_mensaje,
                            fecha_hora_disparo, estado_envio, enviado_en)
                        VALUES ($1, $2, $3, '120000000000000000@g.us', '[prueba staging]', $4, 'enviado', $4)""",
                        usuario, cuenta, sesiones[0] if sesiones else None, cuando)
                if extra.get("pendiente_sin_sesion"):
                    # Sin sesión: a su hora el programador lo pasa a "vencido" por plan vencido (nada se envía)
                    await conn.execute("""
                        INSERT INTO mensajes_programados (id_usuario, id_cuenta, id_sesion, id_grupo, texto_mensaje,
                            fecha_hora_disparo, estado_envio)
                        VALUES ($1, $2, NULL, '120000000000000000@g.us', '[prueba staging] pendiente', $3, 'programado')""",
                        usuario, cuenta, AHORA + timedelta(minutes=10))
        print("✅ Cuentas de prueba listas en STAGING:")
        for n, nombre, estado, plan, *_ in CUENTAS:
            print(f"   {nombre}@enviafast.test  ·  {estado}{' · ' + plan if plan else ''}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
