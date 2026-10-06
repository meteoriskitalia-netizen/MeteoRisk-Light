#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — supercell.py (SIGNATURE INDEX, EXPERIMENTAL)

Supercell Signature Index (SSI) 0-100 per track radar. NON è una diagnosi
meteorologica definitiva: il dataset Radar-DPC VMI è a singola riflettività
(nessun Doppler → nessun mesocyclone, hook-echo o velocity couplet misurabile).
L'SSI è una FIRMA RADAR COMPOSITA che combina i descrittori Fase 1 già validati
(cella + tracking + Organization Score) in un indice di probabilità relativa:

    intensity_core    -> 25%  (core convettivo reale: max e media dBZ della track)
    organization      -> 30%  (Organization Score Fase 1, banda Organizzata)
    core_morphology   -> 10%  (solidità e compattezza: core coeso, NON a linee)
    persistence       -> 15%  (n-frame nella finestra di analisi, longevità)
    growth_sustained  -> 20%  (crescita area + mantenimento intensità)

livelli:
    0-49   non_supercell    nessuna firma
    50-64  weak             firma debole (da tenere sotto osservazione)
    65-79  possible         possibile supercella (attenzione)
    80-100 marked           firma supercellulare marcata

Il booleano candidate=True richiede GATE espliciti PRIMA di ogni claim:
intensità reale (max_dbz >= soglia), frame minimi e SSI >= candidate_ssi.
La "nascita" di una possibile supercella (phase='birth') è la forma più
interessante per il monitoraggio in quasi-realtime: track candidata a pochi
frame nella finestra (noll'organizzazione recente).
"""

import math

from . import models

DATA_LIMITS_NOTE = (
    "Radar-DPC VMI a singola riflettività (nessun Doppler): mesocyclone e "
    "hook-echo non misurabili. SSI = firma composita sperimentale, NON una "
    "diagnosi confermata di supercella."
)

_DEF_BANDS = [
    (0.0, 49.0, "non_supercell"),
    (50.0, 64.0, "weak"),
    (65.0, 79.0, "possible"),
    (80.0, 100.0, "marked"),
]

_DEF_WEIGHTS = {
    "intensity_core": 0.25,
    "organization": 0.30,
    "core_morphology": 0.10,
    "persistence": 0.15,
    "growth_sustained": 0.20,
}


def _cfg_defaults():
    """Blocco supercell di default (riflette config.CONFIG)."""
    from . import config as _cfg
    return _cfg.CONFIG["supercell"]


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _norm(x, lo, hi):
    return _clamp01((x - lo) / max(hi - lo, 1e-9))


def _track_values(track):
    """max/mean dBZ e attributi morfologici medi dai punti della track.

    I TrackPoint/StormTrackPoint espongono max_dbz, mean_dbz, solidity e
    compactness (NON p90/eccentricità: non propagati a livello di track)."""
    maxes = [p.max_dbz for p in track.points]
    means = [p.mean_dbz for p in track.points]
    solidity = getattr(track.motion, "get", lambda _k, _d: None)("_avg_solidity", None)
    compactness = getattr(track.motion, "get", lambda _k, _d: None)("_avg_compactness", None)
    if solidity is None:
        solidity = sum(p.solidity for p in track.points) / max(len(track.points), 1)
    if compactness is None:
        compactness = sum(p.compactness for p in track.points) / max(len(track.points), 1)
    return maxes, means, float(solidity), float(compactness)


def _components(track, cfg):
    """Componenti 0..1 della firma SSI per una track (>=2 punti)."""
    n = len(track.points)
    maxes, means, sol, comp = _track_values(track)
    ref_hi = float(cfg["intensity_ref_dbz"][1])
    ref_lo = float(cfg["intensity_ref_dbz"][0])

    max_dbz_max = max(maxes) if maxes else 0.0
    mean_dbz_mean = sum(means) / len(means) if means else 0.0
    intensity_core = 0.6 * _norm(max_dbz_max, ref_lo, ref_hi) + \
        0.4 * _norm(mean_dbz_mean, 20.0, 45.0)

    org = track.motion.get("organization_score", 0) or 0
    organization = _clamp01(float(org) / 100.0)

    # core_morphology: core COESO (solidità alta) e COMPATTO (compattezza bassa);
    # penalizza l'eco a linea (compactness molto alta) e le celle fragili.
    sol_comp = _clamp01((sol - 0.5) / 0.4)
    comp_ref = float(cfg.get("compactness_ref", 8.0))
    inv_comp = 1.0 - _clamp01((comp - 1.0) / max(comp_ref - 1.0, 1e-9))
    core_morphology = 0.5 * sol_comp + 0.5 * inv_comp

    persistence = _clamp01(n / float(cfg.get("persistence_ref_frames", 5)))

    areas = [p.area_km2 for p in track.points]
    if areas[-1] > areas[0] and areas[0] > 0:
        growth = _clamp01((areas[-1] - areas[0]) / max(areas[0], 1e-9) / 2.0)
    else:
        growth = 0.0
    intensity_kept = 1.0 - _clamp01((maxes[0] - maxes[-1]) / max(maxes[0], 1e-9)) \
        if maxes and maxes[0] > 0 else 1.0
    growth_sustained = 0.6 * growth + 0.4 * intensity_kept

    return {
        "intensity_core": round(intensity_core, 4),
        "organization": round(organization, 4),
        "core_morphology": round(core_morphology, 4),
        "persistence": round(persistence, 4),
        "growth_sustained": round(growth_sustained, 4),
    }


def _gates(track, cfg):
    """Gate espliciti (prima di qualunque claim di supercella)."""
    max_dbz_max = max((p.max_dbz for p in track.points), default=0.0)
    org = track.motion.get("organization_score", 0) or 0
    return {
        "intensity_core_dbz": max_dbz_max >= float(cfg["intensity_core_dbz"]),
        "min_frames": len(track.points) >= int(cfg.get("min_frames", 2)),
        "organization_organized": float(org) >= 56.0,
    }


def classify_level(ssi, cfg=None):
    """Banda di livello per l'SSI (0-100)."""
    cfg = cfg or _cfg_defaults()
    bands = cfg.get("bands", _DEF_BANDS)
    for lo, hi, name in bands:
        if lo <= ssi <= hi:
            return name
    return "non_supercell"


def _integral_ssi(track, cfg):
    """SSI (0-100) + livelli + componenti + gate. richiede >=2 punti."""
    if len(track.points) < 2:
        return 0, "non_supercell", False, {}, {}, {}
    components = _components(track, cfg)
    weights = cfg.get("weights", _DEF_WEIGHTS)
    ssi = 0.0
    for key, comp in components.items():
        ssi += comp * float(weights.get(key, 0.0))
    ssi = int(round(100.0 * max(0.0, min(1.0, ssi))))
    level = classify_level(ssi, cfg)
    gates = _gates(track, cfg)
    candidate = (ssi >= float(cfg.get("candidate_ssi", 65.0))
                 and gates["intensity_core_dbz"] and gates["min_frames"])
    return ssi, level, candidate, gates, components, weights


def supercell_signature_index(track, cfg=None):
    """API pubblica: (ssi, level, candidate, gates, components)."""
    cfg = cfg or _cfg_defaults()
    ssi, level, candidate, gates, components, _w = _integral_ssi(track, cfg)
    return ssi, level, candidate, gates, components


def _candidate_dict(track, track_type, latest_frame_index, cfg, ssi, level,
                    candidate, gates, components):
    """Rappresentazione JSON per supercells.json (solo track candidate)."""
    n = len(track.points)
    last = track.points[-1]
    on_latest = (last.frame_index == latest_frame_index) if latest_frame_index is not None \
        else False
    phase = "birth" if candidate and n <= int(cfg.get("birth_frames", 3)) else \
        ("sustained" if candidate else "n/a")
    max_dbz_max = max(p.max_dbz for p in track.points)
    mean_dbz_mean = sum(p.mean_dbz for p in track.points) / max(n, 1)
    return {
        "supercell_id": "SC-{}-{}{:03d}".format(
            track.points[-1].timestamp_iso[:13], track_type[:3], int(track.track_id)),
        "track_id": track.track_id,
        "track_type": track_type,
        "ssi": ssi,
        "level": level,
        "candidate": candidate,
        "phase": phase,
        "status": track.status,
        "on_latest_frame": on_latest,
        "first_seen": track.points[0].timestamp_iso,
        "last_seen": last.timestamp_iso,
        "position": [round(last.lonlat[0], 5), round(last.lonlat[1], 5)],
        "intensity": {
            "max_dbz": round(max_dbz_max, 2),
            "mean_dbz": round(mean_dbz_mean, 2),
            "delta_dbz": track.motion.get("intensity_delta_dbz"),
        },
        "organization": {
            "organization_score": track.motion.get("organization_score"),
            "classification": track.motion.get("classification"),
        },
        "motion": {
            "velocity_kmh": track.motion.get("velocity_kmh"),
            "direction_toward_deg": track.motion.get("direction_toward_deg"),
            "distance_km": track.motion.get("distance_km"),
            "duration_min": track.motion.get("duration_min"),
            "area_growth_pct": track.motion.get("area_growth_pct"),
        },
        "components": components,
        "gates": gates,
        "confidence": "low",
    }


def _evaluated_tracks(bundle):
    """Lista (track, track_type) da valutare (layer cella + storm object)."""
    out = [(t, "cell") for t in getattr(bundle, "tracks", [])]
    out += [(t, "storm_object") for t in getattr(bundle, "storm_tracks", [])]
    return out


def evaluate(bundle, cfg=None):
    """Valuta tutte le track (cella + storm object) e popola
    bundle.supercells (solo candidati) e bundle.supercell_tracks_evaluated.

    Layer ADDITIVO ed EXPERIMENTAL: mai fallisce il run (errori -> warning in
    main.py). Ritorna la lista dei candidati."""
    cfg = cfg or _cfg_defaults()
    latest_frame_index = None
    if getattr(bundle, "cells_by_frame", None):
        n_frames = sum(1 for f in bundle.cells_by_frame if f)
        latest_frame_index = n_frames - 1 if n_frames else None
    candidates = []
    for track, track_type in _evaluated_tracks(bundle):
        if len(track.points) < 2:
            continue
        ssi, level, candidate, gates, components, _w = _integral_ssi(track, cfg)
        if not candidate:
            continue
        candidates.append(_candidate_dict(
            track, track_type, latest_frame_index, cfg, ssi, level,
            candidate, gates, components))
    bundle.supercells = candidates
    bundle.supercell_tracks_evaluated = len(_evaluated_tracks(bundle))
    return candidates