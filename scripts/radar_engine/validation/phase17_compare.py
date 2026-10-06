#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.phase17_compare (Fase 1.7, Parte F)

Confronte l'IDENTITÀ a livello STORM OBJECT vs il layer CELL (Fase 1).

Metriche (definizioni identiche a validation.metrics, parametrizzate sul
layer): ID-switch rate, track fragmentation (birth rate), track continuity,
motion stability (motion_error/direction_stability), temporal persistence,
birth rate. Per lo storm layer la metrica ID-switch perde parte del significato
(identità = oggetto concettuale); resta come confronto operativo.

Success criteria Fase 1.7 (almeno UNO positivo, NON la riduzione ID-switch
se non semanticamente appropriata):
    1) riduzione della frammentazione (birth_rate storm < cell)
    2) miglioramento della stabilità motion (motion_error o direction_stability)
    3) motion_confidence più realistica nei cluster densi (report qualitative)

Nessuna affermazione di verità assoluta: le metriche sono PROXY operative su
dati osservati (same caveat di metriche.py Fase 1.5).
"""

import math

import numpy as np

from ..tracking import haversine_km, bearing_deg


def _cv(values):
    values = [v for v in values if v is not None and v > 0]
    if len(values) < 2:
        return 0.0
    arr = np.asarray(values, dtype=float)
    return float(arr.std() / arr.mean())


def _long_tracks(tracks):
    return [t for t in tracks if len(t.points) >= 2]


def storm_motion_error(tracks):
    errs = []
    for t in _long_tracks(tracks):
        if len(t.points) < 3:
            continue
        base = bearing_deg(t.points[0].lonlat, t.points[-1].lonlat)
        for a, b in zip(t.points, t.points[1:]):
            s = bearing_deg(a.lonlat, b.lonlat)
            d = abs(s - base) % 360.0
            errs.append(min(d, 360.0 - d))
    return round(float(np.mean(errs)) if errs else 0.0, 2)


def storm_direction_stability(tracks):
    stdevs = []
    for t in _long_tracks(tracks):
        headings = [bearing_deg(a.lonlat, b.lonlat)
                    for a, b in zip(t.points, t.points[1:])]
        if len(headings) < 2:
            continue
        ref = headings[0]
        rel = [(h - ref + 180.0) % 360.0 - 180.0 for h in headings]
        stdevs.append(float(np.std(rel)))
    return round(float(np.mean(stdevs)) if stdevs else 0.0, 2)


def storm_track_continuity(tracks, n_frames):
    if not n_frames:
        return 0.0
    cont = [len(t.points) / n_frames for t in _long_tracks(tracks)]
    return round(float(np.mean(cont)), 3) if cont else 0.0


def storm_birth_rate(tracks, total_objects):
    if not total_objects:
        return None
    n_born = len([t for t in tracks if "birth" in t.events])
    return round(n_born / total_objects, 3)


def storm_track_id_switches(tracks, objs_by_frame):
    """Proxy ID-switch per storm object (identità concettuale): oggetti
    spazialmente co-localizzati in frame consecutivi su track DIVERSE."""
    track_of = {}
    for t in tracks:
        for p in t.points:
            track_of[(p.frame_index, p.storm_object_id)] = t.track_id
    switches = compared = 0
    for (fa, objs_a), (fb, objs_b) in zip(enumerate(objs_by_frame),
                                           list(enumerate(objs_by_frame))[1:]):
        for a in objs_a:
            for b in objs_b:
                if haversine_km(a.centroid_lonlat, b.centroid_lonlat) > 25.0:
                    continue
                compared += 1
                ta = track_of.get((fa, a.storm_object_id))
                tb = track_of.get((fb, b.storm_object_id))
                if ta is not None and tb is not None and ta != tb:
                    switches += 1
    rate = switches / compared if compared else 0.0
    return {"switches": switches, "compared": compared, "rate": round(rate, 4)}


def _conf_distribution(attr, tracks):
    dist = {}
    for t in tracks:
        v = getattr(t, attr, None)
        if v is not None:
            dist[v] = dist.get(v, 0) + 1
    return dist


def compare_layers(cells_by_frame, cell_tracks, storm_objects_by_frame,
                   storm_tracks):
    """Metriche CELL vs STORM OBJECT + valutazione dei success criteria."""
    from . import metrics as cell_m

    n_frames = len(cells_by_frame)
    cell_id = cell_m.track_id_switches_by_frame(cell_tracks, cells_by_frame)
    storm_id = storm_track_id_switches(storm_tracks, storm_objects_by_frame)
    cell_metrics = {
        "n_frames": n_frames,
        "cells": sum(len(c) for c in cells_by_frame),
        "tracks": len(_long_tracks(cell_tracks)),
        "id_switch": cell_id,
        "birth_rate": cell_m.birth_rate(cell_tracks, sum(len(c) for c in cells_by_frame)),
        "track_continuity": cell_m.track_continuity(cell_tracks, n_frames),
        "motion_error_deg": cell_m.motion_error(cell_tracks),
        "direction_stability_deg": cell_m.direction_stability(cell_tracks),
        "mean_track_duration_min": cell_m.mean_track_duration_min(cell_tracks, n_frames),
    }
    storm_metrics = {
        "n_frames": n_frames,
        "objects": sum(len(o) for o in storm_objects_by_frame),
        "tracks": len(_long_tracks(storm_tracks)),
        "id_switch": storm_id,
        "birth_rate": storm_birth_rate(
            storm_tracks, sum(len(o) for o in storm_objects_by_frame)),
        "track_continuity": storm_track_continuity(storm_tracks, n_frames),
        "motion_error_deg": storm_motion_error(storm_tracks),
        "direction_stability_deg": storm_direction_stability(storm_tracks),
        "mean_track_duration_min": round(sum(
            (t.points[-1].timestamp_ms - t.points[0].timestamp_ms)
            for t in _long_tracks(storm_tracks)) / 60000.0 /
            max(len(_long_tracks(storm_tracks)), 1), 1),
        "motion_confidence": _conf_distribution("motion_confidence", storm_tracks),
        "tracking_ambiguity": _conf_distribution("tracking_ambiguity", storm_tracks),
    }

    def _better(kind, a, b):
        """a vs b: True se a <= b (minore = meglio)."""
        if a is None or b is None:
            return None
        return a <= b

    crit1_frag = (
        storm_metrics["birth_rate"] is not None and
        cell_metrics["birth_rate"] is not None and
        storm_metrics["birth_rate"] < cell_metrics["birth_rate"] * 0.95)
    crit2_motion_stab = (
        _better(kind="motion_error_deg", a=storm_metrics["motion_error_deg"],
                b=cell_metrics["motion_error_deg"] * 0.95) or
        _better(kind="direction_stability_deg",
                a=storm_metrics["direction_stability_deg"],
                b=cell_metrics["direction_stability_deg"] * 0.95))
    crit3_confidence = (
        storm_metrics["motion_confidence"].get("high", 0) +
        storm_metrics["motion_confidence"].get("medium", 0) > 0
        and storm_metrics["tracking_ambiguity"].get("high", 0) > 0)

    return {
        "cell": cell_metrics,
        "storm_object": storm_metrics,
        "deltas": {
            "id_switch_rate_delta": round(
                storm_id["rate"] - cell_id["rate"], 4),
            "birth_rate_delta": round(
                (storm_metrics["birth_rate"] or 0.0) -
                (cell_metrics["birth_rate"] or 0.0), 4),
            "continuity_delta": round(
                storm_metrics["track_continuity"] - cell_metrics["track_continuity"], 3),
            "motion_error_delta_deg": round(
                storm_metrics["motion_error_deg"] - cell_metrics["motion_error_deg"], 2),
            "direction_stability_delta_deg": round(
                storm_metrics["direction_stability_deg"] -
                cell_metrics["direction_stability_deg"], 2),
        },
        "success_criteria": {
            "c1_reduced_fragmentation": bool(crit1_frag),
            "c2_motion_stability": bool(crit2_motion_stab),
            "c3_confidence_dynamic": bool(crit3_confidence),
        },
        "success": bool(crit1_frag or crit2_motion_stab or crit3_confidence),
    }