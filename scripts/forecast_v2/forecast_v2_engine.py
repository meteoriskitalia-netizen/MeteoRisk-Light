#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_v2_engine.py — MeteoRisk "Mappe previsionali V2" (ENGINE-ONLY, server).

Motore che costruisce in GitHub Actions una CACHE GITHUB di mappe previsionali
MULTI-DOMINIO: per ogni dominio richiesto e per ogni istante di previsione
produce UN file `.om` multi-variabile croppato sull'Italia, leggibile dal reader
WML 0.2.2 (stesso formato del POC `spike_omfiles/`, verificato bit-exact).

    python scripts/forecast_v2/forecast_v2_engine.py \
        [--domains dwd_icon_eu ecmwf_ifs025 ...] [--valid-frames 0] \
        [--out data/fm2] [--once] [--no-prune] [--dry-run] [--force] \
        [--frames-cache .cache_fm2_frames]

    --valid-frames 0 (default) o negativo = pubblica l'INTERO orizzonte di ogni
    dominio (tutti i valid_times del rispettivo manifest). N > 0 = primi N frame.

Dati sorgente (Open-Meteo "spatial" files, accesso S3 anonimo):
    https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/<YYYY>/<MM>/<DD>/<HH00Z>/<YYYY-MM-DDTHH00.om
    https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/latest.json   (manifest)

Disegno (V2 del motore, scelte DICHIARATE):
  - MULTI-DOMINIO INDIPENDENTE: ogni dominio elencato in `--domains` e' trattato
    in modo AUTONOMO (fine del paradigma primario+secondario). Ogni dominio
    produce la PROPRIA cache, sulla PROPRIA griglia, nel proprio sottodirectory
    `data/fm2/<dominio>/`, con il proprio `latest.json`. Un dominio non fornisce
    mai frame a un altro.
  - RICONOSCIMENTO CRS: dal `crs_wkt` del manifest (e del frame) si riconosce il
    sistema di riferimento. Se NON inizia per `GEOGCRS` (es. il `PROJCRS`
    Lambert del `chmi_aladin_central_europe_2km`) il dominio viene SKIPPATO con
    log esplicito `SKIP dominio '<d>': griglia proiettata non supportata
    (PROJCRS/Lambert)` e la run prosegue con gli altri.
  - GRIGLIA DERIVATA: `latMin/lonMin/latMax/lonMax` vengono letti dal `BBOX[...]`
    del `crs_wkt` (regex); `nx,ny` dal `.shape == (ny,nx)` del PRIMO frame `.om`
    del dominio (via `OmFileReader`, stesso `fetch_frame` -> va in frames-cache).
    Poi `dx=(lonMax-lonMin)/(nx-1)`, `dy=(latMax-latMin)/(ny-1)`. Il dict griglia
    e' memorizzato in una cache per-run. La tabella `GRIDS` resta solo come
    FALLBACK (dry-run, dove il frame non si scarica) e come GUARDIA di coerenza:
    se i dx/dy derivati si scostano >1% dai valori noti (e il dominio e' in
    GRIDS) si logga un warning ma si USANO i valori derivati.
  - CROP PER-DOMINIO: ogni dominio e' croppato sul PROPRIO bbox via `bbox_for(domain)`/`DOMAIN_BBOX`
    (default/fallback `V2_BBOX` = Italia (36.5, 6.5, 47.2, 18.6)), applicato alla griglia via `bbox_to_indices`/`target_latlon` (generici).
  - ORIZZONTE INTERO: si pubblicano TUTTI i valid_times del manifest del dominio
    (nessuna troncatura a un numero fisso di frame). --valid-frames 0/negativo
    (default) = intero orizzonte; N > 0 = primi N.
  - VARIABILI: lista UNICA `V2_RISK_VARS` (24) per tutti i domini. Nessun set
    per-dominio: la presenza reale e' decisa FILE-PER-FILE dai child del `.om`.
    Le variabili assenti nella sorgente sono OMESSE con warning (es.
    `surface_pressure`, spesso non disponibile).
  - NO-OP GUARD PER-DOMINIO: se `data/fm2/<dominio>/latest.json` esiste con lo
    stesso `reference_time` e gli stessi `valid_times` (e non --force) il dominio
    viene saltato SENZA scaricare/riscrivere (evita commit inutili).
  - PRUNING PER-DOMINIO: in `data/fm2/<dominio>/` resta ESATTAMENTE la finestra
    corrente (le ancore `valid_times` pubblicate in questo run); ogni `.om`
    estraneo (reference_time precedenti) viene rimosso. `V2_MAX_FRAMES` e' un cap
    OPZIONALE di sicurezza del pruning (0 default = nessun cap).
  - FRAMES-CACHE (--frames-cache): cache PERSISTENTE dei frame `.om` full-size
    (ripristinata in CI via actions/cache), layout `<dir>/<dominio>/<VALID>.om`.
    Un frame in cache viene riusato SENZA download se la sua Content-Length
    combacia con quella remota (HEAD leggero; fallback: size locale).
  - EXIT: se TUTTI i domini falliscono -> exit 1. Se almeno UN dominio e' `ok`
    (anche solo NO-OP) -> exit 0, con summary esplicito `ok`/`skip`/`fail` (il
    workflow deve poter committare la cache dei domini riusciti).
  - Scrittura ATOMICA (.tmp -> os.replace) e nessun segreto: l'accesso S3 e'
    anonimo. Nessuna dipendenza oltre `omfiles` e `numpy`.

LICENZE / ATTRIBUZIONE:
  - Dati meteo: Open-Meteo (https://open-meteo.com), licenza CC BY 4.0 ->
    attribuzione a Open-Meteo richiesta in ogni ridistribuzione.
  - `omfiles` (GPL-2.0-only) e' usato SOLO a runtime/CI per leggere e scrivere
    il formato `.om`; NON viene venduto ne' incluso nel repository (dipendenza
    pip dichiarata in requirements.txt).
