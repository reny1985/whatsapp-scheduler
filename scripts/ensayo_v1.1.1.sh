#!/usr/bin/env bash
# ensayo_v1.1.1.sh — ensaya la migración de v1.1.1 sobre una COPIA local de un respaldo de production.
#
#   scripts/ensayo_v1.1.1.sh <respaldo.sql>
#
# 1) restaura el respaldo en la base local enviafast_ensayo (la crea de cero; nunca toca otra base)
# 2) foto ANTES (conteo + hash por tabla de v1.1.0)
# 3) migración de v1.1.1 DOS veces → foto DESPUÉS idéntica + verificaciones (candados, tablas, triggers)
# 4) rollback_v1.1.1.sql → arranque de v1.1.0 (sus migraciones, desde la etiqueta en git) → vuelve a subir a v1.1.1
#    → foto idéntica y candados de nuevo puestos
# 5) borra la base enviafast_ensayo
# No imprime claves ni datos: la conexión local no lleva contraseña y la foto solo muestra conteos y hashes.
set -euo pipefail

RESPALDO="${1:?Uso: $0 <respaldo.sql>}"
BASE="enviafast_ensayo"
PGBIN="${PGBIN:-/opt/homebrew/opt/postgresql@18/bin}"
LOCAL="${ENSAYO_PG_URL:-postgresql://$(whoami)@localhost:5432}"
DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$DIR/venv/bin/python"
TMP="${TMPDIR:-/tmp}"

psql_ensayo() { "$PGBIN/psql" "$LOCAL/$BASE" -X -v ON_ERROR_STOP=1 -q "$@"; }
migrar_v111() {
  ( cd "$DIR" && "$PY" -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from app.migrate import run_migrations
async def main():
    e = create_async_engine('${LOCAL/postgresql:/postgresql+asyncpg:}/$BASE')
    await run_migrations(e); await e.dispose()
asyncio.run(main())" ) 2>&1 | grep -v "Migraciones aplicadas" || true
}
arrancar_v110() {
  ( cd "$DIR" && git show v1.1.0:app/migrate.py > "$TMP/migrate_v110.py" && "$PY" -c "
import asyncio, runpy
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
pasos = runpy.run_path('$TMP/migrate_v110.py')['_STEPS']
async def main():
    e = create_async_engine('${LOCAL/postgresql:/postgresql+asyncpg:}/$BASE')
    async with e.begin() as c:
        for p in pasos: await c.execute(text(p.strip()))
    await e.dispose()
asyncio.run(main())
print('v1.1.0 arrancó sin errores sobre la base revertida')" && rm -f "$TMP/migrate_v110.py" )
}
foto() { psql_ensayo -A -F ' | ' -f "$DIR/scripts/foto_v1.1.1.sql"; }
candados() {
  psql_ensayo -A -t -c "SELECT count(*) FROM pg_constraint WHERE conname IN ('ck_cuentas_estado', 'ck_cuentas_plan',
    'ck_cuentas_estado_antes', 'ck_cuentas_interna', 'ck_cuentas_con_vencimiento')"
}

echo "== 1. Restaurar $(basename "$RESPALDO") en $BASE (local) =="
"$PGBIN/psql" "$LOCAL/postgres" -X -q -c "DROP DATABASE IF EXISTS $BASE WITH (FORCE)" -c "CREATE DATABASE $BASE"
psql_ensayo -f "$RESPALDO" > /dev/null

echo; echo "== 2. Foto ANTES =="
foto | tee "$TMP/foto_antes_v111.txt"

echo; echo "== 3. Migración v1.1.1 (dos veces) =="
migrar_v111; migrar_v111
echo "Foto DESPUÉS:"
foto | tee "$TMP/foto_despues_v111.txt"
if diff -q "$TMP/foto_antes_v111.txt" "$TMP/foto_despues_v111.txt" > /dev/null; then
  echo "✅ Foto idéntica: ningún dato cambió ni se perdió"
else
  echo "❌ LA FOTO CAMBIÓ"; diff "$TMP/foto_antes_v111.txt" "$TMP/foto_despues_v111.txt" || true; exit 1
fi
echo "Verificaciones:"
psql_ensayo -A -F ' | ' <<'SQL'
SELECT 'candados de cuentas', string_agg(conname, ', ' ORDER BY conname) FROM pg_constraint WHERE conname LIKE 'ck_cuentas_%';
SELECT 'tablas nuevas', string_agg(table_name, ', ' ORDER BY table_name) FROM information_schema.tables
 WHERE table_name IN ('auditoria_admin', 'periodos_cuenta');
SELECT 'filas en auditoria_admin / periodos_cuenta', (SELECT count(*) FROM auditoria_admin) || ' / ' || (SELECT count(*) FROM periodos_cuenta);
SELECT 'triggers de la auditoría', string_agg(tgname, ', ' ORDER BY tgname) FROM pg_trigger WHERE tgname LIKE 'trg_auditoria_%';
SELECT 'columna estado_antes_suspension', count(*) FROM information_schema.columns
 WHERE table_name = 'cuentas' AND column_name = 'estado_antes_suspension';
SELECT 'cuentas', string_agg(concat_ws(' ', id_cuenta, plan, estado, coalesce(periodo_fin::text, 'sin vencimiento'),
                                       CASE WHEN es_interna THEN 'interna' END), '; ') FROM cuentas;
SELECT 'superadministradores', string_agg(id_usuario::text, ', ') FROM usuarios WHERE es_superadmin;
SQL
N="$(candados)"
if [ "$N" != "5" ]; then echo "❌ Solo se aplicaron $N de 5 candados (ver WARNING arriba)"; exit 1; fi
echo "✅ Los 5 candados de cuentas están puestos"
psql_ensayo -c "UPDATE cuentas SET estado = 'suspendida' WHERE es_interna" > /dev/null 2>&1 \
  && { echo "❌ La BD dejó suspender la cuenta interna"; exit 1; } || echo "✅ La BD no deja suspender la cuenta interna"

echo; echo "== 4. Rollback → arranque de v1.1.0 → volver a v1.1.1 =="
psql_ensayo -f "$DIR/scripts/rollback_v1.1.1.sql"
echo "rollback_v1.1.1.sql aplicado (candados después del rollback: $(candados))"
arrancar_v110
migrar_v111
foto > "$TMP/foto_resubida_v111.txt"
if diff -q "$TMP/foto_antes_v111.txt" "$TMP/foto_resubida_v111.txt" > /dev/null; then
  echo "✅ Tras rollback y nueva subida la foto sigue idéntica"
else
  echo "❌ La foto cambió tras el rollback"; exit 1
fi
N="$(candados)"
if [ "$N" != "5" ]; then echo "❌ Tras volver a subir solo hay $N de 5 candados"; exit 1; fi
echo "✅ Al volver a subir, los 5 candados están de nuevo"

echo; echo "== 5. Borrar $BASE =="
"$PGBIN/psql" "$LOCAL/postgres" -X -q -c "DROP DATABASE $BASE WITH (FORCE)"
rm -f "$TMP/foto_antes_v111.txt" "$TMP/foto_despues_v111.txt" "$TMP/foto_resubida_v111.txt"
echo "Base de ensayo borrada."
