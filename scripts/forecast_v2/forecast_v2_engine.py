#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
forecast_v2_engine.py — MeteoRisk "Mappe previsionali V2" (ENGINE-ONLY, server).

Motore che costruisce in GitHub Actions una CACHE GITHUB di mappe previsionali:
per ogni istante di previsione produce UN file `.om` multi-variabile croppato
sull'Italia, leggibile dal reader WML 0.2.2 (stesso formato del POC
`spike_omfiles/`, verificato bit-exact).

    python scripts/forecast_v2/forecast_v2_engine.py \
        [--domains dwd_icon_eu ecmwf_ifs025] [--valid-frames 25] \
        [--out data/fm2] [--once] [--no-prune] [--dry-run] [--force] \
        [--frames-cache .cache_fm2_frames]

Dati sorgente (Open-Meteo "spatial" files, accesso S3 anonimo):
    https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/<YYYY>/<MM>/<DD>/<HH00Z>/<YYYY-MM-DDTHH00.om
    https://openmeteo.s3.amazonaws.com/data_spatial/<dominio>/latest.json   (manifest)

Disegno (V1 del motore, scelte DICHIARATE):
  - GRIGLIA UNICA: il crop e' calcolato sulla griglia del dominio PRIMARIO
    (V2_DOMAINS[0] = dwd_icon_eu). Tutte le variabili pubblicate condividono la
    stessa scatola (grid del crop nel manifest), cosi' il client legge con un
    solo set di range.
  - MULTI-DOMINIO: per ogni variabile si prende il PRIMO dominio (in ordine
    V2_DOMAINS) che la contiene *nel file di quell'istante*. Le variabili non
    presenti nel primario vengono cercate nel secondario e RI-CAMPIONATE sulla
    griglia del primario con nearest-neighbor sui centri-cella (nessuna
    interpolazione: solo nearest, documentato). Questo e' esatto per celle
    allineate e approssimato altrove (il secondario ha risoluzione diversa).
  - Il manifest `latest.json` di un dominio elenca un SUPERSET delle variabili:
    alcune (accumuli/flussi, es. precipitation/rain/showers/weather_code/
    wind_gusts_10m) NON esistono nel frame di analisi (valid_time ==
    reference_time) e compaiono solo ai passi successivi -> la presenza reale
    viene rilevata PER-FILE dai child del `.om` scaricato, non dal manifest.
  - DERIVATE lato server quando manca la sorgente diretta:
        wind_speed_10m      = sqrt(u^2 + v^2)
        wind_direction_10m  = (atan2(-u, -v) in gradi, 0..360)  [direzione "da"]
        dew_point_2m        = Magnus(T2m, RH2m)
    (surface_pressure non e' derivabile senza quota: se assente in tutti i
    domini viene OMESSA con warning.)
  - NO-OP GUARD: se il manifest esistente ha reference_time e valid_times
    identici ai nuovi (e non --force), esce 0 SENZA scaricare/riscrivere nulla
    (evita ~144 commit/giorno inutili, come phenomena-verify).
  - PRUNING: in `--out` restano solo gli ultimi V2_MAX_FRAMES `.om`; i piu'
    vecchi vengono rimossi. Gli altri fanno parte della cache servita da Pages.
  - FRAMES-CACHE (--frames-cache): cache PERSISTENTE dei frame `.om` full-size
    (ripristinata in CI via actions/cache), layout `<dir>/<dominio>/<VALID>.om`.
    Un frame in cache viene riusato SENZA download se la sua Content-Length
    combacia con quella remota (HEAD leggero; fallback: size locale). Riduce
    l'egress CI riusando i frame gia' scaricati tra run consecutive.
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

# Dominio primario PRIMO (definisce la griglia del crop), poi i secondari.
V2_DOMAINS = ["dwd_icon_eu", "ecmwf_ifs025"]

