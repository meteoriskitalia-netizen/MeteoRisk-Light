# -*- coding: utf-8 -*-
"""Test satellite_ot.py (PHASE2_VERSION 0.5.0): overshooting top da IR_108 DPC.

Funzioni PURE su griglie di temperatura di sommita' (°C), metodo IRW-texture
(Bedka et al. 2010): pixel freddo (CTT <= -58 °C) + anomalia >= 6.5 °C rispetto
all'anvillo campionato a raggio 8 px in 16 direzioni, poi erosione 3x3.

Griglie sintetiche: base -50 °C (anvillo valido, <= -48), blob freddo -60 °C
(cima convettiva). Nessun valore dedotto a mano: le asserzioni sono soglie
strutturali (flag presente/assente, conteggi > 0, minimo) e proprieta' esatte
(minimo CTT) verificate dal codice.
"""
import os
import sys

import numpy as np
import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(os.path.dirname(_TESTS_DIR))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import radar_engine.phase2.satellite_ot as sot  # noqa: E402


def _grid(rows=41, cols=41, base=-50.0):
    return np.full((rows, cols), float(base), dtype="float64")


def _with_disc(grid, row, col, radius, value):
    rows, cols = grid.shape
    rr, cc = np.ogrid[:rows, :cols]
    grid[(rr - row) ** 2 + (cc - col) ** 2 <= radius ** 2] = float(value)
    return grid


# ---------------------------------------------------------------------------
# Maschera cima fredda
# ---------------------------------------------------------------------------

def test_cold_cloud_top_mask_threshold_and_validity():
    ctt = _grid(base=-50.0)
    ctt[10, 10] = -60.0                  # freddo
    ctt[20, 20] = np.nan                 # non finito
    m = sot.cold_cloud_top_mask(ctt)
    assert bool(m[10, 10]) is True
    assert bool(m[0, 0]) is False        # -50 non e' <= -58
    assert bool(m[20, 20]) is False      # NaN mai freddo
    # override soglia: -50 diventa "freddo"
    m2 = sot.cold_cloud_top_mask(ctt, threshold=-50.0)
    assert bool(m2[0, 0]) is True
    # valid_mask esclude il blob
    vm = np.ones_like(ctt, dtype=bool)
    vm[10, 10] = False
    assert bool(sot.cold_cloud_top_mask(ctt, valid_mask=vm)[10, 10]) is False


def test_cold_cloud_top_mask_shape_mismatch():
    with pytest.raises(ValueError, match="shape_mismatch:valid_mask"):
        sot.cold_cloud_top_mask(_grid(5, 5), valid_mask=np.ones((4, 4), bool))


# ---------------------------------------------------------------------------
# Anvillo e anomalia
# ---------------------------------------------------------------------------

def test_anvil_mean_grid_uniform_warm():
    ctt = _grid(base=-50.0)
    anvil = sot.anvil_mean_grid(ctt)
    # lontano dai bordi l'anvillo e' uniforme -50
    assert anvil[20, 20] == pytest.approx(-50.0, abs=1e-9)


def test_anvil_mean_grid_nan_when_too_few_samples():
    # griglia piccola: raggio 8 px esce quasi ovunque -> campioni < 5
    ctt = _grid(rows=6, cols=6, base=-50.0)
    anvil = sot.anvil_mean_grid(ctt)
    assert bool(np.isnan(anvil[3, 3])) is True


def test_cold_anomaly_positive_over_blob():
    ctt = _grid(base=-50.0)
    _with_disc(ctt, 20, 20, 2, -60.0)
    anom = sot.cold_anomaly_grid(ctt)
    # il centro del blob e' ~10 °C piu' freddo dell'anvillo
    assert anom[20, 20] == pytest.approx(10.0, abs=1e-9)
    # pixel d'anvillo lontano: anomalia nulla
    assert anom[0, 0] == pytest.approx(0.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Flag OT + erosione
# ---------------------------------------------------------------------------

def test_ot_flags_detect_blob_and_erode():
    ctt = _grid(base=-50.0)
    _with_disc(ctt, 20, 20, 2, -60.0)
    flags = sot.ot_flags(ctt)
    assert bool(flags[20, 20]) is True
    assert bool(flags[0, 0]) is False
    n_eroded = int(flags.sum())
    no_erosion = sot.ot_flags(ctt, erosion_kernel=1)
    assert int(no_erosion.sum()) > n_eroded > 0


def test_ot_flags_requires_local_anomaly_not_just_cold():
    # tutto SUPER-freddo (-60) ma uniforme: nessuna anomalia locale -> no OT
    ctt = _grid(base=-60.0)
    flags = sot.ot_flags(ctt)
    assert int(flags.sum()) == 0
    assert bool(sot.cold_cloud_top_mask(ctt).any()) is True   # e' "freddo"...


def test_ot_flags_valid_mask_excludes_blob():
    ctt = _grid(base=-50.0)
    _with_disc(ctt, 20, 20, 2, -60.0)
    vm = np.ones_like(ctt, dtype=bool)
    vm[18:23, 18:23] = False
    assert int(sot.ot_flags(ctt, valid_mask=vm).sum()) == 0


# ---------------------------------------------------------------------------
# evaluate_ir_ot (entry point usato da main)
# ---------------------------------------------------------------------------

def test_evaluate_ir_ot_cold_spot():
    ctt = _grid(base=-50.0)
    # blob raggio 3: dopo l'erosione 3x3 restano 9 px -> area 9 km2 -> score 100
    _with_disc(ctt, 20, 20, 3, -60.0)
    res = sot.evaluate_ir_ot(ctt, pixel_area_km2=1.0)
    assert res["ot_flag"] is True
    assert res["cold_top"] is True
    assert res["n_flags"] == 9
    assert res["ctt_min_c"] == pytest.approx(-60.0, abs=1e-9)
    assert res["score"] == 100.0
    assert res["valid_pixels"] == 41 * 41
    assert res["ctt_threshold_c"] == sot.IR_CTT_THRESHOLD_C
    assert res["anomaly_threshold_c"] == sot.IR_ANOMALY_THRESHOLD_C


def test_evaluate_ir_ot_flat_warm_zero():
    res = sot.evaluate_ir_ot(_grid(base=-50.0), pixel_area_km2=1.0)
    assert res["ot_flag"] is False
    assert res["cold_top"] is False
    assert res["score"] == 0.0
    assert res["n_flags"] == 0
    assert res["ctt_min_c"] == pytest.approx(-50.0, abs=1e-9)


def test_evaluate_ir_ot_all_nan_no_valid_pixels():
    ctt = np.full((41, 41), np.nan)
    res = sot.evaluate_ir_ot(ctt, pixel_area_km2=1.0)
    assert res["valid_pixels"] == 0
    assert res["ctt_min_c"] is None
    assert res["ot_flag"] is False
    assert res["score"] == 0.0


def test_evaluate_ir_ot_shape_mismatch_raises():
    with pytest.raises(ValueError, match="shape_mismatch:valid_mask"):
        sot.evaluate_ir_ot(_grid(10, 10), valid_mask=np.ones((9, 9), bool))
