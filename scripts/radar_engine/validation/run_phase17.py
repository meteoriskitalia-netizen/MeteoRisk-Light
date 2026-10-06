#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk Radar Engine — validation.run_phase17 (Fase 1.7)

Esegue la validazione multiscala sui casi retrospettivi reali (GeoTIFF FUORI
dal repo, RADAR_SAMPLES_DIR): detect -> cell tracking (layer locale Fase 1.6,
config di produzione) -> aggregazione storm objects -> StormObjectTracker ->
confronto CELL vs STORM OBJECT (metrics/phase17_compare) -> aggregation
comparison (3 metodi).

Output (data/validation/, mai raw):
    phase17_validation.json           - metriche + success criteria per caso
    aggregation_comparison.json       - confronto distance_cc/dbscan/dilation
    phase17_storm_sample.geojson      - storm objects ultimo frame (SIT 20260711)
    phase17_storm_tracks_sample.json  - storm tracks (SIT 20260711)

Uso: python scripts/radar_engine/validation/run_phase17.py [--case-id ID]
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from radar_engine import (aggregation, detect, models, preprocess,  # noqa: E402
                          storm_tracking, tracking)
from radar_engine.config import CONFIG, load_config  # noqa: E402
from radar_engine.validation import cases, phase17_compare  # noqa: E402

OUT_VALIDATION = os.path.abspath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))), "data", "validation"))
SAMPLES_ROOT = cases.samples_root()


def _day_frames(day):
    """Tutti i frame VMI disponibili di un giorno (path, ts_ms) ordinati."""
    daydir = os.path.join(SAMPLES_ROOT, day)
    out = []
    if not os.path.isdir(daydir):
        return out
    for fname in sorted(os.listdir(daydir)):
        if not (fname.startswith("VMI-") and fname.endswith(".tif")):
            continue
        hhmm = fname[4:8]
        ts_ms = int(datetime.strptime(f"{day}T{hhmm}", "%Y-%m-%dT%H%M")
                    .replace(tzinfo=timezone.utc).timestamp() * 1000)
        out.append((os.path.join(daydir, fname), ts_ms))
    return out


def _load_case_frames(case):
    return cases.resolve_case_frames(case, root=SAMPLES_ROOT)


def _run_case(case):
    frames = _load_case_frames(case)
    if not frames:
        return case["id"], f"nessun frame per {case['id']}"
    return _exec(frames, case)


def _run_day_full(day, case_id, case_type="ORGANIZED_MULTICELL"):
    """Serie COMPLETA del giorno (per la finestra estesa SIT20260711 32f)."""
    frames = _day_frames(day)
    case = {"id": case_id, "type": case_type, "start": f"{day}T00:00:00Z",
            "end": f"{day}T23:59:59Z"}
    res, err = _exec(frames, case)
    return res, err


def _exec(frames, case):
    if not frames:
        return case["id"], f"nessun frame per {case['id']}"
    n = len(frames)
    cells_by_frame = []
    for idx, (path, ts) in enumerate(frames):
        raster = preprocess.read_raster(
            str(path), nodata_values=CONFIG["preprocess"]["nodata_values"],
            geo_plausible_bbox=CONFIG["preprocess"]["geo_plausible_bbox"])
        raster.time_ms = int(ts)
        raster.time_iso = datetime.fromtimestamp(ts / 1000.0, timezone.utc)\
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        cells = detect.detect_cells(raster, CONFIG["detect"])
        for c in cells:
            c.frame_index = idx
        cells_by_frame.append(cells)

    tracker = tracking.Tracker(CONFIG["tracking"], cfg_scoring=CONFIG["scoring"])
    for idx, cells in enumerate(cells_by_frame):
        tracker.update(cells, idx)
    tracker.complete_cycles()
    cell_tracks = tracker.finalize(cfg_scoring=CONFIG["scoring"])

    storm_cfg = CONFIG["storm"]
    objs_by_frame = [aggregation.aggregate_frame(cells, idx, storm_cfg)
                     for idx, cells in enumerate(cells_by_frame)]
    storm_tracker = storm_tracking.StormObjectTracker(
        storm_cfg, cfg_scoring=CONFIG["scoring"])
    for idx, objs in enumerate(objs_by_frame):
        storm_tracker.update(objs, idx)
    storm_tracker.complete_cycles()
    storm_tracks = storm_tracker.finalize(cfg_scoring=CONFIG["scoring"])

    comparison = phase17_compare.compare_layers(
        cells_by_frame, cell_tracks, objs_by_frame, storm_tracks)
    agg_cmp = aggregation.compare_aggregation_methods(cells_by_frame, storm_cfg)

    return {
        "case": case["id"],
        "type": case["type"],
        "window": f"{case['start']}->{case['end']}",
        "frames": n,
        "cells_by_frame": [len(c) for c in cells_by_frame],
        "objects_by_frame": [len(o) for o in objs_by_frame],
        "comparison": comparison,
        "aggregation_comparison": agg_cmp,
        "sample_objects": objs_by_frame[-1],
        "sample_tracks": storm_tracks,
    }, None


