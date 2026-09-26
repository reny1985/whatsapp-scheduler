"""
auth.py
Utilidades de autenticación: hash bcrypt, JWT HS256, dependencias FastAPI.
"""
import os
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import Cookie, Depends, HTTPException, status
from jose import JWTError, jwt
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Usuario

SECRET_KEY: str = os.getenv("SECRET_KEY", "dev-secret-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 8


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False


def create_access_token(user_id: uuid.UUID, rol: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "rol": rol, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


async def get_current_user(
    access_token: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No autenticado",
        )
    try:
        payload = jwt.decode(access_token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise JWTError()
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
        )
    user = await db.get(Usuario, uuid.UUID(user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado",
        )
    return user


async def require_admin(user: Usuario = Depends(get_current_user)) -> Usuario:
    if user.rol != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol admin",
        )
    return user
