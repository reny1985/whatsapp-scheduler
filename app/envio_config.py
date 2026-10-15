"""
envio_config.py — ÚNICO lugar con las constantes del envío escalonado y de la protección del número.
Cambiar un valor aquí lo cambia en el programador, el semáforo, las estimaciones y la subida de archivos.
"""
import os

# ── Programador ──────────────────────────────────────────────────────────────
CICLO_SEG = 20                      # cada cuánto el programador toma mensajes y arranca colas
ESPERA_INICIAL_COLA = (0, 20)       # s antes del PRIMER mensaje de una cola (siempre después de la hora programada)
PAUSA_INDIVIDUAL = (20, 45)         # s entre envíos de la misma sesión cuando el destino es un número individual
PAUSA_GRUPO = (8, 20)               # s cuando el destino es un grupo
MULT_PAUSA_ROJO = 2                 # con semáforo rojo las pausas se duplican
CAIDA_LARGA_MIN = 60                # si el servidor estuvo caído más que esto, lo "en_cola" pasa a vencido

# ── "Escribiendo…" (options.delay / options.presence de Evolution v1) ────────
# Desactivado por defecto: se activa con ENVIO_ESCRIBIENDO=1 tras probarlo con un envío real.
ESCRIBIENDO_ACTIVO = os.getenv("ENVIO_ESCRIBIENDO", "0") == "1"
ESCRIBIENDO_MS = (1000, 3000)

# ── Semáforo anti-spam (por sesión y por día de Ecuador) ─────────────────────
SEMAFORO_AMARILLO = 100             # carga >= esto → amarillo
SEMAFORO_ROJO = 300                 # carga >  esto → rojo
NUMERO_NUEVO_DIAS = 7               # un número vinculado hace menos de esto tiene los umbrales a la mitad
PESO_GRUPO = 0.2                    # un mensaje a grupo cuenta como esto de un individual
TEXTO_REPETIDO_MAX = 20             # el mismo texto a más de N números individuales en el día → aviso

# ── Aviso al programar ───────────────────────────────────────────────────────
VENTANA_AVISO_MIN = 10              # "mensajes en una ventana cercana": ± minutos alrededor de la hora elegida

# ── Peso máximo por archivo (MB) ─────────────────────────────────────────────
LIMITE_MB = {"image": 5, "audio": 16, "video": 16, "document": 25}   # audio/video: DESPUÉS de convertir
MAX_ENTRADA_MB = 50                 # tope interno del archivo original (un video de galería pesa mucho antes de convertirse)


def promedio(rango: tuple[float, float]) -> float:
    return (rango[0] + rango[1]) / 2