"""

import argparse
import datetime as _dt
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

import numpy as np

try:
    from omfiles import OmFileReader, OmFileWriter
except ImportError:  # pragma: no cover - dipendenza CI
    print("[forecast_v2] ERRORE: modulo 'omfiles' assente. "
          "Eseguire: python -m pip install -r scripts/forecast_v2/requirements.txt")
    raise

LOG = "[forecast_v2]"

# ---------------------------------------------------------------------------
# Configurazione (costanti dichiarate)
# ---------------------------------------------------------------------------
S3_BASE = "https://openmeteo.s3.amazonaws.com/data_spatial"

# (lat_min, lon_min, lat_max, lon_max) — bbox Italia.
V2_BBOX = (36.5, 6.5, 47.2, 18.6)

# Per-domain crop window [lat_min, lon_min, lat_max, lon_max].
# Each domain is cut to its own real area (prima: tutti tagliati sull'Italia).
DOMAIN_BBOX = {
    "dwd_icon_eu": (34.0, -15.0, 62.0, 40.0),
    "dwd_icon_d2": (43.18, -3.94, 58.08, 20.34),
    "italia_meteo_arpae_icon_2i": (36.5, 6.5, 47.2, 18.6),
    "ecmwf_ifs025": (-90.0, -180.0, 90.0, 179.75),
    "ncep_gfs025": (-90.0, -180.0, 90.0, 179.75),
    "dwd_icon": (-90.0, -180.0, 90.0, 179.75),
    "meteofrance_arpege_world025": (-90.0, -180.0, 90.0, 179.75),
    "ukmo_global_deterministic_10km": (-90.0, -180.0, 89.90625, 179.85938),
    "jma_gsm": (-90.0, -180.0, 90.0, 179.5),
    "cma_grapes_global": (-89.9375, -180.0, 89.9375, 179.875),
}

def bbox_for(domain):
    return DOMAIN_BBOX.get(domain, V2_BBOX)

# Domini di default (MULTI-DOMINIO INDIPENDENTE: nessun ordine di priorita').
V2_DOMAINS = ["dwd_icon_eu", "dwd_icon_d2", "italia_meteo_arpae_icon_2i", "ecmwf_ifs025", "ncep_gfs025", "dwd_icon", "meteofrance_arpege_world025", "ukmo_global_deterministic_10km", "jma_gsm", "cma_grapes_global"]

# Lista ORDINATA delle variabili di rischio desiderate, con sinonimi/forme.
# name = nome pubblicato nel `.om`; sources = nomi candidati nel file sorgente
# (il primo presente vince). La presenza reale e' determinata per-file.
V2_RISK_VARS = [
    {"name": "temperature_2m",       "sources": ["temperature_2m"]},
    {"name": "temperature_850hPa",   "sources": ["temperature_850hPa"]},      # FM_CACHE core
    {"name": "temperature_500hPa",   "sources": ["temperature_500hPa"]},      # FM_CACHE core
    {"name": "relative_humidity_2m", "sources": ["relative_humidity_2m"]},
    {"name": "relative_humidity_850hPa", "sources": ["relative_humidity_850hPa"]},  # FM_CACHE core
    {"name": "pressure_msl",         "sources": ["pressure_msl", "msl_pressure"]},
    {"name": "geopotential_height_500hPa", "sources": ["geopotential_height_500hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_850hPa", "sources": ["geopotential_height_850hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_1000hPa", "sources": ["geopotential_height_1000hPa"]},  # FM_CACHE core
    {"name": "surface_pressure",     "sources": ["surface_pressure"]},        # spesso assente
    {"name": "wind_u_component_10m", "sources": ["wind_u_component_10m"]},
    {"name": "wind_v_component_10m", "sources": ["wind_v_component_10m"]},
    {"name": "wind_gusts_10m",       "sources": ["wind_gusts_10m"]},
    {"name": "cloud_cover",          "sources": ["cloud_cover"]},
    {"name": "cloud_cover_low",      "sources": ["cloud_cover_low"]},        # FM_CACHE core
    {"name": "cloud_cover_mid",      "sources": ["cloud_cover_mid"]},        # FM_CACHE core
    {"name": "cloud_cover_high",     "sources": ["cloud_cover_high"]},       # FM_CACHE core
    {"name": "cape",                 "sources": ["cape"]},
    {"name": "snow_depth",           "sources": ["snow_depth"]},
    {"name": "precipitation",        "sources": ["precipitation"]},
    {"name": "rain",                 "sources": ["rain"]},
    {"name": "showers",              "sources": ["showers"]},
    {"name": "weather_code",         "sources": ["weather_code"]},
    {"name": "freezing_level_height", "sources": ["freezing_level_height"]},  # FM_CACHE core
]

# Cap OPZIONALE di sicurezza del pruning (numero massimo di frame `.om`
# mantenuti in una dir di dominio). 0 = NESSUN CAP: si mantiene l'orizzonte
# INTERO del manifest (tutti i valid_times). E' anche il default di
# --valid-frames (0 = intero).
V2_MAX_FRAMES = 0
V2_OUT = "data/fm2"

# Passo di quantizzazione voluto per variabile pubblicata (.om).
# CONVENZIONE omfiles: il writer memorizza int = round(valore * scale_factor)
# e il lettore rilegge valore = int / scale_factor: quindi il passo effettivo
# e' 1/scale_factor. Qui si dichiarano i PASSI (unita' della variabile); il
# writer riceve scale_factor = 1.0 / passo (vedi write_frame_om).
VARIABLE_STEP = {
    "temperature_2m": 0.1,              # 0.1 degC
    "temperature_850hPa": 0.1,          # 0.1 degC
    "temperature_500hPa": 0.1,          # 0.1 degC
    "relative_humidity_2m": 1.0,        # 1 %
    "relative_humidity_850hPa": 1.0,    # 1 %
    "pressure_msl": 0.1,                # 0.1 hPa
    "geopotential_height_500hPa": 1.0,  # 1 m
    "geopotential_height_850hPa": 1.0,  # 1 m
    "geopotential_height_1000hPa": 1.0, # 1 m
    "surface_pressure": 0.1,            # 0.1 hPa
    "wind_u_component_10m": 0.1,        # 0.1 m/s
    "wind_v_component_10m": 0.1,        # 0.1 m/s
    "wind_gusts_10m": 0.1,              # 0.1 m/s
    "cloud_cover": 1.0,                 # 1 %
    "cloud_cover_low": 1.0,             # 1 %
    "cloud_cover_mid": 1.0,             # 1 %
    "cloud_cover_high": 1.0,            # 1 %
    "cape": 10.0,                       # 10 J/kg
    "snow_depth": 0.1,                  # 0.1 m
    "precipitation": 0.1,               # 0.1 mm
    "rain": 0.1,                        # 0.1 mm
    "showers": 0.1,                     # 0.1 mm
    "weather_code": 1.0,                # 1 (codice WMO)
    "freezing_level_height": 10.0,      # 10 m
}
DEFAULT_STEP = 1.0
DEFAULT_CHUNKS = [32, 32]
DEFAULT_COMPRESSION = "pfor_delta_2d_int16"

# Prefisso WKT dei CRS geografici supportati. Tutto cio' che non inizia per
# GEOGCRS (es. PROJCRS/Lambert) viene SKIPPATO.
GEOGRAPHIC_CRS_PREFIX = "GEOGCRS"

# Regex del BBOX nel WKT (ordine WKT: [latMin, lonMin, latMax, lonMax]).
_BBOX_RE = re.compile(
    r"BBOX\s*\[\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*"
    r"([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\]")

# Tabella delle griglie NOTE dei 10 domini GEOGCRS. NON e' la fonte unica:
# la griglia di run e' DERIVATA da crs_wkt (BBOX) + shape del frame. Questa
# tabella serve SOLO come (a) FALLBACK nel --dry-run (dove il frame non si
# scarica, quindi nx/ny non e' derivabile) e (b) GUARDIA di coerenza sui dx/dy.
GRIDS = {
    "dwd_icon_eu":                  {"nx": 1377, "ny": 657,  "dx": 0.0625,   "dy": 0.0625},
    "ecmwf_ifs025":                 {"nx": 1440, "ny": 721,  "dx": 0.25,     "dy": 0.25},
    "ncep_gfs025":                  {"nx": 1440, "ny": 721,  "dx": 0.25,     "dy": 0.25},
    "dwd_icon":                     {"nx": 2879, "ny": 1441, "dx": 0.125,    "dy": 0.125},
    "meteofrance_arpege_world025":  {"nx": 1440, "ny": 721,  "dx": 0.25,     "dy": 0.25},
    "ukmo_global_deterministic_10km": {"nx": 2560, "ny": 1920, "dx": 0.140625, "dy": 0.09375},
    "jma_gsm":                      {"nx": 720,  "ny": 361,  "dx": 0.5,      "dy": 0.5},
    "cma_grapes_global":            {"nx": 2880, "ny": 1440, "dx": 0.125,    "dy": 0.125},
    "dwd_icon_d2":                  {"nx": 1215, "ny": 746,  "dx": 0.02,     "dy": 0.02},
    "italia_meteo_arpae_icon_2i":   {"nx": 761,  "ny": 761,  "dx": 0.025,    "dy": 0.02},
}


# ---------------------------------------------------------------------------
# Utility I/O di rete
# ---------------------------------------------------------------------------
def _urlopen(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "MeteoRisk/1.2.5 multi-domain"})
    return urllib.request.urlopen(req, timeout=timeout)


def http_read_json(url, retries=3):
    """GET+parse JSON con retry/backoff. Ritorna (data, None) o (None, errore)."""
    last = None
    for attempt in range(1, retries + 1):
        try:
            with _urlopen(url, timeout=60) as r:
                return json.loads(r.read().decode("utf-8")), None
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(2 * attempt)
    return None, last


def download(url, dest, retries=3):
    """Scarica `url` in `dest` (atomico .part -> replace). Ritorna True/False."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    last = None
    for attempt in range(1, retries + 1):
        try:
            with _urlopen(url, timeout=180) as r, open(part, "wb") as fh:
                shutil.copyfileobj(r, fh, length=1 << 20)
            os.replace(part, dest)
            return True
        except Exception as exc:  # noqa: BLE001
            last = exc
            try:
                os.remove(part)
            except OSError:
                pass
            if attempt < retries:
                time.sleep(3 * attempt)
    print("%s [WARN] download fallito %s: %s" % (LOG, url, last))
    return False


