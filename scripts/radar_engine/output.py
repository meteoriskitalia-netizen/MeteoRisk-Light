#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — output.py

Scrittura ATOMICA degli output derivati (data/radar):
    latest.json            — riepilogo stato
    storms.geojson         — FeatureCollection: celle correnti (Point) + track paths (LineString)
    tracks.json            — dettaglio delle track con motion e classificazione
    storm_objects.geojson  — FeatureCollection: STORM OBJECT layer (Fase 1.7)
    storm_tracks.json      — dettaglio delle storm track (motion + confidenze)
    supercells.json        — candidati Supercell Signature Index (Fase 1 SUPERCELL)

Garanzie:
  - scrittura su *.tmp nella stessa directory -> json load di validazione ->
    os.replace() (mai file parziali visibili);
  - su errore le versioni precedenti NON vengono sovrascritte;
  - non viene MAI scritto un GeoTIFF/raw in out_dir.
"""

import json
import os
import tempfile

import numpy as np

from . import models
from . import supercell as sc_mod

SOURCE_LABEL = "Radar-DPC VMI (GeoTIFF quantitativo)"


def _json_default(obj):
    """default= per json.dumps: numpy -> Python nativi, altro -> str.

    Un run normale non lo invoca mai (i valori sono già float/int/str/None);
    interviene solo su oggetti numpy che altrimenti farebbero crashare il
    processo con TypeError (exit 1 senza GITHUB_OUTPUT)."""
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, (list, tuple)):
        return list(obj)
    return str(obj)


def _atomic_write_text(path, text):
    """Scrive text in modo atomico (tmp nella stessa dir + os.replace)."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    json.loads(text)  # validazione JSON PRIMA della replace
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".engine-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise models.OutputError(f"atomic_write_failed:{exc}") from exc


def build_latest(bundle, engine_meta=None):
    """latest.json (stato del run, mai dati raw)."""
    return {
        "status": bundle.status,                 # ok | degraded | error
        "generated_at": bundle.generated_at_iso,
        "engine": engine_meta or {},
        "source": bundle.source,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "radar_timestamp_ms": bundle.radar_timestamp_ms,
        "data_latency_minutes": bundle.data_latency_minutes,
        "frames_count": len(bundle.frames),
        "cells_count": sum(len(c) for c in bundle.cells_by_frame),
        "tracks_count": len([t for t in bundle.tracks if len(t.points) >= 2]),
        "storm_objects_count": sum(len(o) for o in
                                   getattr(bundle, "storm_objects_by_frame", [])),
        "storm_tracks_count": len([t for t in
                                   getattr(bundle, "storm_tracks", [])
                                   if len(t.points) >= 2]),
        "tracking_confidence": _tracking_confidence(bundle.tracks),
        "supercell_tracks_evaluated": getattr(bundle, "supercell_tracks_evaluated", 0),
        **_supercell_summary(bundle),
        **_phase2_summary(bundle),
        "warnings": bundle.warnings[-50:],
    }


def _tracking_confidence(tracks):
    full = sum(1 for t in tracks if t.tracking_confidence == "full")
    low = sum(1 for t in tracks if t.tracking_confidence == "low")
    if not tracks:
        return "no_tracks"
    if full == len(tracks):
        return "full"
    if low:
        return "low"
    return "insufficient"


def _supercell_summary(bundle):
    """Riepilogo candidati supercell (Fase 1 SUPERCELL) per latest.json."""
    cells = getattr(bundle, "supercells", None) or []
    if not cells:
        return {
            "supercell_candidates": 0,
            "supercell_births": 0,
            "supercell_on_latest": 0,
            "supercell_ssi_max": None,
        }
    births = sum(1 for c in cells if c.get("phase") == "birth")
    on_latest = sum(1 for c in cells if c.get("on_latest_frame"))
    ssi_max = max((float(c.get("ssi", 0)) for c in cells), default=None)
    return {
        "supercell_candidates": len(cells),
        "supercell_births": births,
        "supercell_on_latest": on_latest,
        "supercell_ssi_max": ssi_max,
    }


def _phase2_summary(bundle):
    """Riepilogo Fase 2 per latest.json (layer additivo, nessun dato raw)."""
    p2 = getattr(bundle, "phase2", None)
    if not isinstance(p2, dict):
        return {"phase2_status": "unavailable"}
    cells = getattr(bundle, "supercells", None) or []
    return {
        "phase2_status": p2.get("status", "unavailable"),
        "phase2_ssi_v2_max": max(
            (float(c["ssi_v2"]) for c in cells
             if c.get("ssi_v2") is not None), default=None),
    }


