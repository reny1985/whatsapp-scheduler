"""
Pruebas de normalización de números de teléfono.
Replica la lógica de _normalizarNumeroWA() del frontend (app.js).
"""


def normalizar_numero_wa(raw: str, dial_code: str) -> str:
    """
    Replica exactamente la función _normalizarNumeroWA del frontend.
    Convierte cualquier formato de entrada a solo dígitos con código de país.
    """
    # 1. Quitar espacios, guiones, paréntesis, punto, +
    import re
    s = re.sub(r'[\s\-()+.]', '', raw)

    # 2. Si empieza con el código de país
    if dial_code and s.startswith(dial_code):
        s = s[len(dial_code):]
        if s.startswith('0'):
            s = s[1:]
    elif s.startswith('0'):
        s = s[1:]

    # 3. Solo dígitos
    s = re.sub(r'\D', '', s)
    return (dial_code + s) if (dial_code and len(s) >= 6) else ''


class TestNormalizarEcuador:
    def test_con_cero_inicial(self):
        assert normalizar_numero_wa('0969829845', '593') == '593969829845'

    def test_sin_cero_inicial(self):
        assert normalizar_numero_wa('969829845', '593') == '593969829845'

    def test_con_espacios_y_cero(self):
        assert normalizar_numero_wa('096 982 9845', '593') == '593969829845'

    def test_formato_internacional(self):
        assert normalizar_numero_wa('+593969829845', '593') == '593969829845'

    def test_autocomplete_movil(self):
        """Caso crítico: teclado móvil autocompleta código + 0 inicial."""
        assert normalizar_numero_wa('+593 0969829845', '593') == '593969829845'

    def test_codigo_pais_pegado(self):
        assert normalizar_numero_wa('5930969829845', '593') == '593969829845'


class TestNormalizarColombia:
    def test_numero_movil(self):
        assert normalizar_numero_wa('3001234567', '57') == '573001234567'

    def test_con_codigo_pais(self):
        assert normalizar_numero_wa('+573001234567', '57') == '573001234567'


class TestNormalizarMexico:
    def test_numero_movil(self):
        assert normalizar_numero_wa('5512345678', '52') == '525512345678'

    def test_con_codigo_pais(self):
        assert normalizar_numero_wa('+525512345678', '52') == '525512345678'


class TestNormalizarEdgeCases:
    def test_vacio(self):
        assert normalizar_numero_wa('', '593') == ''

    def test_muy_corto(self):
        assert normalizar_numero_wa('123', '593') == ''

    def test_guiones_y_espacios(self):
        assert normalizar_numero_wa('09-698-29845', '593') == '593969829845'