def http_content_length(url, timeout=30):
    """HEAD leggero: Content-Length remoto, o None se non ottenibile.

    Usato SOLO nel ramo `--frames-cache` per decidere se un frame gia' in cache
    e' aggiornato. Se la HEAD fallisce (rete/endpoint) ritorna None e il chiamante
    ricade sulla dimensione del file locale (nessun download forzato).
    """
    try:
        req = urllib.request.Request(
            url, method="HEAD",
            headers={"User-Agent": "MeteoRisk/1.2.5 multi-domain"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            cl = r.headers.get("Content-Length")
            return int(cl) if cl is not None else None
    except Exception:  # noqa: BLE001
        return None


def fetch_frame(url, domain, valid_name, cache_dir, frames_cache, label="frame"):
    """Percorso locale del frame, riusando la cache quando valida.

    Con `frames_cache` attivo (cache persistente, ripristinata via
    actions/cache): file in `<frames_cache>/<domain>/<valid_name>.om`. Se esiste
    ed e' aggiornato (Content-Length via HEAD combacia; fallback: size locale se
    la HEAD non e' disponibile) NON esegue alcuna GET e riusa il file. Se assente
    o stale lo scarica (atomico) in quella posizione.

    Senza `frames_cache`: comportamento INVARIATO su `cache_dir`
    (`<cache_dir>/<domain>__<valid_name>.om`, flat, nessuna HEAD).

    Ritorna (percorso, riusato) oppure (None, False) se il download fallisce.
    """
    if frames_cache:
        dst = os.path.join(frames_cache, domain, valid_name + ".om")
        if os.path.isfile(dst):
            expected = http_content_length(url)
            local = os.path.getsize(dst)
            if expected is None or local == expected:
                print("%s   %s reuse (cache %s, %d byte%s)"
                      % (LOG, label, dst, local,
                         "" if expected is None else ", Content-Length ok"))
                return dst, True
            print("%s   %s cache stale (%d != %d byte, %s): riscarico"
                  % (LOG, label, local, expected, dst))
        if download(url, dst):
            return dst, False
        return None, False

    dst = os.path.join(cache_dir, "%s__%s.om" % (domain, valid_name))
    if os.path.isfile(dst):
        return dst, True
    if download(url, dst):
        return dst, False
    return None, False


# ---------------------------------------------------------------------------
# Tempo
# ---------------------------------------------------------------------------
def parse_iso(ts):
    """'2026-10-11T15:00Z' -> datetime UTC (aware)."""
    return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))


