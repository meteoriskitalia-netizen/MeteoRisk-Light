#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test supercell.py: Supercell Signature Index (SSI) 0-100, gate di intensità,
livelli, phase birth/sustained, output solo candidati, data_limits esplicita."""

import json

import pytest

from radar_engine import models
from radar_engine import output
from radar_engine import supercell as sc_mod
from radar_engine.config import CONFIG

from conftest import make_cell, make_track


T0 = 1_777_027_200_000


def _make_tracked(frames=5, dbz=52.0, mean_dbz=48.0, area0=200.0, growth=0.4,
                  solidity=0.92, compactness=2.5, org=90, track_id=1):
    """Track con motion (Organization Score) coerente con la pipeline Fase 1."""
    lo = 12.0
    dlon = 0.01
    cells = []
    for i in range(frames):
        area = area0 * (1 + growth * i / max(frames - 1, 1))
        cells.append(make_cell(
            lo + i * dlon, 41.0, T0 + i * 300000,
            area_km2=area, max_dbz=dbz, mean_dbz=mean_dbz, p90_dbz=mean_dbz,
            ecc=0.5, solidity=solidity, compactness=compactness,
            frame_index=i, cell_id="c-{}-{}".format(track_id, i)))
    track = make_track(cells, track_id)
    track.motion = {
        "_avg_solidity": solidity,
        "_avg_compactness": compactness,
        "organization_score": org,
        "classification": "Organized Convective Cell",
        "velocity_kmh": 45.0,
        "direction_toward_deg": 90.0,
        "distance_km": 5.0,
        "duration_min": 5.0 * (frames - 1),
        "area_growth_pct": round(growth * 100, 1),
        "intensity_delta_dbz": 0.0,
    }
    return track


def test_ssi_range_and_components_bounds():
    for frames, dbz in [(2, 22.0), (5, 40.0), (7, 55.0)]:
        ssi, level, candidate, gates, comp = sc_mod.supercell_signature_index(
            _make_tracked(frames=frames, dbz=dbz))
        assert 0 <= ssi <= 100
        assert set(comp.keys()) == set(CONFIG["supercell"]["weights"].keys())
        for v in comp.values():
            assert 0.0 <= v <= 1.0
        assert isinstance(candidate, bool)
        assert set(gates.keys()) == {"intensity_core_dbz", "min_frames",
                                     "organization_organized"}


def test_weights_sum_to_one():
    assert abs(sum(CONFIG["supercell"]["weights"].values()) - 1.0) < 1e-9


def test_weak_track_not_candidate():
    ssi, level, candidate, gates, comp = sc_mod.supercell_signature_index(
        _make_tracked(frames=2, dbz=22.0, mean_dbz=20.0, area0=60.0, org=15))
    assert ssi < 50
    assert level == "non_supercell"
    assert candidate is False
    assert gates["intensity_core_dbz"] is False


def test_intensity_gate_blocks_claim():
    """SSI alto (-> 'possible'/'marked') ma core <45 dBZ: candidate=False."""
    ssi, level, candidate, gates, _comp = sc_mod.supercell_signature_index(
        _make_tracked(frames=7, dbz=35.0, mean_dbz=32.0, area0=200.0,
                      growth=0.5, org=100))
    assert ssi >= 65
    assert level in ("possible", "marked")
    assert gates["intensity_core_dbz"] is False
    assert candidate is False


def test_strong_track_is_candidate():
    ssi, level, candidate, gates, _comp = sc_mod.supercell_signature_index(
        _make_tracked(frames=7, dbz=55.0, mean_dbz=52.0, area0=250.0,
                      growth=0.6, org=90))
    assert ssi >= 80
    assert level == "marked"
    assert gates["intensity_core_dbz"] is True
    assert gates["min_frames"] is True
    assert candidate is True


def test_phase_birth_vs_sustained():
    birth = sc_mod.supercell_signature_index(
        _make_tracked(frames=2, dbz=52.0, org=88))
    assert birth[1] in ("possible", "marked")
    blevel = birth[1]
    # birth: <= birth_frames (3) -> 'birth'
    assert birth[2] is True
    sustained = sc_mod.supercell_signature_index(
        _make_tracked(frames=7, dbz=52.0, org=88))
    assert sustained[2] is True

    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.frames = []
    b.cells_by_frame = [[]]
    t_b = _make_tracked(frames=2, dbz=52.0, org=88)
    t_s = _make_tracked(frames=7, dbz=55.0, org=92, track_id=2)
    b.tracks = [t_b, t_s]
    sc_mod.evaluate(b, CONFIG["supercell"])
    phases = {c["track_id"]: c["phase"] for c in b.supercells}
    assert phases[1] == "birth"
    assert phases[2] == "sustained"
    _ = blevel  # livello già coperto sopra


def test_evaluate_emits_only_candidates():
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.frames = []
    b.cells_by_frame = [[make_cell(12.0, 41.0, T0)], [make_cell(12.01, 41.0, T0 + 300000)]]
    strong = _make_tracked(frames=2, dbz=54.0, org=90)
    weak = _make_tracked(frames=2, dbz=24.0, org=10, track_id=2)
    b.tracks = [strong, weak]
    sc_mod.evaluate(b, CONFIG["supercell"])
    assert len(b.supercells) == 1
    cand = b.supercells[0]
    assert cand["track_id"] == 1
    assert cand["candidate"] is True
    assert cand["track_type"] == "cell"
    assert cand["on_latest_frame"] is True
    for key in ("supercell_id", "ssi", "level", "phase", "status", "position",
                "intensity", "organization", "motion", "components", "gates",
                "confidence", "first_seen", "last_seen"):
        assert key in cand
    assert cand["confidence"] == "low"
    assert b.supercell_tracks_evaluated == 2


def test_storm_object_tracks_evaluated():
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.frames = []
    b.cells_by_frame = [[]]
    # storm track con la stessa interfaccia (points + motion) del layer cella:
    # evaluate è generica sul tipo di track.
    strong = _make_tracked(frames=4, dbz=54.0, org=90)
    b.storm_tracks = [strong]
    sc_mod.evaluate(b, CONFIG["supercell"])
    assert len(b.supercells) == 1
    assert b.supercells[0]["track_type"] == "storm_object"


def test_data_limits_note_is_explicit():
    assert "Doppler" in sc_mod.DATA_LIMITS_NOTE
    assert "riflettività" in sc_mod.DATA_LIMITS_NOTE


def test_supercells_json_output(tmp_path):
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = "2026-04-30T10:00:00Z"
    b.frames = []
    b.cells_by_frame = [[], []]
    b.tracks = [_make_tracked(frames=2, dbz=54.0, org=90)]
    sc_mod.evaluate(b, CONFIG["supercell"])
    out = str(tmp_path / "radar")
    paths = output.write_outputs(b, out, engine_meta={"phase": 1})
    assert "supercells.json" in paths
    with open(paths["supercells.json"], encoding="utf-8") as fh:
        payload = json.load(fh)
    assert isinstance(payload["candidates"], list)
    assert len(payload["candidates"]) == 1
    assert payload["summary"]["candidates"] == 1
    assert payload["data_limits"] == sc_mod.DATA_LIMITS_NOTE
    with open(paths["latest.json"], encoding="utf-8") as fh:
        latest = json.load(fh)
    assert latest["supercell_candidates"] == 1
    assert output.validate_outputs(out) == []


def test_supercells_empty_bundle_ok(tmp_path):
    import os as _os
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.frames = []
    b.cells_by_frame = []
    b.tracks = []
    sc_mod.evaluate(b, CONFIG["supercell"])
    out = str(tmp_path / "radar")
    output.write_outputs(b, out)
    with open(_os.path.join(out, "supercells.json"), encoding="utf-8") as fh:
        payload = json.load(fh)
    assert payload["candidates"] == []
    assert payload["summary"]["candidates"] == 0