# -*- coding: utf-8 -*-
"""Test A2 - vertical_structure.py (A1): VIL/ETM/POH/overhang.

Tutti i valori attesi sono quelli calcolati dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import numpy as np
import pytest

import vertical_structure as vs

N = 16


def _grids():
    """Griglie 16x16 di riferimento (verificate dallo scratch A2)."""
    vil = np.full((N, N), 50.0, dtype="float64")
    etm = np.full((N, N), 12.0, dtype="float64")
    poh = np.full((N, N), 70.0, dtype="float64")
    low = np.full((N, N), 10.0, dtype="float64")   # sotto soglia 40 dBZ
    high = np.full((N, N), 10.0, dtype="float64")
    high[4:12, 4:12] = 45.0                        # eco alta 64 px
    return vil, etm, poh, low, high


def test_structure_score_complete_products():
    vil, etm, poh, low, high = _grids()
    assert vs.structure_score(vil, etm, poh, low, high) == 100.0


def test_structure_score_vil_missing_lowers_to_65():
    vil, etm, poh, low, high = _grids()
    nan_grid = np.full((N, N), np.nan)
    assert vs.structure_score(nan_grid, etm, poh, low, high) == 65.0


def test_structure_score_three_missing_lowers_to_15():
    _, _, _, low, high = _grids()
    nan_grid = np.full((N, N), np.nan)
    assert vs.structure_score(nan_grid, nan_grid, nan_grid, low, high) == 15.0


def test_overhang_index_values():
    _, _, _, low, high = _grids()
    assert vs.overhang_index(low, high) == 1.0
    # nessun eco alta -> dato assente -> 0.0 (mai inventato)
    assert vs.overhang_index(low, low) == 0.0
    assert vs.overhang_index(low, np.full((N, N), np.nan)) == 0.0


def test_structure_features_exact_dict():
    vil, etm, poh, low, high = _grids()
    assert vs.structure_features(vil, etm, poh, low, high) == {
        "vil_max": 50.0,
        "etm_max": 12.0,
        "poh_max": 70.0,
        "overhang": 1.0,
        "density_max": None,
        "m_vil": 1.0,
        "m_etm": 1.0,
        "m_poh": 1.0,
        "m_overhang": 1.0,
    }


def test_poh_etm_scalars_all_nan():
    nan_grid = np.full((N, N), np.nan)
    assert vs.poh_etm_scalars(nan_grid, nan_grid) == {
        "poh_max": None,
        "poh_mean": None,
        "etm_max": None,
        "etm_mean": None,
        "n_valid": 0,
    }


def test_vil_density_valid_and_invalid_etm():
    vil = np.full((N, N), 50.0)
    assert float(vs.vil_density(vil, np.full((N, N), 10.0))[0, 0]) == 5.0
    assert float(vs.vil_density(vil, np.zeros((N, N)))[0, 0]) == 0.0


def test_shape_mismatch_raises():
    vil, etm, poh, low, high = _grids()
    with pytest.raises(ValueError, match="shape_mismatch:grid_1"):
        vs.structure_score(vil, etm[:, :8], poh, low, high)


def test_weights_sum_gt_1_raises():
    vil, etm, poh, low, high = _grids()
    with pytest.raises(ValueError, match="weights_sum_gt_1"):
        vs.structure_score(vil, etm, poh, low, high,
                           weights={"vil": 2.0, "etm": 0.1, "poh": 0.1,
                                    "overhang": 0.1})
