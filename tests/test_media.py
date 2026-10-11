"""
Pruebas de conversión de audio/video al subir y del envío por el scheduler.
Requieren ffmpeg/ffprobe instalados. No tocan Evolution API real.
"""
import json
import subprocess

import httpx
import pytest
from httpx import AsyncClient, ASGITransport

from app import scheduler
from app.main import app
from app.routers import media

pytestmark = pytest.mark.asyncio(loop_scope="function")


def _ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def _probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)["streams"]


@pytest.fixture(scope="module")
def muestras(tmp_path_factory):
    d = tmp_path_factory.mktemp("muestras")
    tono = ["-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    video = ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=15:duration=1"]
    _ffmpeg(*tono, "-c:a", "libopus", str(d / "a.webm"))
    _ffmpeg(*tono, "-c:a", "aac", str(d / "a.mp4"))
    _ffmpeg(*video, *tono, "-c:v", "libvpx-vp9", "-c:a", "libopus", str(d / "v.webm"))
    _ffmpeg(*video, *tono, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mov", str(d / "v.mov"))
    # Video con dimensiones impares
    _ffmpeg("-f", "lavfi", "-i", "testsrc=size=853x479:rate=15:duration=1", "-pix_fmt", "yuv444p",
            "-c:v", "libx264", str(d / "impar.mp4"))
    return d


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "UPLOAD_DIR", tmp_path)
    return tmp_path


async def _subir(path, nombre, content_type):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post("/media/subir", files={"file": (nombre, path.read_bytes(), content_type)})


def _archivos(uploads):
    return sorted(p.name for p in uploads.iterdir())


@pytest.mark.parametrize("archivo,nombre,ct", [
    ("a.webm", "grabacion.webm", "audio/webm;codecs=opus"),
    ("a.mp4", "nota.mp4", "audio/mp4"),
])
async def test_audio_se_convierte_a_opus(muestras, uploads, archivo, nombre, ct):
    r = await _subir(muestras / archivo, nombre, ct)
    assert r.status_code == 201, r.text
    assert r.json()["tipo_media"] == "audio"
    assert r.json()["url"].endswith(".ogg")
    assert len(_archivos(uploads)) == 1  # sin original
    (s,) = _probe(uploads / _archivos(uploads)[0])
    assert s["codec_name"] == "opus"
    assert s["channels"] == 1 and int(s["sample_rate"]) == 48000


@pytest.mark.parametrize("archivo,nombre,ct", [
    ("v.webm", "grabacion.webm", "video/webm;codecs=vp9,opus"),
    ("v.mov", "clip.mov", "video/quicktime"),
])
async def test_video_se_convierte_a_h264_aac(muestras, uploads, archivo, nombre, ct):
    r = await _subir(muestras / archivo, nombre, ct)
    assert r.status_code == 201, r.text
    assert r.json()["tipo_media"] == "video"
    assert r.json()["url"].endswith(".mp4")
    assert len(_archivos(uploads)) == 1
    streams = {s["codec_type"]: s for s in _probe(uploads / _archivos(uploads)[0])}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["video"]["pix_fmt"] == "yuv420p"
    assert streams["video"]["width"] == 1280 and streams["video"]["height"] == 720
    assert streams["audio"]["codec_name"] == "aac"


async def test_video_dimensiones_pares(muestras, uploads):
    r = await _subir(muestras / "impar.mp4", "impar.mp4", "video/mp4")
    assert r.status_code == 201, r.text
    v = next(s for s in _probe(uploads / _archivos(uploads)[0]) if s["codec_type"] == "video")
    assert v["width"] % 2 == 0 and v["height"] % 2 == 0 and v["width"] <= 1280


@pytest.mark.parametrize("nombre,ct", [("roto.webm", "audio/webm"), ("roto.mov", "video/quicktime")])
async def test_archivo_corrupto_da_error_y_no_guarda(uploads, muestras, nombre, ct):
    basura = muestras / "basura.bin"
    basura.write_bytes(b"esto no es multimedia" * 50)
    r = await _subir(basura, nombre, ct)
    assert r.status_code == 422
    assert "No se pudo procesar" in r.json()["detail"]
    assert _archivos(uploads) == []


# ── Scheduler ────────────────────────────────────────────────────────────────

class _FakeClient:
    """Sustituye httpx.AsyncClient y registra las llamadas."""
    llamadas: list = []
    respuestas: list = []

    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def post(self, url, json=None, headers=None):
        _FakeClient.llamadas.append((url, json))
        status, body = _FakeClient.respuestas.pop(0) if _FakeClient.respuestas else (201, {"key": {"id": "X"}})
        return httpx.Response(status, json=body, request=httpx.Request("POST", url))


@pytest.fixture
def evo(monkeypatch):
    _FakeClient.llamadas, _FakeClient.respuestas = [], []
    monkeypatch.setattr(scheduler.httpx, "AsyncClient", _FakeClient)
    monkeypatch.setattr(scheduler, "EVOLUTION_URL", "http://evo")
    monkeypatch.setattr(scheduler, "BASE_PUBLIC_URL", "https://app.test")
    return _FakeClient