def fmt_iso(dt):
    return dt.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def valid_time_to_filename(vt):
    """'2026-10-11T15:00Z' -> '2026-10-11T1500' (nome file locale)."""
    return vt.replace(":", "").replace("Z", "")


def s3_url(domain, ref_time_iso, valid_time_iso):
    d = parse_iso(ref_time_iso)
    name = valid_time_to_filename(valid_time_iso)
    return "%s/%s/%04d/%02d/%02d/%02d00Z/%s.om" % (
        S3_BASE, domain, d.year, d.month, d.day, d.hour, name)


def s3_manifest_url(domain):
    return "%s/%s/latest.json" % (S3_BASE, domain)


# ---------------------------------------------------------------------------
# Griglie e crop (derivazione da crs_wkt + frame; GRIDS solo fallback/guardia)
# ---------------------------------------------------------------------------
def is_geographic_crs(crs_wkt):
    """True se il CRS e' geografico (WKT GEOGCRS...). PROJCRS/Lambert -> False."""
    return (crs_wkt or "").lstrip().upper().startswith(GEOGRAPHIC_CRS_PREFIX)


def crs_kind(crs_wkt):
    """Etichetta leggibile del tipo di CRS (per log/report SKIP)."""
    head = (crs_wkt or "").lstrip().split("[", 1)[0].strip()
    return head or "(assente)"


def parse_bbox(crs_wkt):
    """Estrae (latMin, lonMin, latMax, lonMax) dal `BBOX[...]` del WKT."""
    m = _BBOX_RE.search(crs_wkt or "")
    if not m:
        return None
    lat_min, lon_min, lat_max, lon_max = (float(x) for x in m.groups())
    return lat_min, lon_min, lat_max, lon_max


def derive_grid(crs_wkt, nx, ny):
    """Griglia RegularGridFromOrigin derivata da crs_wkt (BBOX) + nx/ny (shape).

    Ritorna il dict {nx,ny,lonMin,latMin,dx,dy} oppure None se BBOX assente o
    dimensioni non valide.
    """
    bbox = parse_bbox(crs_wkt)
    if bbox is None:
        return None
    lat_min, lon_min, lat_max, lon_max = bbox
    if nx is None or ny is None or int(nx) < 2 or int(ny) < 2:
        return None
    nx, ny = int(nx), int(ny)
    dx = (lon_max - lon_min) / (nx - 1)
    dy = (lat_max - lat_min) / (ny - 1)
    return {"nx": nx, "ny": ny, "lonMin": lon_min, "latMin": lat_min,
            "dx": dx, "dy": dy}


def grid_guard(domain, grid):
    """Confronta la griglia derivata con quella nota (GRIDS). Ritorna warning.

    Se il dominio NON e' in GRIDS non c'e' nulla da confrontare (nessun log).
    Scarto > 1% su dx/dy -> warning, ma il chiamante usa comunque i derivati.
    """
    known = GRIDS.get(domain)
    if not known:
        return []
    msgs = []
    for key in ("dx", "dy"):
        kv = known.get(key)
        gv = grid.get(key)
        if kv and gv and abs(gv - kv) / kv > 0.01:
            msgs.append("griglia derivata %s=%.6g != noto %.6g (>1%%): uso derivato"
                        % (key, gv, kv))
    return msgs


def frame_shape(path):
    """(ny, nx) dal PRIMO child array del frame `.om` (o None)."""
    reader = OmFileReader.from_path(path)
    try:
        for i in range(reader.num_children):
            c = reader.get_child_by_index(i)
            if not c.is_scalar:
                shp = getattr(c, "shape", None)
                if shp is not None and len(shp) == 2:
                    return int(shp[0]), int(shp[1])
        return None
    finally:
        reader.close()


