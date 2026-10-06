#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk Radar Engine — validation.run_phase18 (Fase 1.8)

Extended Real-World Validation. Nessuna modifica al motore (FREEZE ALGORITHM):
riusa detect / tracking (cell) / aggregation / StormObjectTracker / scoring
con la configurazione di produzione; per la SENSITIVITY applica varianti di
parametro SOLO in esecuzione, mai salvate come default.

Casi: cases/cases_extended.json (25 casi reali, finestre VMI inclusive).
Input raw: RADAR_SAMPLES_DIR (esterno al repo). Output derivato (mai raw):
    data/validation/extended_summary.json

Sezioni metriche (Part F) per caso — QUIET cases (false activity),
SENSITIVITY multi-parametrica (Part H), analysis regionale (Part I),
FAILURE analysis (Part J), EXTREME EVENTS (Part K). Metriche = PROXY operativi
con definizioni esplicite (stesso caveat di metrics.py): nessuna affermazione
di verità assoluta e nessuna classificazione di supercelle.

Uso: python scripts/radar_engine/validation/run_phase18.py [--limit N] \
     [--case-id ID] [--skip-sensitivity]
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

from radar_engine import (aggregation, detect, models, preprocess,  # noqa: E402
                          storm_tracking, tracking)
from radar_engine.config import CONFIG, load_config  # noqa: E402
from radar_engine.validation import cases, metrics, phase17_compare  # noqa: E402

OUT_VALIDATION = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "data", "validation"))
SAMPLES_ROOT = cases.samples_root()

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))))
CASES_EXTENDED = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))),
    "cases", "cases_extended.json"))

QUIET = "QUIET"
SEVERE = "SEVERE_DOCUMENTED"


