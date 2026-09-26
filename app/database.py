"""
app/database.py
Motor asíncrono (asyncpg) y fabrica de sesiones para SQLAlchemy 2.0.
"""
import os
from collections.abc import AsyncGenerator
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

# Railway provee DATABASE_URL como "postgres://" o "postgresql://".
# asyncpg necesita "postgresql+asyncpg://".
_raw_url: str = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_scheduler",
)
DATABASE_URL: str = (
    _raw_url
    .replace("postgres://", "postgresql+asyncpg://", 1)
    .replace("postgresql://", "postgresql+asyncpg://", 1)
)
DB_POOL_SIZE: int = int(os.getenv("DB_POOL_SIZE", "10"))
DB_MAX_OVERFLOW: int = int(os.getenv("DB_MAX_OVERFLOW", "20"))
DB_ECHO: bool = os.getenv("DB_ECHO", "false").lower() == "true"

engine: AsyncEngine = create_async_engine(
    DATABASE_URL,
    echo=DB_ECHO,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=1800,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
