#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test Fase 1.6 — Tracking Stabilization & Configuration Fix.

Verifica (P0) l'iniezione della configurazione scoring nel Tracker (nessun
mutable state globale), la cost function improved con motion prediction,
gating fisico configurabile, diagnostica per-match (opzionale, mai pubblica)
e il nuovo campo score_confidence."""

import copy

import pytest

from radar_engine import models
from radar_engine.config import CONFIG
from radar_engine.tracking import Tracker
from radar_engine.validation import idswitch

from conftest import make_cell


T0 = 1_777_027_200_000


def _scfg(weights):
    cfg = copy.deepcopy(CONFIG["scoring"])
    cfg["weights"] = dict(weights)
    return cfg


def _cfg(**over):
    cfg = dict(CONFIG["tracking"])
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------------------
# P0: configurazione scoring INIETTATA -> deve cambiare lo score
# ---------------------------------------------------------------------------
def test_config_scoring_injected_weights_change_score():
    cells = [make_cell(12.0 + 0.1 * i, 41.0, T0 + i * 300000,
                       cell_id=f"c{i}", area_km2=140, max_dbz=44.0)
             for i in range(5)]

    cfg_a = _scfg({"persistence": 1.0, "intensity": 0.0,
                   "intensity_consistency": 0.0, "spatial_coherence": 0.0,
                   "motion_consistency": 0.0, "growth_sustained": 0.0})
    cfg_b = _scfg({"persistence": 0.0, "intensity": 1.0,
                   "intensity_consistency": 0.0, "spatial_coherence": 0.0,
                   "motion_consistency": 0.0, "growth_sustained": 0.0})

    def run(scfg):
        tr = Tracker(_cfg(), cfg_scoring=scfg)
        for i, c in enumerate(cells):
            tr.update([c], i)
        tr.complete_cycles()
        tr.finalize()
        return tr.tracks[0].motion["organization_score"]

    score_a, score_b = run(cfg_a), run(cfg_b)
    assert score_a != score_b, \
        "Nessun effetto dei pesi scoring: configurazione NON iniettata"
    # lascia forte la dominance di ciascun peso scelto
    assert score_a > score_b  # persistence pura >> intensita' pura su 5 frame


def test_finalize_accepts_scoring_override():
    tr = Tracker(_cfg())
    for i in range(5):
        tr.update([make_cell(12.0 + 0.1 * i, 41.0, T0 + i * 300000,
                             cell_id=f"c{i}")], i)
    tr.complete_cycles()

    tr.finalize(cfg_scoring=_scfg({"persistence": 1.0, "intensity": 0.0,
                                   "intensity_consistency": 0.0,
                                   "spatial_coherence": 0.0,
                                   "motion_consistency": 0.0,
                                   "growth_sustained": 0.0}))
    s1 = tr.tracks[0].motion["organization_score"]
    tr.finalize(cfg_scoring=_scfg({"persistence": 0.0, "intensity": 1.0,
                                   "intensity_consistency": 0.0,
                                   "spatial_coherence": 0.0,
                                   "motion_consistency": 0.0,
                                   "growth_sustained": 0.0}))
    s2 = tr.tracks[0].motion["organization_score"]
    assert s1 != s2


# ---------------------------------------------------------------------------
# Improved formula + motion prediction (NON Kalman)
# ---------------------------------------------------------------------------
def _linear_motion_frames():
    # velocita' costante ~151 km/h verso est (passo 10 min ~25.2 km)
    f0 = make_cell(12.00, 41.0, T0, cell_id="seed", area_km2=150, max_dbz=42)
    f1 = make_cell(12.30, 41.0, T0 + 600000, cell_id="mid", area_km2=150, max_dbz=42)
    cont = make_cell(12.60, 41.0, T0 + 1200000, cell_id="continue",
                     area_km2=150, max_dbz=42)   # sul punto previsto
    lag = make_cell(12.295, 41.0, T0 + 1200000, cell_id="lag",
                    area_km2=150, max_dbz=42)    # "blob" stazionario ~0 km
    return [f0, f1], [cont, lag]


def test_prediction_prefers_continuation_over_proximity():
    frames, (cont, lag) = _linear_motion_frames()
    f0, f1 = frames

    legacy = Tracker(_cfg())  # default cost_formula=legacy, senza predizione
    for i, c in enumerate([f0, f1]):
        legacy.update([c], i)
    legacy.update([cont, lag], 2)
    legacy.complete_cycles()
    main_track = next(t for t in legacy.tracks if len(t.points) >= 3)
    # legacy (solo prossimita') aggancia il blob stazionario (d ~ 0)
    assert lag.cell_id in {p.cell_id for p in main_track.points}

    improved = Tracker(_cfg(cost_formula="improved", w_prediction=5.0,
                            w_overlap=1.0))
    for i, c in enumerate([f0, f1]):
        improved.update([c], i)
    improved.update([cont, lag], 2)
    improved.complete_cycles()
    main_imp = next(t for t in improved.tracks if len(t.points) >= 3)
    imp_ids = {p.cell_id for p in main_imp.points}
    # la predizione di moto deve vincere sulla prossimita' statica
    assert "continue" in imp_ids
    assert "lag" not in imp_ids


# ---------------------------------------------------------------------------
# Gating fisico (configurabile, MAI hardcodato)
# ---------------------------------------------------------------------------
def test_gating_rejects_implausible_speed():
    tr = Tracker(_cfg(max_storm_speed_kmh=10.0))
    tr.update([make_cell(12.0, 41.0, T0, cell_id="a")], 0)
    # 0.1 deg lon ~8.5 km in 5 min ~102 km/h >> gate 10 km/h
    tr.update([make_cell(12.10, 41.0, T0 + 300000, cell_id="b")], 1)
    tr.complete_cycles()
    t_dead, t_new = tr.tracks[0], tr.tracks[1]
    assert models.EVENT_DEATH in t_dead.events
    assert t_new.track_id != t_dead.track_id
    assert t_new.points[0].cell_id == "b"


def test_gating_disabled_with_none():
    tr = Tracker(_cfg(max_storm_speed_kmh=None))
    tr.update([make_cell(12.0, 41.0, T0, cell_id="a")], 0)
    tr.update([make_cell(12.10, 41.0, T0 + 300000, cell_id="b")], 1)
    tr.complete_cycles()
    assert len(tr.tracks) == 1
    assert len(tr.tracks[0].points) == 2  # nessun gate -> match


# ---------------------------------------------------------------------------
# Diagnostica per-match (Parte B, opzionale, mai nel dataset pubblico)
# ---------------------------------------------------------------------------
def test_diagnostics_enabled_records_statuses():
    tr = Tracker(_cfg(), diagnostics=True)
    tr.update([make_cell(12.0, 41.0, T0, cell_id="a")], 0)
    tr.update([make_cell(12.1, 41.0, T0 + 300000, cell_id="b")], 1)
    tr.complete_cycles()
    all_rows = [r for frame in tr.diag for r in frame["matches"]]
    statuses = {r["assignment_status"] for r in all_rows}
    assert "birth_start" not in statuses  # noise-proof naming
    assert {"matched", "new_track"} <= statuses
    m = next(r for r in all_rows if r["assignment_status"] == "matched")
    for key in ("track_id", "prev_cell_id", "current_cell_id", "distance_km",
                "area_ratio", "intensity_difference", "iou", "total_cost"):
        assert key in m


def test_diagnostics_off_by_default():
    tr = Tracker(_cfg())
    tr.update([make_cell(12.0, 41.0, T0, cell_id="a")], 0)
    tr.update([make_cell(12.1, 41.0, T0 + 300000, cell_id="b")], 1)
    assert tr.diag == []


def test_diagnostics_flags_merge_split_candidate():
    tr = Tracker(_cfg(max_assignment_cost=2000.0), diagnostics=True)
    tr.update([make_cell(12.0, 41.0, T0, cell_id="m1", area_km2=100),
               make_cell(12.15, 41.0, T0, cell_id="m2", area_km2=100)], 0)
    tr.update([make_cell(12.07, 41.0, T0 + 300000, cell_id="merged",
                         area_km2=190)], 1)
    tr.complete_cycles()
    rows = [r for f in tr.diag for r in f["matches"]]
    statuses = {r["assignment_status"] for r in rows}
    assert "ambiguous_merge_candidate" in statuses


# ---------------------------------------------------------------------------
# score_confidence (Parte F — separato, non confondere con organization_score)
# ---------------------------------------------------------------------------
def test_score_confidence_levels():
    def run(n_frames, dbz=40.0):
        tr = Tracker(_cfg())
        for i in range(n_frames):
            tr.update([make_cell(12.0 + 0.1 * i, 41.0, T0 + i * 300000,
                                 cell_id=f"c{i}", area_km2=120, max_dbz=dbz)], i)
        tr.complete_cycles()
        tr.finalize()
        return tr.tracks[0].score_confidence

    assert run(5, dbz=40.0) == "high"
    assert run(3, dbz=25.0) == "medium"
    assert run(2, dbz=40.0) == "low"


def test_score_confidence_in_output_dict():
    tr = Tracker(_cfg())
    for i in range(5):
        tr.update([make_cell(12.0 + 0.1 * i, 41.0, T0 + i * 300000,
                             cell_id=f"c{i}")], i)
    tr.complete_cycles()
    tr.finalize()
    assert tr.tracks[0].to_dict()["score_confidence"] == "high"


# ---------------------------------------------------------------------------
# ID-switch classification (Parte C)
# ---------------------------------------------------------------------------
def _tracker_result(tr, cells_by_frame):
    return {
        "cells_by_frame": cells_by_frame,
        "tracks": tr.tracks,
        "tracker": tr,
        "n_frames": len(cells_by_frame),
    }


def test_idswitch_classifies_disappearance():
    # gating molto basso forza la fine della track -> nuova identita' in situ
    tr = Tracker(_cfg(max_storm_speed_kmh=10.0), diagnostics=True)
    c0 = make_cell(12.0, 41.0, T0, cell_id="a", area_km2=120, max_dbz=42)
    c1 = make_cell(12.05, 41.0, T0 + 300000, cell_id="b", area_km2=120, max_dbz=42)
    tr.update([c0], 0)
    tr.update([c1], 1)
    tr.complete_cycles()
    res = _tracker_result(tr, [[c0], [c1]])
    clf = idswitch.classify_id_switches(res)
    assert clf["counts"]["disappearance"] >= 1
    assert clf["total"] >= 1
    statuses = {r["assignment_status"] for f in tr.diag for r in f["matches"]}
    assert "new_track" in statuses
    assert "terminated" in statuses


def test_idswitch_all_switches_classified():
    tr = Tracker(_cfg(), diagnostics=True)
    tr.update([make_cell(12.0, 41.0, T0, cell_id="a")], 0)
    tr.update([make_cell(12.1, 41.0, T0 + 300000, cell_id="b")], 1)
    tr.update([make_cell(12.2, 41.0, T0 + 600000, cell_id="c")], 2)
    tr.complete_cycles()
    res = _tracker_result(tr, [
        [make_cell(12.0, 41.0, T0, cell_id="a")],
        [make_cell(12.1, 41.0, T0 + 300000, cell_id="b")],
        [make_cell(12.2, 41.0, T0 + 600000, cell_id="c")],
    ])
    clf = idswitch.classify_id_switches(res)
    assert set(clf["counts"]) == set(idswitch.CLASSES)