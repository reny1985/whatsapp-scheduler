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
    and_,
    func,
    or_,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# Cuenta interna (el dueño de Enviafast): la migración pasa a ella al usuario piloto y todos sus datos.
CUENTA_INTERNA_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class Cuenta(Base):
    """El cliente. Usuarios, sesiones, mensajes y plantillas pertenecen a una cuenta; nada se comparte entre cuentas."""
    __tablename__ = "cuentas"
    __mapper_args__ = {"eager_defaults": True}

    id_cuenta: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    nombre: Mapped[str | None] = mapped_column(String(150))
    plan: Mapped[str | None] = mapped_column(String(20))           # pro | team | enterprise (NULL en prueba)
    estado: Mapped[str] = mapped_column(String(20), nullable=False, server_default=text("'prueba'"))
    prueba_fin: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    periodo_fin: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))   # NULL = sin vencimiento
    mensajes_prueba_usados: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
    es_interna: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    motivo_suspension: Mapped[str | None] = mapped_column(Text)
    creada_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return f"<Cuenta {self.id_cuenta} [{self.estado}]>"


class Usuario(Base):
    __tablename__ = "usuarios"
    __mapper_args__ = {"eager_defaults": True}

    id_usuario: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    id_cuenta: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cuentas.id_cuenta", name="usuarios_id_cuenta_fkey"), nullable=False,
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
    # Dueño de Enviafast (panel de superadministrador). Se lee de la BD en cada request, nunca del JWT.
    es_superadmin: Mapped[bool] = mapped_column(nullable=False, server_default=text("FALSE"))
    # Sube al cambiar la contraseña: invalida todos los JWT emitidos antes (cierra las sesiones abiertas).
    version_token: Mapped[int] = mapped_column(nullable=False, server_default=text("0"))
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
    __table_args__ = (Index("idx_sesiones_usuario", "id_usuario"), Index("idx_sesiones_cuenta", "id_cuenta"))

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
    id_cuenta: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cuentas.id_cuenta", name="sesiones_whatsapp_id_cuenta_fkey"), nullable=False,
    )
    nombre_sesion: Mapped[str | None] = mapped_column(String(100))
    numero_telefono: Mapped[str | None] = mapped_column(String(30))
    perfil_nombre: Mapped[str | None] = mapped_column(String(255))
    perfil_foto_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    perfil_consultado_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    aviso_desconexion_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    desconectada_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    instancia_evolution: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False
    )
    token_autorizacion: Mapped[str | None] = mapped_column(String(255))
    # Evolution envía los eventos a /webhook/evolution/<secreto_webhook>: nadie puede falsificar eventos de otra cuenta
    secreto_webhook: Mapped[str | None] = mapped_column(String(64), unique=True)
    # Cuándo se comprobó en Evolution que la instancia ya usa la URL con secreto (desde entonces la ruta vieja la ignora)
    webhook_confirmado_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
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
        cascade="save-update, merge",  # borrar la sesión NO borra mensajes (FK SET NULL)
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
        Index("idx_mensajes_usuario_estado", "id_usuario", "estado_envio", "fecha_hora_disparo"),
        Index("idx_mensajes_cuenta_estado", "id_cuenta", "estado_envio", "fecha_hora_disparo"),
    )

    id_mensaje: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=text("uuid_generate_v4()"),
    )
    id_sesion: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sesiones_whatsapp.id_sesion", ondelete="SET NULL", name="fk_sesion"),
        nullable=True,
    )
    # Dueño del mensaje: independiente de la sesión (si la sesión se borra, el mensaje sigue siendo del usuario)
    id_usuario: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("usuarios.id_usuario", ondelete="CASCADE", name="mensajes_programados_id_usuario_fkey"),
        nullable=False,
    )
    # Cuenta dueña: todo filtro de mensajes va por aquí. La BD exige que coincida con la del autor y la de la sesión.
    id_cuenta: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cuentas.id_cuenta", name="mensajes_programados_id_cuenta_fkey"), nullable=False,
    )
    numero_origen: Mapped[str | None] = mapped_column(String(30))   # número con el que se programó
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
    error_envio: Mapped[str | None] = mapped_column(Text)   # motivo del fallo o del vencimiento
    tomado_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    enviado_en: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    fecha_creacion: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), server_default=func.now()
    )
    sesion: Mapped["SesionWhatsApp | None"] = relationship(
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
    id_cuenta: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cuentas.id_cuenta", name="plantillas_id_cuenta_fkey"), nullable=False,
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


def condicion_sesion_nunca_conectada():
    """Sesión que jamás tuvo número ni conexión. Nunca incluye una sesión que alguna vez conectó."""
    return and_(
        or_(SesionWhatsApp.numero_telefono.is_(None), SesionWhatsApp.numero_telefono == ""),
        SesionWhatsApp.fecha_ultima_conexion.is_(None),
    )
