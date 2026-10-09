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
                                      [--history-dir <dir>]
                                      [--dry-run]
"""

import argparse
import datetime as _dt
import json
import math
import os
import sys

# Bootstrap: consente l'esecuzione sia come `python scripts/radar_engine/main.py`
# sia come modulo package (import assoluti vs via `radar_engine`).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import radar_engine as _engine
from radar_engine import models
from radar_engine import output
from radar_engine import history
from radar_engine import fetch
from radar_engine import detect as detect_mod
from radar_engine import tracking as track_mod
from radar_engine.config import load_config

_OK = 0
_DEGRADED = 3
_ERROR = 4

# Convergenza locale: 1 grado di latitudine ~ 111.32 km (WGS84 medio).
_KM_PER_DEG_LAT = 111.32
# Prodotti DPC di struttura -> nome interno (ordine di structure_score).
_STRUCTURE_PRODUCTS = (("vil", "product_vil"), ("etm", "product_etm"),
                       ("poh", "product_poh"), ("low", "product_low"),
                       ("high", "product_high"))
# Componenti Fase 2 calcolati PER CANDIDATO (B2).
_PHASE2_COMPONENTS = ("hook", "structure", "env", "ot", "lightning")


def _utcnow_str():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _engine_meta():
    return {
        "name": _engine.ENGINE_NAME,
        "version": _engine.ENGINE_VERSION,
        "phase": _engine.ENGINE_PHASE,
    }


def _candidate_position(candidate):
    """(lon, lat) float del candidato; None se assente/invalido.

    Nessuna posizione inventata: chiave 'position' assente, corta o non
    numerica -> None (i layer per-candidato restano None)."""
    pos = candidate.get("position") or []
    if len(pos) != 2:
        return None
    try:
        lon, lat = float(pos[0]), float(pos[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lon) and math.isfinite(lat)):
        return None
    return lon, lat


def _candidate_window(frame, lon, lat, radius_km):
    """Finestra INCLUSIVA (r0, r1, c0, c1) che copre il disco di raggio
    `radius_km` attorno a (lon, lat) sul raster `frame`.

    Conversione inversa lon/lat -> (row, col) con transform/geo_transform del
    raster (models.RasterData.lonlat_to_pixel: transformer CRS<->EPSG:4326 in
    direzione INVERSA + affine inversa ~transform). Gli4 angoli del bounding
    box geografico del disco piu' il centro danno gli estremi: l'intersezione
    con la griglia copre il disco anche dove la proiezione deforma gli assi.

    Ritorna None se la posizione non e' invertibile (fuori dominio CRS) o se
    la finestra non interseca la griglia (nessun pixel inventato, FAIL SAFE)."""
    try:
        rows, cols = int(frame.rows), int(frame.cols)
        radius = float(radius_km)
        lat = float(lat)
        lon = float(lon)
    except (TypeError, ValueError):
        return None
    if radius <= 0.0 or rows <= 0 or cols <= 0:
        return None
    dlat = radius / _KM_PER_DEG_LAT
    dlon = radius / max(_KM_PER_DEG_LAT
                        * abs(math.cos(math.radians(lat))), 1e-6)
    try:
        r_c, c_c = frame.lonlat_to_pixel(lon, lat)
    except ValueError:
        return None
    rmin = rmax = r_c
    cmin = cmax = c_c
    for s_lat in (-1.0, 1.0):
        for s_lon in (-1.0, 1.0):
            try:
                r, c = frame.lonlat_to_pixel(lon + s_lon * dlon,
                                             lat + s_lat * dlat)
            except ValueError:
                continue            # angolo fuori dominio CRS: nessun dato li'
            rmin, rmax = min(rmin, r), max(rmax, r)
            cmin, cmax = min(cmin, c), max(cmax, c)
    r0, r1 = max(0, rmin), min(rows - 1, rmax)
    c0, c1 = max(0, cmin), min(cols - 1, cmax)
    if r0 > r1 or c0 > c1:
        return None
    return r0, r1, c0, c1


def _crop(window, arr):
    """Ritaglio ndarray sulla finestra (r0, r1, c0, c1) inclusiva."""
    r0, r1, c0, c1 = window
    return arr[r0:r1 + 1, c0:c1 + 1]


def _fetch_structure_products(config, scfg, ref_shape, warnings):
    """Scarica UNA VOLTA i prodotti DPC di struttura (griglia di riferimento).

    Ritorna dict {nome: RasterData | None}: prodotto non configurato, non
    scaricabile o con shape diverso dal riferimento VMI -> None con warning
    (nessun dato inventato, nessun crash del layer)."""
    out = {}
    for name, key in _STRUCTURE_PRODUCTS:
        product = scfg.get(key)
        if not product:
            out[name] = None
            continue
        try:
            gframes, gwarn, _ts = fetch.fetch_frames(config, product=product,
                                                      max_frames=1)
            warnings.extend(gwarn)
        except Exception as exc:
            warnings.append(f"phase2 structure {name}: {exc}")
            out[name] = None
            continue
        if not gframes:
            warnings.append(f"phase2 structure {name}: no frames")
            out[name] = None
            continue
        rd = gframes[-1]
        if tuple(rd.data.shape) != tuple(ref_shape):
            warnings.append(f"phase2 structure {name}: shape mismatch")
            out[name] = None
            continue
        out[name] = rd
    return out


def _structure_at_window(products, window, vs):
    """Score + features struttura verticale SULLA FINESTRA del candidato.

    products: dict nome -> RasterData (None = prodotto assente); window:
    (r0, r1, c0, c1) o None. Ritorna (score, features) oppure (None, None)
    quando nessun prodotto e' disponibile, la finestra e' vuota/fuori griglia
    o non contiene alcun valore finito (dato assente -> nessun punteggio).

    UNITA': al ritaglio l'ETM di prodotto (metri) e' convertito in km
    (vs.etm_to_km) e il POH (frazione) in % (vs.poh_to_percent), in modo che
    le soglie 6/9/12 km e 30/50/70 % siano applicate alle unita' giuste.
    I prodotti non configurati restano NaN -> membership 0 (mai inventati)."""
    import numpy as np

    if window is None or not any(rd is not None for rd in products.values()):
        return None, None
    r0, r1, c0, c1 = window
    shape = (int(r1) - int(r0) + 1, int(c1) - int(c0) + 1)
    if shape[0] <= 0 or shape[1] <= 0:
        return None, None
    nan_grid = np.full(shape, np.nan, dtype="float64")
    grids = {}
    any_finite = False
    for name, _key in _STRUCTURE_PRODUCTS:
        rd = products.get(name)
        if rd is None:
            grids[name] = nan_grid
            continue
        g = _crop(window, rd.data)
        if name == "etm":
            g = vs.etm_to_km(g)            # metri -> km (x/1000)
        elif name == "poh":
            g = vs.poh_to_percent(g)       # frazione -> % (x100)
        grids[name] = g
        if not any_finite and bool(np.isfinite(g).any()):
            any_finite = True
    if not any_finite:
        return None, None
    g5 = tuple(grids[n] for n, _k in _STRUCTURE_PRODUCTS)
    return vs.structure_score(*g5), vs.structure_features(*g5)


def _hook_for_candidate(frames, lon, lat, radius_km, hcfg, hook_mod):
    """Hook morfologico sul FOOTPRINT del candidato (ultime N griglie).

    Ogni frame viene ritagliato sulla finestra ±radius_km attorno al
    candidato e la coppia (data, valid_mask) passata a
    hook.hook_score_footprint: finestra assente/fuori griglia -> None.
    Ritorna lo score persistito (float) oppure None."""
    try:
        n_hist = max(1, int(hcfg.get("history_frames", 3)))
    except (TypeError, ValueError):
        n_hist = 3
    windows = []
    for fr in frames[-n_hist:]:
        win = _candidate_window(fr, lon, lat, radius_km)
        if win is None:
            return None
        windows.append((_crop(win, fr.data), _crop(win, fr.valid_mask)))
    if not windows:
        return None
    return hook_mod.hook_score_footprint(
        windows, dbz_threshold=hcfg.get("dbz_threshold"))


def _phase2_evaluate(bundle, config):
    """Layer Fase 2 SUPERCELL (additivo): sub-layer A1 -> SSI v2 PER CANDIDATO.

    Policy: ogni sotto-layer e' OPZIONALE; errore/dato assente -> warning in
    bundle.warnings + sub-score None PER IL CANDIDATO toccato (mai crash del
    run, nessun dato inventato). Da B2 hook/struttura/ambiente/fulmini sono
    calcolati sulla FINESTRA LOCALE del candidato (footprint ±km, cache per
    slot/bucket condivise fra i candidati) e l'SSI v2 rinormalizza i pesi
    sui componenti presenti (aggregate 0.4.0). I candidati Fase 1 sono solo
    ARRICCHITI: ssi/level/... e bundle.status restano intatti (lo status e'
    gia' calcolato alla riga 85 del pipeline, prima di questo layer)."""
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

    # --- CONFIG (raggi finestra per-candidato + finestra fulmini) ----------
    hcfg = p2_cfg.get("hook") or {}
    scfg = p2_cfg.get("structure") or {}
    ecfg = p2_cfg.get("environment") or {}
    lcfg = p2_cfg.get("lightning") or {}
    hook_radius = float(hcfg.get("footprint_radius_km", 45.0))
    struct_radius = float(scfg.get("local_radius_km", 45.0))
    ltg_radius = float(lcfg.get("radius_km", 30.0))
    try:
        ltg_min_strikes = int(lcfg.get("min_strikes", 1))
    except (TypeError, ValueError):
        ltg_min_strikes = 1
    env_enabled = bool(ecfg.get("enabled", True))

    def _warn(msg):
        """Warning deduplicato (i layer per-candidato girano N volte)."""
        if msg not in warnings:
            warnings.append(msg)

    # --- OVERSHOOTING TOP: DN->K non calibrato in A2 -> layer assente -------
    ot_score = None
    if (p2_cfg.get("ot") or {}).get("dn_to_kelvin") is None:
        warnings.append("ot_unavailable:dn_to_kelvin_non_configurato")
    else:
        warnings.append("ot_unavailable:pipeline_satellite_non_inclusa_in_A2")

    # --- STRUTTURA: prodotti DPC scaricati UNA VOLTA (rif. = ultima VMI) ----
    products = {}
    if frames:
        products = _fetch_structure_products(config, scfg,
                                             frames[-1].data.shape, warnings)
        if not any(rd is not None for rd in products.values()):
            warnings.append("phase2 structure: no products available")
    else:
        warnings.append("phase2 structure: no frames")

    # --- FULMINI: slot scaricati UNA VOLTA, strike in cache per slot --------
    # Scansione all'INDIETRO fino a max_backoff_steps: si raccolgono gli slot
    # pubblicati PIU' RECENTI fino a window_slots disponibili, cosi' un ritardo
    # di pubblicazione oltre la finestra base non azzera il layer fulmini.
    try:
        window = max(2, min(int(lcfg.get("window_slots", 4)),
                            lightning.LIGHTNING_TREND_WINDOW))
    except (TypeError, ValueError):
        window = lightning.LIGHTNING_TREND_WINDOW
    try:
        max_backoff = max(0, int(lcfg.get("max_backoff_steps", 36)))
    except (TypeError, ValueError):
        max_backoff = 36
    slot_cache = {}
    ltg_slots_recent = []
    for k in range(0, max_backoff + 1):   # dal piu' recente all'indietro
        if len(ltg_slots_recent) >= window:
            break
        epoch = lightning.ltg_epoch_floor(backoff_steps=k)
        if epoch in slot_cache:
            slot = slot_cache[epoch]
        else:
            try:
                slot = lightning.fetch_ltg(epoch_ms=epoch)["strikes"]
            except lightning.LightningFetchError as exc:
                warnings.append(f"phase2 lightning slot-{k}: {exc}")
                slot = None
            except Exception as exc:      # guasto imprevisto -> slot assente
                warnings.append(f"phase2 lightning failed: {exc}")
                slot = None
            slot_cache[epoch] = slot
        if slot is not None:
            ltg_slots_recent.append(slot)
    # dal piu' vecchio al piu' recente (ordine atteso da lightning_spatial_score)
    ltg_slots = list(reversed(ltg_slots_recent))
    if not ltg_slots:
        warnings.append("phase2 lightning: no slots available (wider window)")

    # --- LAYER PER CANDIDATO: hook/struttura/fulmini/ambiente ---------------
    # Cache condivise: env per bucket (0.1 gradi), fulmini per slot epoch ->
    # N candidati = UNA chiamata per bucket/slot. Ogni sub-valore finisce
    # sul SINGOLO candidato (chiavi lette dai chip dell'app).
    env_cache = {}
    env_bucket_deg = ecfg.get("cache_grid_deg", environment.ENV_CACHE_GRID_DEG)
    env_failures = 0
    no_position = False
    for c in candidates:
        pos = _candidate_position(c)
        if pos is None:
            no_position = True
            lon = lat = None
        else:
            lon, lat = pos

        # Finestra struttura sul riferimento (ultima VMI): hook usa la sua
        # propria finestra per ciascuno degli ultimi frame della storia.
        window = (_candidate_window(frames[-1], lon, lat, struct_radius)
                  if frames and lon is not None else None)

        hook_score = None
        if lon is not None:
            try:
                hook_score = _hook_for_candidate(frames, lon, lat, hook_radius,
                                                 hcfg, hook)
            except Exception as exc:
                _warn(f"phase2 hook failed: {exc}")

        structure_score = None
        structure_feats = None
        if window is not None:
            try:
                structure_score, structure_feats = _structure_at_window(
                    products, window, vertical_structure)
            except Exception as exc:
                _warn(f"phase2 structure failed: {exc}")

        lightning_score = None
        if lon is not None:
            try:
                counts = [(lightning.count_strikes_in_radius(
                    s, lon, lat, ltg_radius) if s is not None else None)
                    for s in ltg_slots]
                lightning_score = lightning.lightning_spatial_score(
                    counts, min_strikes=ltg_min_strikes)
            except Exception as exc:
                _warn(f"phase2 lightning failed: {exc}")

        env_score = None
        if env_enabled and lon is not None:
            detail = None
            try:
                detail = environment.evaluate_environment_cached(
                    lat, lon, cache=env_cache, grid_deg=env_bucket_deg,
                    timeout_s=ecfg.get("timeout_s"))
            except Exception as exc:
                _warn(f"phase2 environment failed: {exc}")
            if detail is None:
                env_failures += 1
            else:
                env_score = detail.get("env_score")
                if result.get("environment") is None:
                    result["environment"] = {
                        "scp": detail.get("scp"), "stp": detail.get("stp"),
                        "ship": detail.get("ship"),
                        "env_score": detail.get("env_score"),
                        "completeness": detail.get("completeness"),
                        "partial": detail.get("partial"),
                        "position": [lon, lat],
                        "bucket": list(environment.cache_bucket(
                            lat, lon, env_bucket_deg)),
                    }

        c["hook"] = hook_score
        c["structure"] = structure_score
        if structure_feats is None:
            c["structure_features"] = None
        else:
            # 'poc' e' il refuso storico di 'poh' (Probability Of Hail): alias
            # emesso con lo stesso valore per compatibilita' con A3.
            c["structure_features"] = dict(structure_feats,
                                            poc=structure_feats.get("poh_max"))
        c["ot"] = ot_score
        c["lightning"] = lightning_score
        c["env"] = env_score
        c["env_scp"] = env_score        # chip 'Env' (0-100; SCP grezzo sopra)

    if no_position:
        _warn("phase2: candidate without valid position -> per-cell layers None")
    if env_failures:
        _warn(f"phase2 environment: {env_failures} candidate bucket(s) "
              "without data")

    # --- STATUS: componenti presenti su ALMENO UN candidato -----------------
    comps = {name: sum(1 for c in candidates if c.get(name) is not None)
             for name in _PHASE2_COMPONENTS}
    result["components_status"] = comps
    present = sum(1 for v in comps.values() if v)
    result["status"] = ("ok" if present == len(_PHASE2_COMPONENTS)
                        else "partial" if present else "unavailable")

    # --- SSI v2 per candidato (pesi rinormalizzati sui presenti) ------------
    weights = (p2_cfg.get("aggregate") or {}).get("weights")
    try:
        for c in candidates:
            v2 = aggregate.aggregate_ssi_v2(
                float(c.get("ssi") or 0.0), c.get("hook"), c.get("structure"),
                c.get("env"), c.get("ot"), c.get("lightning"), weights=weights)
            c["ssi_v2"] = v2["ssi_v2"]
    except ValueError as exc:
        warnings.append(f"phase2 aggregate weights invalid: {exc}")


def _run(config, out_dir, product, max_frames, dry_run, history_dir=None):
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
    if history_dir:
        _archive_history(bundle, history_dir)
    return bundle, _OK if bundle.status == "ok" else _DEGRADED


def _archive_history(bundle, history_dir):
    """Archivio rolling 2h (history.build_history): BEST-EFFORT.

    L'output derivato e' gia' scritto: un errore dell'archivio viene loggato
    come warning (in bundle.warnings -> summary) e NON cambia l'rc del run."""
    try:
        index = history.build_history(bundle, history_dir)
    except Exception as exc:
        bundle.warnings.append(f"history archive failed: {exc}")
        print(f"[history] WARNING: archivio rolling non aggiornato: {exc}")
        return None
    if index is None:
        print("[history] nessuno scan da archiviare.")
        return None
    print(f"[history] slots={len(index.get('slots', []))} "
          f"window={index.get('window_slots')}")
    return index


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
    ap.add_argument("--history-dir", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    config = load_config(args.config)
    if args.keep_frames:
        config["source"]["keep_raw_frames"] = True
    out_dir = os.path.abspath(args.out_dir or config["output"]["out_dir"])

    bundle, rc = _run(config, out_dir, args.product, args.max_frames,
                      args.dry_run, history_dir=args.history_dir)

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