# Lista ORDINATA delle variabili di rischio desiderate, con sinonimi/forme.
# name = nome pubblicato nel `.om`; sources = nomi candidati nel file sorgente
# (il primo presente vince). La presenza reale e' determinata per-file; le
# variabili derivate sono calcolate solo se la sorgente diretta manca.
V2_RISK_VARS = [
    {"name": "temperature_2m",       "sources": ["temperature_2m"]},
    {"name": "temperature_850hPa",   "sources": ["temperature_850hPa"]},      # FM_CACHE core
    {"name": "temperature_500hPa",   "sources": ["temperature_500hPa"]},      # FM_CACHE core
    {"name": "relative_humidity_2m", "sources": ["relative_humidity_2m"]},
    {"name": "relative_humidity_850hPa", "sources": ["relative_humidity_850hPa"]},  # FM_CACHE core
    {"name": "dew_point_2m",         "sources": ["dew_point_2m"]},            # deriva da T+RH
    {"name": "pressure_msl",         "sources": ["pressure_msl", "msl_pressure"]},
    {"name": "geopotential_height_500hPa", "sources": ["geopotential_height_500hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_850hPa", "sources": ["geopotential_height_850hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_1000hPa", "sources": ["geopotential_height_1000hPa"]},  # FM_CACHE core
    {"name": "surface_pressure",     "sources": ["surface_pressure"]},        # spesso assente
    {"name": "wind_speed_10m",       "sources": ["wind_speed_10m"]},          # deriva da u,v
    {"name": "wind_direction_10m",   "sources": ["wind_direction_10m"]},      # deriva da u,v
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

V2_MAX_FRAMES = 25
V2_OUT = "data/fm2"

# Tolleranza (ore) per agganciare il valid_time del secondario all'ancora.
V2_SECONDARY_MAX_DT_HOURS = 3

# Fattori di scala usati SOLO per le variabili derivate (le altre ereditano il
# scale_factor originale del file sorgente). Convenzione Open-Meteo:
#  - velocita' vento: 0.1 m/s -> scale 10
#  - direzione vento: 1 grado -> scale 1
#  - dew point: 0.05 degC -> scale 20 (come temperature_2m)
DERIVED_SCALE = {
    "wind_speed_10m": 10.0,
    "wind_direction_10m": 1.0,
    "dew_point_2m": 20.0,
}
DEFAULT_SCALE = 1.0
DEFAULT_CHUNKS = [32, 32]
DEFAULT_COMPRESSION = "pfor_delta_2d_int16"

# Griglie (RegularGridFromOrigin): origine a cella (lonMin/latMin) + passo.
GRIDS = {
    "dwd_icon_eu": {"nx": 1377, "ny": 657, "lonMin": -23.5, "latMin": 29.5,
                    "dx": 0.0625, "dy": 0.0625},
    "ecmwf_ifs025": {"nx": 1440, "ny": 721, "lonMin": -180.0, "latMin": -90.0,
                     "dx": 0.25, "dy": 0.25},
}


# ---------------------------------------------------------------------------
# Utility I/O di rete
# ---------------------------------------------------------------------------
def _urlopen(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "MeteoRisk-Light/1.2.4.2"})
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
            headers={"User-Agent": "MeteoRisk-Light/1.2.4.2"})
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
# Griglie e crop
# ---------------------------------------------------------------------------
def grid_for(domain):
    """Ritorna la griglia (dict) del dominio; KeyError se ignoto."""
    if domain not in GRIDS:
        raise KeyError("grid sconosciuta per dominio '%s'" % domain)
    return dict(GRIDS[domain])


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

    Per la griglia del primario il risultato e' il crop esatto (nearest = identita'
    perche' le due griglie coincidono e i centri sono allineati). Per griglie
    diverse e' un nearest-neighbor sui centri-cella del target.
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


def child_meta(reader, name):
    c = reader.get_child_by_name(name)
    if c is None:
        return None
    sf = c.scale_factor if c.scale_factor else DEFAULT_SCALE
    off = c.add_offset if c.add_offset is not None else 0.0
    return {"scale": float(sf), "offset": float(off)}


def array_child_names(reader):
    names = []
    for i in range(reader.num_children):
        c = reader.get_child_by_index(i)
        if not c.is_scalar:
            names.append(c.name)
    return set(names)


# ---------------------------------------------------------------------------
# Derivate
# ---------------------------------------------------------------------------
def derive_wind_speed(u, v):
    return np.sqrt(u.astype(np.float32) ** 2 + v.astype(np.float32) ** 2).astype(np.float32)


def derive_wind_direction(u, v):
    # Direzione METEOROLOGICA "da cui" soffia il vento (0=N, 90=E), 0..360.
    ang = np.degrees(np.arctan2(-u.astype(np.float64), -v.astype(np.float64)))
    return ((ang + 360.0) % 360.0).astype(np.float32)


def derive_dew_point(t2m, rh):
    # Magnus: gamma = ln(RH/100) + a*T/(b+T); Td = b*gamma/(a-gamma).
    a, b = 17.62, 243.12
    rh = np.clip(rh.astype(np.float64), 1e-3, 100.0)
    t = t2m.astype(np.float64)
    gamma = np.log(rh / 100.0) + (a * t) / (b + t)
    td = (b * gamma) / (a - gamma)
    return td.astype(np.float32)


# ---------------------------------------------------------------------------
# Risoluzione variabili per frame
# ---------------------------------------------------------------------------
def resolve_direct(risk, child_names):
    """Primo candidato presente nei child; ritorna nome sorgente o None."""
    for cand in risk["sources"]:
        if cand in child_names:
            return cand
    return None


def resolve_frame(primary_reader, primary_grid, secondary_reader, secondary_grid,
                  risk_vars, tgt_lats, tgt_lons, idx):
    """Costruisce gli array di output del frame + metadati di provenienza.

    Ritorna (arrays, variables_meta, warnings). `arrays` e' dict
    nome_pubblicato -> np.float32 crop.
    """
    arrays = {}
    meta = {}
    warnings = []

    primary_names = array_child_names(primary_reader)
    primary_crop = {}

    # 1) risoluzione diretta dal primario
    for rv in risk_vars:
        src = resolve_direct(rv, primary_names)
        if src is None:
            continue
        primary_crop[rv["name"]] = read_and_resample(
            primary_reader, src, primary_grid, tgt_lats, tgt_lons)
        meta[rv["name"]] = {"source_domain": "primary", "source_var": src, "derived": False}

    # 2) risoluzione dal secondario per le variabili dirette mancanti
    if secondary_reader is not None:
        sec_names = array_child_names(secondary_reader)
        for rv in risk_vars:
            if rv["name"] in meta:
                continue
            src = resolve_direct(rv, sec_names)
            if src is None:
                continue
            arrays[rv["name"]] = read_and_resample(
                secondary_reader, src, secondary_grid, tgt_lats, tgt_lons)
            meta[rv["name"]] = {"source_domain": "secondary",
                                "source_var": src, "derived": False}

    arrays.update(primary_crop)

    # 3) derivate: vento
    u = arrays.get("wind_u_component_10m")
    v = arrays.get("wind_v_component_10m")
    if u is not None and v is not None:
        if "wind_speed_10m" not in arrays:
            arrays["wind_speed_10m"] = derive_wind_speed(u, v)
            meta["wind_speed_10m"] = {"source_domain": "derived",
                                      "source_var": "wind_u_component_10m+wind_v_component_10m",
                                      "derived": True}
        if "wind_direction_10m" not in arrays:
            arrays["wind_direction_10m"] = derive_wind_direction(u, v)
            meta["wind_direction_10m"] = {"source_domain": "derived",
                                          "source_var": "wind_u_component_10m+wind_v_component_10m",
                                          "derived": True}

    # 3b) derivate: dew point
    t2m = arrays.get("temperature_2m")
    rh = arrays.get("relative_humidity_2m")
    if "dew_point_2m" not in arrays and t2m is not None and rh is not None:
        arrays["dew_point_2m"] = derive_dew_point(t2m, rh)
        meta["dew_point_2m"] = {"source_domain": "derived",
                                "source_var": "temperature_2m+relative_humidity_2m",
                                "derived": True}

    # 4) variabili richieste ma irrisolte -> warning
    for rv in risk_vars:
        if rv["name"] not in arrays:
            warnings.append("variabile '%s' assente in tutti i domini (%s): omessa"
                            % (rv["name"], ",".join(rv["sources"])))

    return arrays, meta, warnings


# ---------------------------------------------------------------------------
# Scrittura frame + manifest
# ---------------------------------------------------------------------------
def write_frame_om(path, ordered, arrays, scales, crs_wkt, coordinates,
                   valid_unix, ref_unix):
    """Scrive il `.om` multi-variabile (atomico). Ritorna (bytes, n_vars)."""
    tmp = path + ".tmp"
    w = OmFileWriter.at_path(tmp)
    children = []
    for name in ordered:
        if name not in arrays:
            continue
        arr = np.ascontiguousarray(np.asarray(arrays[name], dtype=np.float32))
        c = w.write_array(arr, chunks=DEFAULT_CHUNKS,
                          scale_factor=float(scales.get(name, DEFAULT_SCALE)),
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
    time piu' vecchi) viene rimosso: cosi' la cache contiene ESATTAMENTE la
    finestra corrente e non si cancella mai un frame appena scritto (che sarebbe
    possibile con un semplice "tieni i max_frames nomi piu' grandi", dato che le
    ancore sono i valid_times PIU' PROSSIMI al reference, non i piu' lontani).
    Cap di sicurezza: se `keep_files` supera `max_frames`, tiene i piu' recenti
    per nome.
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
    if len(kept) > max_frames:
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
# Pianificazione
# ---------------------------------------------------------------------------
def load_manifest(domain):
    data, err = http_read_json(s3_manifest_url(domain))
    if data is None:
        print("%s [WARN] manifest non disponibile per %s: %s" % (LOG, domain, err))
    return data


def pick_secondary_valid(sec_valid_times, anchor_iso):
    """valid_time del secondario piu' vicino all'ancora entro tolleranza."""
    anchor = parse_iso(anchor_iso)
    best, best_dt = None, None
    for sv in sec_valid_times:
        d = abs((parse_iso(sv) - anchor).total_seconds())
        if best_dt is None or d < best_dt:
            best, best_dt = sv, d
    if best is None or best_dt > V2_SECONDARY_MAX_DT_HOURS * 3600:
        return None
    return best


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="MeteoRisk Forecast V2 — cache GitHub di mappe .om")
    ap.add_argument("--domains", nargs="+", default=list(V2_DOMAINS),
                    help="Domini in ordine di priorita' (il primo definisce la griglia del crop).")
    ap.add_argument("--valid-frames", type=int, default=V2_MAX_FRAMES,
                    help="Numero di frame (valid_times) da pubblicare (default %d)." % V2_MAX_FRAMES)
    ap.add_argument("--out", default=V2_OUT, help="Directory di output (default %s)." % V2_OUT)
    ap.add_argument("--once", action="store_true",
                    help="Esegue un solo ciclo ed esce (default/compat).")
    ap.add_argument("--no-prune", action="store_true", help="Non rimuove i frame oltre il limite.")
    ap.add_argument("--dry-run", action="store_true", help="Stampa il piano senza scaricare/scrivere.")
    ap.add_argument("--force", action="store_true", help="Ignora il NO-OP guard (riscrive sempre).")
    ap.add_argument("--cache-dir", default=None,
                    help="Directory cache dei download (default: temp di sistema).")
    ap.add_argument("--frames-cache", default="",
                    help="Cache PERSISTENTE dei frame .om (<dir>/<dominio>/<VALID>.om), "
                         "riusata se la Content-Length remota combacia (default: vuoto = "
                         "disabilitata, comportamento invariato).")
    args = ap.parse_args(argv)

    if args.valid_frames < 1:
        print("%s ERRORE: --valid-frames deve essere >= 1" % LOG)
        return 1
    if not args.domains:
        print("%s ERRORE: --domains vuoto" % LOG)
        return 1

    primary = args.domains[0]
    if primary not in GRIDS:
        print("%s ERRORE: dominio primario '%s' senza griglia nota." % (LOG, primary))
        return 1
    for d in args.domains[1:]:
        if d not in GRIDS:
            print("%s ERRORE: dominio '%s' senza griglia nota." % (LOG, d))
            return 1

    primary_grid = grid_for(primary)
    idx = bbox_to_indices(primary_grid, V2_BBOX)
    tgt_lats, tgt_lons = target_latlon(primary_grid, idx)
    out_grid = idx["out_grid"]

    print("%s bbox=%s -> crop %dx%d su %s (lonMin=%.4f latMin=%.4f dx=dy=%.4f)"
          % (LOG, V2_BBOX, out_grid["nx"], out_grid["ny"], primary,
             out_grid["lonMin"], out_grid["latMin"], out_grid["dx"]))

    # manifest dei domini
    manifests = {}
    for d in args.domains:
        m = load_manifest(d)
        if m:
            manifests[d] = m

    if primary not in manifests:
        print("%s ERRORE: manifest del primario '%s' non disponibile." % (LOG, primary))
        return 1

    pm = manifests[primary]
    ref_time = pm.get("reference_time")
    valid_times = sorted(set(pm.get("valid_times", [])))
    if not ref_time or not valid_times:
        print("%s ERRORE: manifest primario privo di reference_time/valid_times." % LOG)
        return 1

    anchors = valid_times[: args.valid_frames]  # i frame piu' prossimi all'ancora temporale
    print("%s reference_time=%s valid_times=%d -> ancore=%d (%s .. %s)"
          % (LOG, ref_time, len(valid_times), len(anchors), anchors[0], anchors[-1]))

    out_dir = os.path.abspath(args.out)
    latest_path = os.path.join(out_dir, "latest.json")

    # ---- NO-OP GUARD (prima di scaricare i frame) ----
    if not args.force and os.path.isfile(latest_path):
        try:
            with open(latest_path, "r", encoding="utf-8") as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = None
        if (old and old.get("reference_time") == ref_time
                and list(old.get("valid_times", [])) == list(anchors)):
            print("%s NO-OP: manifest invariato (reference_time/valid_times identici). Esco 0."
                  % LOG)
            return 0

    if args.dry_run:
        plan = {
            "dry_run": True,
            "primary": primary,
            "domains": args.domains,
            "out": out_dir,
            "reference_time": ref_time,
            "grid": out_grid,
            "anchors": anchors,
            "risk_vars": [rv["name"] for rv in V2_RISK_VARS],
            "max_frames": args.valid_frames,
            "prune": not args.no_prune,
            "manifest_present": os.path.isfile(latest_path),
        }
        print("%s DRY-RUN plan:\n%s" % (LOG, json.dumps(plan, indent=2, ensure_ascii=False)))
        return 0

    # ---- cache download ----
    cache_dir = args.cache_dir or os.path.join(tempfile.gettempdir(), "mri_fm2_cache")
    os.makedirs(cache_dir, exist_ok=True)
    frames_cache = os.path.abspath(args.frames_cache) if args.frames_cache else ""
    if frames_cache:
        os.makedirs(frames_cache, exist_ok=True)
        print("%s frames-cache attiva: %s" % (LOG, frames_cache))
    os.makedirs(out_dir, exist_ok=True)

    sec_domain = args.domains[1] if len(args.domains) > 1 else None
    sec_valid_times = sorted(set(manifests[sec_domain].get("valid_times", []))) if sec_domain else []
    sec_ref = manifests[sec_domain].get("reference_time") if sec_domain else None

    ordered_names = [rv["name"] for rv in V2_RISK_VARS]
    frames = []
    variables_map = {}
    sizes_map = {}
    all_warnings = []
    written_files = []

    for vi, anchor in enumerate(anchors):
        print("%s frame %d/%d valid_time=%s" % (LOG, vi + 1, len(anchors), anchor))
        # primario
        p_url = s3_url(primary, ref_time, anchor)
        p_dst, _ = fetch_frame(p_url, primary, valid_time_to_filename(anchor),
                               cache_dir, frames_cache, label="primario")
        if p_dst is None:
            print("%s [WARN] salto frame %s (download primario fallito)." % (LOG, anchor))
            all_warnings.append("frame %s: download primario fallito" % anchor)
            continue

        primary_reader = OmFileReader.from_path(p_dst)
        try:
            crs_wkt = ""
            coordinates = "lat lon"
            try:
                crs_wkt = primary_reader.get_child_by_name("crs_wkt").read_scalar()
            except Exception:  # noqa: BLE001
                pass

            # secondario (solo se qualche variabile diretta manca nel primario)
            secondary_reader = None
            secondary_grid = None
            sec_anchor = None
            if sec_domain:
                p_names = array_child_names(primary_reader)
                missing_direct = []
                for rv in V2_RISK_VARS:
                    if rv["name"] in ("wind_speed_10m", "wind_direction_10m", "dew_point_2m"):
                        continue  # derivabili
                    if resolve_direct(rv, p_names) is None:
                        missing_direct.append(rv)
                sec_manifest_vars = set(manifests[sec_domain].get("variables", []))
                wanted = [rv for rv in missing_direct
                          if any(c in sec_manifest_vars for c in rv["sources"])]
                if wanted:
                    sec_anchor = pick_secondary_valid(sec_valid_times, anchor)
                    if sec_anchor:
                        s_url = s3_url(sec_domain, sec_ref, sec_anchor)
                        s_dst, _ = fetch_frame(
                            s_url, sec_domain, valid_time_to_filename(sec_anchor),
                            cache_dir, frames_cache, label="secondario")
                        if s_dst is not None:
                            secondary_reader = OmFileReader.from_path(s_dst)
                            secondary_grid = grid_for(sec_domain)

            arrays, vmeta, warns = resolve_frame(
                primary_reader, primary_grid, secondary_reader, secondary_grid,
                V2_RISK_VARS, tgt_lats, tgt_lons, idx)

            # rinomina i provenance generici in nome dominio reale
            for k, m in vmeta.items():
                if m["source_domain"] == "primary":
                    m["source_domain"] = primary
                elif m["source_domain"] == "secondary":
                    m["source_domain"] = sec_domain

            # scale factor: originale per i passthrough, default per le derivate
            scales = {}
            for name, m in vmeta.items():
                if m["derived"]:
                    scales[name] = DERIVED_SCALE.get(name, DEFAULT_SCALE)
                else:
                    dom_key = m["source_domain"]
                    rd = primary_reader if dom_key == primary else secondary_reader
                    cm = child_meta(rd, m["source_var"]) if rd is not None else None
                    scales[name] = cm["scale"] if cm else DEFAULT_SCALE

            vt_dt = parse_iso(anchor)
            ref_dt = parse_iso(ref_time)
            fname = valid_time_to_filename(anchor) + ".om"
            fpath = os.path.join(out_dir, fname)
            size, nvars = write_frame_om(
                fpath, ordered_names, arrays, scales, crs_wkt, coordinates,
                int(vt_dt.timestamp()), int(ref_dt.timestamp()))
            written_files.append(fname)
            frames.append({"valid_time": anchor, "file": fname, "bytes": size,
                           "variables": nvars,
                           "secondary_valid_time": sec_anchor})
            variables_map[anchor] = vmeta
            sizes_map[anchor] = size
            all_warnings.extend(["%s: %s" % (anchor, w) for w in warns])
            print("%s   scritto %s (%d byte, %d var)" % (LOG, fname, size, nvars))

            if secondary_reader is not None:
                secondary_reader.close()
        finally:
            primary_reader.close()

    if not frames:
        print("%s ERRORE: nessun frame scritto (tutti i download falliti?)." % LOG)
        return 1

    removed = []
    if not args.no_prune:
        keep_files = [valid_time_to_filename(a) + ".om" for a in anchors]
        removed = prune_frames(out_dir, keep_files, args.valid_frames)

    manifest = {
        "version": "fm2-1",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "reference_time": ref_time,
        "valid_times": anchors,
        "domains": args.domains,
        "primary_domain": primary,
        "bbox": {"lat_min": V2_BBOX[0], "lon_min": V2_BBOX[1],
                 "lat_max": V2_BBOX[2], "lon_max": V2_BBOX[3]},
        "grid": out_grid,
        "var_order": ordered_names,
        "resampling": "nearest (centri-cella) dal secondario alla griglia del primario",
        "license": "Open-Meteo (CC BY 4.0) — attribuzione richiesta",
        "frames": frames,
        "variables": variables_map,
        "sizes": sizes_map,
        "pruned": removed,
        "warnings": all_warnings,
    }
    write_json_atomic(latest_path, manifest)

    total_bytes = sum(f["bytes"] for f in frames)
    print("%s FATTO: %d frame scritti, %d byte totali (%.1f KiB)."
          % (LOG, len(frames), total_bytes, total_bytes / 1024.0))
    if removed:
        print("%s prune: rimossi %d frame vecchi (%s)" % (LOG, len(removed), ", ".join(removed)))
    uniq_warn = sorted(set(all_warnings))
    if uniq_warn:
        print("%s warning (%d):" % (LOG, len(uniq_warn)))
        for w in uniq_warn:
            print("%s   - %s" % (LOG, w))
    return 0


if __name__ == "__main__":
    sys.exit(main())
