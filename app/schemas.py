"""
schemas.py
Esquemas Pydantic v2 (Create / Read).
Los estados se validan aquí con Literal porque el SQL no tiene CHECK constraints.
"""
import uuid
from datetime import datetime, timezone
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

EstadoSuscripcion = Literal["prueba", "activa", "suspendida", "cancelada"]
EstadoConexion = Literal["qr_pendiente", "conectado", "desconectado"]
EstadoEnvio = Literal["programado", "enviando", "enviado", "fallido", "cancelado"]
TipoMedia = Literal["image", "video", "audio", "document"]
Recurrencia = Literal["none", "daily", "weekly", "monthly"]


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class UsuarioCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class UsuarioRead(ORMBase):
    id_usuario: uuid.UUID
    email: EmailStr
    estado_suscripcion: EstadoSuscripcion
    fecha_registro: datetime


# UUID fijo para el modo piloto (sin autenticación)
PILOTO_ID_USUARIO = uuid.UUID("00000000-0000-0000-0000-000000000001")


class SesionWhatsAppCreate(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id_usuario: uuid.UUID = Field(default=PILOTO_ID_USUARIO)
    nombre_sesion: str | None = Field(default=None, max_length=100)
    instancia_evolution: str = Field(
        min_length=3, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$"
    )
    token_autorizacion: str | None = Field(default=None, max_length=255)


class SesionWhatsAppRead(ORMBase):
    id_sesion: uuid.UUID
    id_usuario: uuid.UUID
    nombre_sesion: str | None
    instancia_evolution: str
    estado_conexion: EstadoConexion
    fecha_creacion: datetime
    fecha_ultima_conexion: datetime | None


class MensajeProgramadoCreate(BaseModel):
    id_sesion: uuid.UUID
    id_grupo: str = Field(
        max_length=255,
        description="JID del destino: grupo (@g.us) o número personal (@s.whatsapp.net o solo dígitos)",
    )
    texto_mensaje: str | None = Field(default=None, max_length=4096)
    tipo_media: TipoMedia | None = None
    url_media: str | None = Field(default=None, max_length=2048)
    fecha_hora_disparo: AwareDatetime
    recurrencia: Recurrencia = "none"

    @field_validator("id_grupo")
    @classmethod
    def normalizar_destino(cls, v: str) -> str:
        v = v.strip()
        if "@g.us" in v or "@s.whatsapp.net" in v:
            return v
        if v.replace("+", "").replace("-", "").replace(" ", "").isdigit():
            digits = "".join(c for c in v if c.isdigit())
            return f"{digits}@s.whatsapp.net"
        raise ValueError(
            "Destino inválido. Use: número@g.us (grupo), número@s.whatsapp.net (personal), o solo el número de teléfono"
        )

    @field_validator("texto_mensaje")
    @classmethod
    def normalizar_texto(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("fecha_hora_disparo")
    @classmethod
    def validar_fecha_futura(cls, v: datetime) -> datetime:
        if v <= datetime.now(timezone.utc):
            raise ValueError("fecha_hora_disparo debe ser una fecha futura")
        return v

    @model_validator(mode="after")
    def validar_contenido(self) -> "MensajeProgramadoCreate":
        if (self.tipo_media is None) != (self.url_media is None):
            raise ValueError("tipo_media y url_media deben enviarse juntos")
        if self.texto_mensaje is None and self.url_media is None:
            raise ValueError("Se requiere texto_mensaje o un adjunto (url_media)")
        return self

    def to_orm_dict(self) -> dict:
        return self.model_dump(mode="json") | {
            "id_sesion": self.id_sesion,
            "fecha_hora_disparo": self.fecha_hora_disparo,
        }


class MensajeProgramadoUpdate(BaseModel):
    texto_mensaje: str | None = Field(default=None, max_length=4096)
    tipo_media: TipoMedia | None = None
    url_media: str | None = Field(default=None, max_length=2048)
    fecha_hora_disparo: AwareDatetime | None = None
    recurrencia: Recurrencia | None = None

    @model_validator(mode="after")
    def validar_media(self) -> "MensajeProgramadoUpdate":
        if (self.tipo_media is None) != (self.url_media is None):
            raise ValueError("tipo_media y url_media deben enviarse juntos o ambos null")
        return self


class PlantillaCreate(BaseModel):
    nombre: str = Field(min_length=1, max_length=100)
    texto_mensaje: str | None = Field(default=None, max_length=4096)
    tipo_media: TipoMedia | None = None
    url_media: str | None = Field(default=None, max_length=2048)

    @model_validator(mode="after")
    def validar_contenido(self) -> "PlantillaCreate":
        if (self.tipo_media is None) != (self.url_media is None):
            raise ValueError("tipo_media y url_media deben enviarse juntos")
        if not self.texto_mensaje and not self.url_media:
            raise ValueError("La plantilla necesita texto o adjunto")
        return self


class PlantillaRead(ORMBase):
    id_plantilla: uuid.UUID
    nombre: str
    texto_mensaje: str | None
    tipo_media: TipoMedia | None
    url_media: str | None
    fecha_creacion: datetime


class MensajeProgramadoRead(ORMBase):
    id_mensaje: uuid.UUID
    id_sesion: uuid.UUID
    id_grupo: str
    texto_mensaje: str | None
    tipo_media: TipoMedia | None
    url_media: str | None
    fecha_hora_disparo: datetime
    estado_envio: EstadoEnvio
    recurrencia: Recurrencia
    id_mensaje_origen: uuid.UUID | None
    fecha_creacion: datetime