def build_storms_geojson(bundle):
    """FeatureCollection: celle dell'ultimo frame valido + tracciati track."""
    features = []
    if bundle.cells_by_frame:
        for cell in bundle.cells_by_frame[-1]:
            track_id = None
            for trk in bundle.tracks:
                if any(p.cell_id == cell.cell_id for p in trk.points):
                    track_id = trk.track_id
                    break
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [cell.centroid_lonlat[0], cell.centroid_lonlat[1]],
                },
                "properties": {
                    **cell.to_dict(),
                    "track_id": track_id,
                    "source": SOURCE_LABEL,
                },
            })
    for trk in bundle.tracks:
        if len(trk.points) < 2:
            continue
        coords = [[p.lonlat[0], p.lonlat[1]] for p in trk.points]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "feature_type": "track_path",
                "track_id": trk.track_id,
                "organization_score": trk.motion.get("organization_score"),
                "classification": trk.motion.get("classification"),
                "source": SOURCE_LABEL,
            },
        })
    return {
        "type": "FeatureCollection",
        "generated_at": bundle.generated_at_iso,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "features": features,
    }


def build_tracks(bundle):
    tracks = [t for t in bundle.tracks if len(t.points) >= 2]
    return {
        "generated_at": bundle.generated_at_iso,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "status": bundle.status,
        "tracks": [t.to_dict() for t in tracks],
    }


def build_storm_objects_geojson(bundle):
    """FeatureCollection dello storm object layer (Fase 1.7).

    Geometry = point del centroide pesato; props includono envelope (bbox),
    convex hull (opzionale), cell_count, area, intensità, motion confidence e
    tracking_ambiguity. storms.geojson (cell-level) NON viene modificato."""
    features = []
    objs_by_frame = getattr(bundle, "storm_objects_by_frame", [])
    if objs_by_frame:
        for obj in objs_by_frame[-1]:
            features.append({
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [obj.centroid_lonlat[0],
                                    obj.centroid_lonlat[1]],
                },
                "properties": {
                    **obj.to_dict(),
                    "envelope": {
                        "west": obj.bbox_lonlat[0],
                        "south": obj.bbox_lonlat[1],
                        "east": obj.bbox_lonlat[2],
                        "north": obj.bbox_lonlat[3],
                    },
                    "source": SOURCE_LABEL,
                },
            })
    for trk in getattr(bundle, "storm_tracks", []):
        if len(trk.points) < 2:
            continue
        coords = [[p.lonlat[0], p.lonlat[1]] for p in trk.points]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "feature_type": "storm_track_path",
                "track_id": trk.track_id,
                "motion_confidence": trk.motion_confidence,
                "tracking_ambiguity": trk.tracking_ambiguity,
                "organization_score": trk.motion.get("organization_score"),
                "classification": trk.motion.get("classification"),
                "mean_cell_count": trk.motion.get("mean_cell_count"),
                "source": SOURCE_LABEL,
            },
        })
    return {
        "type": "FeatureCollection",
        "generated_at": bundle.generated_at_iso,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "features": features,
    }


def build_storm_tracks(bundle):
    tracks = [t for t in getattr(bundle, "storm_tracks", [])
              if len(t.points) >= 2]
    return {
        "generated_at": bundle.generated_at_iso,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "status": bundle.status,
        "tracks": [t.to_dict() for t in tracks],
    }


def build_supercells(bundle, engine_meta=None):
    """supercells.json — candidati Supercell Signature Index (Fase 1 SUPERCELL).

    Solo track con candidate=True (SSI + gate di intensità + frame minimi).
    Nessun claim Doppler: la nota data_limits è inclusa sempre nel payload."""
    cells = getattr(bundle, "supercells", None) or []
    summary = {
        "tracks_evaluated": getattr(bundle, "supercell_tracks_evaluated", 0),
        "candidates": len(cells),
        "births": sum(1 for c in cells if c.get("phase") == "birth"),
        "possible": sum(1 for c in cells if c.get("level") == "possible"),
        "marked": sum(1 for c in cells if c.get("level") == "marked"),
        "on_latest_frame": sum(1 for c in cells if c.get("on_latest_frame")),
        "ssi_max": max((float(c.get("ssi", 0)) for c in cells), default=None),
        "ssi_v2_max": max((float(c["ssi_v2"]) for c in cells
                           if c.get("ssi_v2") is not None), default=None),
    }
    return {
        "generated_at": bundle.generated_at_iso,
        "radar_timestamp": bundle.radar_timestamp_iso,
        "status": bundle.status,
        "engine": engine_meta or {},
        "source": bundle.source,
        "data_limits": sc_mod.DATA_LIMITS_NOTE,
        "phase2": getattr(bundle, "phase2", None),
        "summary": summary,
        "candidates": cells,
    }


