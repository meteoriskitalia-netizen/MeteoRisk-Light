#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test fusion/opera_cirrus.py — LOGICA PURA di consenso (NESSUNA RETE).

Verifica `source_agreement`, `candidate_agreement` e `parse_key_nominal_time`
con input SINTETICI: la validazione live dell'accesso OPERA (S3/ORD) NON e'
testata qui (dipende dalla rete e dal tempo), ma e' documentata nel modulo."""

import datetime as _dt
import os
import sys

import numpy as np
import pytest

# Rende importabile il package radar_engine da scripts/.
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from radar_engine.fusion.opera_cirrus import (
    source_agreement, candidate_agreement, parse_key_nominal_time,
    _presence,
)


def _grid(vmi_rows, opera_rows, dbz=40.0, shape=(10, 10), fill=0.0):
    """Crea due griglie: righe in `vmi_rows` valgono `dbz` sulla prima, etc."""
    vmi = np.full(shape, fill, dtype="float64")
    opera = np.full(shape, fill, dtype="float64")
    for r in vmi_rows:
        vmi[r, :] = dbz
    for r in opera_rows:
        opera[r, :] = dbz
    return vmi, opera


def test_identical_grids_full_agreement():
    vmi, opera = _grid(range(10), range(10))
    out = source_agreement(vmi, opera)
    assert out["presence_class"] == "both"
    assert out["agreement_score"] == 1.0
    assert out["iou"] == 1.0
    assert out["confidence"] == "high"
    assert out["counts"]["both"] == 100
    assert out["counts"]["only_vmi"] == 0
    assert out["counts"]["only_opera"] == 0
    assert out["bias_dbz"] == pytest.approx(0.0, abs=1e-9)
    assert out["mean_abs_diff_dbz"] == pytest.approx(0.0, abs=1e-9)
    assert out["within_tolerance_fraction"] == 1.0


def test_only_vmi_when_opera_empty():
    vmi, opera = _grid([2, 3, 4], [])
    out = source_agreement(vmi, opera)
    assert out["presence_class"] == "only_vmi"
    assert out["confidence"] == "low"
    assert out["counts"]["both"] == 0
    assert out["counts"]["only_opera"] == 0
    assert out["counts"]["vmi_present"] == 30
    assert out["agreement_score"] == 0.0


def test_only_opera_when_vmi_empty():
    vmi, opera = _grid([], [6, 7])
    out = source_agreement(vmi, opera)
    assert out["presence_class"] == "only_opera"
    assert out["counts"]["only_opera"] == 20
    assert out["counts"]["only_vmi"] == 0


def test_neither_when_both_below_threshold():
    vmi, opera = _grid([], [], fill=10.0)
    out = source_agreement(vmi, opera)
    assert out["presence_class"] == "neither"
    assert out["confidence"] == "none"
    assert out["agreement_score"] == 0.0
    assert out["counts"]["union"] == 0


def test_partial_overlap_iou_and_counts():
    # VMI righe 0-4 (50 celle), OPERA righe 2-9 (80): both righe 2-4 (30).
    vmi, opera = _grid(range(5), range(2, 10))
    out = source_agreement(vmi, opera)
    assert out["presence_class"] == "both"
    assert out["counts"]["vmi_present"] == 50
    assert out["counts"]["opera_present"] == 80
    assert out["counts"]["both"] == 30
    assert out["counts"]["only_vmi"] == 20
    assert out["counts"]["only_opera"] == 50
    assert out["counts"]["union"] == 100
    # IoU = 30/100 = 0.30
    assert out["agreement_score"] == pytest.approx(0.30, abs=1e-4)
    assert out["iou"] == pytest.approx(0.30, abs=1e-4)
    assert out["overlap_fraction"] == pytest.approx(30 / 80, abs=1e-4)
    assert out["confidence"] == "medium"


