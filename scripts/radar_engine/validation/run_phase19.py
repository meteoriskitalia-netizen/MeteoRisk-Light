#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk Radar Engine — validation.run_phase19 (Fase 1.9)

Motion Physics Validation & Kinematic Sanitization.

Pipeline STESSA di run_phase18 (detection, cell tracking, aggregation,
StormObjectTracker, scoring) MAI modificati; feature della fase:
  * raw/validated motion split (Parte A) — nessun clamp;
  * robust multi-frame velocity = MEDIAN segment (Parte C, no Kalman);
  * coordinate metriche: distanze geodesiche in km, mai gradi (Parte D);
  * physical speed gate `storm.motion.max_validated_velocity_kmh` applicato
    SOLO in finalize (validazione). Il matching storm resta Fase 1.6:
    `storm.tracking.max_storm_speed_kmh` NON viene toccato dalla calibrazione
    (esperimento preliminare: gate sul matching -> continuità 0.585->0.28 e
    birth rate 0.116->0.34 -> violazione della success criterion n.4);
  * merge/split -> motion_status ambiguous_geometry + penalty (Parte E).

Calibrazione gate (Part B): [100, 150, 200, 250, 300] km/h + baseline None.
Metriche per soglia richieste: rejected segments, track fragmentation,
track continuity, motion confidence, distribuzione velocità valida
(p50/p90/p95/p99/max). La scelta finale del gate è DOCUMENTATA (CRITICAL RULE:
mai 'meno rejection' o 'miglior continuity' come criteri unici).

Part H: 25 casi reali, BEFORE (raw path speed, semantics Fase 1.8) vs AFTER
(validated robust median con gate raccomandato).

Output (derivati, mai raw):
    data/validation/motion_summary.json

Uso: python scripts/radar_engine/validation/run_phase19.py [--limit N] \
     [--case-id ID] [--out DIR]
