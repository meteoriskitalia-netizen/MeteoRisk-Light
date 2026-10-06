#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test preprocess.py: validity del dato (DATA VALIDITY) separata dalla soglia
meteorologica, gestione nodata, corruzione GeoTIFF, FAIL SAFE su CRS."""

import os

import numpy as np
import pytest

from radar_engine import models
from radar_engine.preprocess import read_raster
from radar_engine.config import CONFIG

from conftest import write_synthetic_tif, blank_grid, DEFAULT_TRANSFORM, DPC_TM_WKT


def _read(path, **kw):
    return read_raster(str(path), nodata_values=CONFIG["preprocess"]["nodata_values"],
                       **kw)


def test_nodata_9999_is_masked(vmi_tif):
    r = _read(vmi_tif)
    assert np.any(r.nodata_mask)
    assert not np.any(r.valid_mask[r.nodata_mask])
    # il fondo è -9999 -> quasi tutto masked
    assert r.valid_mask.sum() > 0


def test_nodata_9998_is_masked(vmi_tif):
    r = _read(vmi_tif)
    # pixel impostato a -9998
    assert not r.valid_mask[700, 300]
    assert r.nodata_mask[700, 300]


def test_valid_value_10dbz_kept(vmi_tif):
    r = _read(vmi_tif)
    assert r.valid_mask[0, 0]
    assert r.data[0, 0] == pytest.approx(10.0)
    # 10 dBZ NON è una soglia meteorologica: qui resta SOLO validità del dato.


def test_nan_is_no_data(vmi_tif):
    r = _read(vmi_tif)
    assert not r.valid_mask[100, 900]
    assert r.nodata_mask[100, 900]


def test_validity_separate_from_threshold(vmi_tif):
    r = _read(vmi_tif)
    # valid_mask contiene valori < soglia meteo (es. 10 dBZ) -> validità != soglia
    below = r.valid_mask & (r.data < 20.0)
    assert np.any(below)
    assert r.valid_mask[0, 0]


def test_corrupted_geotiff_rejected(tmp_path):
    p = tmp_path / "corrupt.tif"
    p.write_bytes(b"II*\x00 not a real geotiff content at all")
    with pytest.raises(models.ValidationError):
        _read(p)


def test_garbage_bytes_rejected(tmp_path):
    p = tmp_path / "garbage.tif"
    p.write_bytes(os.urandom(64))
    with pytest.raises(models.ValidationError):
        _read(p)


def test_crs_missing_failsafe(tmp_path):
    arr = blank_grid()
    path = write_synthetic_tif(str(tmp_path / "nocrs.tif"), arr, crs=None)
    with pytest.raises(models.CrsError):
        _read(path)


def test_geographic_plausibility_failsafe(tmp_path):
    # Il FAIL SAFE blocca coordinate fuori Italia: bbox volutamente restrittivo
    # (esclude il centro reale della griglia VMI), quindi CrsError.
    arr = blank_grid()
    path = write_synthetic_tif(str(tmp_path / "ok_transform.tif"), arr)
    with pytest.raises(models.CrsError):
        _read(path, geo_plausible_bbox={"lon": [13.0, 21.0], "lat": [30.0, 40.0]})
    # con la bbox corretta (Italia) il medesimo file passa
    r = _read(path, geo_plausible_bbox=CONFIG["preprocess"]["geo_plausible_bbox"])
    assert (r.rows, r.cols) == (1400, 1200)


def test_centroid_conversion_is_plausible_italy(vmi_tif):
    r = _read(vmi_tif)
    lon, lat = r.pixel_to_lonlat(700.0, 600.0)  # centro griglia VMI reale
    assert 4.0 <= lon <= 21.0
    assert 34.0 <= lat <= 48.0
    assert 11.5 <= lon <= 13.5 and 40.0 <= lat <= 43.0  # centro Italia


def test_geometry_metadata_extracted(vmi_tif):
    r = _read(vmi_tif)
    assert r.pixel_area_km2 == pytest.approx(1.0, rel=0.01)
    assert (r.rows, r.cols) == (1400, 1200)
    assert r.crs_wkt  # non assume EPSG arbitrario: legge la WKT dal file
    assert r.declared_nodata is None  # VMI reale non dichiara -9999 nei tag