#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — vortex.py: logica VORTICI (proxy da RIFLETTIVITA', NO Doppler).

Tier (THRESHOLDS_VERSION in phenomena/__init__.py):
  SUSPECT       — organization_score >= 60 (track/candidato) OPPURE morfologia
                  "hook-ish" (eccentricita' alta + solidity bassa + compattita'
                  alta) E persistenza (>= 2 punti recenti o duration_min >= 10).
                  L'evidenza fulmini (gate provider-agnostico
                  lightning_corroborates: strength/soglia della sorgente con
                  fallback count_near per DPC, OPPURE trend in salita) viene
                  COMBINATA in labels+score quando il provider e' disponibile:
                  con provider unavailable l'evento resta SUSPECT, come per la
                  grandine (nessuna promozione senza evidenza).
  CORROBORATED  — organization_score >= 70 E gate fulmini (sopra). Con
                  organization_score assente (solo morfologia) o fulmini
                  unavailable -> massimo SUSPECT.
  VERIFIED      — NON popolato (arrivera' con report esterni ESWD).

Sorgente non puntuale (MLI/AFA): evidence.lightning.note = "AFA proxy (no
flash puntuali)" + attribution EUMETSAT, count_near in PIXEL (label esplicita).

Ogni label esplicita: "proxy riflettività, nessun dato Doppler" — il motore
NON dichiara mai mesociclone/tornado.
"""

from . import THRESHOLDS_VERSION
from .lightning import lightning_corroborates, lightning_strength

VORTEX_ORG_SUSPECT = 60         # organization_score per SUSPECT
VORTEX_ORG_CORROBORATED = 70    # organization_score per CORROBORATED
VORTEX_MIN_POINTS = 2           # punti recenti di persistenza
VORTEX_MIN_DURATION_MIN = 10.0  # persistenza per target senza punti
VORTEX_LTG_MIN_STRIKES = 10     # fulmini nel raggio (gate DPC legacy)
# Proxy morfologico "hook-ish" (soglie sperimentali sulle celle di storms.geojson)
MORPH_ECC_MIN = 0.90
MORPH_SOLIDITY_MAX = 0.85
MORPH_COMPACTNESS_MIN = 3.0

DOPPLER_DISCLAIMER = "proxy riflettività, nessun dato Doppler"


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def is_hookish(obs):
    """True se la cella e' "hook-ish" per eccentricity/solidity/compattita'.

    Tutte e tre le condizioni devono reggere (i valori medi delle celle sono
    gia' eccentrici: con una sola condizione il gate non discrimina nulla)."""
    if not isinstance(obs, dict):
        return False
    ecc = _num(obs.get("eccentricity_max"))
    sol = _num(obs.get("solidity_min"))
    comp = _num(obs.get("compactness_max"))
    if ecc is None or sol is None or comp is None:
        return False
    return (ecc >= MORPH_ECC_MIN and sol <= MORPH_SOLIDITY_MAX
            and comp >= MORPH_COMPACTNESS_MIN)


def vortex_score(state, organization_score, lightning):
    """Score 0-100: base dal tier + contributi (organization, fulmini, trend).

    Contributi limitati a 29 punti -> SUSPECT <= 69, CORROBORATED <= 99."""
    if state == "VERIFIED":
        return 100.0
    base = {"SUSPECT": 40.0, "CORROBORATED": 70.0}.get(state, 40.0)
    org = _num(organization_score)
    org_t = max(0.0, min(1.0, (org - VORTEX_ORG_SUSPECT) / 20.0)) \
        if org is not None else 0.0
    ltg = lightning if isinstance(lightning, dict) else {}
    ltg_t = lightning_strength(ltg)        # strength 0..1, fallback count/30
    direction = (ltg.get("trend") or {}).get("direction") \
        if isinstance(ltg.get("trend"), dict) else None
    trend_t = 1.0 if direction == "up" else 0.0
    raw = base + 29.0 * (0.50 * org_t + 0.30 * ltg_t + 0.20 * trend_t)
    return round(min(99.9, raw), 1)


def evaluate_vortex(obs, lightning=None):
    """Valuta VORTEX su un'osservazione normalizzata -> evento dict oppure None."""
    if not isinstance(obs, dict) or not obs.get("anchor"):
        return None
    source = obs.get("persistence_source") or "points"
    pts = [p for p in (obs.get("recent_points") or []) if isinstance(p, dict)]
    duration = _num(obs.get("duration_min"))
    if source == "duration":
        persistent = bool(obs.get("on_latest_frame")) \
            and duration is not None and duration >= VORTEX_MIN_DURATION_MIN
    else:
        persistent = len(pts) >= VORTEX_MIN_POINTS \
            or (duration is not None and duration >= VORTEX_MIN_DURATION_MIN)
    if not persistent:
        return None

    org = _num(obs.get("organization_score"))
    hookish = is_hookish(obs)
    org_gate = org is not None and org >= VORTEX_ORG_SUSPECT
    if not (org_gate or hookish):
        return None

    ltg = lightning if isinstance(lightning, dict) else {"available": False}
    ltg_avail = bool(ltg.get("available"))
    count_near = int(ltg.get("count_near") or 0) if ltg_avail else 0
    strength = lightning_strength(ltg) if ltg_avail else None
    trend_dir = None
    if ltg_avail and isinstance(ltg.get("trend"), dict):
        trend_dir = ltg["trend"].get("direction")
    ltg_score = ltg.get("score") if ltg_avail else None
    ltg_strong = ltg_avail and (trend_dir == "up"
                                or lightning_corroborates(ltg))
    corroborated = (org is not None and org >= VORTEX_ORG_CORROBORATED
                    and ltg_strong)
    state = "CORROBORATED" if corroborated else "SUSPECT"

    evidence = {
        "anchor": obs.get("anchor"),
        "track_type": obs.get("type"),
        "classification": obs.get("classification"),
        "persistence_source": source,
        "organization_score": int(org) if org is not None else None,
        "hookish_morphology": bool(hookish),
        "morphology": {
            "eccentricity_max": _num(obs.get("eccentricity_max")),
            "solidity_min": _num(obs.get("solidity_min")),
            "compactness_max": _num(obs.get("compactness_max")),
        },
        "recent_points": len(pts),
        "duration_min": duration,
        "n_frames": obs.get("n_frames"),
        "lightning": {
            "available": ltg_avail,
            "source": ltg.get("source"),
            "count_near": count_near,
            "count_total": int(ltg.get("count_total") or 0),
            "radius_km": ltg.get("radius_km"),
            "direction": trend_dir,
            "score": ltg_score,
            "reason": ltg.get("reason"),
            "strength": strength,
            "note": ltg.get("note"),
            "attribution": ltg.get("attribution"),
        },
        "doppler": False,
        "labels": [],
    }

    labels = [f"tier {state}"]
    if org is not None:
        labels.append(f"organization_score {org:.0f} "
                      f"(>= {VORTEX_ORG_SUSPECT} per suspect, "
                      f">= {VORTEX_ORG_CORROBORATED} per corroborated)")
    if hookish:
        labels.append("morfologia hook-proxy (ecc alta / solidity bassa)")
    if source == "duration":
        labels.append(f"persistenza {duration:.0f} min "
                      f"(>= {VORTEX_MIN_DURATION_MIN:.0f})")
    else:
        labels.append(f"persistenza {len(pts)} punti recenti")
    if ltg_avail:
        radius = ltg.get("radius_km")
        note = ltg.get("note")
        if note:
            # sorgente non puntuale (MLI): count_near e' in PIXEL, mai
            # presentato come numero di fulmini
            labels.append(f"{count_near} px AFA nel raggio {radius} km "
                          f"(trend {trend_dir}) — {note}")
        else:
            labels.append(f"fulmini {count_near} in {radius} km "
                          f"(trend {trend_dir}, score {ltg_score})")
        if ltg.get("attribution"):
            labels.append(str(ltg["attribution"]))
    else:
        labels.append("fulmini non disponibili")
    labels.append(DOPPLER_DISCLAIMER)
    evidence["labels"] = labels

    return {
        "id": f"VORTEX-{obs['anchor']}",
        "type": "VORTEX",
        "state": state,
        "score": vortex_score(state, org, ltg),
        "first_seen": obs.get("first_seen"),
        "last_seen": obs.get("last_seen"),
        "position": list(obs.get("position") or []),
        "evidence": evidence,
        "thresholds_version": THRESHOLDS_VERSION,
    }
