#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — scoring.py

Preliminary Organization Score (Fase 1): punteggio 0-100 PER TRACK composto da
componenti normalizzate (info puramente radar/geometriche, senza hook-echo,
mesocyclone o rotation:

    persistence            -> 20%   (n-frame / ref, ref=finestra prevista 5)
    intensity              -> 20%   (max dBZ normalizzato su [20, 55])
    intensity_consistency  -> 15%   (1 - std(max dBZ) normalizzato)
    spatial_coherence      -> 15%   (solidità e compattezza medie)
    motion_consistency     -> 15%   (regolarità velocità + direzione)
    growth_sustained       -> 15%   (crescita area e mantensi intensità)

Classificazione probabilistica (NON "Supercell Detected"):
    0-30   Weak Convective Cell
    31-55  Convective Cell
    56-75  Organized Convective Cell
    76-100 Highly Organized Convective Cell

Le probabilità di classe (class_probs) sono una assegnazione soft deterministica
basata sulla distanza dai centri banda (reference probabilistica, EXPERIMENTAL).
"""

import math

from . import models


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _norm_intensity(max_dbz, ref_lo, ref_hi):
    return _clamp01((max_dbz - ref_lo) / max(ref_hi - ref_lo, 1e-9))


def _fallback_scoring():
    """Config scoring di fallback (SOLO per caller senza injection).

    Fase 1.6: percorso deprecato — il motore (main.py) e la validation
    iniettano sempre la configurazione esplicita. Mantenuta per retro-compat."""
    from . import config as _cfg
    return _cfg.CONFIG["scoring"]


def _full_score_components(track, cfg):
    """Componenti 0..1 per una track con >=2 punti (timestamps reali)."""
    w = cfg["weights"]
    ref_frames = float(cfg.get("persistence_ref_frames", 5))
    ref_lo, ref_hi = cfg.get("intensity_ref_dbz", [20.0, 55.0])

    n = len(track.points)
    persistence = _clamp01(n / ref_frames)

    maxes = [p.max_dbz for p in track.points]
    areas = [p.area_km2 for p in track.points]
    intensity = _norm_intensity(max(maxes), ref_lo, ref_hi)

    intensity_consistency = 1.0 - _clamp01(
        (max(maxes) - min(maxes)) / max(ref_hi - ref_lo, 1e-9)
    ) if n > 1 else 1.0

    # spatial coherence: solidità media + inverse della compattezza media
    compact_ref = 6.0  # valore "tipo" per celle estese (~cerchio->~1)
    solidity_avg = track.motion.get("_avg_solidity", 1.0)
    compact_avg = track.motion.get("_avg_compactness", 1.0)
    spatial_coherence = 0.5 * solidity_avg + 0.5 * _clamp01(compact_ref / max(compact_avg, 1e-9))

    # motion consistency: coerenza velocità (CV) e direzione (turning medio)
    speeds, headings = [], []
    for a, b in zip(track.points, track.points[1:]):
        dt_h = (b.timestamp_ms - a.timestamp_ms) / 3600000.0
        if dt_h <= 0:
            continue
        from .tracking import haversine_km, bearing_deg
        speeds.append(haversine_km(a.lonlat, b.lonlat) / dt_h)
        headings.append(bearing_deg(a.lonlat, b.lonlat))
    motion_consistency = 0.5
    if speeds:
        mean_s = sum(speeds) / len(speeds)
        cv = math.sqrt(sum((s - mean_s) ** 2 for s in speeds) / len(speeds)) / \
            max(mean_s, 1e-9)
        speed_component = _clamp01(2.0 - cv)
        turn = 0.0
        if len(headings) > 1:
            diffs = []
            for h1, h2 in zip(headings, headings[1:]):
                d = abs(h2 - h1) % 360.0
                diffs.append(min(d, 360.0 - d))
            turn = sum(diffs) / len(diffs)
        turn_component = _clamp01(1.0 - turn / 60.0)
        motion_consistency = 0.6 * speed_component + 0.4 * turn_component

    # growth_sustained: crescita area (+ sostenuta) + mantenimento intensità
    if areas[-1] > areas[0] and areas[0] > 0:
        growth = _clamp01((areas[-1] - areas[0]) / max(areas[0], 1e-9) / 2.0)
    elif areas[0] > 0:
        growth = 0.0
    else:
        growth = 0.5
    intensity_kept = 1.0 - _clamp01((maxes[0] - maxes[-1]) / max(maxes[0], 1e-9)) \
        if maxes[0] > 0 else 1.0
    growth_sustained = 0.6 * growth + 0.4 * intensity_kept

    return {
        "persistence": round(persistence, 4),
        "intensity": round(intensity, 4),
        "intensity_consistency": round(intensity_consistency, 4),
        "spatial_coherence": round(spatial_coherence, 4),
        "motion_consistency": round(motion_consistency, 4),
        "growth_sustained": round(growth_sustained, 4),
    }, w


def organization_score(track, cfg=None):
    """Score 0-100 + banda + prob. di classe. Richiede >=2 punti.

    cfg: blocco CONFIG['scoring'] (default embedded ragionevole per i test).
    Ritorna (score:int, label:str, class_probs:dict, components:dict)."""
    from . import config as _cfg
    if cfg is None:
        cfg = _cfg.CONFIG["scoring"]
    pts = track.points
    if len(pts) < 2:
        return 0, "Weak Convective Cell", {}, {}

    components, w = _full_score_components(track, cfg)
    score = 0.0
    for key, comp in components.items():
        score += comp * w[key]
    # Gate sull'intensità: elementi organizzativi (spaziale/motion) contano solo
    # se la cella mantiene intensità convettiva reale (damping dell'eco debole).
    intensity_gate = 0.3 + 0.7 * components["intensity"]
    score = int(round(100.0 * max(0.0, min(1.0, score * intensity_gate))))

    label = classify_band(score, cfg).name
    probs = soft_class_probs(score, cfg)
    return score, label, probs, components


def classify_band(score, cfg=None):
    """Banda di classificazione per lo score (0-100)."""
    from . import config as _cfg
    if cfg is None:
        cfg = _cfg.CONFIG["scoring"]
    for lo, hi, name in cfg.get("bands", []):
        if lo <= score <= hi:
            return _Band(name, lo, hi)
    return _Band("Weak Convective Cell", 0.0, 30.0)


class _Band:
    def __init__(self, name, lo, hi):
        self.name = name
        self.lo = lo
        self.hi = hi


def soft_class_probs(score, cfg=None):
    """Assegnazione soft deterministica alle bande (reference probabilistica,
    EXPERIMENTAL). Banda corrente esclusa? No: tutti i centri, sigma=12.5."""
    from . import config as _cfg
    if cfg is None:
        cfg = _cfg.CONFIG["scoring"]
    bands = cfg.get("bands", [])
    centers = [(lo + hi) / 2.0 for lo, hi, _ in bands]
    sigma = 12.5
    weights = [math.exp(-((score - c) / sigma) ** 2) for c in centers]
    tot = sum(weights)
    probs = {name: round(w / tot, 4) for w, (_, _, name) in zip(weights, bands)}
    return probs