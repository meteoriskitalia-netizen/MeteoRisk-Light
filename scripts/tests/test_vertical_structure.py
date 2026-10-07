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


# ---------------------------------------------------------------------------
# B2 (PHASE2_VERSION 0.4.0): unita' dei prodotti al ritaglio per-candidato
# Valori verificati eseguendo vertical_structure.py reale
# (scratch_b2_values.py, radarvenv Python 3.13).
# ---------------------------------------------------------------------------

def test_etm_to_km_and_poh_to_percent_pure_conversion():
    etm_m = np.array([[2000.0, 9000.0, 12500.0]])
    poh_f = np.array([[0.0, 0.35, 0.72]])
    etm_src = etm_m.copy()
    poh_src = poh_f.copy()
    assert np.allclose(vs.etm_to_km(etm_m), [[2.0, 9.0, 12.5]])
    assert np.allclose(vs.poh_to_percent(poh_f), [[0.0, 35.0, 72.0]])
    # nessuna modifica in-place del prodotto originale
    assert np.array_equal(etm_m, etm_src)
    assert np.array_equal(poh_f, poh_src)
    # NaN preservati, dtype intero accettato
    assert np.isnan(vs.etm_to_km(np.array([[np.nan]]))[0, 0])
    assert np.isnan(vs.poh_to_percent(np.array([[np.nan]]))[0, 0])
    assert vs.etm_to_km(np.array([[9000]]))[0, 0] == 9.0


def test_structure_features_units_conversion_matches_native_units():
    vil = np.array([[20.0, 20.0, 20.0]])
    etm_m = np.array([[2000.0, 9000.0, 12500.0]])
    poh_f = np.array([[0.0, 0.35, 0.72]])
    nan = np.full((1, 3), np.nan)
    converted = vs.structure_features(vil, vs.etm_to_km(etm_m),
                                      vs.poh_to_percent(poh_f), nan, nan)
    native = vs.structure_features(vil, np.array([[2.0, 9.0, 12.5]]),
                                   np.array([[0.0, 35.0, 72.0]]), nan, nan)
    assert converted == native
    assert converted["etm_max"] == 12.5
    assert converted["poh_max"] == 72.0


def test_structure_score_conversion_is_not_optional():
    # ETM in METRI senza conversione: le soglie 6/9/12 km vedono valori 1000x
    vil = np.array([[20.0, 20.0, 20.0]])
    etm_m = np.array([[2000.0, 9000.0, 12500.0]])
    poh_f = np.array([[0.0, 0.35, 0.72]])
    nan = np.full((1, 3), np.nan)
    assert vs.structure_score(vil, etm_m, poh_f, nan, nan) == 25.0
    assert vs.structure_score(vil, vs.etm_to_km(etm_m),
                              vs.poh_to_percent(poh_f), nan, nan) == 50.0
