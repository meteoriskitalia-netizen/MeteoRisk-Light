# -*- coding: utf-8 -*-
"""Test A2 - overshoot.py (A1): BTD WV-IR, flag OT, score OT.

Il layer OT del run NON e' attivo in A2 (config dn_to_kelvin=None: il PNG
EUMETView grayscale non e' calibrato); qui si testano le funzioni PURE su
griglie Kelvin sintetiche. Valori attesi dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import numpy as np
import pytest

import overshoot as ots


def _grids():
    """11x11 con blocco 5x5 freddo (BTD 15 K, IR 210 K)."""
    ir = np.full((11, 11), 250.0)
    wv = np.full((11, 11), 240.0)
    ir[3:8, 3:8] = 210.0
    wv[3:8, 3:8] = 225.0
    return wv, ir


def test_btd_wv_minus_ir():
    wv, ir = _grids()
    btd = ots.btd_wv_ir(wv, ir)
    assert btd[0, 0] == -10.0
    assert np.unique(btd[3:8, 3:8]).tolist() == [15.0]


def test_ot_flag_erosion_and_kernel_override():
    wv, ir = _grids()
    btd = ots.btd_wv_ir(wv, ir)
    flags = ots.ot_flag(btd, ir)
    assert int(flags.sum()) == 9            # 5x5 eroso a 3x3
    flags_no_erosion = ots.ot_flag(btd, ir, kernel=1)
    assert int(flags_no_erosion.sum()) == 25


def test_ot_score_from_flags():
    wv, ir = _grids()
    btd = ots.btd_wv_ir(wv, ir)
    flags = ots.ot_flag(btd, ir)
    assert ots.ot_score_from_flags(flags) == 100.0      # 9 km2 -> pieno merito
    four = np.pad(np.ones((2, 2), dtype=bool), 1)
    assert ots.ot_score_from_flags(four) == 50.0        # 4 km2 -> 0.5
    two = np.array([[True, True]], dtype=bool)
    assert ots.ot_score_from_flags(two) == 16.7         # 2 km2 -> rampa
    assert ots.ot_score_from_flags(np.zeros((4, 4), bool)) == 0.0


def test_nan_cells_are_not_flags():
    wv, ir = _grids()
    ir_nan = ir.copy()
    ir_nan[0, 0] = np.nan
    btd = ots.btd_wv_ir(wv, ir_nan)
    assert np.isnan(btd[0, 0])
    assert bool(ots.ot_flag(btd, ir_nan)[0, 0]) is False


def test_shape_mismatch_raises():
    wv, ir = _grids()
    btd = ots.btd_wv_ir(wv, ir)
    with pytest.raises(ValueError, match="shape_mismatch:ir_grid"):
        ots.btd_wv_ir(wv, ir[:, :8])
    with pytest.raises(ValueError, match="shape_mismatch:ir_grid"):
        ots.ot_flag(btd, ir[:, :8])
