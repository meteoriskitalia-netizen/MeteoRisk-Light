#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk — test Fase 1.9 (Motion Physics Validation & Kinematic Sanitization).

Coprono:
    Parte A  — data model: raw_velocity_kmh / velocity_kmh / velocity_valid /
               motion_status / motion_rejection_reason (NON clamp).
    Parte B  — physical speed gate configurabile (default None), mai hardcodato.
    Parte C  — robust velocity estimation: path (raw) vs net vs robust median;
               nessun Kalman.
    Parte D  — coordinate metriche: distanze in km via geodesica, MAI gradi.
    Parte E  — merge/split ambigui -> ambiguous_geometry + penalty confidence.
    Parte H  — contrattualizzazione output (campi esposti in to_dict).
"""

import copy

import pytest

from radar_engine import models
from radar_engine import aggregation as agg
from radar_engine import storm_tracking as st
from radar_engine.config import CONFIG
from radar_engine.tracking import haversine_km

from conftest import make_cell

T0 = 1_777_027_200_000
STEP_MS = 300_000  # 5 min VMI


def _object(idx, lon, lat, ts, frame_index=0, merge_split=False):
    obj = models.StormObject(
        storm_object_id=f"o{idx}", timestamp_ms=ts,
        timestamp_iso=models.utcnow_iso(),
        frame_index=int(frame_index), cells=[],
        centroid_lonlat=(float(lon), float(lat)), area_km2=120.0,
        equiv_radius_km=6.2,
        bbox_lonlat=(float(lon), float(lat), float(lon) + 0.1, float(lat) + 0.1),
        convex_hull_lonlat=[], max_dbz=42.0, mean_dbz=38.0, p90_dbz=40.0,
        cell_density=1,
    )
    if merge_split:
        obj.ambiguity_reason = {"merge_split": True, "competiing": 2}
    else:
        obj.ambiguity_reason = {"merge_split": False, "competing": 1}
    obj.tracking_ambiguity = "high" if merge_split else "low"
    return obj


def _points(lons, lat=41.0, step_ms=STEP_MS):
    track = models.StormTrack(1, T0)
    pts = []
    for i, lon in enumerate(lons):
        ts = T0 + i * step_ms
        obj = _object(i, lon, lat, ts, frame_index=i)
        pts.append(models.StormTrackPoint(1, i, obj))
    track.points = pts
    return pts


# ---------------------------------------------------------------------------
# Parte D — coordinate metriche (km geodesici, non gradi)
# ---------------------------------------------------------------------------
def test_haversine_metric_km_not_degrees():
    d_lat = haversine_km((0.0, 0.0), (0.0, 1.0))   # 1° in latitudine
    d_lon = haversine_km((0.0, 0.0), (1.0, 0.0))   # 1° in longitudine a lat 0
    assert 105.0 < d_lat < 118.0                   # ~111.2 km, NON ~1 (gradi)
    assert 105.0 < d_lon < 118.0


def test_segment_velocity_kmh_metric_units():
    # 0.1° longitudine a 41°N ≈ 8.40 km in 5 min ≈ 100.8 km/h
    pts = _points([10.0, 10.1, 10.2])
    speeds = st.segment_velocities_kmh(pts)
    assert len(speeds) == 2
    for s in speeds:
        assert 98.0 < s < 104.0
    kin = st.robust_track_velocity(pts)
    assert kin["n_segments"] == 2
    assert 98.0 < kin["raw_velocity_kmh"] < 104.0
    assert 98.0 < kin["net_velocity_kmh"] < 104.0
    assert 98.0 < kin["median_segment_velocity_kmh"] < 104.0


# ---------------------------------------------------------------------------
# Parte C — robust multi-frame (path vs net vs median)
# ---------------------------------------------------------------------------
def test_median_robust_to_single_centroid_jump():
    # 3 stime confrontate su una track con UN salto di centroide (re-emergence):
    # path e net sono inutilizzabili (~2000 km/h), il robust median resta ~100.
    pts = _points([10.0, 10.1, 10.2, 20.0, 20.1, 20.2])
    kin = st.robust_track_velocity(pts)
    assert kin["n_segments"] == 5
    assert kin["raw_velocity_kmh"] > 1000.0       # inflazionato dal salto
    assert kin["net_velocity_kmh"] > 1000.0       # inflazionato dal salto
    assert 95.0 < kin["median_segment_velocity_kmh"] < 110.0
    # il gate a soglia fisica rifiuta il SALTO ma non la track nel suo insieme
    a = st.assess_motion(kin, 300.0, [])
    assert a["velocity_valid"] is True
    assert a["rejected_segments"] == 1


# ---------------------------------------------------------------------------
# Parte B — physical speed gate (configurabile, default None)
# ---------------------------------------------------------------------------
def test_gate_default_none_is_off():
    pts = _points([10.0, 10.1, 10.2])
    kin = st.robust_track_velocity(pts)
    a = st.assess_motion(kin, None, [])
    assert a["velocity_valid"] is True
    assert a["motion_status"] == st.MOTION_STATUS_VALID
    assert a["velocity_kmh"] is not None


def test_rejected_velocity_not_clamped():
    # velocità fisicamente impossibile: 7° lon/step ~ 7056 km/h
    pts = _points([10.0, 17.0, 24.0])
    kin = st.robust_track_velocity(pts)
    a = st.assess_motion(kin, 200.0, [])
    assert a["velocity_valid"] is False
    assert a["motion_status"] == st.MOTION_STATUS_REJECTED
    assert a["motion_rejection_reason"] == st.REASON_SPEED_GATE
    assert a["velocity_kmh"] is None                     # rifiutata, NON 200
    assert kin["raw_velocity_kmh"] > 6000.0              # raw conservata (audit)
    assert a["rejected_segments"] == 2


def test_not_estimable_insufficient_observations():
    pts = _points([10.0, 17.0, 24.0], step_ms=0)         # dt=0 su tutti i segmenti
    kin = st.robust_track_velocity(pts)
    assert kin["n_segments"] == 0
    a = st.assess_motion(kin, 200.0, [])
    assert a["motion_status"] == st.MOTION_STATUS_NOT_ESTIMABLE
    assert a["motion_rejection_reason"] == st.REASON_INSUFFICIENT_OBS


def test_gate_after_publish_never_exceeds_threshold():
    for lons in ([10.0, 17.0, 24.0], [10.0, 10.1, 20.0, 20.1]):
        kin = st.robust_track_velocity(_points(lons))
        a = st.assess_motion(kin, 300.0, [])
        assert a["velocity_kmh"] is None or a["velocity_kmh"] <= 300.0


# ---------------------------------------------------------------------------
# Parte E — merge/split ambigui
# ---------------------------------------------------------------------------
def test_ambiguous_segments_detected():
    pts = _points([10.0, 10.1, 10.2])
    pts[1]._obj.ambiguity_reason["merge_split"] = True
    assert st.ambiguous_segments(pts) == [0, 1]  # segmenti che toccano il pt 1


def test_ambiguous_geometry_status_and_penalty():
    pts = _points([10.0, 10.1, 10.2])
    pts[1]._obj.ambiguity_reason["merge_split"] = True
    kin = st.robust_track_velocity(pts)
    amb = st.ambiguous_segments(pts)
    a = st.assess_motion(kin, 200.0, amb)
    assert a["motion_status"] == st.MOTION_STATUS_AMBIGUOUS_GEOMETRY
    assert a["velocity_valid"] is True
    # penalty di confidence
    assert st.apply_ambiguity_penalty("high", amb) == ("medium", True)
    assert st.apply_ambiguity_penalty("medium", amb) == ("low", True)
    assert st.apply_ambiguity_penalty("low", amb) == ("low", False)
    assert st.apply_ambiguity_penalty("high", []) == ("high", False)


# ---------------------------------------------------------------------------
# Parte A/H — contratto nel modello di output
# ---------------------------------------------------------------------------
def _pipeline(series, gate=None):
    """Aggregazione + StormObjectTracker (stesso flusso di test_multiscale).

    gate = max_validated_velocity_kmh (VALIDAZIONE SOLO finalize; il matching
    storm resta Fase 1.6, max_storm_speed_kmh=None)."""
    cfg = copy.deepcopy(CONFIG["storm"])
    cfg["tracking"]["max_storm_speed_kmh"] = None   # matching invariato 1.6
    cfg["motion"]["max_validated_velocity_kmh"] = gate  # validazione finalize
    objs_by_frame = []
    for (fidx, ts, lons_lats) in series:
        cells = [make_cell(lon, lat, ts, frame_index=fidx,
                           cell_id=f"c{fidx}-{i}")
                 for i, (lon, lat) in enumerate(lons_lats)]
        objs_by_frame.append(agg.aggregate_frame(cells, fidx, cfg))
    tracker = st.StormObjectTracker(cfg)
    for fidx, objs in enumerate(objs_by_frame):
        tracker.update(objs, fidx)
    tracker.complete_cycles()
    return objs_by_frame, tracker.finalize()


def _moving_series(n_frames, lons_per_frame, lat=41.0):
    return [(f, T0 + f * STEP_MS, [(lons_per_frame[f][i], lat)
                                   for i in range(len(lons_per_frame[f]))])
            for f in range(n_frames)]


def test_finalize_populates_contract_fields():
    series = _moving_series(
        5, [[10.0 + f * 0.06] for f in range(5)])
    objs, tracks = _pipeline(series)
    assert len(tracks) == 1
    t = tracks[0]
    assert t.motion["velocity_valid"] is True
    assert t.motion["motion_status"] in (
        st.MOTION_STATUS_VALID, st.MOTION_STATUS_AMBIGUOUS_GEOMETRY)
    assert t.motion["velocity_kmh"] > 0
    assert t.motion["raw_velocity_kmh"] > 0
    assert t.motion["net_velocity_kmh"] > 0
    assert t.motion["median_segment_velocity_kmh"] > 0
    assert t.motion["total_segments"] == 4
    assert "motion_rejection_reason" in t.motion
    d = t.to_dict()
    for key in ("raw_velocity_kmh", "net_velocity_kmh", "velocity_valid",
                "motion_status", "motion_rejection_reason", "rejected_segments",
                "total_segments", "ambiguous_segments"):
        assert key in d
    obj = objs[0][0]
    assert obj.motion_speed_kmh is not None
    assert obj.raw_motion_speed_kmh is not None
    assert obj.motion_status is not None


def test_gate_validation_rejects_but_preserves_tracking_identity():
    # Gate di VALIDAZIONE: NON tocca il matching. La stessa serie col salto
    # produce la STESSA track (identità preservata, success criterion 4) ma la
    # velocità robusta oltre il gate diventa None (velocità rifiutata, non
    # clampata) e il raw resta.
    series = [(f, T0 + f * STEP_MS, [(lon, 41.0)])
              for f, lon in enumerate([10.0, 17.0, 24.0, 24.06])]
    _, off = _pipeline(series, gate=None)
    _, on = _pipeline(series, gate=200.0)
    assert len(off) == len(on)                       # stessa identità
    for t_off, t_on in zip(off, on):
        assert len(t_off.points) == len(t_on.points)
    # baseline (Fase 1.8) pubblicava la velocità impossibile
    assert off[0].motion["velocity_valid"] is True
    assert off[0].motion["velocity_kmh"] > 6000.0
    # con il gate calibrato: rifiutate, raw conservato, nessun clamp
    assert on[0].motion["velocity_valid"] is False
    assert on[0].motion["motion_status"] == st.MOTION_STATUS_REJECTED
    assert on[0].motion["velocity_kmh"] is None
    assert on[0].motion["raw_velocity_kmh"] > 4000.0


def test_validation_gate_does_not_degrade_tracking():
    # serie pulita: gate on/off -> count/id/punti identici, velocità valida.
    series = [(f, T0 + f * STEP_MS,
               [(10.0 + 0.06 * f, 41.0)]) for f in range(5)]
    _, off = _pipeline(series, gate=None)
    _, on = _pipeline(series, gate=200.0)
    assert [len(t.points) for t in on] == [len(t.points) for t in off]
    assert [t.motion["direction_toward_deg"] for t in on] == \
        [t.motion["direction_toward_deg"] for t in off]
    assert on[0].motion["velocity_valid"] is True
    assert on[0].motion["velocity_kmh"] <= 200.0