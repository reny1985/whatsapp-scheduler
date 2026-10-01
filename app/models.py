"""
app/models.py
Modelos ORM (SQLAlchemy 2.0, estilo Mapped).
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    ForeignKey,
    Index,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Usuario(Base):
    __tablename__ = "usuarios"
    __mapper_args__ = {"eager_defaults": True}

    id_usuario: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    rol: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'admin'")
    )
    estado_suscripcion: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'prueba'")
    )
    email_verificado: Mapped[bool] = mapped_column(
        nullable=False, server_default=text("FALSE")
    )
    codigo_verificacion: Mapped[str | None] = mapped_column(String(10))
    codigo_expira_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    fecha_registro: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    sesiones: Mapped[list["SesionWhatsApp"]] = relationship(
        back_populates="usuario",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<Usuario {self.email}>"


class SesionWhatsApp(Base):
    __tablename__ = "sesiones_whatsapp"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (Index("idx_sesiones_usuario", "id_usuario"),)

    id_sesion: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    id_usuario: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usuarios.id_usuario", ondelete="CASCADE", name="fk_usuario"),
        nullable=False,
    )
    nombre_sesion: Mapped[str | None] = mapped_column(String(100))
    numero_telefono: Mapped[str | None] = mapped_column(String(30))
    instancia_evolution: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )
    token_autorizacion: Mapped[str | None] = mapped_column(String(255))
    estado_conexion: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'qr_pendiente'")
    )
    fecha_creacion: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    fecha_ultima_conexion: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    usuario: Mapped["Usuario"] = relationship(back_populates="sesiones", lazy="raise")
    mensajes: Mapped[list["MensajeProgramado"]] = relationship(
        back_populates="sesion",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="raise",
    )

    def __repr__(self) -> str:
        return f"<SesionWhatsApp {self.instancia_evolution} [{self.estado_conexion}]>"


class MensajeProgramado(Base):
    __tablename__ = "mensajes_programados"
    __mapper_args__ = {"eager_defaults": True}
    __table_args__ = (
        Index("idx_mensajes_fecha_estado", "fecha_hora_disparo", "estado_envio"),
    )

    id_mensaje: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    id_sesion: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sesiones_whatsapp.id_sesion", ondelete="CASCADE", name="fk_sesion"),
        nullable=False,
    )
    id_grupo: Mapped[str] = mapped_column(String(255), nullable=False)
    texto_mensaje: Mapped[str | None] = mapped_column(Text)
    tipo_media: Mapped[str | None] = mapped_column(String(50))
    url_media: Mapped[str | None] = mapped_column(Text)
    fecha_hora_disparo: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    estado_envio: Mapped[str] = mapped_column(
        String(50), nullable=False, server_default=text("'programado'")
    )
    recurrencia: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text("'none'")
    )
    id_mensaje_origen: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mensajes_programados.id_mensaje", ondelete="SET NULL", name="fk_origen"),
        nullable=True,
    )
    fecha_creacion: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    sesion: Mapped["SesionWhatsApp"] = relationship(
        back_populates="mensajes", lazy="raise"
    )

    def __repr__(self) -> str:
        return f"<MensajeProgramado {self.id_mensaje} -> {self.id_grupo} [{self.estado_envio}]>"


class Plantilla(Base):
    __tablename__ = "plantillas"
    __mapper_args__ = {"eager_defaults": True}

    id_plantilla: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    id_usuario: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usuarios.id_usuario", ondelete="CASCADE", name="fk_plantilla_usuario"),
        nullable=False,
    )
    nombre: Mapped[str] = mapped_column(String(100), nullable=False)
    texto_mensaje: Mapped[str | None] = mapped_column(Text)
    tipo_media: Mapped[str | None] = mapped_column(String(50))
    url_media: Mapped[str | None] = mapped_column(Text)
    fecha_creacion: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Plantilla {self.nombre}>"
