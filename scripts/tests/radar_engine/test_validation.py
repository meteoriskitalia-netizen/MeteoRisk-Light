#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test del package radar_engine.validation (Fase 1.5) — solo raster/test sintetici."""
import json
import os

import pytest

from conftest import make_cell, make_track
from radar_engine import models
from radar_engine.validation import cases, metrics
from radar_engine.validation import calibration as cal


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


def test_cases_load_metadata():
    cs = cases.load_cases(PROJECT_ROOT)
    assert len(cs) >= 3
    for c in cs:
        assert c["type"] in cases.EVENT_TYPES
    assert all("reference_sources" in c for c in cs)


def test_cases_reject_bad_type():
    with pytest.raises(ValueError):
        cases._validate_case({
            "id": "x", "name": "x", "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:05:00Z", "type": "SUPERCELL_AUTO",
            "region": "IT", "description": "", "reference_sources": []})


def test_cases_documented_supercell_requires_source():
    with pytest.raises(ValueError):
        cases._validate_case({
            "id": "x", "name": "x", "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-01T00:05:00Z", "type": "DOCUMENTED_SUPERCELL",
            "region": "IT", "description": "", "reference_sources": []})


def test_confusion_and_rates(cell_factory):
    ref = [cell_factory(11.0, 44.0, 1000, cell_id="r1"),
           cell_factory(12.5, 44.2, 1000, cell_id="r2")]
    cand_same = [cell_factory(11.05, 44.02, 1000, cell_id="c1"),
                 cell_factory(12.45, 44.18, 1000, cell_id="c2")]
    conf = metrics.confusion(ref, cand_same, radius_km=25.0)
    assert (conf["tp"], conf["fp"], conf["fn"]) == (2, 0, 0)
    assert metrics.detection_rate(conf) == 1.0
    assert metrics.false_positive_rate(conf) == 0.0

    cand_extra = cand_same + [cell_factory(9.0, 40.0, 1000, cell_id="c3")]
    conf2 = metrics.confusion(ref, cand_extra)
    assert conf2["fp"] == 1
    assert metrics.false_positive_rate(conf2) == pytest.approx(1 / 3, abs=1e-6)

    cand_few = [cell_factory(11.05, 44.02, 1000, cell_id="c1")]
    conf3 = metrics.confusion(ref, cand_few)
    assert conf3["fn"] == 1
    assert metrics.false_negative_rate(conf3) == pytest.approx(0.5, abs=1e-6)


def test_score_stats_and_false_high(cell_factory, track_factory):
    low = track_factory([cell_factory(11, 44, 1000, cell_id="a1",
                                       max_dbz=30),
                         cell_factory(11.05, 44.02, 300000, cell_id="a2",
                                      max_dbz=31)], track_id=1)
    high_short = track_factory([cell_factory(12, 44, 1000, cell_id="b1",
                                              max_dbz=45),
                                cell_factory(12.06, 44.03, 300000, cell_id="b2",
                                             max_dbz=46)], track_id=2)
    for t, score in ((low, 40.0), (high_short, 60.0)):
        t.motion["organization_score"] = score
        t.motion["classification"] = "Convective Cell" if score < 56 else \
            "Organized Convective Cell"
    stats = metrics.score_stats([low, high_short])
    assert stats["n"] == 2
    assert stats["min"] == 40 and stats["max"] == 60
    bad = metrics.false_high_scores([high_short])
    assert len(bad) == 1 and bad[0]["n_frames"] == 2


def test_calibration_deep_set():
    base = {"a": {"b": {"c": 1.0}}, "x": 2.0}
    cal._deep_set(base, ["a", "b", "c"], 9.0)
    assert base["a"]["b"]["c"] == 9.0


def test_grid_search_shapes():
    items = [(str(i), i * 1000) for i in range(4)]
    rows = cal.grid_search(items, [(["tracking", "w_distance"], [1.0, 2.0]),
                                   (["tracking", "max_assignment_cost"], [400.0])])
    # percorso inesistente -> raster scartati, ma le 2 combinazioni escono
    assert len(rows) == 2
    assert all("combo" in r for r in rows)
    assert rows[0]["combo"]["tracking.w_distance"] == 1.0


def test_report_summary_atomic(tmp_path):
    from radar_engine.validation import report
    out = tmp_path / "summary.json"
    report.write_summary(str(out), {"k": 1})
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["k"] == 1
    assert data["phase"] == "1.5"
    assert not os.path.exists(str(out) + ".tmp")