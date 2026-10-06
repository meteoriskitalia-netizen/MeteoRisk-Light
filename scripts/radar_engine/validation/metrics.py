#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.metrics (Fase 1.5)

Metriche operative con DEFINIZIONI ESPLICITE. In assenza di ground truth
etichettato, tutte le metriche "rate" sono PROXY operativi calcolati rispetto a
un RIFERIMENTO (configurazione baseline su un caso) o rispetto ad assunzioni
dichiarate; nessuna affermazione di verità assoluta. Le metriche di stabilità
sono invarianti rispetto al riferimento e descrivono il comportamento del motore.

Detection:
  - confusion(reference, candidates, radius_km): match greedy per distanza
    centroide (<= radius_km) -> tp/fp/fn
  - detection_rate, false_positive_rate, false_negative_rate
  - cell_count_stability  (CV del numero di celle per frame)
  - area_stability        (media CV dell'area media per frame)
  - intensity_stability   (media CV del max dBZ per frame)

Tracking (solo dati osservati, no truth):
  - mean_track_duration_min
  - track_continuity      (media di n_punti / n_frame finestra, track>=2)
  - birth_rate            (n track nate / n celle totali -> frammentazione proxy)
  - mean_gap_cells        (celle singole non collegate -> frag. 1-frame)
  - motion_error          (media deviazione |heading segmento - heading track|)
  - direction_stability   (media std circolare degli heading dei segmenti)

Score:
  - score_stats           (distribuzione 0-100, conto bande)
  - score_temporal_evolution (media score per frame, score attribuito ai punti)
  - false_high_scores     (score>=56 ma n_frame<5 o max_dbz<40)
  - score_jump / window comparison (in stability: stabilità inter-finestra)
"""

import math

import numpy as np

from .. import models
from ..tracking import haversine_km, bearing_deg

MATCH_RADIUS_KM = 25.0
FALSE_HIGH_MIN_SCORE = 56
FALSE_HIGH_MIN_FRAMES = 5
FALSE_HIGH_MIN_DBZ = 40.0


def _cv(values):
    values = [v for v in values if v is not None and v > 0]
    if len(values) < 2:
        return 0.0
    arr = np.asarray(values, dtype=float)
    return float(arr.std() / arr.mean())


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------
def match_cells(reference, candidates, radius_km=MATCH_RADIUS_KM):
    """Greedy 1-a-1: per ogni cella di riferimento la candidata più vicina
    entro radius_km. Ritorna set di indici (ref_idx, cand_idx)."""
    matched = []
    used = set()
    for ri, ref in enumerate(reference):
        best, best_d = None, radius_km
        for ci, cand in enumerate(candidates):
            if ci in used:
                continue
            d = haversine_km(ref.centroid_lonlat, cand.centroid_lonlat)
            if d <= best_d:
                best, best_d = ci, d
        if best is not None:
            used.add(best)
            matched.append((ri, best))
    return matched


def confusion(reference, candidates, radius_km=MATCH_RADIUS_KM):
    """Counts frame-level: tp (matched pairs), fp, fn."""
    matched = match_cells(reference, candidates, radius_km)
    tp = len(matched)
    fn = max(0, len(reference) - tp)
    fp = max(0, len(candidates) - tp)
    return {"tp": tp, "fp": fp, "fn": fn, "matched": matched}


def detection_rate(conf):
    den = conf["tp"] + conf["fn"]
    return conf["tp"] / den if den else None


def false_positive_rate(conf):
    den = conf["fp"] + conf["tp"]
    return conf["fp"] / den if den else None


def false_negative_rate(conf):
    den = conf["tp"] + conf["fn"]
    return conf["fn"] / den if den else None


def _per_frame(data_func, cells_by_frame):
    per = []
    for cells in cells_by_frame:
        vals = [data_func(c) for c in cells]
        per.append(sum(vals) / len(vals) if vals else None)
    return per


def cell_count_stability(cells_by_frame):
    counts = [len(c) for c in cells_by_frame]
    return {"mean": round(float(np.mean(counts)), 3),
            "std": round(float(np.std(counts)), 3),
            "cv": round(_cv(counts), 3),
            "min": int(min(counts)), "max": int(max(counts))}


def area_stability(cells_by_frame):
    per = _per_frame(lambda c: c.area_km2, cells_by_frame)
    vals = [v for v in per if v]
    return {"mean_frame_area": round(np.mean(vals), 1) if vals else None,
            "cv": round(_cv(per), 3)}


def intensity_stability(cells_by_frame):
    per = _per_frame(lambda c: c.max_dbz, cells_by_frame)
    vals = [v for v in per if v]
    return {"mean_frame_max_dbz": round(np.mean(vals), 2) if vals else None,
            "cv": round(_cv(per), 3)}


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------
def _track_frames(tracks):
    return [t for t in tracks if len(t.points) >= 2]


def mean_track_duration_min(tracks, n_frames=None):
    ts = [t.points[-1].timestamp_ms - t.points[0].timestamp_ms
          for t in _track_frames(tracks)]
    return round(float(np.mean(ts)) / 60000.0, 1) if ts else 0.0


def track_continuity(tracks, n_frames):
    if not n_frames:
        return 0.0
    cont = [len(t.points) / n_frames for t in _track_frames(tracks)]
    return round(float(np.mean(cont)), 3) if cont else 0.0


def birth_rate(tracks, total_cells):
    if not total_cells:
        return None
    n_born = len([t for t in tracks if models.EVENT_BIRTH in t.events])
    return round(n_born / total_cells, 3)


def motion_error(tracks):
    """Media (per segmento) di |heading segmento - heading overall track|."""
    errs = []
    for t in _track_frames(tracks):
        if len(t.points) < 3:
            continue
        base = bearing_deg(t.points[0].lonlat, t.points[-1].lonlat)
        for a, b in zip(t.points, t.points[1:]):
            s = bearing_deg(a.lonlat, b.lonlat)
            d = abs(s - base) % 360.0
            errs.append(min(d, 360.0 - d))
    return round(float(np.mean(errs)) if errs else 0.0, 2)


def _ang_dev(a, b):
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def direction_stability(tracks):
    """Media della std circolare degli heading dei segmenti per track."""
    stdevs = []
    for t in _track_frames(tracks):
        headings = [bearing_deg(a.lonlat, b.lonlat)
                    for a, b in zip(t.points, t.points[1:])]
        if len(headings) < 2:
            continue
        ref = headings[0]
        rel = [(h - ref + 180.0) % 360.0 - 180.0 for h in headings]
        stdevs.append(float(np.std(rel)))
    return round(float(np.mean(stdevs)) if stdevs else 0.0, 2)


def track_id_switches_by_frame(tracks, cells_by_frame):
    """Proxy ID-switch: per coppie di frame consecutivi, celle spazialmente
    identiche (match centroide) che appartengono a TRACK DIVERSE.

    Ritorna (num_switches, num_compared_cells, rate)."""
    track_of = {}
    for t in tracks:
        for p in t.points:
            track_of[(p.frame_index, p.cell_id)] = t.track_id
    switches = compared = 0
    for (fa, cells_a), (fb, cells_b) in zip(enumerate(cells_by_frame),
                                            list(enumerate(cells_by_frame))[1:]):
        for ri, a in enumerate(cells_a):
            for b in cells_b:
                if haversine_km(a.centroid_lonlat, b.centroid_lonlat) > 25.0:
                    continue
                compared += 1
                ta = track_of.get((fa, a.cell_id))
                tb = track_of.get((fb, b.cell_id))
                if ta is not None and tb is not None and ta != tb:
                    switches += 1
    rate = switches / compared if compared else 0.0
    return {"switches": switches, "compared": compared,
            "rate": round(rate, 4)}


# ---------------------------------------------------------------------------
# Score (Organization Score, riferimento: bande config)
# ---------------------------------------------------------------------------
def score_stats(tracks):
    scores = [t.motion.get("organization_score") for t in _track_frames(tracks)
              if t.motion.get("organization_score") is not None]
    if not scores:
        return {"n": 0}
    arr = np.asarray(scores, dtype=float)
    counts = {}
    for t in _track_frames(tracks):
        label = t.motion.get("classification")
        counts[label] = counts.get(label, 0) + 1
    return {
        "n": len(arr),
        "min": int(arr.min()), "p25": int(np.percentile(arr, 25)),
        "median": int(np.median(arr)), "mean": round(float(arr.mean()), 1),
        "p75": int(np.percentile(arr, 75)), "p90": int(np.percentile(arr, 90)),
        "p95": int(np.percentile(arr, 95)), "max": int(arr.max()),
        "std": round(float(arr.std()), 1),
        "band_counts": counts,
    }


def score_temporal_evolution(tracks, n_frames):
    """Media score per frame (score attribuito a ogni punto della track)."""
    by_frame = [[] for _ in range(n_frames)]
    for t in _track_frames(tracks):
        score = t.motion.get("organization_score")
        if score is None:
            continue
        for p in t.points:
            if 0 <= p.frame_index < n_frames:
                by_frame[p.frame_index].append(score)
    series = []
    for i, vals in enumerate(by_frame):
        series.append({"frame": i,
                       "mean_score": round(float(np.mean(vals)), 1) if vals else None,
                       "n": len(vals)})
    return series


def false_high_scores(tracks):
    """Score bande 'Organized'+ con supporto osservativo insufficiente."""
    bad = []
    for t in _track_frames(tracks):
        score = t.motion.get("organization_score")
        if score is None or score < FALSE_HIGH_MIN_SCORE:
            continue
        pts = t.points
        fullish = len(pts) >= FALSE_HIGH_MIN_FRAMES
        intense = max(p.max_dbz for p in pts) >= FALSE_HIGH_MIN_DBZ
        if not (fullish and intense):
            bad.append({
                "track_id": t.track_id, "score": score,
                "n_frames": len(pts),
                "max_dbz": round(max(p.max_dbz for p in pts), 1),
                "label": t.motion.get("classification"),
            })
    return bad