#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.calibration (Fase 1.5)

Esegue il motore su CASE LOCALI (serie VMI reali fuori repo) con override di
configurazione, per:
  - sweep a 1 parametro (sensitivity)   -> righe per parametro/valore
  - grid search LIMITATA (combinazioni piccole)
  - stabilità temporale su finestre sovrapposte (Fase 6)

NON modifica i parametri del motore: lavora su copie deepcopy e produce
esclusivamente OUTPUT (risultati + report). Nessuna auto-ottimizzazione.
"""

import copy

from .. import detect as detect_mod
from .. import tracking as track_mod
from ..config import load_config
from .cases import stamp_raster


def run_window(frame_items, cfg=None, with_scoring=True):
    """Esegue detection+tracking(+score) su una serie [(path, ts_ms)].

    Ritorna dict: frames, cells_by_frame, tracks, n_frames, errors."""
    cfg = cfg if cfg is not None else load_config()
    rasters = []
    errors = []
    for path, ts in frame_items:
        try:
            rasters.append(stamp_raster(path, ts))
        except Exception as exc:  # file corrotto: frame scartato, NON si inventa tempo
            errors.append(f"{path}: {exc}")

    cells_by_frame = []
    for raster in rasters:
        cells = detect_mod.detect_cells(raster, cfg["detect"])
        for c in cells:
            c.frame_index = len(cells_by_frame)
        cells_by_frame.append(cells)

    tracker = track_mod.Tracker(cfg["tracking"],
                                cfg_scoring=cfg.get("scoring"))
    for idx, cells in enumerate(cells_by_frame):
        tracker.update(cells, idx)
    tracker.complete_cycles()
    tracks = tracker.finalize(cfg_scoring=cfg.get("scoring"))
    return {
        "rasters": rasters,
        "cells_by_frame": cells_by_frame,
        "tracks": tracks,
        "tracker": tracker,
        "n_frames": len(rasters),
        "errors": errors,
    }


def _deep_set(cfg, path, value):
    """Imposta cfg[a][b][...] = value su una COPIA (mutazione locale)."""
    node = cfg
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def summarize_window(result):
    """Metriche sintetiche di una run (per tabelle sensitivity/stability)."""
    from . import metrics
    cells = result["cells_by_frame"]
    tracks = result["tracks"]
    total_cells = sum(len(c) for c in cells)
    return {
        "frames": result["n_frames"],
        "cells_total": total_cells,
        "cells_median": round(float(_median([len(c) for c in cells])), 1)
        if cells else 0.0,
        "tracks": len([t for t in tracks if len(t.points) >= 2]),
        "mean_duration_min": metrics.mean_track_duration_min(tracks),
        "continuity": metrics.track_continuity(tracks, result["n_frames"]),
        "birth_rate": metrics.birth_rate(tracks, total_cells),
        "motion_error_deg": metrics.motion_error(tracks),
        "dir_stability_deg": metrics.direction_stability(tracks),
        "score": metrics.score_stats(tracks),
        "false_high": len(metrics.false_high_scores(tracks)),
    }


def _median(vals):
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def sweep_parameter(frame_items, param_path, values, base_cfg=None):
    """Sweep 1 parametro. Ritorna lista {param, value, **summary}."""
    base = base_cfg if base_cfg is not None else load_config()
    rows = []
    for v in values:
        cfg = copy.deepcopy(base)
        _deep_set(cfg, param_path, v)
        result = run_window(frame_items, cfg=cfg)
        row = {"param": ".".join(param_path), "value": v}
        row.update(summarize_window(result))
        rows.append(row)
    return rows


def grid_search(frame_items, path_values, base_cfg=None):
    """Grid search LIMITATA: product delle liste valori di ogni parametro.

    path_values: lista di (path_list, [values...]). Ritorna righe con 'combo'."""
    import itertools
    base = base_cfg if base_cfg is not None else load_config()
    rows = []
    value_lists = [list(vals) for _, vals in path_values]
    for combo in itertools.product(*value_lists):
        cfg = copy.deepcopy(base)
        label = {}
        for (path, _), val in zip(path_values, combo):
            _deep_set(cfg, path, val)
            label[".".join(path)] = val
        result = run_window(frame_items, cfg=cfg)
        row = {"combo": json_safe(label)}
        row.update(summarize_window(result))
        rows.append(row)
    return rows


def json_safe(obj):
    import json
    return json.loads(json.dumps(obj, default=str))


def stability_windows(frame_items, base_cfg=None, window_size=6, step=1):
    """Fase 6: finestre sovrapposte [0..w), [1..w+1), ... Ritorna per finestra
    la lista frame_items slice + run + summary. Il confronto inter-finestra
    (ID switch / score jump) è calcolato dal chiamante (report.py)."""
    base = base_cfg if base_cfg is not None else load_config()
    windows = []
    i = 0
    while i + window_size <= len(frame_items):
        slice_items = frame_items[i:i + window_size]
        result = run_window(slice_items, cfg=copy.deepcopy(base))
        windows.append({
            "window": list(range(i, i + window_size)),
            "run": result,
            "summary": summarize_window(result),
        })
        i += step
    return windows