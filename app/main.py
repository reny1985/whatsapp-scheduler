"""
main.py
Punto de entrada de la API + servidor del frontend HTML.
Ejecutar: PYTHONPATH=app uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
"""
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path


from fastapi import Depends, FastAPI, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import engine, get_db
from app.migrate import run_migrations
from app.routers import auth, media, mensajes, plantillas, sesiones
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
app.mount("/static/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

app.include_router(auth.router)
app.include_router(sesiones.router)
app.include_router(mensajes.router)
app.include_router(plantillas.router)
app.include_router(media.router)


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


@app.get("/", response_class=FileResponse, include_in_schema=False)
async def frontend():
    index = BASE_DIR / "templates" / "index.html"
    return FileResponse(str(index))