def bbox_to_indices(grid, bbox):
    """Converte bbox (lat_min, lon_min, lat_max, lon_max) in slice di indice.

    Ritorna dict con i0/i1 (lon, [i0,i1] inclusivo), j0/j1 (lat, [j0,j1]
    inclusivo) e la griglia del crop (out_grid). Clamp ai limiti della griglia.
    """
    lat_min, lon_min, lat_max, lon_max = bbox
    dx, dy = grid["dx"], grid["dy"]
    i0 = int(round((lon_min - grid["lonMin"]) / dx))
    i1 = int(math.floor((lon_max - grid["lonMin"]) / dx))
    j0 = int(round((lat_min - grid["latMin"]) / dy))
    j1 = int(math.floor((lat_max - grid["latMin"]) / dy))
    i0 = max(0, min(grid["nx"] - 1, i0))
    i1 = max(i0, min(grid["nx"] - 1, i1))
    j0 = max(0, min(grid["ny"] - 1, j0))
    j1 = max(j0, min(grid["ny"] - 1, j1))
    out_grid = {
        "nx": i1 - i0 + 1,
        "ny": j1 - j0 + 1,
        "lonMin": round(grid["lonMin"] + i0 * dx, 10),
        "latMin": round(grid["latMin"] + j0 * dy, 10),
        "dx": dx,
        "dy": dy,
    }
    return {"i0": i0, "i1": i1, "j0": j0, "j1": j1, "out_grid": out_grid}


def target_latlon(grid, idx):
    """Array lat/lon dei centri-cella del crop (ordine riga=lat)."""
    lats = grid["latMin"] + np.arange(idx["j0"], idx["j1"] + 1) * grid["dy"]
    lons = grid["lonMin"] + np.arange(idx["i0"], idx["i1"] + 1) * grid["dx"]
    return lats.astype(np.float64), lons.astype(np.float64)


def nearest_indices(grid, lats, lons):
    """Indici (jj, ii) della cella piu' vicina di `grid` a ogni (lat, lon)."""
    jj = np.rint((lats - grid["latMin"]) / grid["dy"]).astype(np.int64)
    ii = np.rint((lons - grid["lonMin"]) / grid["dx"]).astype(np.int64)
    jj = np.clip(jj, 0, grid["ny"] - 1)
    ii = np.clip(ii, 0, grid["nx"] - 1)
    return jj, ii


def read_and_resample(reader, name, src_grid, tgt_lats, tgt_lons):
    """Legge `name` dal reader e lo porta sulla griglia target (nearest).

    Nel multi-dominio indipendente la griglia target E' la griglia del dominio
    stesso (per-domain crop): nearest = identita' perche' i centri-cella del target
    coincidono con quelli della sorgente, quindi il risultato e' il crop esatto.
    """
    jj, ii = nearest_indices(src_grid, tgt_lats, tgt_lons)
    jmin, jmax = int(jj.min()), int(jj.max())
    imin, imax = int(ii.min()), int(ii.max())
    child = reader.get_child_by_name(name)
    if child is None:
        raise KeyError(name)
    window = child.read_array((slice(jmin, jmax + 1), slice(imin, imax + 1)))
    window = np.asarray(window, dtype=np.float32)
    return np.ascontiguousarray(window[np.ix_(jj - jmin, ii - imin)]).astype(np.float32)


def array_child_names(reader):
    names = []
    for i in range(reader.num_children):
        c = reader.get_child_by_index(i)
        if not c.is_scalar:
            names.append(c.name)
    return set(names)


# ---------------------------------------------------------------------------
# Risoluzione variabili per frame (multi-dominio INDIPENDENTE, no secondario)
# ---------------------------------------------------------------------------
def resolve_direct(risk, child_names):
    """Primo candidato presente nei child; ritorna nome sorgente o None."""
    for cand in risk["sources"]:
        if cand in child_names:
            return cand
    return None


def resolve_domain_frame(reader, grid, risk_vars, tgt_lats, tgt_lons, idx, domain):
    """Costruisce gli array di output del frame del dominio + metadati.

    Ritorna (arrays, variables_meta, warnings). `arrays` e' dict
    nome_pubblicato -> np.float32 crop. Nessun dominio secondario: la griglia
    sorgente E' la griglia del dominio (il crop e' esatto).
    """
    arrays = {}
    meta = {}
    warnings = []

    names = array_child_names(reader)

    # 1) risoluzione diretta (file-per-file) dalla griglia del dominio
    for rv in risk_vars:
        src = resolve_direct(rv, names)
        if src is None:
            continue
        arrays[rv["name"]] = read_and_resample(
            reader, src, grid, tgt_lats, tgt_lons)
        meta[rv["name"]] = {"source_domain": domain, "source_var": src,
                            "derived": False}

    # 3) variabili richieste ma irrisolte -> warning
    for rv in risk_vars:
        if rv["name"] not in arrays:
            warnings.append("variabile '%s' assente nel dominio '%s' (%s): omessa"
                            % (rv["name"], domain, ",".join(rv["sources"])))

    return arrays, meta, warnings


