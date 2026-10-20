"""
app/planes.py
Planes y límites: el ÚNICO lugar donde se definen. (v1.1.0 solo los declara; los aplica v1.1.2.)
"""

PRUEBA_DIAS = 7
PRUEBA_NUMEROS = 1
PRUEBA_TOPE_MENSAJES = 50          # cuenta cada mensaje creado (también los clones de recurrencia); cancelar no lo devuelve
DIAS_PERIODO_PAGADO = 30
DIAS_GRACIA_PAGADO = 3             # solo cuentas pagadas: el cobro es manual
DIAS_MES_REGALO = 30

PLANES: dict[str, dict] = {
    "pro":        {"nombre": "Pro",        "numeros": 1,  "usuarios": 1,  "precio_usd": 9.99},
    "team":       {"nombre": "Team",       "numeros": 4,  "usuarios": 4,  "precio_usd": 29.99},
    "enterprise": {"nombre": "Enterprise", "numeros": 10, "usuarios": 10, "precio_usd": 69.99},
}

ESTADOS_CUENTA = ("prueba", "activa", "cortesia", "vencida", "suspendida")
