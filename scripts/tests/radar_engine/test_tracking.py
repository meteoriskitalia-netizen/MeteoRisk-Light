#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test tracking.py: cost function, Hungarian assignment, birth/death, eventi
'ambiguous' (merge/split candidato), motion con timestamp reali (mai clock)."""

import numpy as np
import pytest

from radar_engine import models
from radar_engine.tracking import Tracker, cell_cost, haversine_km
from radar_engine.config import CONFIG

from conftest import make_cell


T0 = 1_777_027_200_000  # base fissa (2026-04-30, deterministico)


def _cfg(**over):
    cfg = dict(CONFIG["tracking"])
    cfg.update(over)
    return cfg


def _id(cell):
    return cell.cell_id


def test_cost_prefers_closer_stronger_match():
    prev = make_cell(12.0, 41.0, T0, area_km2=150.0, max_dbz=40.0)
    far = make_cell(13.2, 41.0, T0, area_km2=150.0, max_dbz=40.0, cell_id="far")
    near = make_cell(12.05, 41.0, T0, area_km2=150.0, max_dbz=40.0, cell_id="near")
    cfg = _cfg()
    assert cell_cost(prev, near, cfg) < cell_cost(prev, far, cfg)


def test_simple_track_id_persists_and_motion():
    tr = Tracker(_cfg())
    c1 = make_cell(12.0, 41.0, T0, area_km2=120, max_dbz=42)
    c2 = make_cell(12.10, 41.0, T0 + 300000, area_km2=120, max_dbz=42)
    c3 = make_cell(12.20, 41.0, T0 + 600000, area_km2=120, max_dbz=42)
    tr.update([c1], 0)
    tr.update([c2], 1)
    tr.update([c3], 2)
    tr.complete_cycles()
    tr.finalize()
    track = tr.tracks[0]
    assert len(track.points) == 3
    assert track.track_id == 1
    assert track.events == [models.EVENT_BIRTH]
    # distanza ~0.10 deg lon a 41°N ~8.5 km per 5 minuto -> ~102 km/h
    vel = track.motion["velocity_kmh"]
    assert 70 <= vel <= 130
    assert 70 <= track.motion["direction_toward_deg"] <= 110
    assert track.motion["duration_min"] == pytest.approx(10.0, abs=0.1)
    assert track.motion["distance_km"] > 10.0
    assert track.tracking_confidence == "low"   # 3 frame < 5


def test_track_terminates_when_not_observed():
    tr = Tracker(_cfg())
    tr.update([make_cell(12.0, 41.0, T0)], 0)
    tr.update([make_cell(12.1, 41.0, T0 + 300000)], 1)
    # frame 3 vuoto -> track muore, nuova cella nasce come track separata
    tr.update([], 2)
    tr.update([make_cell(13.0, 43.0, T0 + 900000, cell_id="new")], 3)
    tr.complete_cycles()
    t_dead, t_new = tr.tracks[0], tr.tracks[1]
    assert t_dead.status == models.STATUS_DEAD
    assert models.EVENT_DEATH in t_dead.events
    assert len(t_dead.points) == 2
    assert t_new.track_id != t_dead.track_id
    assert len(t_new.points) == 1


def test_five_frames_full_confidence():
    tr = Tracker(_cfg())
    for i in range(5):
        lon = 12.0 + 0.1 * i
        tr.update([make_cell(lon, 41.0, T0 + i * 300000, cell_id=f"c{i}")], i)
    tr.complete_cycles()
    tr.finalize()
    assert tr.tracks[0].tracking_confidence == "full"


def test_merge_candidate_flagged_ambiguous():
    tr = Tracker(_cfg())
    # due celle in f1 (due track), convergono in f2, in f3 una sola cella:
    # entrambe le track hanno costo basso -> candidato merge -> ambiguous.
    a1 = make_cell(12.0, 41.0, T0, area_km2=100, max_dbz=40)
    b1 = make_cell(12.15, 41.0, T0, area_km2=100, max_dbz=40)
    a2 = make_cell(12.05, 41.0, T0 + 300000, area_km2=100, max_dbz=40)
    b2 = make_cell(12.10, 41.0, T0 + 300000, area_km2=100, max_dbz=40)
    merged = make_cell(12.075, 41.0, T0 + 600000, area_km2=180, max_dbz=40)
    tr.update([a1, b1], 0)
    tr.update([a2, b2], 1)
    tr.update([merged], 2)
    tr.complete_cycles()
    amb = [t for t in tr.tracks if models.EVENT_AMBIGUOUS in t.events]
    dead = [t for t in tr.tracks if models.EVENT_DEATH in t.events]
    assert len(amb) >= 1
    assert len(dead) >= 1


def test_split_candidate_flagged_ambiguous():
    tr = Tracker(_cfg())
    one = make_cell(12.0, 41.0, T0, area_km2=200, max_dbz=40)
    two_a = make_cell(11.98, 41.0, T0 + 300000, area_km2=110, max_dbz=40)
    two_b = make_cell(12.03, 41.0, T0 + 300000, area_km2=110, max_dbz=40)
    tr.update([one], 0)
    tr.update([two_a, two_b], 1)
    tr.complete_cycles()
    flagged = [t for t in tr.tracks if models.EVENT_AMBIGUOUS in t.events]
    # la track originale è ambigua (2 celle candidate = split) + born new track
    assert len(flagged) == 1
    assert len(tr.tracks) == 2


def test_none_objection_to_empty_first_frame():
    tr = Tracker(_cfg())
    tr.update([], 0)
    tr.update([make_cell(12.0, 41.0, T0 + 300000)], 1)
    tr.update([make_cell(12.1, 41.0, T0 + 600000)], 2)
    tr.complete_cycles()
    assert len(tr.tracks) == 1
    assert len(tr.tracks[0].points) == 2


def test_haversine_and_velocity_real_timestamps():
    # 0.1 deg lon a 41°N ~ 8.5 km; passo 5 min
    d = haversine_km((12.0, 41.0), (12.1, 41.0))
    assert 7.0 <= d <= 10.0


def test_trackpoint_to_dict_serializes_morphology():
    cell = make_cell(12.0, 41.0, T0, ecc=0.9, solidity=0.8, compactness=4.0)
    pt = models.TrackPoint(1, 0, cell)
    d = pt.to_dict()
    assert d["eccentricity"] == 0.9
    assert d["solidity"] == 0.8
    assert d["compactness"] == 4.0


def test_ambiguous_tags_not_in_json_output(tmp_path):
    # smoke: un track con ambiguous serializza correttamente a Dict
    tr = Tracker(_cfg())
    tr.update([make_cell(12.0, 41.0, T0)], 0)
    tr.update([make_cell(12.1, 41.0, T0 + 300000)], 1)
    tr.complete_cycles()
    d = tr.tracks[0].to_dict()
    assert d["n_frames"] == 2
    assert d["events"] == [models.EVENT_BIRTH]