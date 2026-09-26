-- Habilitar la extensión para generar UUIDs nativos en PostgreSQL
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ==========================================
-- 1. TABLA: USUARIOS
-- ==========================================
CREATE TABLE usuarios (
    id_usuario UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    estado_suscripcion VARCHAR(50) NOT NULL DEFAULT 'prueba',
    fecha_registro TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- ==========================================
-- 2. TABLA: SESIONES_WHATSAPP
-- ==========================================
CREATE TABLE sesiones_whatsapp (
    id_sesion UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    id_usuario UUID NOT NULL,
    instancia_evolution VARCHAR(100) UNIQUE NOT NULL,
    token_autorizacion VARCHAR(255),
    estado_conexion VARCHAR(50) NOT NULL DEFAULT 'qr_pendiente',
    fecha_creacion TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    fecha_ultima_conexion TIMESTAMP WITH TIME ZONE,
    
    CONSTRAINT fk_usuario 
        FOREIGN KEY (id_usuario) 
        REFERENCES usuarios(id_usuario) 
        ON DELETE CASCADE
);

-- ==========================================
-- 3. TABLA: MENSAJES_PROGRAMADOS
-- ==========================================
CREATE TABLE mensajes_programados (
    id_mensaje UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    id_sesion UUID NOT NULL,
    id_grupo VARCHAR(255) NOT NULL,
    texto_mensaje TEXT,
    tipo_media VARCHAR(50), 
    url_media TEXT,         
    fecha_hora_disparo TIMESTAMP WITH TIME ZONE NOT NULL,
    estado_envio VARCHAR(50) NOT NULL DEFAULT 'programado',
    fecha_creacion TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT fk_sesion 
        FOREIGN KEY (id_sesion) 
        REFERENCES sesiones_whatsapp(id_sesion) 
        ON DELETE CASCADE
);

-- ==========================================
-- ÍNDICES DE RENDIMIENTO 
-- ==========================================
CREATE INDEX idx_mensajes_fecha_estado 
ON mensajes_programados (fecha_hora_disparo, estado_envio);

CREATE INDEX idx_sesiones_usuario 
ON sesiones_whatsapp (id_usuario);