def write_outputs(bundle, out_dir, engine_meta=None):
    """Scrive i cinque file derivati in modo atomico. Ritorna le path scritte."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {
        "latest.json": os.path.join(out_dir, "latest.json"),
        "storms.geojson": os.path.join(out_dir, "storms.geojson"),
        "tracks.json": os.path.join(out_dir, "tracks.json"),
        "storm_objects.geojson": os.path.join(out_dir, "storm_objects.geojson"),
        "storm_tracks.json": os.path.join(out_dir, "storm_tracks.json"),
        "supercells.json": os.path.join(out_dir, "supercells.json"),
    }
    contents = {
        "latest.json": json.dumps(build_latest(bundle, engine_meta),
                                  default=_json_default),
        "storms.geojson": json.dumps(build_storms_geojson(bundle),
                                     default=_json_default),
        "tracks.json": json.dumps(build_tracks(bundle),
                                  default=_json_default),
        "storm_objects.geojson": json.dumps(build_storm_objects_geojson(bundle),
                                            default=_json_default),
        "storm_tracks.json": json.dumps(build_storm_tracks(bundle),
                                        default=_json_default),
        "supercells.json": json.dumps(build_supercells(bundle, engine_meta),
                                      default=_json_default),
    }
    # Fase 1: scrivi tutti i tmp e poi replace (atomicità per file);
    # su errore il blocco viene interrotto e i vecchi file restano intatti.
    for name, path in paths.items():
        _atomic_write_text(path, contents[name])
    return paths


def write_status_only(out_dir, status, generated_at_iso, warnings, engine_meta=None):
    """Aggiorna SOLO latest.json (status). storms/tracks restano intatti."""
    bundle = models.EngineBundle(status, generated_at_iso, SOURCE_LABEL)
    bundle.warnings = warnings
    bundle.frames = []
    bundle.cells_by_frame = []
    bundle.tracks = []
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "latest.json")
    _atomic_write_text(path, json.dumps(build_latest(bundle, engine_meta),
                                        default=_json_default))
    return path


def validate_outputs(out_dir):
    """Verifica che gli output derivati esistano e siano JSON/GeoJSON validi."""
    errors = []
    try:
        with open(os.path.join(out_dir, "latest.json"), encoding="utf-8") as fh:
            latest = json.load(fh)
    except (OSError, ValueError) as exc:
        errors.append(f"latest.json: {exc}")
        return errors
    if "status" not in latest:
        errors.append("latest.json missing 'status'")
    if "phase2_status" not in latest:
        errors.append("latest.json missing 'phase2_status'")
    try:
        with open(os.path.join(out_dir, "storms.geojson"), encoding="utf-8") as fh:
            geojson = json.load(fh)
    except (OSError, ValueError) as exc:
        errors.append(f"storms.geojson: {exc}")
        return errors
    if geojson.get("type") != "FeatureCollection":
        errors.append("storms.geojson not FeatureCollection")
    try:
        with open(os.path.join(out_dir, "tracks.json"), encoding="utf-8") as fh:
            tracks = json.load(fh)
    except (OSError, ValueError) as exc:
        errors.append(f"tracks.json: {exc}")
        return errors
    if not isinstance(tracks.get("tracks"), list):
        errors.append("tracks.json missing 'tracks' list")
    # Fase 1.7 storm layer (additivo; richiesto se presenti i file)
    for name in ("storm_objects.geojson", "storm_tracks.json"):
        path = os.path.join(out_dir, name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                payload = json.load(fh)
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {exc}")
            continue
        if name.endswith(".geojson") and payload.get("type") != "FeatureCollection":
            errors.append(f"{name} not FeatureCollection")
        if name == "storm_tracks.json" and not isinstance(payload.get("tracks"), list):
            errors.append("storm_tracks.json missing 'tracks' list")
    # Fase 1 SUPERCELL layer (additivo; richiesto se presente)
    spath = os.path.join(out_dir, "supercells.json")
    if os.path.exists(spath):
        try:
            with open(spath, encoding="utf-8") as fh:
                sp = json.load(fh)
        except (OSError, ValueError) as exc:
            errors.append(f"supercells.json: {exc}")
            return errors
        if not isinstance(sp.get("candidates"), list):
            errors.append("supercells.json missing 'candidates' list")
        if "data_limits" not in sp:
            errors.append("supercells.json missing 'data_limits'")
        if "phase2" not in sp:
            errors.append("supercells.json missing 'phase2'")
    return errors