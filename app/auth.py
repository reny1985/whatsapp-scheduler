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

from app.cuentas import ENCABEZADO_SUSPENDIDA, MSG_SUSPENDIDA, estado_efectivo
from app.database import get_db
from app.models import Cuenta, Usuario

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


def create_access_token(user_id: uuid.UUID, rol: str, version: int = 0) -> str:
    """`version` = usuarios.version_token al emitirlo: si después sube (cambio de contraseña), el token deja de valer."""
    expire = datetime.now(timezone.utc) + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    return jwt.encode(
        {"sub": str(user_id), "rol": rol, "ver": version, "exp": expire},
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
        uid = uuid.UUID(user_id)
    except (JWTError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido o expirado",
        )
    user = await db.get(Usuario, uid)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Usuario no encontrado",
        )
    # Los tokens emitidos antes de v1.1.0 no traen "ver": valen como versión 0 (nadie pierde su sesión al desplegar)
    if payload.get("ver", 0) != user.version_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Tu sesión se cerró. Vuelve a iniciar sesión.",
        )
    # Cuenta suspendida: ninguna petición pasa, aunque tenga un token abierto
    cuenta = await db.get(Cuenta, user.id_cuenta)
    if cuenta is None or estado_efectivo(cuenta).estado == "suspendida":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=MSG_SUSPENDIDA,
            headers=ENCABEZADO_SUSPENDIDA,
        )
    return user


async def superadmin_o_none(access_token: str | None, db: AsyncSession) -> Usuario | None:
    """El usuario si es el superadministrador; si no (sin login, token inválido, usuario normal), None.
    es_superadmin se lee de la BD en esta misma petición (nunca del JWT) y además su cuenta tiene que ser la interna."""
    try:
        user = await get_current_user(access_token, db)
    except HTTPException:
        return None
    if not user.es_superadmin:
        return None
    cuenta = await db.get(Cuenta, user.id_cuenta)
    return user if cuenta is not None and cuenta.es_interna else None


async def require_superadmin(
    access_token: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_db),
) -> Usuario:
    """Para cualquier otro usuario (o sin login) responde igual que una ruta que no existe."""
    user = await superadmin_o_none(access_token, db)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not Found")
    return user


async def cerrar_todas_las_sesiones(db: AsyncSession, user: Usuario) -> None:
    """Invalida todos los JWT del usuario (los nuevos se emiten con la versión nueva). No hace commit."""
    user.version_token = (user.version_token or 0) + 1


async def require_admin(user: Usuario = Depends(get_current_user)) -> Usuario:
    if user.rol != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Se requiere rol admin",
        )
    return user
