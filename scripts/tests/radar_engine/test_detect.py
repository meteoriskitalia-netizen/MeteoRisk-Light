#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test detect.py: threshold (soglia meteo separata dalla validità), morfologia,
connected components, metriche geometriche, filtri area."""

import numpy as np
import pytest

from radar_engine.preprocess import read_raster
from radar_engine.detect import detect_cells
from radar_engine.config import CONFIG

from conftest import write_synthetic_tif, blank_grid, add_blob


def _detect(arr, tmp_path, **over):
    path = tmp_path / "t.tif"
    write_synthetic_tif(str(path), arr)
    raster = read_raster(str(path), nodata_values=CONFIG["preprocess"]["nodata_values"])
    cfg = dict(CONFIG["detect"])
    cfg.update(over)
    cells = detect_cells(raster, cfg)
    return cells, raster


def test_single_blob_metrics(tmp_path):
    arr = blank_grid()
    add_blob(arr, 600, 600, 20, 45.0)
    cells, raster = _detect(arr, tmp_path)
    assert len(cells) == 1
    c = cells[0]
    expected_px = int(np.count_nonzero((arr >= 20.0)))
    assert c.pixel_count == pytest.approx(expected_px, rel=0.05)
    assert c.area_km2 == pytest.approx(expected_px * raster.pixel_area_km2, rel=0.05)
    assert c.max_dbz == pytest.approx(45.0, abs=0.5)
    assert 35.0 <= c.mean_dbz <= 45.0
    assert c.p90_dbz >= c.mean_dbz <= c.max_dbz
    r, ccol = c.centroid_raster
    assert abs(r - 600) <= 3 and abs(ccol - 600) <= 3
    lon, lat = c.centroid_lonlat
    assert 11.5 <= lon <= 13.5 and 40.0 <= lat <= 43.0
    assert 0.0 <= c.eccentricity <= 1.0
    assert 0.0 < c.solidity <= 1.0
    assert c.compactness > 0.0
    minr, minc, maxr, maxc = c.bbox
    assert 0 <= minr <= 600 <= maxr < 1400
    assert 0 <= minc <= 600 <= maxc < 1200


def test_two_separated_blobs_two_cells(tmp_path):
    arr = blank_grid()
    add_blob(arr, 300, 300, 15, 40.0)
    add_blob(arr, 900, 900, 15, 40.0)
    cells, _ = _detect(arr, tmp_path)
    assert len(cells) == 2


def test_touching_blobs_one_cell(tmp_path):
    # due quadrati con bordo condiviso -> 1 sola cella (8-connectivity)
    arr = blank_grid()
    arr[500:540, 500:540] = 40.0
    arr[500:540, 540:580] = 35.0
    cells, _ = _detect(arr, tmp_path)
    assert len(cells) == 1


def test_small_component_dropped_by_min_area(tmp_path):
    arr = blank_grid(fill=-9999.0)
    add_blob(arr, 600, 600, 20, 40.0)   # ~1257 px (raggio 20)
    add_blob(arr, 200, 200, 4, 40.0)    # ~50 px
    cells, _ = _detect(arr, tmp_path, min_area_km2=200.0)
    assert len(cells) == 1
    assert cells[0].pixel_count > 500


def test_subthreshold_dbz_not_detected(tmp_path):
    arr = blank_grid()
    add_blob(arr, 600, 600, 20, 15.0)  # valido (>=10) ma sotto soglia meteo 20
    cells, _ = _detect(arr, tmp_path, dbz_threshold=20.0)
    assert len(cells) == 0


def test_ten_dbz_valid_but_not_a_storm(tmp_path):
    # 10 dBZ resta un dato VALIDO (test_preprocess) ma non una cella convettiva.
    arr = blank_grid(fill=-9999.0)
    arr[600, 600] = 10.0
    raster = read_raster(
        str(tmp_path / "t.tif") if False else _write(arr, tmp_path),
        nodata_values=CONFIG["preprocess"]["nodata_values"],
    )
    assert raster.valid_mask[600, 600]
    cells = detect_cells(raster, CONFIG["detect"])
    assert len(cells) == 0


def _write(arr, tmp_path):
    p = tmp_path / "w.tif"
    write_synthetic_tif(str(p), arr)
    return p


def test_max_area_cap_drops_mosaic(tmp_path):
    arr = blank_grid()
    add_blob(arr, 600, 600, 150, 40.0)  # ~70k px > cap
    cells, _ = _detect(arr, tmp_path, max_area_km2=40000.0)
    assert len(cells) == 0


def test_two_cells_distinct_ids_and_timestamps(tmp_path):
    arr = blank_grid()
    add_blob(arr, 300, 300, 15, 44.0)
    add_blob(arr, 900, 900, 15, 30.0)
    cells, raster = _detect(arr, tmp_path)
    assert len({c.cell_id for c in cells}) == 2
    assert all(c.timestamp_ms == (raster.time_ms or 0) for c in cells)


def test_morphology_separates_nearby_blobs(tmp_path):
    arr = blank_grid()
    add_blob(arr, 600, 580, 10, 40.0)
    add_blob(arr, 600, 700, 10, 40.0)  # gap 120px: resta separato NONOSTANTE opening
    cells, _ = _detect(arr, tmp_path, morph_radius_px=3)
    assert len(cells) == 2