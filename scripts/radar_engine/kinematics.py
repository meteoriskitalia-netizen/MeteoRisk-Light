#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — kinematics.py (Fase 1.9, Parti A/B/C/D/E)

FONTE UNICA della cinematica delle track (estratto da storm_tracking.py):
stime in coordinate METRICHE, distanze via haversine (geodetica) in km, MAI
aritmetica in gradi EPSG:4326. Nessun Kalman: la stima robusta è la MEDIANA
delle velocità segmentali consecutive (immune ai centroid jumps di merge/split),
confrontata con la path-speed RAW e la net speed primo->ultimo (audit).

`storm_tracking.py` importa e ri-esporta questi simboli per retro-compatibilità
(i test accedono a `storm_tracking.segment_velocities_kmh`, ecc.).
"""

import math

import numpy as np

from .tracking import haversine_km

_EPS = 1e-12

_AMB_CODE = {"low": 0, "medium": 1, "high": 2}
_AMB_TEXT = {0: "low", 1: "medium", 2: "high"}

# Stati cinematici della track (Fase 1.9, Parte A)
MOTION_STATUS_VALID = "valid"
MOTION_STATUS_REJECTED = "rejected"
MOTION_STATUS_AMBIGUOUS_GEOMETRY = "ambiguous_geometry"
MOTION_STATUS_NOT_ESTIMABLE = "not_estimable"
REASON_SPEED_GATE = "physical_speed_gate"
REASON_INSUFFICIENT_OBS = "insufficient_observations"


# ---------------------------------------------------------------------------
# Stime cinematiche (Parti C/D/E)
# ---------------------------------------------------------------------------
def segment_deltas(points):
    """[(d_km, dt_h)] consecutivi con dt>0 (geodesico)."""
    out = []
    for a, b in zip(points, points[1:]):
        dt_h = (b.timestamp_ms - a.timestamp_ms) / 3600000.0
        if dt_h <= 0:
            continue
        out.append((haversine_km(a.lonlat, b.lonlat), dt_h))
    return out


def segment_velocities_kmh(points):
    """Velocità segmentali consecutive in km/h."""
    return [d / dt for d, dt in segment_deltas(points)]


def robust_track_velocity(points):
    """Stima cinematica della track (Parte C).

    Ritorna dict con tre stime + diagnostica:
      raw_velocity_kmh   : path-speed (somma spostamenti / durata) — 1:1 con
                           lo storico Fase 1.8 (solo rename).
      net_velocity_kmh   : spostamento primo->ultimo / durata.
      median_segment_velocity_kmh : MEDIANA delle velocità segmentali (scelta
                           documentata: robusta ai centroid jumps; nessun
                           Kalman). Il valore VALIDATO è questo.
    """
    speeds = segment_velocities_kmh(points)
    if not speeds:
        return {"n_segments": 0, "segment_speeds_kmh": [],
                "raw_velocity_kmh": None, "net_velocity_kmh": None,
                "median_segment_velocity_kmh": None}
    dur_h = (points[-1].timestamp_ms - points[0].timestamp_ms) / 3600000.0
    total_d = sum(d for d, _ in segment_deltas(points))
    net_d = haversine_km(points[0].lonlat, points[-1].lonlat)
    return {
        "n_segments": len(speeds),
        "segment_speeds_kmh": speeds,
        "raw_velocity_kmh": (total_d / dur_h if dur_h > 0 else None),
        "net_velocity_kmh": (net_d / dur_h if dur_h > 0 else None),
        "median_segment_velocity_kmh": float(np.median(speeds)),
    }


def ambiguous_segments(points):
    """Indici dei segmenti (0-based) con endpoint merge/split candidato.

    PARTE E: la geometria può cambiare per merge/split; il segmento coinvolto
    NON ha moto affidabile punto-a-punto. Il track NON termina (identità
    conservata), ma la cinematica del segmento è marcata 'ambiguous_geometry'."""
    indices = []
    for i, (a, b) in enumerate(zip(points, points[1:])):
        amb = False
        for p in (a, b):
            obj = getattr(p, "_obj", None)
            if obj is None:
                continue
            reason = getattr(obj, "ambiguity_reason", None) or {}
            if isinstance(reason, dict) and reason.get("merge_split"):
                amb = True
        if amb:
            indices.append(i)
    return indices


def assess_motion(kin, gate, amb_seg):
    """Parte A/B/E — validazione cinematica.

    NON clampare mai: 12300 resta in raw_velocity_kmh; velocity_kmh è il
    valore VALIDATO (mediana) se passa il gate fisico, altrimenti None. Il
    gate NON è scelto qui: arriva da configurazione (default None fino a
    calibrazione). Ritorna il dict motion_status/rejection."""
    rejected = sum(1 for s in kin.get("segment_speeds_kmh") or []
                   if gate is not None and s > gate)
    median = kin.get("median_segment_velocity_kmh")
    if median is None:
        return {"velocity_kmh": None, "velocity_valid": False,
                "motion_status": MOTION_STATUS_NOT_ESTIMABLE,
                "motion_rejection_reason": REASON_INSUFFICIENT_OBS,
                "rejected_segments": 0}
    if gate is not None and median > gate:
        return {"velocity_kmh": None, "velocity_valid": False,
                "motion_status": MOTION_STATUS_REJECTED,
                "motion_rejection_reason": REASON_SPEED_GATE,
                "rejected_segments": rejected}
    if amb_seg:
        return {"velocity_kmh": float(median), "velocity_valid": True,
                "motion_status": MOTION_STATUS_AMBIGUOUS_GEOMETRY,
                "motion_rejection_reason": None,
                "rejected_segments": rejected}
    return {"velocity_kmh": float(median), "velocity_valid": True,
            "motion_status": MOTION_STATUS_VALID,
            "motion_rejection_reason": None,
            "rejected_segments": rejected}


def apply_ambiguity_penalty(conf, amb_seg):
    """Parte E — penalty motion_confidence se ci sono segmenti merge/split."""
    if not amb_seg or conf == "low":
        return conf, False
    return ("medium" if conf == "high" else "low"), True


# ---------------------------------------------------------------------------
# Parte C — motion_confidence a livello track/storm object
# ---------------------------------------------------------------------------
def motion_confidence(track, motion_cfg, tracking_cfg):
    """Confidenza della stima di moto (low/medium/high).

    Dipende da: track age (n osservazioni), continuità geometrica (mean-IoU),
    coerenza velocità (CV) + std circolare dell'heading, ambiguità residua.
    Estratto 1:1 da StormObjectTracker._motion_confidence (unica fonte)."""
    cfg = motion_cfg
    pts = track.points
    n = len(pts)
    if n < 2:
        return "low"
    ious = [p.iou_to_prev for p in pts[1:] if p.iou_to_prev is not None]
    speeds = [p.speed_to_prev_kmh for p in pts[1:]
              if p.speed_to_prev_kmh is not None]
    headings = [p.bearing_to_prev_deg for p in pts[1:]
                if p.bearing_to_prev_deg is not None]
    geom_iou = sum(ious) / len(ious) if ious else 0.0
    mean_s = sum(speeds) / len(speeds) if speeds else 0.0
    cv = math.sqrt(sum((s - mean_s) ** 2 for s in speeds) / len(speeds)) / \
        max(mean_s, _EPS) if speeds else 1.0
    turn_std = 0.0
    if len(headings) > 1:
        ref = headings[0]
        rel = [(h - ref + 180.0) % 360.0 - 180.0 for h in headings]
        turn_std = float(np.std(rel))
    amb_codes = [p.ambiguity for p in pts if p.ambiguity is not None]
    mean_amb = sum(_AMB_CODE.get(a, 1) for a in amb_codes) / len(amb_codes) \
        if amb_codes else 0.0

    score = 0.0
    if n >= int(tracking_cfg.get("full_confidence_frames", 5)):
        score += 1.0
    elif n >= int(cfg.get("min_frames_clock", 3)):
        score += 0.5
    if geom_iou >= float(cfg.get("geom_iou_high", 0.35)):
        score += 1.0
    elif geom_iou >= float(cfg.get("geom_iou_medium", 0.20)):
        score += 0.5
    if cv <= float(cfg.get("cv_speed_high", 0.40)) \
            and turn_std <= float(cfg.get("turn_high_deg", 15.0)):
        score += 1.0
    elif cv <= 0.9:
        score += 0.5
    if mean_amb <= 0.5:
        score += 0.5

    if score >= 2.5:
        return "high"
    if score >= 1.5:
        return "medium"
    return "low"