"""

import argparse
import copy
import json
import os
import sys
from collections import Counter
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import numpy as np  # noqa: E402

from radar_engine.config import load_config  # noqa: E402
from radar_engine.validation.run_phase18 import (  # noqa: E402
    OUT_VALIDATION, SAMPLES_ROOT, _load_extended_cases, _resolve_frames_inclusive,
    _run_pipeline, _case_metrics, _quiet_metrics, _long_tracks)

QUIET = "QUIET"

# soglie da calibrare (plus baseline None)
GATES_KMH = [100, 150, 200, 250, 300]

# gate raccomandato a valle della calibrazione (costante: NESSUN criterio
# automatico; scelta documentata in docs/PHASE19_MOTION_VALIDATION_REPORT.md)
RECOMMENDED_GATE_KMH = 200


def _vel_stats(series):
    """p50/p90/p95/p99/max (più mean/min/n/cv) su valori non-None in km/h."""
    arr = np.asarray([float(x) for x in series if x is not None], dtype=float)
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "mean": round(float(arr.mean()), 1),
        "p50": round(float(np.median(arr)), 1),
        "p90": round(float(np.percentile(arr, 90)), 1),
        "p95": round(float(np.percentile(arr, 95)), 1),
        "p99": round(float(np.percentile(arr, 99)), 1),
        "min": round(float(arr.min()), 1),
        "max": round(float(arr.max()), 1),
        "cv": round(float(arr.std() / max(abs(float(arr.mean())), 1e-9)), 2),
    }


def _storm_view(tracks):
    """Contatori/velocità aggregate di un singolo caso (layer storm)."""
    long = _long_tracks(tracks)
    status = Counter(t.motion.get("motion_status") or "not_set"
                     for t in long)
    valid_vel = [t.motion.get("median_segment_velocity_kmh") for t in long
                 if t.motion.get("velocity_valid")
                 and t.motion.get("median_segment_velocity_kmh") is not None]
    raw_vel = [t.motion.get("raw_velocity_kmh") for t in long
               if t.motion.get("raw_velocity_kmh") is not None]
    seg_total = sum(t.motion.get("total_segments") or 0 for t in long)
    seg_rejected = sum(t.motion.get("rejected_segments") or 0 for t in long)
    seg_amb = sum(t.motion.get("ambiguous_segments") or 0 for t in long)
    mc = Counter(getattr(t, "motion_confidence", "low") for t in long)
    n_penalized = sum(1 for t in long
                      if t.motion.get("motion_confidence_ambiguity_penalty"))
    return {
        "n_long": len(long),
        "status": dict(status),
        "validated_velocity_kmh": _vel_stats(valid_vel),
        "raw_velocity_kmh": _vel_stats(raw_vel),
        "valid_vel": valid_vel,
        "raw_vel": raw_vel,
        "seg_total": seg_total,
        "seg_rejected": seg_rejected,
        "seg_ambiguous": seg_amb,
        "mc": dict(mc),
        "mc_penalized": n_penalized,
    }


def _merge_views(views):
    valid_vel, raw_vel = [], []
    status = Counter()
    mc = Counter()
    seg_total = seg_rejected = seg_amb = n_long = n_penalized = 0
    for v in views:
        valid_vel.extend(v["valid_vel"])
        raw_vel.extend(v["raw_vel"])
        status.update(v["status"])
        mc.update(v["mc"])
        seg_total += v["seg_total"]
        seg_rejected += v["seg_rejected"]
        seg_amb += v["seg_ambiguous"]
        n_long += v["n_long"]
        n_penalized += v["mc_penalized"]
    n_valid = status.get("valid", 0) + status.get("ambiguous_geometry", 0)
    n_rejected = status.get("rejected", 0)
    return {
        "n_long": n_long,
        "status": dict(status),
        "n_valid": n_valid,
        "n_rejected": n_rejected,
        "valid_velocity_fraction": round(n_valid / n_long, 4)
        if n_long else None,
        "rejected_velocity_fraction": round(n_rejected / n_long, 4)
        if n_long else None,
        "segment_statistics": {
            "total": seg_total,
            "rejected": seg_rejected,
            "rejected_fraction": round(seg_rejected / seg_total, 4)
            if seg_total else None,
            "ambiguous": seg_amb,
        },
        "motion_confidence": dict(mc),
        "mc_penalized": n_penalized,
        "validated_velocity_kmh": _vel_stats(valid_vel),
        "raw_velocity_kmh": _vel_stats(raw_vel),
    }


def _main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--case-id", default=None)
    ap.add_argument("--out", default=OUT_VALIDATION)
    args = ap.parse_args(argv)

    base_cfg = load_config()
    # matching storm resta Fase 1.6 (max_storm_speed_kmh=None); il gate di
    # VALIDAZIONE varia su storm.motion.max_validated_velocity_kmh
    base_cfg = copy.deepcopy(base_cfg)
    base_cfg["storm"]["tracking"]["max_storm_speed_kmh"] = None
    base_cfg["storm"]["motion"]["max_validated_velocity_kmh"] = None

    cases = _load_extended_cases()
    if args.case_id:
        cases = [c for c in cases if c["id"] == args.case_id]
    if args.limit:
        cases = cases[:args.limit]

    gate_results = {str(g): {"views": [], "cms": [], "cases": []}
                    for g in [None] + GATES_KMH}
    per_case = {}
    quiet_reports = []

    for case in cases:
        frames = _resolve_frames_inclusive(case, SAMPLES_ROOT)
        if not frames:
            print(f"[{case['id']}] WARN nessun frame")
            continue
        cid = case["id"]
        print(f"[{cid}] frames={len(frames)} ...")
        res0 = _run_pipeline(frames, base_cfg)
        cache = {"cells_by_frame": res0["cells_by_frame"],
                 "cell_tracks": res0["cell_tracks"]}
        entry = {"case": cid, "category": case["category"],
                 "date": case["date"], "frames": len(frames)}
        if case["category"] == QUIET:
            qm = _quiet_metrics(res0, case)
            quiet_reports.append(qm)
            entry["quiet"] = qm["false_activity"]
            per_case[cid] = entry
            continue
        for gate in [None] + GATES_KMH:
            cfg = copy.deepcopy(base_cfg)
            cfg["storm"]["motion"]["max_validated_velocity_kmh"] = gate
            res = _run_pipeline(frames, cfg, cache=cache)
            view = _storm_view(res["storm_tracks"])
            cm = _case_metrics(res, case)
            gate_results[str(gate)]["views"].append(view)
            gate_results[str(gate)]["cms"].append(cm)
            gate_results[str(gate)]["cases"].append(cid)
            if gate == RECOMMENDED_GATE_KMH or (gate is None and
                                                RECOMMENDED_GATE_KMH is None):
                entry["motion"] = view
                entry["tracking"] = {
                    "continuity": cm["tracking"]["continuity"],
                    "birth_rate": cm["tracking"]["birth_rate"],
                    "n_long_tracks": cm["tracking"]["n_long_tracks"],
                    "n_storm_tracks": cm["tracking"]["n_storm_tracks"],
                    "edge_of_window_share": cm["tracking"]["edge_of_window_share"],
                }
                entry["motion_stats"] = {
                    "continuity": cm["tracking"]["continuity"],
                    "birth_rate": cm["tracking"]["birth_rate"],
                    "mc_high_frac": cm["motion"]["motion_confidence_high_frac"],
                    "org_median": cm["organization"]["organization_score"].get("median"),
                }
        per_case[cid] = entry

    calibration = []
    for gate in [None] + GATES_KMH:
        gk = str(gate)
        grp = gate_results[gk]
        merged = _merge_views(grp["views"])
        calibration.append({
            "gate_kmh": gate,
            "gate_label": "Fase 1.8 (nessun gate)" if gate is None
                          else f"max_validated_velocity_kmh={gate}",
            "n_cases": len(grp["cases"]),
            "n_long_tracks": merged["n_long"],
            "valid_velocity_fraction": merged["valid_velocity_fraction"],
            "rejected_velocity_fraction": merged["rejected_velocity_fraction"],
            "status": merged["status"],
            "segment_statistics": merged["segment_statistics"],
            "continuity": _case_agg(grp["cms"], "continuity"),
            "track_fragmentation_birth_rate": _case_agg_birth(grp["cms"]),
            "motion_confidence": merged["motion_confidence"],
            "mc_high_frac": round(
                merged["motion_confidence"].get("high", 0) /
                max(merged["n_long"], 1), 4),
            "validated_velocity_kmh": merged["validated_velocity_kmh"],
            "raw_velocity_kmh": merged["raw_velocity_kmh"],
            "organization_score_median": None,
        })

    for row, gate in zip(calibration, [None] + GATES_KMH):
        grp = gate_results[str(gate)]
        orgs = [c["organization"]["organization_score"].get("median")
                for c in grp["cms"]
                if c["organization"]["organization_score"].get("n")]
        row["organization_score_median"] = round(
            float(np.median(orgs)), 1) if orgs else None

    before = next(r for r in calibration if r["gate_kmh"] is None)
    after = next(r for r in calibration
                 if r["gate_kmh"] == RECOMMENDED_GATE_KMH)
    delta = {
        "valid_velocity_fraction": round(
            (after["valid_velocity_fraction"] or 0.0) -
            (before["valid_velocity_fraction"] or 0.0), 4),
        "rejected_velocity_fraction": round(
            (after["rejected_velocity_fraction"] or 0.0) -
            (before["rejected_velocity_fraction"] or 0.0), 4),
        "velocity_p50": round(
            (after["validated_velocity_kmh"]["p50"] if
             after["validated_velocity_kmh"].get("n", 0) else 0.0) -
            (before["raw_velocity_kmh"]["p50"] if
             before["raw_velocity_kmh"].get("n", 0) else 0.0), 1),
        "velocity_p90": round(
            (after["validated_velocity_kmh"]["p90"] if
             after["validated_velocity_kmh"].get("n", 0) else 0.0) -
            (before["raw_velocity_kmh"]["p90"] if
             before["raw_velocity_kmh"].get("n", 0) else 0.0), 1),
        "velocity_p99": round(
            (after["validated_velocity_kmh"]["p99"] if
             after["validated_velocity_kmh"].get("n", 0) else 0.0) -
            (before["raw_velocity_kmh"]["p99"] if
             before["raw_velocity_kmh"].get("n", 0) else 0.0), 1),
        "velocity_max": round(
            (after["validated_velocity_kmh"]["max"] if
             after["validated_velocity_kmh"].get("n", 0) else 0.0) -
            (before["raw_velocity_kmh"]["max"] if
             before["raw_velocity_kmh"].get("n", 0) else 0.0), 1),
        "continuity": round((after["continuity"] or 0.0) -
                            (before["continuity"] or 0.0), 4),
        "birth_rate": round((after["track_fragmentation_birth_rate"] or 0.0) -
                            (before["track_fragmentation_birth_rate"] or 0.0), 4),
        "mc_high_frac": round((after["mc_high_frac"] or 0.0) -
                              (before["mc_high_frac"] or 0.0), 4),
    }

    summary = {
        "schema_version": "1.0",
        "phase": "1.9",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "freeze_algorithm": {
            "unchanged": ["detect", "cell_tracking", "aggregation",
                          "storm_tracking_matching", "scoring",
                          "organization_score"],
            "added": ["storm motion validation (finalize): raw/validated split",
                      "robust median multi-frame velocity",
                      "physical speed gate in finalize",
                      "merge/split ambiguous_geometry + penalty"],
        },
        "method": {
            "distance": "haversine geodesica in km (mai gradi EPSG:4326)",
            "robust_velocity": "mediana velocità segmentali consecutive "
                               "(no Kalman, no ML)",
            "raw_velocity": "path-speed (somma spostamenti / durata) "
                            "identica alla semantics Fase 1.8",
            "gate_scope": "SOLO finalize (validazione). Matching storm invariato "
                          "(max_storm_speed_kmh=None, Fase 1.6): l'esperimento "
                          "preliminare col gate anche nel matching degradava "
                          "continuità/birth (violazione success criterion 4) "
                          "e l'analisi è documentata nel report",
        },
        "recommended_gate_kmh": RECOMMENDED_GATE_KMH,
        "calibration": calibration,
        "before_after": {
            "before": {"label": "Fase 1.8 (no gate) — velocity_kmh=raw path",
                       **before},
            "after": {"label": f"Fase 1.9 — velocity_kmh=validated robust "
                               f"median (gate {RECOMMENDED_GATE_KMH})",
                      **after},
            "delta": delta,
        },
        "metrics_by_case": per_case,
        "quiet_cases": quiet_reports,
    }

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "motion_summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {path}")
    return 0


def _case_agg(cms, key):
    vals = [c["tracking"][key] for c in cms
            if c["tracking"].get(key) is not None]
    return round(float(np.mean(vals)), 4) if vals else None


def _case_agg_birth(cms):
    vals = [c["tracking"]["birth_rate"] for c in cms
            if c["tracking"].get("birth_rate") is not None]
    return round(float(np.mean(vals)), 4) if vals else None


if __name__ == "__main__":
    raise SystemExit(_main())