def _write_storm_samples(objs, tracks, out_dir):
    """scrive gli output storm layer (Parte E) ridotto per la validazione."""
    from radar_engine import output
    import datetime as _dt
    bundle = models.EngineBundle(
        "ok", _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        output.SOURCE_LABEL)
    bundle.radar_timestamp_iso = objs[-1].timestamp_iso if objs else None
    bundle.radar_timestamp_ms = objs[-1].timestamp_ms if objs else None
    bundle.storm_objects_by_frame = [objs]
    bundle.storm_tracks = tracks
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "storm_objects.geojson": os.path.join(out_dir, "storm_objects.geojson"),
        "storm_tracks.json": os.path.join(out_dir, "storm_tracks.json"),
    }
    for name, path in paths.items():
        text = (output.build_storm_tracks(bundle)
                if name == "storm_tracks.json"
                else output.build_storm_objects_geojson(bundle))
        import json as _json
        _json.dumps(text)
        with open(path, "w", encoding="utf-8") as fh:
            _json.dump(text, fh, indent=2, ensure_ascii=False)
    print(f"wrote sample storm outputs -> {out_dir}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--case-id", default=None)
    ap.add_argument("--day", default=None,
                    help="esegui l'intero giorno (serie estesa, es. 2026-07-11)")
    ap.add_argument("--sample-out-dir", default=None,
                    help="scrive anche storm_objects.geojson/storm_tracks.json")
    ap.add_argument("--out", default=OUT_VALIDATION)
    args = ap.parse_args(argv)
    cases_list = cases.load_cases()
    runs = {}

    def collect(res, err):
        if err:
            runs[res] = {"error": err}
            print(f"[{res}] ERROR {err}")
            return
        cid = res["case"]
        sample_objects = res.pop("sample_objects")
        sample_tracks = res.pop("sample_tracks")
        if args.sample_out_dir and sample_objects:
            _write_storm_samples(sample_objects, sample_tracks,
                                 args.sample_out_dir)
        runs[cid] = {k: v for k, v in res.items() if k != "comparison"}
        runs[cid]["comparison_metrics"] = res["comparison"]["cell"], \
            res["comparison"]["storm_object"]
        runs[cid]["success"] = res["comparison"]["success"]
        runs[cid]["success_criteria"] = res["comparison"]["success_criteria"]
        runs[cid]["deltas"] = res["comparison"]["deltas"]
        runs[cid]["aggregation_summary"] = {
            k: res["aggregation_comparison"]["methods"][k]["mean_objects_per_frame"]
            for k in res["aggregation_comparison"]["methods"]}
        print(f"[{cid}] frames={res['frames']} "
              f"success={res['comparison']['success']} "
              f"criteria={res['comparison']['success_criteria']}")
        print(f"  cell:  id={res['comparison']['cell']['id_switch']['rate']} "
              f"birth={res['comparison']['cell']['birth_rate']} "
              f"motion_err={res['comparison']['cell']['motion_error_deg']}")
        print(f"  storm: id={res['comparison']['storm_object']['id_switch']['rate']} "
              f"birth={res['comparison']['storm_object']['birth_rate']} "
              f"motion_err={res['comparison']['storm_object']['motion_error_deg']} "
              f"mc={res['comparison']['storm_object']['motion_confidence']}")

    if args.day:
        collect(*_run_day_full(args.day, f"DAY{args.day}"))
    else:
        for case in cases_list:
            if args.case_id and case["id"] != args.case_id:
                continue
            collect(*_run_case(case))

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "phase17_validation.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(runs, fh, indent=2, ensure_ascii=False, default=str)
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())