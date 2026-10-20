#!/usr/bin/env bash
# ensayo_migracion.sh — ensaya la migración de v1.1.0 sobre una COPIA local de un respaldo de production.
#
#   scripts/ensayo_migracion.sh <respaldo.sql>
#
# 1) restaura el respaldo en la base local enviafast_ensayo (la crea de cero; nunca toca otra base)
# 2) foto ANTES (conteo + hash por tabla, solo columnas de v1.0.8)
# 3) corre la migración de v1.1.0 DOS veces → foto DESPUÉS y verificaciones (cero filas sin id_cuenta, etc.)
# 4) rollback_v1.1.0.sql → arranque de v1.0.8 (sus migraciones, desde la etiqueta en git) → vuelve a subir a v1.1.0
# 5) borra la base enviafast_ensayo
# No imprime claves: la conexión local no lleva contraseña y el respaldo no se muestra.
set -euo pipefail

RESPALDO="${1:?Uso: $0 <respaldo.sql>}"
BASE="enviafast_ensayo"
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@17/bin}"
LOCAL="${ENSAYO_PG_URL:-postgresql://$(whoami)@localhost:5432}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/venv/bin/python"

psql_ensayo() { "$PGBIN/psql" "$LOCAL/$BASE" -X -v ON_ERROR_STOP=1 -q "$@"; }
migrar_v110() {
  ( cd "$DIR" && "$PY" -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.migrate import run_migrations
async def main():
    e = create_async_engine('${LOCAL/postgresql:/postgresql+asyncpg:}/$BASE')
    await run_migrations(e); await e.dispose()
asyncio.run(main())" ) 2>&1 | grep -v "Migraciones aplicadas" || true
}
arrancar_v108() {
  ( cd "$DIR" && git show v1.0.8:app/migrate.py > "${TMPDIR:-/tmp}/migrate_v108.py" && "$PY" -c "
import asyncio, runpy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
pasos = runpy.run_path('${TMPDIR:-/tmp}/migrate_v108.py')['_STEPS']
async def main():
    e = create_async_engine('${LOCAL/postgresql:/postgresql+asyncpg:}/$BASE')
    async with e.begin() as c:
        for p in pasos: await c.execute(text(p.strip()))
    await e.dispose()
asyncio.run(main())
print('v1.0.8 arrancó sin errores sobre la base revertida')" && rm -f "${TMPDIR:-/tmp}/migrate_v108.py" )
}
foto() { psql_ensayo -A -F ' | ' -f "$DIR/scripts/foto_migracion.sql"; }

echo "== 1. Restaurar $(basename "$RESPALDO") en $BASE (local) =="
"$PGBIN/psql" "$LOCAL/postgres" -X -q -c "DROP DATABASE IF EXISTS $BASE WITH (FORCE)" -c "CREATE DATABASE $BASE"
psql_ensayo -f "$RESPALDO" > /dev/null

echo; echo "== 2. Foto ANTES =="
foto | tee "${TMPDIR:-/tmp}/foto_antes.txt"

echo; echo "== 3. Migración v1.1.0 (dos veces) =="
migrar_v110; migrar_v110
echo "Foto DESPUÉS:"
foto | tee "${TMPDIR:-/tmp}/foto_despues.txt"
if diff -q "${TMPDIR:-/tmp}/foto_antes.txt" "${TMPDIR:-/tmp}/foto_despues.txt" > /dev/null; then
  echo "✅ Foto idéntica: ningún dato cambió ni se perdió"
else
  echo "❌ LA FOTO CAMBIÓ"; diff "${TMPDIR:-/tmp}/foto_antes.txt" "${TMPDIR:-/tmp}/foto_despues.txt" || true; exit 1
fi
echo "Verificaciones:"
psql_ensayo -A -F ' | ' <<'SQL'
SELECT 'filas sin id_cuenta (4 tablas)', (SELECT count(*) FROM usuarios WHERE id_cuenta IS NULL)
     + (SELECT count(*) FROM sesiones_whatsapp WHERE id_cuenta IS NULL)
     + (SELECT count(*) FROM mensajes_programados WHERE id_cuenta IS NULL)
     + (SELECT count(*) FROM plantillas WHERE id_cuenta IS NULL);
SELECT 'filas en otra cuenta que …0001', (SELECT count(*) FROM usuarios WHERE id_cuenta <> '00000000-0000-0000-0000-000000000001')
     + (SELECT count(*) FROM sesiones_whatsapp WHERE id_cuenta <> '00000000-0000-0000-0000-000000000001')
     + (SELECT count(*) FROM mensajes_programados WHERE id_cuenta <> '00000000-0000-0000-0000-000000000001')
     + (SELECT count(*) FROM plantillas WHERE id_cuenta <> '00000000-0000-0000-0000-000000000001');
SELECT 'cuentas', string_agg(concat_ws(' ', id_cuenta, plan, estado, coalesce(periodo_fin::text, 'sin vencimiento'),
                                       CASE WHEN es_interna THEN 'interna' END), '; ') FROM cuentas;
SELECT 'superadministradores', string_agg(id_usuario::text, ', ') FROM usuarios WHERE es_superadmin;
SELECT 'id_cuenta NOT NULL en', string_agg(table_name, ', ' ORDER BY table_name) FROM information_schema.columns
 WHERE column_name = 'id_cuenta' AND is_nullable = 'NO';
SELECT 'candados', string_agg(conname, ', ' ORDER BY conname) FROM pg_constraint
 WHERE conname IN ('fk_mensaje_sesion_misma_cuenta', 'uq_sesion_cuenta');
SELECT 'triggers', string_agg(tgname, ', ' ORDER BY tgname) FROM pg_trigger WHERE tgname LIKE 'trg_cuenta_%';
SELECT 'sesiones con secreto de 64', count(*) FILTER (WHERE length(secreto_webhook) = 64) || ' de ' || count(*) FROM sesiones_whatsapp;
SQL

echo; echo "== 4. Rollback → arranque de v1.0.8 → volver a v1.1.0 =="
psql_ensayo -f "$DIR/scripts/rollback_v1.1.0.sql"
echo "rollback_v1.1.0.sql aplicado"
arrancar_v108
migrar_v110
foto > "${TMPDIR:-/tmp}/foto_resubida.txt"
if diff -q "${TMPDIR:-/tmp}/foto_antes.txt" "${TMPDIR:-/tmp}/foto_resubida.txt" > /dev/null; then
  echo "✅ Tras rollback y nueva subida la foto sigue idéntica"
else
  echo "❌ La foto cambió tras el rollback"; exit 1
fi
psql_ensayo -A -c "SELECT 'id_cuenta NOT NULL de nuevo en ' || count(*) || ' tablas' FROM information_schema.columns WHERE column_name='id_cuenta' AND is_nullable='NO'"

echo; echo "== 5. Borrar $BASE =="
"$PGBIN/psql" "$LOCAL/postgres" -X -q -c "DROP DATABASE $BASE WITH (FORCE)"
rm -f "${TMPDIR:-/tmp}/foto_antes.txt" "${TMPDIR:-/tmp}/foto_despues.txt" "${TMPDIR:-/tmp}/foto_resubida.txt"
echo "Base de ensayo borrada."
