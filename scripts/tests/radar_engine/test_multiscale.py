#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk — test Fase 1.7 (Multi-Scale Storm Object Tracking).

9 scenari sintetici multiscala richiesti:
    1. cella isolata                 2. due celle separate
    3. due celle vicine              4. merge
    5. split                         6. cluster denso
    7. crescita rapida               8. cella che scompare
    9. storm object persistente con celle interne variabili

+ unit test: metodi di aggregazione (distance_cc/dbscan/dilation_overlap),
  DBSCAN reference, convex hull, ambiguity_class, output storm files.
"""

import json
import os

import pytest

from radar_engine import models
from radar_engine import output
from radar_engine import aggregation as agg
from radar_engine import storm_tracking as storm_mod
from radar_engine.config import CONFIG

from conftest import make_cell

T0 = 1_777_027_200_000
STEP_MS = 300_000  # 5 min VMI


def _cfg():
    return CONFIG["storm"]


def _cells_frame(lons_lats, frame_idx, ts_ms, area=120.0, max_dbz=42.0):
    """Costruisce una lista di celle da (lon, lat)."""
    cells = []
    for i, (lon, lat) in enumerate(lons_lats):
        cells.append(make_cell(lon, lat, ts_ms, area_km2=area, max_dbz=max_dbz,
                               frame_index=frame_idx,
                               cell_id=f"c{frame_idx}-{i}"))
    return cells


def _run_storm_pipeline(series):
    """Esegue aggregazione + StormObjectTracker su una serie di celle/frame.

    series: list[(frame_idx, ts_ms, lons_lats)] -> (storm_objects_by_frame,
            storm_tracks)."""
    cfg = _cfg()
    objs_by_frame = []
    for (fidx, ts, lons_lats) in series:
        cells = _cells_frame(lons_lats, fidx, ts)
        objs = agg.aggregate_frame(cells, fidx, cfg)
        objs_by_frame.append(objs)
    tracker = storm_mod.StormObjectTracker(cfg)
    for fidx, objs in enumerate(objs_by_frame):
        tracker.update(objs, fidx)
    tracker.complete_cycles()
    tracks = tracker.finalize()
    return objs_by_frame, tracks


def _single_moving_lons(n_frames, start_lon=10.0, step_lon=0.06):
    """Serie di una cella che si muove costantemente verso est (~5 km/step)."""
    series = []
    for f in range(n_frames):
        series.append((f, T0 + f * STEP_MS, [(start_lon + f * step_lon, 41.0)]))
    return series


# ---------------------------------------------------------------------------
# Unità: aggregazione / DBSCAN / hull / ambiguity
# ---------------------------------------------------------------------------
def test_aggregation_methods_three():
    cfg = _cfg()["aggregation"]
    cells = _cells_frame([(10.0, 41.0), (10.05, 41.0), (10.9, 41.0)], 0, T0)
    dc = agg.distance_cc_clusters(cells, cfg["dbscan_eps_km"])
    db = agg.dbscan_clusters(cells, cfg["dbscan_eps_km"], cfg["dbscan_min_cells"])
    do = agg.dilation_overlap_clusters(cells, cfg["merge_gap_km"], cfg["dilate_km"])
    assert [len(g) for g in do] == [2, 1]      # due vicine unite, una separata
    assert [len(g) for g in db] == [2]         # DBSCAN: cella isolata = rumore
    assert [len(g) for g in dc] == [2, 1]      # distance_cc non ha 'noise'


def test_aggregate_frame_storm_object_fields():
    cfg = _cfg()
    cells = _cells_frame([(10.0, 41.0), (10.03, 41.0)], 1, T0)
    objs = agg.aggregate_frame(cells, 1, cfg)
    assert len(objs) == 1
    o = objs[0]
    assert o.cell_count == 2
    assert o.area_km2 > 0 and o.max_dbz == 42.0
    assert len(o.cell_ids) == 2
    # geometrie originali delle celle intatte (layer 1 NON modificato)
    assert cells[0].area_km2 == 120.0 and cells[0].cell_id == "c1-0"


def test_dbscan_reference_noise_and_clusters():
    cells = _cells_frame([(10.0, 41.0), (10.01, 41.0), (10.02, 41.0),
                          (11.0, 41.0), (11.01, 41.0), (12.5, 41.0)], 0, T0)
    db = agg.dbscan_clusters(cells, eps_km=3.0, min_cells=2)
    sizes = sorted(len(g) for g in db)
    assert sizes == [2, 3]           # la cella a 12.5 è rumore


def test_convex_hull():
    pts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.5, 0.5), (0.0, 1.0)]
    hull = agg.convex_hull(pts)
    assert set(hull) == {(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)}


def test_compare_aggregation_methods_schema():
    cfg = _cfg()
    series = _single_moving_lons(3)
    cells_by_frame = [_cells_frame(lons, f, ts) for f, ts, lons in series]
    out = agg.compare_aggregation_methods(cells_by_frame, cfg)
    assert set(out["methods"]) == {"distance_cc", "dbscan", "dilation_overlap"}
    assert out["default_method"] == "dilation_overlap"
    assert all("object_counts_by_frame" in v for v in out["methods"].values())


def test_ambiguity_class_thresholds():
    amb = _cfg()["ambiguity"]
    low, r = storm_mod.ambiguity_class(1, 3, 1.0, False, amb)
    assert low == "low" and r["class"] == "low"
    med, _ = storm_mod.ambiguity_class(4, 6, 0.5, False, amb)
    assert med == "medium"
    high, _ = storm_mod.ambiguity_class(5, 15, 0.05, True, amb)
    assert high == "high"


# ---------------------------------------------------------------------------
# Scenario 1: cella isolata
# ---------------------------------------------------------------------------
def test_scenario1_isolated_cell_stable_track():
    objs, tracks = _run_storm_pipeline(_single_moving_lons(6))
    assert all(len(o) == 1 for o in objs)
    assert len(tracks) == 1
    t = tracks[0]
    assert len(t.points) == 6
    assert t.motion_confidence == "high"
    assert t.tracking_ambiguity == "low"
    assert t.motion["velocity_kmh"] > 0


# ---------------------------------------------------------------------------
# Scenario 2: due celle separate
# ---------------------------------------------------------------------------
def test_scenario2_two_separate_cells_two_tracks():
    series = [(f, T0 + f * STEP_MS,
               [(10.0 + f * 0.06, 41.0), (10.0 + f * 0.06, 42.0)])
              for f in range(5)]
    objs, tracks = _run_storm_pipeline(series)
    assert all(len(o) == 2 for o in objs)
    assert len(tracks) == 2
    # due tracce indipendenti (ogni frame ogni oggetto matcha SE stesso)
    assert all(len(t.points) == 5 for t in tracks)


# ---------------------------------------------------------------------------
# Scenario 3: due celle vicine (gap < merge_gap)
# ---------------------------------------------------------------------------
def test_scenario3_nearby_cells_merge_into_one_object():
    series = [(f, T0 + f * STEP_MS,
               [(10.0 + f * 0.06, 41.0), (10.04 + f * 0.06, 41.0)])
              for f in range(4)]
    objs, tracks = _run_storm_pipeline(series)
    # gap 4 km < merge_gap 5 km -> 1 STORM OBJECT per frame
    assert all(len(o) == 1 and o[0].cell_count == 2 for o in objs)
    assert len(tracks) == 1 and len(tracks[0].points) == 4


# ---------------------------------------------------------------------------
# Scenario 4: merge
# ---------------------------------------------------------------------------
def test_scenario4_merge_objects():
    # due oggetti separati convergono -> un solo storm object al merge
    series = [
        (0, T0,               [(10.00, 41.0), (10.71, 41.0)]),
        (1, T0 + STEP_MS,     [(10.00, 41.0), (10.71, 41.0)]),
        (2, T0 + 2 * STEP_MS, [(10.00, 41.0), (10.35, 41.0)]),
        (3, T0 + 3 * STEP_MS, [(10.35, 41.0), (10.35, 41.0)]),  # merged
    ]
    objs, tracks = _run_storm_pipeline(series)
    assert [len(o) for o in objs] == [2, 2, 2, 1]
    assert len(tracks) == 2
    by_id = {t.track_id: t for t in tracks}
    merged = objs[3][0]
    assert merged.track_id in by_id            # continua UNA delle due track
    # l'altra track è terminata (merge observativo al frame 3)
    dead = [t for t in tracks if t.track_id != merged.track_id][0]
    assert dead.death_ms == objs[2][1].timestamp_ms or \
        dead.death_ms == objs[2][0].timestamp_ms


# ---------------------------------------------------------------------------
# Scenario 5: split
# ---------------------------------------------------------------------------
def test_scenario5_split_objects():
    # un oggetto si separa in due -> seconda storm track nasce (fork)
    series = [
        (0, T0,               [(10.00, 41.0)]),
        (1, T0 + STEP_MS,     [(10.00, 41.0), (10.40, 41.0)]),
        (2, T0 + 2 * STEP_MS, [(10.00, 41.0), (10.40, 41.0)]),
    ]
    objs, tracks = _run_storm_pipeline(series)
    assert [len(o) for o in objs] == [1, 2, 2]
    assert len(tracks) == 2
    born = [t for t in tracks if len(t.points) == 2 and "birth" in t.events]
    assert len(born) == 1


# ---------------------------------------------------------------------------
# Scenario 6: cluster denso
# ---------------------------------------------------------------------------
def test_scenario6_dense_cluster():
    cluster = [(10.00 + 0.01 * i, 41.0) for i in range(6)]
    series = [(f, T0 + f * STEP_MS, cluster) for f in range(3)]
    objs, tracks = _run_storm_pipeline(series)
    assert all(len(o) == 1 for o in objs)      # cluster compatto -> 1 oggetto
    obj = objs[0][0]
    assert obj.cell_density >= 6
    assert obj.tracking_ambiguity is not None  # metrica diagnostica attiva
    assert len(tracks) == 1


# ---------------------------------------------------------------------------
# Scenario 7: crescita rapida
# ---------------------------------------------------------------------------
def test_scenario7_rapid_growth_no_split():
    series = []
    for f in range(6):
        ts = T0 + f * STEP_MS
        area = 120.0 if f < 4 else 1200.0       # x10 in un passo
        cells = _cells_frame([(10.0 + f * 0.06, 41.0)], f, ts, area=area)
        series.append((f, ts, [(10.0 + f * 0.06, 41.0)]))
    # rebuild con area variabile
    objs_by_frame = []
    cfg = _cfg()
    for f in range(6):
        ts = T0 + f * STEP_MS
        cells = _cells_frame([(10.0 + f * 0.06, 41.0)], f, ts,
                             area=120.0 if f < 4 else 1200.0)
        objs_by_frame.append(agg.aggregate_frame(cells, f, cfg))
    tracker = storm_mod.StormObjectTracker(cfg)
    for f, objs in enumerate(objs_by_frame):
        tracker.update(objs, f)
    tracker.complete_cycles()
    tracks = tracker.finalize()
    assert len(tracks) == 1                     # nessuna frammentazione
    assert len(tracks[0].points) == 6
    assert tracks[0].motion["area_growth_pct"] > 0


# ---------------------------------------------------------------------------
# Scenario 8: cella che scompare
# ---------------------------------------------------------------------------
def test_scenario8_disappearing_cell():
    series = [
        (0, T0,           [(10.0, 41.0)]),
        (1, T0 + STEP_MS, [(10.06, 41.0)]),
        (2, T0 + 2 * STEP_MS, [(10.12, 41.0)]),
        (3, T0 + 3 * STEP_MS, []),
        (4, T0 + 4 * STEP_MS, []),
    ]
    objs, tracks = _run_storm_pipeline(series)
    assert objs[-1] == [] and objs[-2] == []
    assert len(tracks) == 1
    t = tracks[0]
    assert len(t.points) == 3
    assert t.death_ms == T0 + 2 * STEP_MS       # terminata al frame 2
    assert "death" in t.events


# ---------------------------------------------------------------------------
# Scenario 9: storm object persistente con celle interne variabili
# ---------------------------------------------------------------------------
def test_scenario9_persistent_object_varying_cells():
    count_by_frame = [2, 4, 3, 5, 3, 4]
    objs_by_frame = []
    cfg = _cfg()
    for f, ccount in enumerate(count_by_frame):
        cells = _cells_frame(
            [(10.0 + f * 0.06 + 0.02 * (i % 3), 41.0) for i in range(ccount)],
            f, T0 + f * STEP_MS)
        objs_by_frame.append(agg.aggregate_frame(cells, f, cfg))
    tracker = storm_mod.StormObjectTracker(cfg)
    for f, objs in enumerate(objs_by_frame):
        tracker.update(objs, f)
    tracker.complete_cycles()
    tracks = tracker.finalize()
    assert all(len(o) == 1 for o in objs_by_frame)
    assert len(tracks) == 1 and len(tracks[0].points) == 6
    t = tracks[0]
    assert t.motion["max_cell_count"] == 5
    assert t.motion_confidence in ("high", "medium")  # identità stabile
    # celle interne cambiano ma l'oggetto concettuale è lo stesso track_id
    assert all(p.storm_object_id for p in t.points)


# ---------------------------------------------------------------------------
# Output storm layer (Parte E)
# ---------------------------------------------------------------------------
def test_storm_outputs_valid(tmp_path):
    objs, tracks = _run_storm_pipeline(_single_moving_lons(5))
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = "2026-04-30T10:00:00Z"
    b.radar_timestamp_ms = T0
    b.frames = []
    b.cells_by_frame = []
    b.tracks = []
    b.storm_objects_by_frame = objs
    b.storm_tracks = tracks
    out = str(tmp_path / "radar")
    paths = output.write_outputs(b, out)
    assert all(n in paths for n in ("storm_objects.geojson", "storm_tracks.json"))
    assert output.validate_outputs(out) == []
    with open(paths["storm_objects.geojson"], encoding="utf-8") as fh:
        gj = json.load(fh)
    props = gj["features"][0]["properties"]
    for key in ("storm_object_id", "track_id", "cell_count", "area_km2",
                "max_dbz", "mean_dbz", "motion_speed_kmh", "motion_direction",
                "motion_confidence", "tracking_ambiguity", "organization_score",
                "score_confidence"):
        assert key in props
    with open(paths["storm_tracks.json"], encoding="utf-8") as fh:
        tj = json.load(fh)
    assert len(tj["tracks"]) == 1
    assert tj["tracks"][0]["type"] == "storm_object"
    assert tj["tracks"][0]["motion_confidence"] in ("low", "medium", "high")


def test_storm_output_attributes_defaults_empty(tmp_path):
    # bundle senza storm layer (retro-compat) -> file vuoti ma validi
    c1 = make_cell(12.0, 41.0, T0, cell_id="c1")
    b = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = c1.timestamp_iso
    b.radar_timestamp_ms = c1.timestamp_ms
    b.frames = []
    b.cells_by_frame = [[c1]]
    b.tracks = []
    out = str(tmp_path / "radar")
    paths = output.write_outputs(b, out)
    assert output.validate_outputs(out) == []
    with open(paths["storm_objects.geojson"], encoding="utf-8") as fh:
        assert json.load(fh)["features"] == []