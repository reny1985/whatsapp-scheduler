"""
main.py
Punto de entrada de la API + servidor del frontend HTML.
Ejecutar: PYTHONPATH=app uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path


from fastapi import Depends, FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import superadmin_o_none
from app.database import engine, get_db
from app.migrate import run_migrations
from app.routers import admin, auth, dashboard, media, mensajes, plantillas, sesiones, webhook
from app.scheduler import crear_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("whatsapp_scheduler")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(BASE_DIR / "static" / "uploads")))


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    logger.info("✅ Conexión a PostgreSQL verificada")
    await run_migrations(engine)
    scheduler = crear_scheduler()
    scheduler.start()
    logger.info("✅ Scheduler iniciado (job cada 60s)")
    yield
    scheduler.shutdown(wait=False)
    await engine.dispose()
    logger.info("Pool de conexiones cerrado")


app = FastAPI(
    title="WhatsApp Scheduler API",
    version="0.4.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = BASE_DIR / "static"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
if static_dir.exists():
    app.mount("/static/css", StaticFiles(directory=str(static_dir / "css")), name="css")
    app.mount("/static/js", StaticFiles(directory=str(static_dir / "js")), name="js")
    if (static_dir / "vendor").exists():
        app.mount("/static/vendor", StaticFiles(directory=str(static_dir / "vendor")), name="vendor")


class _UploadsStaticFiles(StaticFiles):
    """Sirve los adjuntos, pero NO la carpeta avatars/: las fotos de perfil solo salen por
    GET /sesiones/{id}/foto, que exige login."""

    async def get_response(self, path, scope):
        if path == "avatars" or path.startswith("avatars/"):
            raise StarletteHTTPException(status_code=404)
        return await super().get_response(path, scope)


app.mount("/static/uploads", _UploadsStaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


@app.middleware("http")
async def candado_admin(request: Request, call_next):
    """Primer candado del panel: todo /admin se comprueba ANTES del enrutador. Para quien no es el superadministrador
    responde exactamente como una ruta inexistente (así ni un 405, 400 o 422 delata que el panel existe).
    El segundo candado es la dependencia require_superadmin del router."""
    ruta = request.url.path
    if ruta == "/admin" or ruta.startswith("/admin/"):
        proveedor = app.dependency_overrides.get(get_db, get_db)
        sesiones_db = proveedor()
        db = await sesiones_db.__anext__()
        try:
            permitido = await superadmin_o_none(request.cookies.get("access_token"), db) is not None
        finally:
            await sesiones_db.aclose()
        if not permitido:
            logger.info("Acceso a /admin rechazado (%s %s)", request.method, ruta[:80])
            return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
    return await call_next(request)


app.include_router(admin.router)
app.include_router(auth.router)
app.include_router(sesiones.router)
app.include_router(mensajes.router)
app.include_router(dashboard.router)
app.include_router(plantillas.router)
app.include_router(media.router)
app.include_router(webhook.router)


@app.get("/health", tags=["infra"])
async def health(db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "ok", "version": "0.3.0"}
    except Exception as exc:
        logger.error("Health check fallido: %s", exc)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "degraded", "database": "unreachable"},
        )


def _version_estaticos() -> str:
    """Cambia en cada despliegue (fecha de los archivos): evita que el navegador use un app.js/app.css viejo."""
    archivos = [BASE_DIR / "static" / "js" / "app.js", BASE_DIR / "static" / "css" / "app.css"]
    return str(int(max((p.stat().st_mtime for p in archivos if p.exists()), default=0)))


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def frontend():
    html = (BASE_DIR / "templates" / "index.html").read_text(encoding="utf-8")
    v = _version_estaticos()
    for ruta in ("/static/js/app.js", "/static/css/app.css"):
        html = html.replace(f'"{ruta}"', f'"{ruta}?v={v}"')
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