# ---------------------------------------------------------------------------
# Scrittura frame + manifest
# ---------------------------------------------------------------------------
def write_frame_om(path, ordered, arrays, crs_wkt, coordinates,
                   valid_unix, ref_unix):
    """Scrive il `.om` multi-variabile (atomico). Ritorna (bytes, n_vars)."""
    tmp = path + ".tmp"
    w = OmFileWriter.at_path(tmp)
    children = []
    for name in ordered:
        if name not in arrays:
            continue
        arr = np.ascontiguousarray(np.asarray(arrays[name], dtype=np.float32))
        # I domini a griglia grossa producono crop piccoli (es. jma_gsm 25x22):
        # il chunk non puo' eccedere la dimensione dell'array, quindi lo si
        # clampa al minimo tra DEFAULT_CHUNKS e la shape effettiva.
        chunks = [min(DEFAULT_CHUNKS[0], int(arr.shape[0])),
                  min(DEFAULT_CHUNKS[1], int(arr.shape[1]))]
        # omfiles memorizza int = round(valore * scale_factor): per ottenere il
        # passo voluto (VARIABLE_STEP) si passa scale_factor = 1.0 / passo.
        c = w.write_array(arr, chunks=chunks,
                          scale_factor=float(1.0 / VARIABLE_STEP.get(name, DEFAULT_STEP)),
                          add_offset=0.0, compression=DEFAULT_COMPRESSION, name=name)
        children.append(c)
    children.append(w.write_scalar(str(crs_wkt), name="crs_wkt"))
    children.append(w.write_scalar(str(coordinates), name="coordinates"))
    children.append(w.write_scalar(int(valid_unix), name="valid_time"))
    children.append(w.write_scalar(int(ref_unix), name="forecast_reference_time"))
    root = w.write_group(name="", children=children)
    w.close(root)
    os.replace(tmp, path)
    return os.path.getsize(path), len(children) - 4


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def prune_frames(out_dir, keep_files, max_frames):
    """Rimuove i `.om` estranei alla finestra corrente (`keep_files`).

    `keep_files` e' l'insieme dei frame pubblicati in questo run (= le ancore
    `valid_times`). Qualsiasi altro `.om` rimasto da run precedenti (reference
    time piu' vecchi) viene rimosso: cosi' la cache del dominio contiene
    ESATTAMENTE la finestra corrente e non si cancella mai un frame appena
    scritto. Cap OPZIONALE di sicurezza: se `max_frames > 0` e `keep_files` lo
    supera, tiene i piu' recenti per nome; `max_frames <= 0` = NESSUN cap.
    """
    keep = set(keep_files)
    all_files = sorted([f for f in os.listdir(out_dir) if f.endswith(".om")])
    removed = []
    for f in all_files:
        if f in keep:
            continue
        try:
            os.remove(os.path.join(out_dir, f))
            removed.append(f)
        except OSError as exc:  # noqa: BLE001
            print("%s [WARN] prune fallito %s: %s" % (LOG, f, exc))
    kept = sorted(keep)
    if max_frames > 0 and len(kept) > max_frames:
        for f in kept[: len(kept) - max_frames]:
            if f in removed:
                continue
            try:
                os.remove(os.path.join(out_dir, f))
                removed.append(f)
            except OSError as exc:  # noqa: BLE001
                print("%s [WARN] prune-cap fallito %s: %s" % (LOG, f, exc))
    return removed


