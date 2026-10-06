#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test scoring.py: Organization Score 0-100, bande, assenza di claim supercell,
probabilità di classe deterministiche."""

import numpy as np
import pytest

from radar_engine import models
from radar_engine.scoring import organization_score, classify_band, soft_class_probs
from radar_engine.config import CONFIG

from conftest import make_cell, make_track


T0 = 1_777_027_200_000


def _track_stable(frames=5, dbz=42.0, area0=120.0, growth=0.0, step_km=0.1):
    lo = 12.0
    dlon = step_km / 85.0 * 0.1  # ~0.1 deg per step
    cells = []
    for i in range(frames):
        cells.append(make_cell(
            lo + i * dlon, 41.0, T0 + i * 300000,
            area_km2=area0 * (1 + growth * i / max(frames - 1, 1)),
            max_dbz=dbz + (0.5 if i % 2 else 0.0),
            solidity=0.95, compactness=3.0,
        ))
    return make_track(cells, 1)


def test_bands_boundaries():
    assert classify_band(10).name == "Weak Convective Cell"
    assert classify_band(30).name == "Weak Convective Cell"
    assert classify_band(31).name == "Convective Cell"
    assert classify_band(55).name == "Convective Cell"
    assert classify_band(56).name == "Organized Convective Cell"
    assert classify_band(75).name == "Organized Convective Cell"
    assert classify_band(76).name == "Highly Organized Convective Cell"
    assert classify_band(100).name == "Highly Organized Convective Cell"


def test_score_range_and_not_supercell():
    for frames in (2, 3, 6):
        score, label, probs, comp = organization_score(_track_stable(frames))
        assert 0 <= score <= 100
        assert "Supercell" not in label
        assert "supercell" not in label.lower()
        assert "hook" not in label.lower()
        assert "meso" not in label.lower()


def test_score_increases_with_strength():
    weak = _track_stable(frames=2, dbz=20.0, area0=40.0)
    strong = _track_stable(frames=7, dbz=55.0, area0=300.0, growth=0.6)
    s_weak, *_ = organization_score(weak)
    s_strong, *_ = organization_score(strong)
    assert s_weak < 30                        # Weak band
    assert s_strong > 75                      # Highly Organized band
    assert s_strong >= s_weak + 40


def test_weak_track_in_weak_band():
    score, label, probs, comp = organization_score(_track_stable(frames=2, dbz=20.0))
    assert score <= 30
    assert label == "Weak Convective Cell"


def test_growth_pushes_score_up():
    stable = _track_stable(frames=4, dbz=40.0, growth=0.0)
    growing = _track_stable(frames=4, dbz=40.0, growth=0.6)
    s1, _, _, c1 = organization_score(stable)
    s2, _, _, c2 = organization_score(growing)
    assert c2["growth_sustained"] > c1["growth_sustained"]
    assert s2 >= s1


def test_components_are_0_1():
    _, _, _, comp = organization_score(_track_stable(5))
    assert set(comp.keys()) == set(CONFIG["scoring"]["weights"].keys())
    for v in comp.values():
        assert 0.0 <= v <= 1.0


def test_class_probs_deterministic_and_normalized():
    p1 = soft_class_probs(50)
    p2 = soft_class_probs(50)
    assert p1 == p2
    from radar_engine.scoring import _Band
    names = [name for _, _, name in CONFIG["scoring"]["bands"]]
    assert set(p1.keys()) == set(names)
    assert abs(sum(p1.values()) - 1.0) < 1e-6


def test_no_score_for_single_point_track():
    cells = [make_cell(12.0, 41.0, T0)]
    track = make_track(cells, 1)
    score, label, probs, comp = organization_score(track)
    assert score == 0
    assert label == "Weak Convective Cell"
    assert probs == {}


def test_classification_label_matches_score_band():
    for frames, dbz in [(2, 20.0), (4, 30.0), (5, 40.0), (7, 52.0)]:
        score, label, _, _ = organization_score(_track_stable(frames=frames, dbz=dbz))
        assert classify_band(score).name == label


def test_no_hook_meso_rotation_terms_in_bands():
    """Fase 1: nessun termine supercell (hook/rotation/mesocyclone) nelle
    bande o nelle label di classificazione esposte."""
    from radar_engine.config import CONFIG
    labels = [name for _, _, name in CONFIG["scoring"]["bands"]]
    for lbl in labels:
        assert "hook" not in lbl.lower()
        assert "rotation" not in lbl.lower()
        assert "mesocyclone" not in lbl.lower()
        assert "supercell" not in lbl.lower()
    for score, track in [(2, _track_stable(frames=2, dbz=20.0)),
                         (4, _track_stable(frames=4, dbz=30.0)),
                         (5, _track_stable(frames=5, dbz=40.0)),
                         (7, _track_stable(frames=7, dbz=52.0))]:
        _, label, _, _ = organization_score(track)
        assert "supercell" not in label.lower()
        assert "hook" not in label.lower()
        assert "meso" not in label.lower()