def _load_extended_cases():
    with open(CASES_EXTENDED, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload.get("cases", [])


def _resolve_frames_inclusive(case, root):
    """Finestra [start, end] INCLUSIVA su griglia PT5M (solo file esistenti)."""
    t0 = int(datetime.strptime(case["start"], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc).timestamp() * 1000)
    t1 = int(datetime.strptime(case["end"], "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=timezone.utc).timestamp() * 1000)
    step_ms = 5 * 60000
    out = []
    ts = t0
    while ts <= t1:
        dt = datetime.fromtimestamp(ts / 1000.0, timezone.utc)
        daydir = os.path.join(root, dt.strftime("%Y-%m-%d"))
        fname = f"VMI-{dt.strftime('%H%M')}.tif"
        path = os.path.join(daydir, fname)
        if os.path.exists(path):
            out.append((path, ts))
        ts += step_ms
    return out


def _run_pipeline(frames, cfg, cache=None):
    """pipeline identica a run_phase17._exec ma con cfg configurabile.
    cache = {'cells_by_frame': [...], 'cell_tracks': [...]} per sensitivity
    storm-only (riusa detect+cell tracking senza ricalcolarli)."""
    n = len(frames)
    cells_by_frame, warnings = [], []
    if cache is not None:
        cells_by_frame = cache["cells_by_frame"]
    else:
        for idx, (path, ts) in enumerate(frames):
            try:
                raster = preprocess.read_raster(
                    str(path),
                    nodata_values=cfg["preprocess"]["nodata_values"],
                    geo_plausible_bbox=cfg["preprocess"]["geo_plausible_bbox"])
            except Exception as exc:  # noqa: BLE001 — diagnostica di validazione
                warnings.append({"frame": idx, "error": str(exc)})
                cells_by_frame.append([])
                continue
            raster.time_ms = int(ts)
            raster.time_iso = datetime.fromtimestamp(ts / 1000.0, timezone.utc)\
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            det = dict(cfg["detect"])
            cells = detect.detect_cells(raster, det)
            for c in cells:
                c.frame_index = idx
            cells_by_frame.append(cells)

    cell_tracks = cache["cell_tracks"] if cache is not None else None
    if cell_tracks is None:
        tracker = tracking.Tracker(cfg["tracking"], cfg_scoring=cfg["scoring"])
        for idx, cells in enumerate(cells_by_frame):
            tracker.update(cells, idx)
        tracker.complete_cycles()
        cell_tracks = tracker.finalize(cfg_scoring=cfg["scoring"])

    storm_cfg = cfg["storm"]
    objs_by_frame = [aggregation.aggregate_frame(cells, idx, storm_cfg)
                     for idx, cells in enumerate(cells_by_frame)]
    storm_tracker = storm_tracking.StormObjectTracker(
        storm_cfg, cfg_scoring=cfg["scoring"])
    for idx, objs in enumerate(objs_by_frame):
        storm_tracker.update(objs, idx)
    storm_tracker.complete_cycles()
    storm_tracks = storm_tracker.finalize(cfg_scoring=cfg["scoring"])

    return {
        "frames": n,
        "cells_by_frame": cells_by_frame,
        "cell_tracks": cell_tracks,
        "objs_by_frame": objs_by_frame,
        "storm_tracks": storm_tracks,
        "warnings": warnings,
    }


def _dist_stats(series, ndigits=2):
    if not series:
        return {"n": 0}
    arr = np.asarray(series, dtype=float)
    return {
        "n": int(len(arr)),
        "mean": round(float(arr.mean()), ndigits),
        "median": round(float(np.median(arr)), ndigits),
        "p25": round(float(np.percentile(arr, 25)), ndigits),
        "p75": round(float(np.percentile(arr, 75)), ndigits),
        "p90": round(float(np.percentile(arr, 90)), ndigits),
        "min": round(float(arr.min()), ndigits),
        "max": round(float(arr.max()), ndigits),
        "cv": round(float(arr.std() / max(abs(float(arr.mean())), 1e-9)),
                    ndigits),
    }


def _long_tracks(tracks):
    return [t for t in tracks if len(t.points) >= 2]


def _case_metrics(res, case):
    """Part F metriche per caso (layer storm + cell)."""
    objs_by_frame = res["objs_by_frame"]
    storm_tracks = res["storm_tracks"]
    cells_by_frame = res["cells_by_frame"]
    n = res["frames"]

    cells_per_frame = [len(c) for c in cells_by_frame]
    objs_per_frame = [len(o) for o in objs_by_frame]
    obj_cell_ratios = [objs / cells for objs, cells in zip(objs_per_frame,
                                                           cells_per_frame)
                       if cells > 0]
    obj_areas = [o.area_km2 for objs in objs_by_frame for o in objs]

    det = {
        "cells_per_frame": {"stats": _dist_stats(cells_per_frame),
                            "series": cells_per_frame},
        "objects_per_frame": {"stats": _dist_stats(objs_per_frame),
                              "series": objs_per_frame},
        "objects_per_cell_ratio": _dist_stats(
            obj_cell_ratios, ndigits=3),
        "object_area_km2": _dist_stats(obj_areas, ndigits=1),
        "detection_persistence": round(
            sum(1 for c in cells_per_frame if c > 0) / n, 3) if n else 0.0,
        "frames": n,
    }

    long = _long_tracks(storm_tracks)
    durations = [(t.points[-1].timestamp_ms - t.points[0].timestamp_ms) / 60000.0
                 for t in long]
    birth_rate = phase17_compare.storm_birth_rate(
        storm_tracks, sum(objs_per_frame))
    continuity = phase17_compare.storm_track_continuity(storm_tracks, n)
    total_objs = sum(objs_per_frame)
    _amb = Counter(o.tracking_ambiguity for objs in objs_by_frame
                   for o in objs if o.tracking_ambiguity is not None)
    edge_share = 0.0
    if long:
        edge = sum(1 for t in long
                   if t.points[0].frame_index == 0
                   or t.points[-1].frame_index == n - 1)
        edge_share = round(edge / len(long), 3)

    vels = [t.motion.get("velocity_kmh") for t in long
            if t.motion.get("velocity_kmh") is not None]
    mc = Counter(getattr(t, "motion_confidence", "low") for t in long)
    sc = Counter(getattr(t, "score_confidence", "low") for t in storm_tracks)

    trk = {
        "n_storm_tracks": int(len(storm_tracks)),
        "n_long_tracks": int(len(long)),
        "track_duration_min": _dist_stats(durations, ndigits=1),
        "birth_rate": (round(birth_rate, 3)
                       if birth_rate is not None else None),
        "continuity": continuity,
        "fragmentation_objects_per_cell": det["objects_per_cell_ratio"]["mean"]
        if det["objects_per_cell_ratio"]["n"] else None,
        "ambiguity_objects": dict(_amb),
        "ambiguity_high_frac": round(
            _amb.get("high", 0) / max(total_objs, 1), 3),
        "edge_of_window_share": edge_share,
    }

    mot = {
        "velocity_kmh": _dist_stats(vels, ndigits=1),
        "direction_stability_deg": phase17_compare.storm_direction_stability(
            storm_tracks),
        "motion_error_deg": phase17_compare.storm_motion_error(storm_tracks),
        "motion_confidence": dict(mc),
        "motion_confidence_high_frac": round(
            mc.get("high", 0) / max(len(long), 1), 3),
    }

    score_stats = metrics.score_stats(storm_tracks)
    temporal = []
    if n:
        by_frame = [[] for _ in range(n)]
        for t in long:
            s = t.motion.get("organization_score")
            if s is None:
                continue
            for p in t.points:
                if 0 <= p.frame_index < n:
                    by_frame[p.frame_index].append(s)
        temporal = []
        for i, vals in enumerate(by_frame):
            frame_objs = objs_by_frame[i]
            max_dbz = max((o.max_dbz for o in frame_objs), default=None)
            temporal.append({
                "frame": i,
                "objects": len(frame_objs),
                "max_dbz": (round(max_dbz, 1)
                            if max_dbz is not None else None),
                "mean_score": round(float(np.mean(vals)), 1) if vals else None,
                "n": len(vals)})
    ovo = [s for row in temporal if row["mean_score"] is not None
           for s in [row["mean_score"]]]
    score_conf_high = sc.get("high", 0)

    org = {
        "organization_score": score_stats,
        "score_confidence": dict(sc),
        "score_confidence_high_frac": round(
            score_conf_high / max(len(storm_tracks), 1), 3),
        "temporal_stability_cv": round(
            float(np.std(ovo) / max(abs(np.mean(ovo)), 1e-9)), 3) if len(ovo) > 1
            else None,
        "temporal": temporal,
        "false_high_storm": metrics.false_high_scores(storm_tracks),
    }

    comparison = phase17_compare.compare_layers(
        cells_by_frame, res["cell_tracks"], objs_by_frame, storm_tracks)

    return {
        "case": case["id"],
        "category": case["category"],
        "date": case["date"],
        "region": case["region"],
        "season": case["season"],
        "ground_truth_quality": case["ground_truth_quality"],
        "window": f"{case['start']}->{case['end']}",
        "frames": n,
        "detection": det,
        "tracking": trk,
        "motion": mot,
        "organization": org,
        "comparison": comparison["storm_object"],
        "phase17_success_criteria": comparison["success_criteria"],
        "phase17_success": comparison["success"],
        "warnings": res["warnings"],
    }


def _quiet_metrics(res, case):
    cells_by_frame = res["cells_by_frame"]
    objs_by_frame = res["objs_by_frame"]
    storm_tracks = res["storm_tracks"]
    objs = [o for objs in objs_by_frame for o in objs]
    return {
        "case": case["id"],
        "category": case["category"],
        "quiet_case": True,
        "frames": res["frames"],
        "false_activity": {
            "total_cells": int(sum(len(c) for c in cells_by_frame)),
            "cells_by_frame": [len(c) for c in cells_by_frame],
            "total_storm_objects": int(sum(len(o) for o in objs_by_frame)),
            "total_tracks": int(len(storm_tracks)),
            "false_high_org": int(len(metrics.false_high_scores(storm_tracks))),
        },
        "warnings": res["warnings"],
    }


def _assess(case_metrics, case):
    """Qualità complessiva del caso (descrittiva, non un giudizio meteo)."""
    if case["category"] == QUIET:
        return {"label": "QUIET_OK", "issues": ["QUIET_OK"],
                "detail": "nessuna attività attesa"}
    det = case_metrics["detection"]
    trk = case_metrics["tracking"]
    mot = case_metrics["motion"]
    n_long = trk["n_long_tracks"]
    issues = []
    if det["objects_per_cell_ratio"]["n"] and \
            det["objects_per_cell_ratio"]["mean"] > 0.85 and \
            det["cells_per_frame"]["stats"]["median"] >= 5:
        issues.append("UNDER_AGGREGATION")
    if det["objects_per_cell_ratio"]["n"] and \
            det["objects_per_cell_ratio"]["mean"] < 0.3 and \
            det["cells_per_frame"]["stats"]["median"] >= 5:
        issues.append("OVER_AGGREGATION")
    if n_long >= 2:
        if (trk["birth_rate"] or 0.0) > 0.5:
            issues.append("TRACKING_FRAGMENTATION")
        if trk["continuity"] < 0.25 and n_long >= 3:
            issues.append("TRACKING_FRAGMENTATION")
        if mot["motion_error_deg"] > 70.0:
            issues.append("MOTION_INSTABILITY")
        if mot["direction_stability_deg"] > 40.0:
            issues.append("MOTION_INSTABILITY")
    if trk["edge_of_window_share"] > 0.7 and n_long >= 3:
        issues.append("EDGE_OF_WINDOW")
    cp = det["cells_per_frame"]["stats"]
    if cp["cv"] > 1.0 and cp["max"] > 2.5 * max(cp["median"], 1):
        issues.append("SEGMENTATION_FAILURE")
    if not issues:
        issues.append("UNKNOWN")
    seen, ordered = set(), []
    for issue in issues:
        if issue not in seen:
            seen.add(issue)
            ordered.append(issue)
    return {"label": "+".join(ordered), "issues": ordered}


def _regional(metrics_by_case, categories_map):
    groups = {}
    for cm in metrics_by_case:
        key = cm["region"]
        groups.setdefault(key, []).append(cm)
    out = {}
    for reg, cms in groups.items():
        n = len(cms)
        out[reg] = {
            "n": n,
            "case_ids": [c["case"] for c in cms],
            "cells_per_frame_mean": round(float(np.mean([
                c["detection"]["cells_per_frame"]["stats"]["mean"]
                for c in cms])), 2),
            "objects_per_frame_mean": round(float(np.mean([
                c["detection"]["objects_per_frame"]["stats"]["mean"]
                for c in cms])), 2),
            "object_area_km2_median": round(float(np.median([
                c["detection"]["object_area_km2"]["median"]
                for c in cms])), 1),
            "ambiguity_high_frac_mean": round(float(np.mean([
                c["tracking"]["ambiguity_high_frac"] for c in cms])), 3),
            "motion_confidence_high_frac_mean": round(float(np.mean([
                c["motion"]["motion_confidence_high_frac"] for c in cms])), 3),
            "organization_score_median": round(float(np.median([
                c["organization"]["organization_score"]["median"]
                if c["organization"]["organization_score"].get("n")
                else 0.0 for c in cms])), 1),
        }
    return out


def _sensitivity_summary(res):
    cells_per_frame = [len(c) for c in res["cells_by_frame"]]
    objs_per_frame = [len(o) for o in res["objs_by_frame"]]
    n = len(cells_per_frame)
    storm = res["storm_tracks"]
    long = _long_tracks(storm)
    mc = Counter(getattr(t, "motion_confidence", "low") for t in long)
    org_med = None
    scores = [t.motion.get("organization_score") for t in long
              if t.motion.get("organization_score") is not None]
    if scores:
        org_med = round(float(np.median(scores)), 1)
    return {
        "cells_mean": round(float(np.mean(cells_per_frame)), 2)
        if cells_per_frame else 0.0,
        "objects_mean": round(sum(objs_per_frame) / max(n, 1), 2),
        "long_tracks": len(long),
        "birth_rate": phase17_compare.storm_birth_rate(
            storm, sum(objs_per_frame)),
        "continuity": phase17_compare.storm_track_continuity(storm, n),
        "mc_high_frac": round(mc.get("high", 0) / max(len(long), 1), 3),
        "org_median": org_med,
    }


def _run_sensitivity(base_cfg, cases_list):
    """Part H — varianti di parametro su casi rappresentativi (default invariati)."""
    det_cases = ["SEV20260721", "SEV20260820", "SIT20260908", "QUIET20260722"]
    storm_cases = ["SEV20260721", "ORG20260824", "SIT20260908"]
    by_id = {c["id"]: c for c in cases_list}
    sens = {"cases": det_cases,
            "parameters": {}}

    def var(label, cfg_patch):
        """Esegue la variante e ritorna summary per ogni caso selezionato."""
        out = {}
        for cid in det_cases:
            case = by_id[cid]
            frames = _resolve_frames_inclusive(case, SAMPLES_ROOT)
            cfg = copy.deepcopy(base_cfg)
            for k, v in cfg_patch.items():
                if k in ("detect", "storm", "tracking", "preprocess"):
                    sec = dict(cfg[k]); sec.update(v); cfg[k] = sec
                else:
                    cfg[k] = v
            res = _run_pipeline(frames, cfg)
            cm = _case_metrics(res, case)
            out[cid] = _sensitivity_summary(res)
            out[cid]["max_cells_per_frame"] = cm["detection"]["cells_per_frame"]["stats"]["max"]
        return out

    base = copy.deepcopy(base_cfg)
    sens["parameters"]["dbz_threshold"] = {"default": 20.0, "values": [20.0, 25.0, 30.0]}
    sens["parameters"]["dbz_threshold"]["results"] = {
        str(v): var(f"dbz_{v}", {"detect": {"dbz_threshold": v}})
        for v in (20.0, 25.0, 30.0)}
    sens["parameters"]["min_area_km2"] = {"default": 30.0, "values": [30.0, 40.0, 60.0]}
    sens["parameters"]["min_area_km2"]["results"] = {
        str(v): var(f"minarea_{v}", {"detect": {"min_area_km2": v}})
        for v in (30.0, 40.0, 60.0)}

    # storm-only: riusa detect+celle (cache) per le varianti di aggregazione/pesi
    cache = {}
    for cid in storm_cases:
        case = by_id[cid]
        frames = _resolve_frames_inclusive(case, SAMPLES_ROOT)
        base_res = _run_pipeline(frames, base)
        cache[cid] = {"cells_by_frame": base_res["cells_by_frame"],
                      "cell_tracks": base_res["cell_tracks"]}

    def var_storm(label, cfg_patch):
        out = {}
        for cid in storm_cases:
            case = by_id[cid]
            frames = _resolve_frames_inclusive(case, SAMPLES_ROOT)
            cfg = copy.deepcopy(base)
            for k, v in cfg_patch.items():
                if k in ("storm",):
                    sec = dict(cfg[k])
                    for k2, v2 in (v or {}).items():
                        if isinstance(v2, dict):
                            sub = dict(sec.get(k2) or {})
                            sub.update(v2)
                            sec[k2] = sub
                        elif v2 is not None:
                            sec[k2] = v2
                    cfg[k] = sec
                else:
                    cfg[k] = v
            res = _run_pipeline(frames, cfg, cache=cache[cid])
            out[cid] = _sensitivity_summary(res)
        return out

    sens["parameters"]["merge_gap_km"] = {"default": 5.0, "values": [5.0, 10.0, 20.0],
        "results": {str(v): var_storm(f"gap_{v}", {"storm": {"aggregation": {"merge_gap_km": v}}})
                    for v in (5.0, 10.0, 20.0)}}
    sens["parameters"]["dilate_km"] = {"default": 8.0, "values": [8.0, 12.0, 16.0],
        "results": {str(v): var_storm(f"dilate_{v}", {"storm": {"aggregation": {"dilate_km": v}}})
                    for v in (8.0, 12.0, 16.0)}}
    sens["parameters"]["storm_w_iou"] = {"default": 6.0, "values": [6.0, 12.0],
        "results": {str(v): var_storm(f"wiou_{v}", {"storm": {"tracking": {"w_iou": v}}})
                    for v in (6.0, 12.0)}}
    sens["parameters"]["storm_w_distance"] = {"default": 1.0, "values": [1.0, 3.0],
        "results": {str(v): var_storm(f"wdist_{v}", {"storm": {"tracking": {"w_distance": v}}})
                    for v in (1.0, 3.0)}}
    return sens


def _extreme(cm, case):
    """Part K — eventi estremi: cronologia strutturale della finestra (derivato)."""
    intens = [{"frame": r["frame"], "objects": r["objects"], "max_dbz": r["max_dbz"],
               "mean_score": r["mean_score"]}
              for r in cm["organization"]["temporal"]]
    return {
        "case": cm["case"],
        "category": cm["category"],
        "ground_truth_quality": cm["ground_truth_quality"],
        "frames": cm["frames"],
        "persistence": cm["detection"]["detection_persistence"],
        "structural_timeline": intens,
        "object_area_series": {"mean": cm["detection"]["object_area_km2"]["mean"],
                               "max": cm["detection"]["object_area_km2"]["max"]},
        "organization_score": cm["organization"]["organization_score"],
        "motion": {"velocity_kmh": cm["motion"]["velocity_kmh"],
                   "direction_stability_deg": cm["motion"]["direction_stability_deg"]},
        "temporal_stability_cv": cm["organization"]["temporal_stability_cv"],
    }


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--case-id", default=None)
    ap.add_argument("--skip-sensitivity", action="store_true")
    ap.add_argument("--out", default=OUT_VALIDATION)
    args = ap.parse_args(argv)

    base_cfg = load_config()
    cases_list = _load_extended_cases()
    if args.case_id:
        cases_list = [c for c in cases_list if c["id"] == args.case_id]
    if args.limit:
        cases_list = cases_list[:args.limit]

    metrics_by_case, quiet_by_case, extreme_list = [], [], []
    assessments = {}
    cat_counts = Counter()
    for case in cases_list:
        frames = _resolve_frames_inclusive(case, SAMPLES_ROOT)
        if not frames:
            print(f"[{case['id']}] WARN nessun frame")
            continue
        res = _run_pipeline(frames, base_cfg)
        if case["category"] == QUIET:
            qm = _quiet_metrics(res, case)
            quiet_by_case.append(qm)
            assessments[case["id"]] = _assess(qm, case)
            print(f"[{case['id']}] QUIET cells={qm['false_activity']['total_cells']} "
                  f"objs={qm['false_activity']['total_storm_objects']} "
                  f"tracks={qm['false_activity']['total_tracks']}")
        else:
            cm = _case_metrics(res, case)
            metrics_by_case.append(cm)
            assessments[case["id"]] = _assess(cm, case)
            cat_counts[case["category"]] += 1
            if case["category"] == SEVERE:
                extreme_list.append(_extreme(cm, case))
            print(f"[{case['id']}] cat={case['category']} frames={cm['frames']} "
                  f"cells_med={cm['detection']['cells_per_frame']['stats']['median']} "
                  f"objs_med={cm['detection']['objects_per_frame']['stats']['median']} "
                  f"tracks={cm['tracking']['n_long_tracks']} "
                  f"birth={cm['tracking']['birth_rate']} "
                  f"motion_err={cm['motion']['motion_error_deg']} "
                  f"org_med={cm['organization']['organization_score'].get('median')} "
                  f"mc_high={cm['motion']['motion_confidence_high_frac']} "
                  f"-> {assessments[case['id']]['label']}")

    aggregate = {
        "n_cases": len(metrics_by_case) + len(quiet_by_case),
        "n_convective": len(metrics_by_case),
        "n_quiet": len(quiet_by_case),
        "by_category": dict(cat_counts),
        "cells_per_frame_median": round(float(np.median([
            c["detection"]["cells_per_frame"]["stats"]["median"]
            for c in metrics_by_case])), 1) if metrics_by_case else None,
        "objects_per_frame_median": round(float(np.median([
            c["detection"]["objects_per_frame"]["stats"]["median"]
            for c in metrics_by_case])), 1) if metrics_by_case else None,
        "org_score_median": round(float(np.median([
            c["organization"]["organization_score"]["median"]
            for c in metrics_by_case
            if c["organization"]["organization_score"].get("n")])), 1)
        if metrics_by_case else None,
        "motion_confidence_high_frac": round(float(np.mean([
            c["motion"]["motion_confidence_high_frac"]
            for c in metrics_by_case])), 3) if metrics_by_case else None,
        "ambiguity_high_frac": round(float(np.mean([
            c["tracking"]["ambiguity_high_frac"]
            for c in metrics_by_case])), 3) if metrics_by_case else None,
        "continuity": round(float(np.mean([
            c["tracking"]["continuity"] for c in metrics_by_case])), 3)
        if metrics_by_case else None,
        "birth_rate": round(float(np.mean([
            c["tracking"]["birth_rate"] for c in metrics_by_case
            if c["tracking"]["birth_rate"] is not None])), 3)
        if metrics_by_case else None,
        "data_warnings_total": int(sum(
            len(c["warnings"]) for c in metrics_by_case + quiet_by_case)),
    }

    failure_examples = {mode: [] for mode in (
        "SEGMENTATION_FAILURE", "TRACKING_FRAGMENTATION", "OVER_AGGREGATION",
        "UNDER_AGGREGATION", "MOTION_INSTABILITY", "EDGE_OF_WINDOW",
        "DATA_QUALITY", "UNKNOWN")}
    warning_cases = {ci["case"]: ci["warnings"]
                     for ci in metrics_by_case + quiet_by_case}
    for cid, a in assessments.items():
        issues = list(a["issues"])
        if warning_cases.get(cid):
            issues.append("DATA_QUALITY")
        for issue in issues:
            if issue in failure_examples:
                failure_examples[issue].append(cid)

    sensitivity = None if args.skip_sensitivity else _run_sensitivity(
        base_cfg, cases_list)

    summary = {
        "schema_version": "1.1",
        "generated_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "freeze_algorithm": True,
        "config_used": {
            "detect.dbz_threshold": base_cfg["detect"]["dbz_threshold"],
            "detect.min_area_km2": base_cfg["detect"]["min_area_km2"],
            "storm.aggregation.merge_gap_km":
                base_cfg["storm"]["aggregation"]["merge_gap_km"],
            "storm.aggregation.dilate_km":
                base_cfg["storm"]["aggregation"]["dilate_km"],
        },
        "coverage": {
            "n_cases_total": len(cases_list),
            "n_frames_total": int(sum(
                (m["frames"] for m in metrics_by_case),
                start=0) + sum((q["frames"] for q in quiet_by_case), start=0)),
            "regions_seen": sorted({c["region"] for c in cases_list
                                    if c["region"] != "UNKNOWN"}),
            "regions_missing": ["COASTAL_OR_MARITIME"],
            "seasons_seen": sorted({c["season"] for c in cases_list}),
            "seasons_missing": ["SPRING", "WINTER"],
        },
        "metrics_by_case": metrics_by_case,
        "quiet_cases": quiet_by_case,
        "extreme_events": extreme_list,
        "regional_analysis": _regional(metrics_by_case, cat_counts),
        "assessments": assessments,
        "failure_analysis": {
            "modes": failure_examples,
            "note": ("proxy euristici operativi su metriche osservate; "
                     "ogni modalità con >=1 esempio è documentata nei report")},
        "aggregate": aggregate,
        "sensitivity": sensitivity,
    }

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "extended_summary.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())