"""Test satellite_engine.py — conversione WebP dei frame GetMap.

Nessuna rete: tutti i frame sono PNG sintetici creati con Pillow e l'helper
migrate_legacy_png gira su tmp_path. Il modulo .py vive in scripts/ (senza
__init__) quindi viene importato via importlib da percorso file.
"""

import importlib.util
import io
import sys
from pathlib import Path

import pytest

pytest.importorskip("PIL")

from PIL import Image  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "satellite_engine", _SCRIPTS / "satellite_engine.py")
satellite_engine = importlib.util.module_from_spec(_SPEC)
sys.modules["satellite_engine"] = satellite_engine
_SPEC.loader.exec_module(satellite_engine)


def _png_bytes(mode, size):
    buf = io.BytesIO()
    if mode == "P":
        img = Image.new("P", size)
        img.putpalette([i % 256 for i in range(768)])
    else:
        img = Image.new(mode, size, (10, 20, 30, 40) if "A" in mode else (10, 20, 30))
    img.save(buf, "PNG")
    return buf.getvalue()


def test_png_to_webp_rgba():
    body = satellite_engine.png_to_webp(_png_bytes("RGBA", (64, 64)))
    assert satellite_engine.is_webp(body)
    assert body[:4] == b"RIFF" and body[8:12] == b"WEBP"
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "WEBP"
        assert img.size == (64, 64)
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0))[3] == 40


def test_png_to_webp_palette():
    body = satellite_engine.png_to_webp(_png_bytes("P", (32, 32)))
    assert satellite_engine.is_webp(body)
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "WEBP"
        assert img.size == (32, 32)


def test_is_webp_true_false():
    webp = satellite_engine.png_to_webp(_png_bytes("RGB", (8, 8)))
    assert satellite_engine.is_webp(webp) is True
    assert satellite_engine.is_webp(_png_bytes("RGB", (8, 8))) is False
    assert satellite_engine.is_webp(b"RIFF\x00\x00\x00\x00PNG\x00\x00") is False
    assert satellite_engine.is_webp(b"RIFF") is False


def test_migrate_legacy_png(tmp_path):
    (tmp_path / "2026-10-06T19-45-00Z.png").write_bytes(_png_bytes("RGB", (16, 16)))
    migrated = satellite_engine.migrate_legacy_png(tmp_path)
    assert migrated == 1
    target = tmp_path / "2026-10-06T19-45-00Z.webp"
    assert target.exists()
    assert not (tmp_path / "2026-10-06T19-45-00Z.png").exists()
    body = target.read_bytes()
    assert satellite_engine.is_webp(body)
    with Image.open(io.BytesIO(body)) as img:
        assert img.format == "WEBP"
        assert img.size == (16, 16)
