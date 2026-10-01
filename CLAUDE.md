# CLAUDE.md — Reglas permanentes para WhatsApp Scheduler

## Reglas de trabajo

1. **Antes de cualquier cambio, haz commit del estado actual.**
   ```bash
   cd /Users/macbook/Desktop/SaaS_WhatsApp/backend
   git add -A && git commit -m "WIP: estado antes de <tarea>"
   ```

2. **Modifica solo los archivos necesarios para la tarea pedida.**
   No reescribas funciones que no tengan relación con el cambio solicitado.

3. **Después de cada cambio, ejecuta las pruebas automáticas.**
   ```bash
   cd /Users/macbook/Desktop/SaaS_WhatsApp/backend
   python -m pytest tests/ -v
   ```
   Si alguna prueba falla, no hagas commit ni deploy.

4. **Al terminar, haz commit describiendo el cambio.**
   ```bash
   git add -A && git commit -m "<descripción concisa del cambio>"
   git push origin main
   ```

5. **Deploy solo desde el directorio `backend/`.**
   ```bash
   cd /Users/macbook/Desktop/SaaS_WhatsApp/backend && railway up --detach
   ```

---

## Stack y entorno

- Backend: FastAPI + PostgreSQL (asyncpg)
- Evolution API v1.8.7 en Hetzner: `http://159.69.248.218` (puerto 80, nginx proxy)
- Deploy: Railway (`thorough-adventure`, proyecto `9d304e4c-...`)
- URL producción: `https://enviafast.net`
- Deploy siempre desde `backend/`, nunca desde la raíz

---

## Flujos críticos que NO deben romperse

### 1. Grupos de una sesión conectada
- `GET /sesiones/{id}/grupos` devuelve lista no vacía para sesión conectada
- Si caché vacía: fetch sincrónico (no background) para no devolver []
- Si caché con datos: devuelve inmediato + refresco en background

### 2. Programar mensaje — grupo y número personal
- `POST /mensajes/` con `id_grupo` terminado en `@g.us` (grupo)
- `POST /mensajes/` con `id_grupo` terminado en `@s.whatsapp.net` (personal)
- Estado inicial: `programado`

### 3. Recurrencia de mensajes
- `recurrencia: "daily" | "weekly" | "monthly" | "none"`
- Scheduler clona el mensaje con la próxima fecha tras envío exitoso

### 4. Normalización de números (pairing code)
- Ecuador `0969829845` → `593969829845`
- Ecuador `969829845` → `593969829845`
- Ecuador `096 982 9845` → `593969829845`
- Ecuador `+593969829845` → `593969829845`
- Ecuador `+593 0969829845` → `593969829845` (autocomplete móvil)
- Colombia `3001234567` → `573001234567`
- México `5512345678` → `525512345678`

### 5. Generar código de vinculación (pairing code)
- `POST /sesiones/iniciar-pairing` con `{"numero": "593969829845"}`
- Retorna `{id_sesion, pairing_code}` en formato `XXXX-XXXX`

### 6. Estado de mensajes
- Ciclo: `programado` → `enviando` → `enviado` | `fallido`
- Solo mensajes en estado `programado` pueden cancelarse o editarse

---

## Pruebas automáticas — ejecutar con `pytest tests/ -v`

Las pruebas están en `tests/`. Para correr una prueba específica:
```bash
python -m pytest tests/test_grupos.py -v
python -m pytest tests/test_mensajes.py -v
python -m pytest tests/test_normalizar.py -v
python -m pytest tests/test_pairing.py -v
```

---

## Advertencias críticas

- **SQL asyncpg**: nunca usar `::json` con named params (`:data::json` falla). Usar `CAST(:data AS json)`.
- **Evolution API**: siempre pasar token explícito por instancia, nunca solo el global key.
- **Railway**: bloquea TCP saliente al puerto 8080. EVOLUTION_API_URL debe ser `http://159.69.248.218` (sin puerto).
- **intl-tel-input**: NO usar `loadUtilsOnInit` si `utils.js` ya está como script estático. NO usar `separateDialCode: true` si se necesita aceptar el prefijo nacional (0 inicial).
- **Deploy correcto**: `cd backend && railway up`. Desde la raíz falla (no detecta Python).
