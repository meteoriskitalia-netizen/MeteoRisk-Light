# -*- coding: utf-8 -*-
"""Test A2 - hook.py (A1): morfologia uncino, score, persistenza.

Tutti i valori attesi sono quelli calcolati dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import numpy as np
import pytest

import hook


def _blank(n=64, bg=5.0):
    return np.full((n, n), bg, dtype="float64")


def _disk(n=64, r=8.0):
    yy, xx = np.mgrid[0:n, 0:n]
    return ((yy - n // 2) ** 2 + (xx - n // 2) ** 2) <= r * r


def _pacman(n=64, r=12.0, mouth_deg=100.0):
    yy, xx = np.mgrid[0:n, 0:n]
    dy = yy - n // 2
    dx = xx - n // 2
    rad = np.hypot(dy, dx)
    ang = np.degrees(np.arctan2(dy, dx))
    half = mouth_deg / 2.0
    return (rad <= r) & (np.abs(ang) > half)


def _annulus(n=64, r_in=6.0, r_out=11.0, a0=60.0, a1=300.0):
    yy, xx = np.mgrid[0:n, 0:n]
    dy = yy - n // 2
    dx = xx - n // 2
    rad = np.hypot(dy, dx)
    ang = np.degrees(np.arctan2(dy, dx)) % 360.0
    return (rad >= r_in) & (rad <= r_out) & (ang >= a0) & (ang <= a1)


def _features(mask):
    g = _blank()
    g[mask] = 55.0
    return hook.compute_hook_features(g, None)


# Dict gate verbatim dallo scratch A2 (input di hook_score_from_features).
GATE = {"found": True, "n_components": 1, "dbz_max": 55.0,
        "core_area_px": 100, "bay_depth_px": 9.0, "bay_arc_px": 30.0,
        "bay_wrap_deg": 200.0, "winding_deg": 360.0,
        "concavity_ratio": 0.7, "breaks": 0, "hook_candidate": True}


def test_filter_persistence_verified_values():
    assert hook.filter_persistence(60, [40, 50]) == 53.3
    assert hook.filter_persistence(70, []) == 70.0
    assert hook.filter_persistence(float("nan"), [50]) == 0.0


def test_score_from_features_verified_values():
    assert hook.hook_score_from_features(GATE) == 72.6
    # bay_wrap 100 < gate 150 -> 0.0
    assert hook.hook_score_from_features(dict(GATE, bay_wrap_deg=100.0)) == 0.0
    # core_area 10 < gate 25 -> 0.0
    assert hook.hook_score_from_features(dict(GATE, core_area_px=10)) == 0.0
    assert hook.hook_score_from_features(dict(GATE, found=False)) == 0.0
    # gate minimi proprio sulla soglia (conc=1.0, wrap=150, arc=8, dbz=45)
    minimal = dict(GATE, concavity_ratio=1.0, bay_wrap_deg=150.0,
                   bay_arc_px=8.0, dbz_max=45.0)
    assert hook.hook_score_from_features(minimal) == 13.1


def test_disk_is_not_a_hook():
    f = _features(_disk())
    assert f["core_area_px"] == 197
    assert f["n_components"] == 1
    assert f["dbz_max"] == 55.0
    assert f["bay_depth_px"] == 0.63
    assert f["bay_arc_px"] == 0.0
    assert f["bay_wrap_deg"] == 0.0
    assert f["winding_deg"] == 225.0
    assert f["concavity_ratio"] == 1.0
    assert f["breaks"] == 3
    assert f["hook_candidate"] is False
    assert hook.hook_score_from_features(f) == 0.0


def test_pacman_wide_mouth_fails_wrap_gate():
    f = _features(_pacman(mouth_deg=60.0))
    assert f["core_area_px"] == 366
    assert f["bay_depth_px"] == 10.61
    assert f["bay_arc_px"] == 12.49
    assert f["bay_wrap_deg"] == 126.52
    assert f["concavity_ratio"] == 0.8927
    assert f["hook_candidate"] is False
    assert hook.hook_score_from_features(f) == 0.0


def test_annulus_pos_geometry_candidate_and_score():
    f = _features(_annulus(r_in=4.0, r_out=12.0, a0=45.0, a1=315.0))
    assert f["core_area_px"] == 303
    assert f["bay_depth_px"] == 7.76
    assert f["bay_arc_px"] == 17.66
    assert f["bay_wrap_deg"] == 275.4
    assert f["concavity_ratio"] == 0.7932
    assert f["hook_candidate"] is True
    assert hook.hook_score_from_features(f) == 69.6


def test_annulus_second_geometry_candidate_and_score():
    f = _features(_annulus(r_in=5.0, r_out=13.0, a0=45.0, a1=315.0))
    assert f["core_area_px"] == 351
    assert f["bay_depth_px"] == 7.84
    assert f["bay_arc_px"] == 23.31
    assert f["bay_wrap_deg"] == 289.7
    assert f["concavity_ratio"] == 0.75  # valore reale runtime (0.7758 era un refuso del riepilogo)
    assert f["hook_candidate"] is True
    assert hook.hook_score_from_features(f) == 80.5


def test_tiny_core_below_area_gate():
    tiny = np.zeros((64, 64), dtype=bool)
    tiny[30:34, 30:34] = True
    f = _features(tiny)
    assert f["core_area_px"] == 16
    assert f["hook_candidate"] is False
    assert hook.hook_score_from_features(f) == 0.0


def test_no_core_returns_empty_features():
    f = hook.compute_hook_features(_blank(), None)
    assert f["found"] is False
    assert hook.hook_score_from_features(f) == 0.0


def test_malformed_input_raises():
    with pytest.raises(ValueError, match="grid_2d_required"):
        hook.compute_hook_features(np.zeros((4, 4, 4)), None)
    with pytest.raises(ValueError, match="shape_mismatch:mask_dbz"):
        hook.compute_hook_features(_blank(), np.zeros((8, 8), dtype=bool))


def test_determinism_double_run():
    m = _annulus(r_in=4.0, r_out=12.0, a0=45.0, a1=315.0)
    assert _features(m) == _features(m)
