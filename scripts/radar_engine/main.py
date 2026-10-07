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
  -> Phase 2 SUPERCELL (additivo): hook/struttura/ambiente/fulmini -> SSI v2
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


def _phase2_evaluate(bundle, config):
    """Layer Fase 2 SUPERCELL (additivo): sub-layer A1 -> SSI v2 candidati.

    Policy: ogni sotto-layer e' OPZIONALE; errore/dato assente -> warning in
    bundle.warnings + sub-score None (mai crash del run, nessun dato
    inventato). I candidati Fase 1 sono solo ARRICCHITI: ssi/level/... e
    bundle.status restano intatti (lo status e' gia' calcolato alla riga 85
    del pipeline, prima di questo layer)."""
    p2_cfg = config.get("phase2") or {}
    warnings = bundle.warnings
    if not p2_cfg.get("enabled"):
        bundle.phase2 = {"status": "disabled", "warnings": warnings}
        return
    candidates = getattr(bundle, "supercells", None) or []
    if not candidates:
        warnings.append("phase2: no candidates")
        bundle.phase2 = {"status": "unavailable", "warnings": warnings}
        return
    try:
        import numpy as np
        from radar_engine import phase2 as p2_mod
        from radar_engine.phase2 import (aggregate, environment, hook,
                                         lightning, vertical_structure)
    except Exception as exc:
        warnings.append(f"phase2 import failed: {exc}")
        bundle.phase2 = {"status": "unavailable", "warnings": warnings}
        return

    result = {"status": "unavailable", "version": p2_mod.PHASE2_VERSION,
              "warnings": warnings}
    bundle.phase2 = result
    frames = list(getattr(bundle, "frames", []) or [])

    # --- HOOK: morfologia uncino sulle ultime N griglie (score condiviso) ---
    hook_score = None
    try:
        hcfg = p2_cfg.get("hook") or {}
        n_hist = max(1, int(hcfg.get("history_frames", 3)))
        scores = []
        for fr in frames[-n_hist:]:
            feats = hook.compute_hook_features(
                fr.data, fr.valid_mask,
                dbz_threshold=hcfg.get("dbz_threshold"))
            scores.append(hook.hook_score_from_features(feats))
        if scores:
            hook_score = hook.filter_persistence(scores[-1], scores[:-1])
        else:
            warnings.append("phase2 hook: no frames")
    except Exception as exc:
        warnings.append(f"phase2 hook failed: {exc}")

    # --- STRUTTURA VERTICALE: VIL/ETM/POH (+ CAPPI se configurati) ---------
    structure_score = None
    structure_feats = None
    try:
        scfg = p2_cfg.get("structure") or {}
        if not frames:
            raise ValueError("no_reference_grid")
        shape = frames[-1].data.shape

        def _grid(product, label):
            if not product:
                return None
            try:
                gframes, gwarn, _ts = fetch.fetch_frames(
                    config, product=product, max_frames=1)
                warnings.extend(gwarn)
            except Exception as exc:
                warnings.append(f"phase2 structure {label}: {exc}")
                return None
            if not gframes:
                warnings.append(f"phase2 structure {label}: no frames")
                return None
            g = gframes[-1].data
            if g.shape != shape:
                warnings.append(f"phase2 structure {label}: shape mismatch")
                return None
            return g

        grids = {name: _grid(scfg.get(key), name)
                 for name, key in (("vil", "product_vil"),
                                   ("etm", "product_etm"),
                                   ("poh", "product_poh"),
                                   ("low", "product_low"),
                                   ("high", "product_high"))}
        if not any(g is not None for g in grids.values()):
            warnings.append("phase2 structure: no products available")
        else:
            nan_grid = np.full(shape, np.nan, dtype="float64")
            g5 = tuple(grids[n] if grids[n] is not None else nan_grid
                       for n in ("vil", "etm", "poh", "low", "high"))
            structure_score = vertical_structure.structure_score(*g5)
            structure_feats = vertical_structure.structure_features(*g5)
    except Exception as exc:
        warnings.append(f"phase2 structure failed: {exc}")

    # --- OVERSHOOTING TOP: DN->K non calibrato in A2 -> layer assente -------
    ot_score = None
    if (p2_cfg.get("ot") or {}).get("dn_to_kelvin") is None:
        warnings.append("ot_unavailable:dn_to_kelvin_non_configurato")
    else:
        warnings.append("ot_unavailable:pipeline_satellite_non_inclusa_in_A2")

    # --- FULMINI: rate Blitz v2 sugli ultimi slot 5 min (S3 DPC) ------------
    lightning_score = None
    try:
        window = int((p2_cfg.get("lightning") or {}).get("window_slots", 4))
        window = max(2, min(window, lightning.LIGHTNING_TREND_WINDOW))
        counts = []
        for k in range(window - 1, -1, -1):   # dal piu' vecchio al recente
            try:
                frame = lightning.fetch_ltg(
                    epoch_ms=lightning.ltg_epoch_floor(backoff_steps=k))
                counts.append(frame["count"])
            except lightning.LightningFetchError as exc:
                warnings.append(f"phase2 lightning slot-{k}: {exc}")
                counts.append(None)
        rate = next((c for c in reversed(counts) if c is not None), None)
        if rate is None:
            warnings.append("phase2 lightning: no slots available")
        else:
            trend = lightning.lightning_trend(counts)
            lightning_score = lightning.lightning_score(rate, trend["jump"])
    except Exception as exc:
        warnings.append(f"phase2 lightning failed: {exc}")

    # --- AMBIENTE NWP: Open-Meteo sul punto del primo candidato -------------
    env_score = None
    try:
        ecfg = p2_cfg.get("environment") or {}
        if ecfg.get("enabled", True):
            pos = candidates[0].get("position") or []
            if len(pos) == 2:
                detail = environment.evaluate_environment(
                    float(pos[1]), float(pos[0]),
                    timeout_s=ecfg.get("timeout_s"))
                env_score = detail["env_score"]
                result["environment"] = {
                    "scp": detail["scp"], "stp": detail["stp"],
                    "ship": detail["ship"], "env_score": detail["env_score"],
                    "completeness": detail["completeness"],
                    "partial": detail["partial"], "position": list(pos),
                }
            else:
                warnings.append("phase2 environment: no candidate position")
    except environment.EnvironmentFetchError as exc:
        warnings.append(f"phase2 environment: {exc}")
    except Exception as exc:
        warnings.append(f"phase2 environment failed: {exc}")

    # --- SSI v2 sui candidati (pesi da config; pesi invalidi -> warning) ----
    sub = {"hook": hook_score, "structure": structure_score,
           "env": env_score, "ot": ot_score, "lightning": lightning_score}
    present = sum(1 for v in sub.values() if v is not None)
    result.update(sub)
    result["status"] = ("ok" if present == len(sub)
                        else "partial" if present else "unavailable")
    if structure_feats is not None:
        # 'poc' e' il refuso storico di 'poh' (Probability Of Hail): alias
        # emesso con lo stesso valore per compatibilita' con A3 (feature_chips).
        structure_feats = dict(structure_feats,
                               poc=structure_feats.get("poh_max"))
        result["structure_features"] = structure_feats
    weights = (p2_cfg.get("aggregate") or {}).get("weights")
    for c in candidates:
        c["hook"] = hook_score
        c["structure"] = structure_score
        c["ot"] = ot_score
        c["lightning"] = lightning_score
        c["env_scp"] = env_score        # chip 'Env' (0-100; SCP grezzo sopra)
    try:
        for c in candidates:
            v2 = aggregate.aggregate_ssi_v2(
                float(c.get("ssi") or 0.0), hook_score, structure_score,
                env_score, ot_score, lightning_score, weights=weights)
            c["ssi_v2"] = v2["ssi_v2"]
    except ValueError as exc:
        warnings.append(f"phase2 aggregate weights invalid: {exc}")


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

    # ---- PHASE 2 (Fase 2, additivo): sub-layer A1 -> SSI v2 sui candidati --
    # Layer ADDITIVO: nessun errore fa fallire il pipeline base (warning) e
    # la firma Fase 1 dei candidati (ssi/level/...) resta intatta.
    try:
        _phase2_evaluate(bundle, config)
    except Exception as exc:  # layer additivo: mai rompere il pipeline base
        bundle.warnings.append(f"phase2 layer failed: {exc}")
        bundle.phase2 = {"status": "unavailable", "warnings": bundle.warnings}

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
        "phase2_status": (getattr(bundle, "phase2", None) or {}).get(
            "status", "unavailable"),
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
            fh.write(f"phase2_status={summary['phase2_status']}\n")
            fh.write(f"radar_timestamp={bundle.radar_timestamp_iso or ''}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())