def test_nan_cells_ignored():
    vmi = np.full((4, 4), 40.0)
    opera = np.full((4, 4), 40.0)
    vmi[0, 0] = np.nan
    opera[1, 1] = np.nan
    out = source_agreement(vmi, opera)
    # 16 - 2 celle non osservate = 14 co-osservate; 14 presenti in entrambe.
    assert out["n_valid_cells"] == 14
    assert out["counts"]["co_observed"] == 14
    assert out["counts"]["both"] == 14
    # VMI osserva 15 celle, OPERA 15; le 2 non condivise sono "missing".
    assert out["counts"]["vmi_observed"] == 15
    assert out["counts"]["opera_observed"] == 15
    assert out["counts"]["opera_missing_on_vmi"] == 1
    assert out["counts"]["vmi_missing_on_opera"] == 1
    # Una cella VMI senza osservazione OPERA NON deve diventare "only_vmi".
    assert out["counts"]["only_vmi"] == 0


def test_bias_sign_and_tolerance():
    vmi = np.full((5, 5), 30.0)
    opera = np.full((5, 5), 38.0)  # OPERA piu' alto di 8 dBZ
    out = source_agreement(vmi, opera, tolerance_dbz=10.0)
    assert out["bias_dbz"] == pytest.approx(8.0, abs=1e-6)
    assert out["mean_abs_diff_dbz"] == pytest.approx(8.0, abs=1e-6)
    assert out["within_tolerance_count"] == 25
    assert out["within_tolerance_fraction"] == 1.0
    # Con tolleranza 5 dBZ, le 25 celle differiscono di 8 -> fuori tolleranza.
    out2 = source_agreement(vmi, opera, tolerance_dbz=5.0)
    assert out2["within_tolerance_count"] == 0
    assert out2["within_tolerance_fraction"] == 0.0


def test_core_confirmation_fractions():
    vmi, opera = _grid(range(5), range(2, 10), dbz=45.0)  # 45 >= core 35
    out = source_agreement(vmi, opera)
    # core VMI = 50, core OPERA = 80, core both = 30
    assert out["core"]["vmi_core"] == 50
    assert out["core"]["opera_core"] == 80
    assert out["core"]["core_both"] == 30
    assert out["core"]["opera_confirms_vmi_core"] == pytest.approx(0.6, abs=1e-4)
    assert out["core"]["vmi_confirms_opera_core"] == pytest.approx(0.375, abs=1e-4)


def test_shape_mismatch_raises():
    with pytest.raises(ValueError):
        source_agreement(np.zeros((3, 3)), np.zeros((4, 4)))


def test_candidate_agreement_windows_and_margin():
    vmi = np.full((20, 20), 0.0)
    opera = np.full((20, 20), 0.0)
    vmi[5:10, 5:10] = 45.0
    opera[5:10, 5:10] = 45.0
    out = candidate_agreement(vmi, opera, 5, 9, 5, 9, margin_px=0)
    assert out["window"] == [5, 9, 5, 9]
    assert out["presence_class"] == "both"
    assert out["counts"]["both"] == 25
    # con margine la finestra si allarga e include celle a 0 dBZ (sotto soglia)
    out2 = candidate_agreement(vmi, opera, 5, 9, 5, 9, margin_px=2)
    assert out2["window"] == [3, 11, 3, 11]
    assert out2["counts"]["both"] == 25  # le celle aggiunte sono sotto soglia


def test_presence_mask_helper():
    arr = np.array([np.nan, 0.0, 20.0, 19.9, 50.0])
    mask = _presence(arr, 20.0)
    assert mask.tolist() == [False, False, True, False, True]


def test_parse_key_nominal_time():
    dt = parse_key_nominal_time(
        "2026/10/10/OPERA/COMP/OPERA@20261010T0645@0@DBZH.tiff")
    assert dt == _dt.datetime(2026, 10, 10, 6, 45, tzinfo=_dt.timezone.utc)
    with pytest.raises(ValueError):
        parse_key_nominal_time("not_a_key.tiff")
