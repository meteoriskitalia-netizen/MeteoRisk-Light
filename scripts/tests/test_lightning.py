# -*- coding: utf-8 -*-
"""Test A2 - lightning.py (A1): Blitz v2, trend, epoch, fetch, PNG, WMS.

Tutti i valori attesi sono quelli calcolati dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
Nessuna rete: il trasporto HTTP e' iniettato via opener.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import datetime as dt
import struct
import zlib

import pytest

import lightning as ltg


def _build_blitz(records, version=2):
    """Costruisce un file Blitz v2 sintetico (verbatim scratch A2)."""
    body = bytearray()
    body.append(version)
    body += struct.pack(">H", len(records))
    for lon_raw, lat_raw, ext8 in records:
        b = bytearray(8 if ext8 else 6)
        b[0:2] = struct.pack(">H", lon_raw & 0xFFFF)
        b[2:4] = struct.pack(">H", lat_raw & 0xFFFF)
        b[4] = (((lon_raw >> 16) & 3) << 6) | (((lat_raw >> 16) & 3) << 4)
        b[5] = 255 if ext8 else 0
        if ext8:
            b[6:8] = b"\x00\x00"
        body += b
    return bytes(body)


REC6 = _build_blitz([(140000, 190783, False)])
REC8 = _build_blitz([(131072, 131072, True)])
REC_MIX = _build_blitz([(131072, 131072, False), (140000, 190783, True)])


def test_decode_6bytes_record():
    out = ltg.decode_blitz_v2(REC6)
    assert out["version"] == 2
    assert out["count"] == 1
    assert out["strikes"] == [(12.260742, 41.00029)]
    assert out["consumed"] == 9


def test_decode_8bytes_record():
    out = ltg.decode_blitz_v2(REC8)
    assert out["count"] == 1
    assert out["strikes"] == [(0.0, 0.0)]
    assert out["consumed"] == 11


def test_decode_mixed_records():
    out = ltg.decode_blitz_v2(REC_MIX)
    assert out["count"] == 2
    assert out["consumed"] == 17


def test_decode_error_paths():
    with pytest.raises(ValueError, match="blitz_version_unsupported:1"):
        ltg.decode_blitz_v2(b"\x01\x00\x00\x00\x00")
    with pytest.raises(ValueError, match="blitz_truncated_record:off=3"):
        ltg.decode_blitz_v2(b"\x02\x00\x01\x00\x10")
    with pytest.raises(ValueError,
                       match="blitz_bytes_mismatch:consumed=9/total=10"):
        ltg.decode_blitz_v2(REC6 + b"\x00")
    with pytest.raises(ValueError, match="blitz_too_short"):
        ltg.decode_blitz_v2(b"\x02\x00")


def test_raw_formula_regression_by_hand():
    lon = 140000 * 360.0 / 262144.0 - 180.0
    lat = 190783 * 180.0 / 262144.0 - 90.0
    assert round(lon, 6) == 12.260742
    assert round(lat, 6) == 41.00029


def test_lightning_trend_gatlin_family():
    assert ltg.lightning_trend([10, 20, 30, 40]) == {
        "jump": 30.0, "trend": 10.0, "window": 4, "n_samples": 4,
        "direction": "up"}
    assert ltg.lightning_trend([50, 20]) == {
        "jump": -30.0, "trend": -30.0, "window": 2, "n_samples": 2,
        "direction": "down"}
    assert ltg.lightning_trend([5]) == {
        "jump": 0.0, "trend": 0.0, "window": 0, "n_samples": 1,
        "direction": "flat"}
    assert ltg.lightning_trend([]) == {
        "jump": 0.0, "trend": 0.0, "window": 0, "n_samples": 0,
        "direction": "flat"}


def test_lightning_score_values():
    assert ltg.lightning_score(100, 100) == 100.0
    assert ltg.lightning_score(50, 0) == 35.0
    assert ltg.lightning_score(0, 50) == 0.0


def test_ltg_epoch_floor_deterministic():
    fixed = dt.datetime(2026, 10, 7, 10, 7, tzinfo=dt.timezone.utc)
    assert ltg.ltg_epoch_floor(now=fixed) == 1791367500000
    assert ltg.ltg_epoch_floor(now=fixed, backoff_steps=9) == 1791364800000


def test_fetch_ltg_backoff_one_403():
    seen = []
    good = _build_blitz([(131072, 131072, False)])

    def opener_ok(url, timeout_s):
        seen.append(url)
        if len(seen) == 1:
            return None                 # slot non pubblicato (403)
        return good

    got = ltg.fetch_ltg(opener=opener_ok)
    assert got["attempts"] == 2
    assert got["count"] == 1
    assert got["version"] == 2
    assert len(seen) == 2 and seen[0] != seen[1]
    ep0 = int(seen[0].rsplit("/", 1)[-1].split(".")[0].split("_")[-1])
    ep1 = int(seen[1].rsplit("/", 1)[-1].split(".")[0].split("_")[-1])
    assert ep0 - ep1 == ltg.LTG_PERIOD_MS == 300000
    assert got["epoch_ms"] == ep1


def test_fetch_ltg_all_403_raises():
    with pytest.raises(ltg.LightningFetchError,
                       match="ltg_unavailable:not_published_403"):
        ltg.fetch_ltg(epoch_ms=1777027200000, opener=lambda u, t: None)


def test_fetch_ltg_invalid_payload_raises():
    with pytest.raises(ltg.LightningFetchError,
                       match="blitz_invalid:blitz_version_unsupported:110"):
        ltg.fetch_ltg(epoch_ms=1777027200000,
                      opener=lambda u, t: b"not-blitz")


def _make_png(width, height, color_type, rows):
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    scan = b"".join(b"\x00" + r for r in rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scan)) + chunk(b"IEND", b""))


def test_count_png_nontransparent():
    # 2x2 RGBA: 3 opachi + 1 trasparente
    png = _make_png(2, 2, 6, [b"\xff\x00\x00\xff\x00\xff\x00\xff",
                              b"\x00\x00\xff\xff\x10\x20\x30\x00"])
    assert ltg.count_png_nontransparent(png) == 3
    # 2x2 gray 8 bit: tutti opachi
    png_g = _make_png(2, 2, 0, [b"\x10\x20", b"\x30\x40"])
    assert ltg.count_png_nontransparent(png_g) == 4
    with pytest.raises(ValueError, match="not_a_png"):
        ltg.count_png_nontransparent(b"nope")


def test_build_getmap_url():
    url = ltg.build_getmap_url()
    assert url.startswith(ltg.DEFAULT_WMS_URL)
    assert "SERVICE=WMS" in url
    assert "REQUEST=GetMap" in url
    assert "TIME=" not in url
    url_t = ltg.build_getmap_url(time_iso="2026-10-07T10:00:00Z")
    assert "TIME=" in url_t
    assert "2026-10-07T10" in url_t
