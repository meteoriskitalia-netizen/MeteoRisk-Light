#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — main.py (Fase 1, CLI)

Pipeline:
  fetch (findLastProductByType + downloadProduct) -> validazione GeoTIFF
  -> preprocessing (validità dato, CRS, georeferenziazione)
  -> convective cell detection (per frame)
  -> storm tracking multi-frame (Hungarian, cost combinata)
  -> preliminary scoring (Organization Score 0-100)
  -> output atomico derivati (data/radar)

FAILURE POLICY (exit codes):
  0  = ok            (almeno 1 frame valido, output scritto, status=ok)
  3  = degraded      (almeno 1 frame valido ma fetch parziale -> status=degraded)
  4  = error         (nessun frame valido / GeoTIFF corrotto / CRS non
                      interpretabile -> SOLLO latest.json con status=error,
                      storms/tracks restano intatti)

Utilizzo:
  python scripts/radar_engine/main.py [--out-dir data/radar]
                                      [--config <json>]
                                      [--product VMI]
                                      [--max-frames 6]
                                      [--keep-frames]
                                      [--dry-run]
"""

import argparse
import datetime as _dt
import json
import os
import sys

# Bootstrap: consente l'esecuzione sia come `python scripts/radar_engine/main.py`
# sia come modulo package (import assoluti vs via `radar_engine`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar_engine as _engine
from radar_engine import models
from radar_engine import output
from radar_engine import fetch
from radar_engine import detect as detect_mod
from radar_engine import tracking as track_mod
from radar_engine.config import load_config

_OK = 0
_DEGRADED = 3
_ERROR = 4


def _utcnow_str():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _engine_meta():
    return {
        "name": _engine.ENGINE_NAME,
        "version": _engine.ENGINE_VERSION,
        "phase": _engine.ENGINE_PHASE,
    }


def _run(config, out_dir, product, max_frames, dry_run):
    bundle = models.EngineBundle("ok", _utcnow_str(), output.SOURCE_LABEL)
    bundle.engine = _engine_meta()

    # ---- FECTH + VALIDAZIONE ------------------------------------------------
    try:
        frames, warnings, latest_ts = fetch.fetch_frames(
            config, product=product, max_frames=max_frames
        )
    except models.SourceError as exc:
        bundle.warnings = [f"fetch failed: {exc}"]
        return bundle, _ERROR

    bundle.frames = frames
    bundle.warnings = list(warnings)
    if not frames:
        bundle.status = "error"
        bundle.warnings.append("no valid frames available")
        return bundle, _ERROR

    bundle.status = "degraded" if warnings else "ok"
    bundle.radar_timestamp_ms = frames[-1].time_ms
    bundle.radar_timestamp_iso = frames[-1].time_iso
    try:
        bundle.data_latency_minutes = round(
            (_dt.datetime.now(_dt.timezone.utc).timestamp() * 1000 - frames[-1].time_ms)
            / 60000.0, 1
        )
    except Exception:
        bundle.data_latency_minutes = None

    # ---- DETECTION ----------------------------------------------------------
    cfg_detect = config["detect"]
    for idx, raster in enumerate(frames):
        try:
            cells = detect_mod.detect_cells(raster, cfg_detect)
        except Exception as exc:
            bundle.warnings.append(f"detect@{raster.time_iso} failed: {exc}")
            cells = []
        for c in cells:
            c.frame_index = idx
        bundle.cells_by_frame.append(cells)

    # ---- TRACKING (>=2 frame validi; altrimenti solo detection) -------------
    tracker = track_mod.Tracker(config["tracking"],
                                cfg_scoring=config.get("scoring"))
    for idx, cells in enumerate(bundle.cells_by_frame):
        tracker.update(cells, idx)
    tracker.complete_cycles()
    bundle.tracks = tracker.finalize(cfg_scoring=config.get("scoring"))

    # ---- MULTI-SCALE: STORM OBJECT LAYER (Fase 1.7, additivo) ---------------
    # Il layer cella resta intatto; l'aggregazione e lo storm tracking sono
    # un layer di identità aggiuntivo. Errori -> warning (non fa fallire il run).
    storm_cfg = config.get("storm", {"enabled": False})
    if storm_cfg.get("enabled"):
        try:
            from radar_engine import aggregation as agg_mod
            from radar_engine import storm_tracking as storm_mod
            bundle.storm_objects_by_frame = [
                agg_mod.aggregate_frame(cells, idx, storm_cfg)
                for idx, cells in enumerate(bundle.cells_by_frame)]
            storm_tracker = storm_mod.StormObjectTracker(
                storm_cfg, cfg_scoring=config.get("scoring"))
            for idx, objs in enumerate(bundle.storm_objects_by_frame):
                storm_tracker.update(objs, idx)
            storm_tracker.complete_cycles()
            bundle.storm_tracks = storm_tracker.finalize(
                cfg_scoring=config.get("scoring"))
        except Exception as exc:  # layer additivo: mai rompere il pipeline base
            bundle.warnings.append(f"storm_layer failed: {exc}")
            bundle.storm_objects_by_frame = []
            bundle.storm_tracks = []

    # ---- SUPERCELL SIGNATURE INDEX (Fase 1 SUPERCELL, additivo) ------------
    # SSI su celle + storm object tracciati (stessi dati Fase 1, zero richieste
    # rete aggiuntive). Sperimentale e senza Doppler: un errore NON fa fallire
    # il run (warning) e i candidati restano [].
    sc_cfg = config.get("supercell", {"enabled": False})
    if sc_cfg.get("enabled"):
        try:
            from radar_engine import supercell as sc_mod
            sc_mod.evaluate(bundle, sc_cfg)
        except Exception as exc:  # layer additivo: mai rompere il pipeline base
            bundle.warnings.append(f"supercell layer failed: {exc}")
            bundle.supercells = []
            bundle.supercell_tracks_evaluated = 0

    # ---- SCORING incluso in finalize (per track) ----------------------------
    if len(frames) < 2:
        bundle.warnings.append("tracking skipped (<2 valid frames); "
                               "detection output only, tracking_confidence=insufficient")

    # ---- OUTPUT -------------------------------------------------------------
    if dry_run:
        return bundle, _OK

    try:
        output.write_outputs(bundle, out_dir, engine_meta=_engine_meta())
    except models.OutputError as exc:
        return _fail_output(bundle, out_dir, str(exc))
    except Exception as exc:  # guasto imprevisto del writer -> OutputError
        return _fail_output(bundle, out_dir,
                            f"unexpected:{exc.__class__.__name__}:{exc}")
    return bundle, _OK if bundle.status == "ok" else _DEGRADED


def _fail_output(bundle, out_dir, reason):
    """Scrivi SOLO latest.json (status=error) e ritorna rc 4, mai crash."""
    bundle.status = "error"
    try:
        output.write_status_only(out_dir, "error", bundle.generated_at_iso,
                                 bundle.warnings + [f"output failed: {reason}"],
                                 engine_meta=_engine_meta())
    except Exception:
        pass  # out_dir irrecuperabile: rc 4 comunque (nessun exit 1)
    return bundle, _ERROR


def main(argv=None):
    ap = argparse.ArgumentParser(description="Meteorisk Radar Engine — Fase 1")
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--config", default=None)
    ap.add_argument("--product", default="VMI")
    ap.add_argument("--max-frames", type=int, default=None)
    ap.add_argument("--keep-frames", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    if args.keep_frames:
        config["source"]["keep_raw_frames"] = True
    out_dir = os.path.abspath(args.out_dir or config["output"]["out_dir"])

    bundle, rc = _run(config, out_dir, args.product, args.max_frames, args.dry_run)

    summary = {
        "status": bundle.status,
        "frames": len(bundle.frames),
        "cells": sum(len(c) for c in bundle.cells_by_frame),
        "tracks": len([t for t in bundle.tracks if len(t.points) >= 2]),
        "radar_timestamp": bundle.radar_timestamp_iso,
        "latency_min": bundle.data_latency_minutes,
        "warnings": bundle.warnings,
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    # GitHub Actions: espone status/stats come step outputs (se presente).
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"status={bundle.status}\n")
            fh.write(f"frames={len(bundle.frames)}\n")
            fh.write(f"cells={summary['cells']}\n")
            fh.write(f"tracks={summary['tracks']}\n")
            fh.write(f"supercell_candidates={len(getattr(bundle, 'supercells', []))}\n")
            fh.write(f"radar_timestamp={bundle.radar_timestamp_iso or ''}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())