#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — validation.cases (Fase 1.5)

Casi retrospettivi: metadata-only nel repository (cases/cases.json). I GeoTIFF
radar DPC vivono esclusivamente FUORI dal repo (RADAR_SAMPLES_DIR); la
risoluzione campioni avviene per pattern della directory samples:
    <RADAR_SAMPLES_DIR>/<YYYY-MM-DD>/VMI-<HHMM>.tif   (HHMM, UTC, griglia 5min)

Tipi evento consentiti (categorie NEUTRE, nessuna inferenza supercell):
    ORDINARY_CONVECTION | ORGANIZED_MULTICELL | LINEAR_CONVECTION |
    SEVERE_CONVECTION | DOCUMENTED_SUPERCELL | UNKNOWN
DOCUMENTED_SUPERCELL è ammesso SOLO con una fonte documentata verificata
(field 'reference_sources'); in Fase 1.5 le celle NON vengono classificate
automaticamente come supercell da nessun modulo.
"""

import glob
import json
import os
from datetime import datetime, timezone

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

from radar_engine import models  # noqa: E402

EVENT_TYPES = (
    "ORDINARY_CONVECTION",
    "ORGANIZED_MULTICELL",
    "LINEAR_CONVECTION",
    "SEVERE_CONVECTION",
    "DOCUMENTED_SUPERCELL",
    "UNKNOWN",
)

DEFAULT_SAMPLES_DIR = r"C:\Users\aless\AppData\Local\Temp\opencode\radar_samples"


def _find_cases_json(start=None):
    """Localizza cases/cases.json risalendo la gerarchia fino al progetto."""
    cur = os.path.abspath(start or os.getcwd())
    for _ in range(8):
        cand = os.path.join(cur, "cases", "cases.json")
        if os.path.exists(cand):
            return cand
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    raise FileNotFoundError("cases/cases.json non trovato nelle directory superiori")


def load_cases(repo_root=None):
    """Legge cases/cases.json (metadata-only). Errore su schema/type non validi."""
    path = _find_cases_json(repo_root)
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    cases = payload if isinstance(payload, list) else payload.get("cases", [])
    if not isinstance(cases, list):
        raise ValueError("cases.json: atteso una lista ('cases').")
    for case in cases:
        _validate_case(case)
    return cases


def _validate_case(case):
    for field in ("id", "name", "start", "end", "type", "region",
                  "description", "reference_sources"):
        if field not in case:
            raise ValueError(f"case '{case.get('id')}': campo '{field}' mancante")
    if case["type"] not in EVENT_TYPES:
        raise ValueError(f"case '{case['id']}': type '{case['type']}' non valido")
    if case["type"] == "DOCUMENTED_SUPERCELL" and not case.get("reference_sources"):
        raise ValueError(
            f"case '{case['id']}': DOCUMENTED_SUPERCELL richiede reference_sources")
    for iso in (case["start"], case["end"]):
        datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")


def samples_root(env_var="RADAR_SAMPLES_DIR"):
    """Directory campioni (esterna al repo). Priorità: env -> default temp."""
    return os.environ.get(env_var) or DEFAULT_SAMPLES_DIR


def resolve_case_frames(case, root=None, step_min=5):
    """Risolve la serie di (path, ts_ms) per un caso dalla directory samples.

    Il caso copre [start, end) sulla griglia 5-min; vengono restituiti solo i
    file realmente presenti in <root>/<date>/VMI-HHMM.tif. Ordine cronologico."""
    root = root or samples_root()
    ts0_ms = int(datetime.strptime(case["start"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
    ts1_ms = int(datetime.strptime(case["end"], "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc).timestamp() * 1000)
    step_ms = step_min * 60000
    out = []
    for ts in range(ts0_ms, ts1_ms, step_ms):
        dt = datetime.fromtimestamp(ts / 1000.0, timezone.utc)
        daydir = os.path.join(root, dt.strftime("%Y-%m-%d"))
        fname = f"VMI-{dt.strftime('%H%M')}.tif"
        path = os.path.join(daydir, fname)
        if os.path.exists(path):
            out.append((path, ts))
    return out


def available_days(root=None):
    """Elenco giorni con campioni (per diagnosi di copertura)."""
    root = root or samples_root()
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


def describe(case, frames=None):
    """Riepilogo leggibile di un caso (+ campioni risolti se forniti)."""
    n = len(frames) if frames is not None else "?"
    return (f"[{case['id']}] {case['name']} :: {case['start']}->{case['end']} "
            f"type={case['type']} region={case['region']} samples={n}")

# compat helper: riapertura raster valido con timestamp reale (per calibration)
def stamp_raster(path, ts_ms):
    """read_raster + timestamp reale (da metadata caso, mai inventato)."""
    from radar_engine import preprocess
    from radar_engine.config import CONFIG
    raster = preprocess.read_raster(
        str(path),
        nodata_values=CONFIG["preprocess"]["nodata_values"],
        geo_plausible_bbox=CONFIG["preprocess"]["geo_plausible_bbox"])
    raster.time_ms = int(ts_ms)
    raster.time_iso = datetime.fromtimestamp(ts_ms / 1000.0, timezone.utc)\
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    return raster