# ---------------------------------------------------------------------------
# Pianificazione / orchestrazione
# ---------------------------------------------------------------------------
def load_manifest(domain):
    data, err = http_read_json(s3_manifest_url(domain))
    if data is None:
        print("%s [WARN] manifest non disponibile per %s: %s" % (LOG, domain, err))
    return data


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def process_domain(domain, args, cache_dir, frames_cache, grid_cache, dry_run):
    """Elabora UN dominio in modo indipendente. Ritorna un dict di esito.

    Status: "ok" (prodotto o NO-OP), "skip" (CRS non supportato), "fail".
    In --dry-run non scarica nulla: la griglia usa il fallback GRIDS per nx/ny.
    """
    res = {"domain": domain, "status": "fail", "reason": None}

    m = load_manifest(domain)
    if not m:
        res["reason"] = "manifest assente"
        print("%s [FAIL] dominio '%s': manifest assente." % (LOG, domain))
        return res

    crs_wkt = m.get("crs_wkt", "") or ""
    ref_time = m.get("reference_time")
    # ORIZZONTE INTERO: si pubblicano TUTTI i valid_times del manifest, cosi'
    # come arrivano dalla sorgente (nessuna decimazione). Il vecchio THINNING
    # (entro 48h passo 1h, fino a 120h passo 3h, oltre passo 6h) e' stato
    # RIMOSSO in 1.2.4.9: la cadenza dei frame e' quella nativa del manifest.
    valid_times = sorted(set(m.get("valid_times", [])))

    res["reference_time"] = ref_time
    res["completed"] = m.get("completed")
    res["n_valid_times"] = len(valid_times)
    res["crs_kind"] = crs_kind(crs_wkt)

    # --- Riconoscimento CRS ---
    if not is_geographic_crs(crs_wkt):
        res["status"] = "skip"
        res["reason"] = "griglia proiettata non supportata (%s)" % crs_kind(crs_wkt)
        print("%s SKIP dominio '%s': griglia proiettata non supportata "
              "(PROJCRS/Lambert)" % (LOG, domain))
        return res

    if not ref_time or not valid_times:
        res["reason"] = "manifest privo di reference_time/valid_times"
        print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
        return res

    if args.valid_frames > 0:
        anchors = valid_times[: args.valid_frames]
    else:
        anchors = list(valid_times)
    res["anchors"] = anchors

    out_dir = os.path.join(os.path.abspath(args.out), domain)
    latest_path = os.path.join(out_dir, "latest.json")
    res["out_dir"] = out_dir
    res["latest_path"] = latest_path

    # --- DRY-RUN: pianifica SENZA scaricare (nx/ny da fallback GRIDS) ---
    if dry_run:
        known = GRIDS.get(domain)
        grid = None
        if known:
            grid = derive_grid(crs_wkt, known["nx"], known["ny"])
        res["grid"] = grid
        res["grid_source"] = "fallback(GRIDS)" if grid else "unknown"
        if grid:
            idx = bbox_to_indices(grid, bbox_for(domain))
            res["crop"] = idx["out_grid"]
        res["status"] = "ok"
        nx = grid["nx"] if grid else "?"
        ny = grid["ny"] if grid else "?"
        crop = res.get("crop")
        crop_s = ("%dx%d" % (crop["nx"], crop["ny"])) if crop else "?"
        print("%s [PLAN] dominio '%s': ref=%s griglia=%sx%s (%s) dx=%.6g dy=%.6g "
              "crop=%s frame=%d out=%s"
              % (LOG, domain, ref_time, nx, ny, res["grid_source"],
                 grid["dx"] if grid else float("nan"),
                 grid["dy"] if grid else float("nan"), crop_s, len(anchors),
                 os.path.join(out_dir, "<VALID>.om")))
        return res

    # --- NO-OP GUARD per-dominio (prima di scaricare i frame) ---
    if not args.force and os.path.isfile(latest_path):
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = None
        bb_cur = bbox_for(domain)
        old_bb = old.get("bbox") if old else None
        bb_same = (old_bb and abs(old_bb.get("lat_min",1e18)-bb_cur[0])<1e-6 and abs(old_bb.get("lon_min",1e18)-bb_cur[1])<1e-6 and abs(old_bb.get("lat_max",-1e18)-bb_cur[2])<1e-6 and abs(old_bb.get("lon_max",-1e18)-bb_cur[3])<1e-6)
        if (old and old.get("reference_time") == ref_time
                and list(old.get("valid_times", [])) == list(anchors)
                and bb_same):
            res["status"] = "ok"
            res["noop"] = True
            res["reason"] = "manifest invariato"
            print("%s NO-OP dominio '%s': manifest invariato "
                  "(reference_time/valid_times/bbox identici). Salto." % (LOG, domain))
            return res

    # --- Griglia derivata dal PRIMO frame (cache per-run) ---
    grid = grid_cache.get(domain)
    if grid is None:
        first = anchors[0]
        fpath, _ = fetch_frame(s3_url(domain, ref_time, first), domain,
                               valid_time_to_filename(first), cache_dir,
                               frames_cache, label="griglia")
        if fpath is None:
            res["reason"] = "download del primo frame (griglia) fallito"
            print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
            return res
        shape = frame_shape(fpath)
        if not shape:
            res["reason"] = "shape del primo frame non leggibile"
            print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
            return res
        ny, nx = shape
        grid = derive_grid(crs_wkt, nx, ny)
        if grid is None:
            res["reason"] = "crs_wkt privo di BBOX o dimensioni non valide"
            print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
            return res
        for w in grid_guard(domain, grid):
            print("%s [WARN] dominio '%s': %s" % (LOG, domain, w))
        grid_cache[domain] = grid

    res["grid"] = grid
    res["grid_source"] = "crs_wkt+frame"
    idx = bbox_to_indices(grid, bbox_for(domain))
    tgt_lats, tgt_lons = target_latlon(grid, idx)
    out_grid = idx["out_grid"]
    res["crop"] = out_grid

    print("%s dominio '%s' ref=%s griglia nx=%d ny=%d dx=%.6g dy=%.6g -> "
          "crop %dx%d" % (LOG, domain, ref_time, grid["nx"], grid["ny"],
                          grid["dx"], grid["dy"], out_grid["nx"], out_grid["ny"]))

    os.makedirs(out_dir, exist_ok=True)

    ordered_names = [rv["name"] for rv in V2_RISK_VARS]
    frames = []
    variables_map = {}
    sizes_map = {}
    all_warnings = []

    for vi, anchor in enumerate(anchors):
        print("%s   frame %d/%d valid_time=%s" % (LOG, vi + 1, len(anchors), anchor))
        fpath, _ = fetch_frame(s3_url(domain, ref_time, anchor), domain,
                               valid_time_to_filename(anchor), cache_dir,
                               frames_cache, label="frame")
        if fpath is None:
            print("%s [WARN] salto frame %s (download fallito)." % (LOG, anchor))
            all_warnings.append("frame %s: download fallito" % anchor)
            continue

        reader = OmFileReader.from_path(fpath)
        try:
            crs_frame = ""
            coordinates = "lat lon"
            try:
                crs_frame = reader.get_child_by_name("crs_wkt").read_scalar()
            except Exception:  # noqa: BLE001
                crs_frame = crs_wkt
            if not crs_frame:
                crs_frame = crs_wkt

            arrays, vmeta, warns = resolve_domain_frame(
                reader, grid, V2_RISK_VARS, tgt_lats, tgt_lons, idx, domain)

            vt_dt = parse_iso(anchor)
            ref_dt = parse_iso(ref_time)
            fname = valid_time_to_filename(anchor) + ".om"
            fpath_out = os.path.join(out_dir, fname)
            size, nvars = write_frame_om(
                fpath_out, ordered_names, arrays, crs_frame, coordinates,
                int(vt_dt.timestamp()), int(ref_dt.timestamp()))
            frames.append({"valid_time": anchor, "file": fname, "bytes": size,
                           "variables": nvars})
            variables_map[anchor] = vmeta
            sizes_map[anchor] = size
            all_warnings.extend(["%s: %s" % (anchor, w) for w in warns])
            print("%s   scritto %s (%d byte, %d var)" % (LOG, fname, size, nvars))
        finally:
            reader.close()

    if not frames:
        res["reason"] = "nessun frame scritto (tutti i download falliti?)"
        print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
        return res

    removed = []
    if not args.no_prune:
        keep_files = [valid_time_to_filename(a) + ".om" for a in anchors]
        removed = prune_frames(out_dir, keep_files, args.valid_frames)

    bb = bbox_for(domain)
    manifest = {
        "version": "fm2-1",
        "generated_at": _now_iso(),
        "domain": domain,
        "reference_time": ref_time,
        "valid_times": anchors,
        "bbox": {"lat_min": bb[0], "lon_min": bb[1],
                 "lat_max": bb[2], "lon_max": bb[3]},
        "grid": out_grid,
        "source_grid": {k: grid[k] for k in ("nx", "ny", "lonMin", "latMin", "dx", "dy")},
        "grid_source": "crs_wkt+frame",
        "var_order": ordered_names,
        "resampling": "per-domain nearest (per-domain crop)",
        "license": "Open-Meteo (CC BY 4.0) — attribuzione richiesta",
        "frames": frames,
        "variables": variables_map,
        "sizes": sizes_map,
        "pruned": removed,
        "warnings": all_warnings,
    }
    write_json_atomic(latest_path, manifest)

    total_bytes = sum(f["bytes"] for f in frames)
    print("%s [OK] dominio '%s': %d frame, %d byte totali (%.1f KiB)."
          % (LOG, domain, len(frames), total_bytes, total_bytes / 1024.0))
    if removed:
        print("%s   prune: rimossi %d frame vecchi." % (LOG, len(removed)))
    uniq_warn = sorted(set(all_warnings))
    if uniq_warn:
        print("%s   warning (%d):" % (LOG, len(uniq_warn)))
        for w in uniq_warn:
            print("%s     - %s" % (LOG, w))

    res["status"] = "ok"
    res["n_frames"] = len(frames)
    res["bytes"] = total_bytes
    return res


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(
        description="MeteoRisk Forecast V2 — cache GitHub multi-dominio di mappe .om")
    ap.add_argument("--domains", nargs="+", default=list(V2_DOMAINS),
                    help="Domini da elaborare, ciascuno INDIPENDENTE (nessun ordine "
                         "di priorita'; ognuno produce data/fm2/<dominio>/).")
    ap.add_argument("--valid-frames", type=int, default=V2_MAX_FRAMES,
                    help="Numero di frame (valid_times) da pubblicare a partire dal "
                         "primo, PER OGNI dominio. 0 (default) o negativo = INTERO "
                         "orizzonte del manifest del dominio (tutti i valid_times).")
    ap.add_argument("--out", default=V2_OUT,
                    help="Directory di output radice (default %s); la cache di ogni "
                         "dominio va in <out>/<dominio>/." % V2_OUT)
    ap.add_argument("--once", action="store_true",
                    help="Esegue un solo ciclo ed esce (default/compat).")
    ap.add_argument("--no-prune", action="store_true", help="Non rimuove i frame oltre il limite.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stampa il piano di TUTTI i domini senza scaricare/scrivere.")
    ap.add_argument("--force", action="store_true", help="Ignora il NO-OP guard (riscrive sempre).")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory cache dei download (default: temp di sistema).")
    ap.add_argument("--frames-cache", default="",
                    help="Cache PERSISTENTE dei frame .om (<dir>/<dominio>/<VALID>.om), "
                         "riusata se la Content-Length remota combacia (default: vuoto = "
                         "disabilitata, comportamento invariato).")
    args = ap.parse_args(argv)

    if not args.domains:
        print("%s ERRORE: --domains vuoto" % LOG)
        return 1

    print("%s multi-dominio: %d domini -> %s"
          % (LOG, len(args.domains), ", ".join(args.domains)))
    print("%s valid-frames=%d (%s) out=%s"
          % (LOG, args.valid_frames,
             "INTERO orizzonte" if args.valid_frames <= 0 else "primi N",
             os.path.abspath(args.out)))

    # ---- cache download / frames-cache (non in dry-run: nessuna directory) ----
    cache_dir = args.cache_dir or os.path.join(tempfile.gettempdir(), "mri_fm2_cache")
    frames_cache = os.path.abspath(args.frames_cache) if args.frames_cache else ""
    if not args.dry_run:
        os.makedirs(cache_dir, exist_ok=True)
        if frames_cache:
            os.makedirs(frames_cache, exist_ok=True)
            print("%s frames-cache attiva: %s" % (LOG, frames_cache))
        os.makedirs(os.path.abspath(args.out), exist_ok=True)

    grid_cache = {}
    results = []
    for d in args.domains:
        results.append(process_domain(d, args, cache_dir, frames_cache, grid_cache,
                                      args.dry_run))

    ok = [r for r in results if r["status"] == "ok"]
    skip = [r for r in results if r["status"] == "skip"]
    fail = [r for r in results if r["status"] == "fail"]

    if args.dry_run:
        plan = {
            "dry_run": True,
            "out": os.path.abspath(args.out),
            "valid_frames": args.valid_frames,
            "all_horizon": args.valid_frames <= 0,
            "max_frames_cap": V2_MAX_FRAMES,
            "prune": not args.no_prune,
            "domains": [
                {
                    "domain": r["domain"],
                    "status": r["status"],
                    "reason": r["reason"],
                    "reference_time": r.get("reference_time"),
                    "completed": r.get("completed"),
                    "n_valid_times": r.get("n_valid_times"),
                    "n_frames": len(r.get("anchors", [])) if r["status"] == "ok" else 0,
                    "crs_kind": r.get("crs_kind"),
                    "grid_source": r.get("grid_source"),
                    "source_grid": r.get("grid"),
                    "crop": r.get("crop"),
                    "out_dir": r.get("out_dir"),
                    "frames_glob": (os.path.join(r["out_dir"], "<VALID>.om")
                                    if r.get("out_dir") else None),
                    "latest_json": r.get("latest_path"),
                }
                for r in results
            ],
        }
        print("%s DRY-RUN plan:\n%s"
              % (LOG, json.dumps(plan, indent=2, ensure_ascii=False)))

    print("%s ===== SUMMARY =====" % LOG)
    print("%s ok   (%d): %s" % (LOG, len(ok), ", ".join(r["domain"] for r in ok) or "-"))
    print("%s skip (%d): %s" % (LOG, len(skip), ", ".join(
        "%s [%s]" % (r["domain"], r["reason"]) for r in skip) or "-"))
    print("%s fail (%d): %s" % (LOG, len(fail), ", ".join(
        "%s [%s]" % (r["domain"], r["reason"]) for r in fail) or "-"))

    # Exit: 0 se almeno un dominio e' ok (anche solo NO-OP); 1 se TUTTI falliscono.
    if not ok:
        print("%s ERRORE: nessun dominio ok (tutti falliti)." % LOG)
        return 1
    print("%s FATTO: %d/%d domini ok." % (LOG, len(ok), len(results)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