async def test_audio_usa_sendWhatsAppAudio(evo):
    await scheduler._enviar_mensaje("wa-1", "593999@s.whatsapp.net", None, "audio", "/static/uploads/a.ogg")
    assert evo.llamadas == [(
        "http://evo/message/sendWhatsAppAudio/wa-1",
        {"number": "593999@s.whatsapp.net", "options": {"encoding": True},
         "audioMessage": {"audio": "https://app.test/static/uploads/a.ogg"}},
    )]


async def test_audio_con_texto_envia_texto_aparte_despues(evo):
    await scheduler._enviar_mensaje("wa-1", "n", "hola", "audio", "/static/uploads/a.ogg")
    assert [u for u, _ in evo.llamadas] == [
        "http://evo/message/sendWhatsAppAudio/wa-1",
        "http://evo/message/sendText/wa-1",
    ]
    assert evo.llamadas[1][1]["textMessage"]["text"] == "hola"


async def test_video_incluye_filename_mp4(evo):
    await scheduler._enviar_mensaje("wa-1", "n", "pie", "video", "/static/uploads/abc_grabacion%20video.mp4")
    url, payload = evo.llamadas[0]
    assert url == "http://evo/message/sendMedia/wa-1"
    mm = payload["mediaMessage"]
    assert mm["mediatype"] == "video" and mm["caption"] == "pie"
    assert mm["fileName"] == "abc_grabacion video.mp4"


async def test_imagen_sigue_usando_sendMedia_sin_filename(evo):
    await scheduler._enviar_mensaje("wa-1", "n", "x", "image", "/static/uploads/f.jpg")
    assert evo.llamadas[0][0] == "http://evo/message/sendMedia/wa-1"
    assert "fileName" not in evo.llamadas[0][1]["mediaMessage"]


async def test_error_de_evolution_en_audio_lanza_y_no_envia_texto(evo):
    evo.respuestas = [(400, {"error": "bad audio"})]
    with pytest.raises(RuntimeError, match="Evolution API 400"):
        await scheduler._enviar_mensaje("wa-1", "n", "hola", "audio", "/static/uploads/a.ogg")
    assert len(evo.llamadas) == 1


async def test_audio_ok_texto_falla_es_error(evo):
    evo.respuestas = [(201, {"key": {}}), (500, {"error": "boom"})]
    with pytest.raises(RuntimeError, match="Audio enviado pero el texto falló"):
        await scheduler._enviar_mensaje("wa-1", "n", "hola", "audio", "/static/uploads/a.ogg")


async def test_error_de_evolution_en_video_lanza(evo):
    evo.respuestas = [(500, {"error": "boom"})]
    with pytest.raises(RuntimeError, match="Evolution API 500"):
        await scheduler._enviar_mensaje("wa-1", "n", None, "video", "/static/uploads/v.mp4")


# ── Documentos ───────────────────────────────────────────────────────────────

HEX = "c8e77d83" * 4  # 32 caracteres hexadecimales

@pytest.mark.parametrize("archivo,mime", [
    ("Reporte.pdf", "application/pdf"),
    ("Contrato final.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("Ventas.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("viejo.xls", "application/vnd.ms-excel"),
    ("datos.csv", "text/csv"),
    ("charla.pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
])
async def test_documento_envia_filename_original_y_mimetype(evo, archivo, mime):
    from urllib.parse import quote
    await scheduler._enviar_mensaje("wa-1", "n", "pie", "document", f"/static/uploads/{HEX}_{quote(archivo)}")
    url, payload = evo.llamadas[0]
    assert url == "http://evo/message/sendMedia/wa-1"
    mm = payload["mediaMessage"]
    assert mm["mediatype"] == "document"
    assert mm["fileName"] == archivo
    assert mm["mimetype"] == mime


async def test_documento_sin_nombre_usa_documento_mas_extension(evo):
    await scheduler._enviar_mensaje("wa-1", "n", None, "document", f"/static/uploads/{HEX}_.pdf")
    mm = evo.llamadas[0][1]["mediaMessage"]
    assert mm["fileName"] == "documento.pdf" and mm["mimetype"] == "application/pdf"


@pytest.mark.parametrize("nombre,ext", [("Reporte.pdf", ".pdf"), ("ventas.xlsx", ".xlsx"),
                                         ("a.xls", ".xls"), ("p.pptx", ".pptx"), ("d.csv", ".csv")])
async def test_subida_documento_octet_stream_conserva_extension(uploads, nombre, ext):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/media/subir", files={"file": (nombre, b"contenido", "application/octet-stream")})
    assert r.status_code == 201, r.text
    assert r.json()["tipo_media"] == "document"
    assert r.json()["url"].endswith(ext)
    assert [p.suffix for p in uploads.iterdir()] == [ext]
