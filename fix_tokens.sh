#!/bin/bash
# Actualiza los apikeys de las instancias existentes con los tokens reales de Evolution
# Ejecutar desde: backend/

echo "=== Obteniendo tokens de Evolution API ==="
TOKENS=$(curl -s -H "apikey: mi-clave-secreta-123" http://localhost:8080/instance/fetchInstances)
echo "$TOKENS" | python3 -c "
import sys, json

data = json.load(sys.stdin)
print('Instancias encontradas:')
updates = []
for item in data:
    inst = item.get('instance', {})
    name = inst.get('instanceName', '')
    apikey = inst.get('apikey', '')
    status = inst.get('status', '')
    print(f'  {name}  |  apikey={apikey}  |  status={status}')
    if name and apikey:
        updates.append((name, apikey))

print()
print('=== Generando SQL de actualización ===')
for name, apikey in updates:
    sql = f\"UPDATE sesiones_whatsapp SET token_autorizacion = '{apikey}' WHERE instancia_evolution = '{name}';\"
    print(sql)
" 

echo ""
echo "=== Aplicando actualizaciones a PostgreSQL ==="
curl -s -H "apikey: mi-clave-secreta-123" http://localhost:8080/instance/fetchInstances | python3 -c "
import sys, json, subprocess

data = json.load(sys.stdin)
for item in data:
    inst = item.get('instance', {})
    name = inst.get('instanceName', '')
    apikey = inst.get('apikey', '')
    if name and apikey:
        sql = f\"UPDATE sesiones_whatsapp SET token_autorizacion = '{apikey}' WHERE instancia_evolution = '{name}';\"
        result = subprocess.run(
            ['psql', '-U', 'macbook', '-d', 'whatsapp_scheduler', '-c', sql],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f'✅ {name} → token actualizado')
        else:
            print(f'❌ {name} → error: {result.stderr.strip()}')
"

echo ""
echo "=== Verificación final ==="
psql -U macbook -d whatsapp_scheduler -c "
SELECT instancia_evolution, 
       LEFT(COALESCE(token_autorizacion, '(NULL)'), 12) || '...' as token_inicio
FROM sesiones_whatsapp 
ORDER BY fecha_creacion;
"
