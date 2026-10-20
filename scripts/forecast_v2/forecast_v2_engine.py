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
        [--frames-cache .cache_fm2_frames] [--no-range]

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
  - VARIABILI: lista UNICA `V2_RISK_VARS` (31: 23 dirette + 8 derivate) per
    tutti i domini. Nessun set per-dominio: la presenza reale e' decisa
    FILE-PER-FILE dai child del `.om`. Le variabili assenti nella sorgente sono
    OMESSE con warning (es. `surface_pressure`, spesso non disponibile). Le
    DERIVATE (dew_point_2m, wind_speed_10m/850hPa/300hPa,
    wind_direction_10m/850hPa/300hPa, precipitation_cum) sono CALCOLATE dal
    motore (registro `DERIVED_VARS`); `V2_INPUT_VARS` elenca le variabili di
    INPUT (componenti u/v a 10 m, 850 e 300 hPa) lette ma NON pubblicate.
    Il vento si pubblica come COPPIA velocita'+direzione: `wind_direction_*`
    e' la secondaria pretesa dalla libreria client per ogni `wind_speed_*`
    (le u/v non sono piu' pubblicate).
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
  - LETTURA SELETTIVA (default): il frame sorgente NON viene scaricato per
    intero. Aprendo il `.om` remoto via fsspec (HTTP Range) si leggono SOLO le
    variabili di `V2_READ_VARS` (pubblicate + input) presenti (risoluzione nomi,
    crop e `scale_factor` INVARIATI) + `crs_wkt` e la shape (header, letture
    piccole).
    I frame sorgente contengono molte piu' variabili di quelle pubblicate
    (es. `ncep_gfs025`: ~315 array -> ~10 pubblicate). Ordine I/O per frame:
    frame in frames-cache -> lettura a intervalli -> DOWNLOAD COMPLETO come
    FALLBACK ESPLICITO. Ogni fallback emette `[WARN]` (dominio/frame/motivo) e
    incrementa `download.fallback_count` nel manifest: mai silenzioso.
    `--no-range` disabilita del tutto la lettura a intervalli.
  - EXIT: se TUTTI i domini falliscono -> exit 1. Se almeno UN dominio e' `ok`
    (anche solo NO-OP) -> exit 0, con summary esplicito `ok`/`skip`/`fail` (il
    workflow deve poter committare la cache dei domini riusciti).
  - Scrittura ATOMICA (.tmp -> os.replace) e nessun segreto: l'accesso S3 e'
    anonimo. Dipendenze: `omfiles`, `numpy`, `Pillow` (mappe) e `fsspec` (+ il
    backend HTTP `aiohttp`) per la lettura a intervalli; se fsspec manca si usa
    il download completo (fallback esplicito).

LICENZE / ATTRIBUZIONE:
  - Dati meteo: Open-Meteo (https://open-meteo.com), licenza CC BY 4.0 ->
    attribuzione a Open-Meteo richiesta in ogni ridistribuzione.
  - `omfiles` (GPL-2.0-only) e' usato SOLO a runtime/CI per leggere e scrivere
    il formato `.om`; NON viene venduto ne' incluso nel repository (dipendenza
    pip dichiarata in requirements.txt).
"""

import argparse
import asyncio
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

# Reader ASINCRONO (lettura a intervalli). Opzionale: se l'omfiles installato
# non lo espone, la lettura selettiva non e' disponibile e si usa il download
# completo (fallback esplicito, vedi `read_frame_selective`).
try:
    from omfiles import OmFileReaderAsync
except ImportError:  # pragma: no cover - omfiles piu' vecchio
    OmFileReaderAsync = None

# fsspec (backend HTTP con Range). Opzionale: se assente idem sopra.
try:
    import fsspec as _fsspec
except ImportError:  # pragma: no cover - dipendenza opzionale
    _fsspec = None

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
    "ecmwf_ifs025": (-90.0, -180.0, 90.0, 179.75),
    "ncep_gfs025": (-90.0, -180.0, 90.0, 179.75),
    # `ncep_gfs013` (0.13 deg) usa lo STESSO bbox europeo di `dwd_icon_eu`.
    # La voce e' OBBLIGATORIA: senza di essa `bbox_for()` ricadrebbe IN SILENZIO
    # su `V2_BBOX` (Italia) e il crop sarebbe 104x92 invece di ~470x239.
    "ncep_gfs013": (34.0, -15.0, 62.0, 40.0),
    "dwd_icon": (-90.0, -180.0, 90.0, 179.75),
    "meteofrance_arpege_world025": (-90.0, -180.0, 90.0, 179.75),
}

def bbox_for(domain):
    return DOMAIN_BBOX.get(domain, V2_BBOX)

# Domini di default (MULTI-DOMINIO INDIPENDENTE: nessun ordine di priorita').
V2_DOMAINS = ["dwd_icon_eu", "dwd_icon_d2", "ecmwf_ifs025", "ncep_gfs025", "ncep_gfs013", "dwd_icon", "meteofrance_arpege_world025"]

# Lista ORDINATA delle variabili di rischio desiderate, con sinonimi/forme.
# name = nome pubblicato nel `.om`; sources = nomi candidati nel file sorgente
# (il primo presente vince). La presenza reale e' determinata per-file.
# NOTA (1.2.4.11): le voci con `sources: []` sono DERIVATE (non esistono nella
# sorgente): compaiono nel registro `DERIVED_VARS` piu' sotto, che ne dichiara
# le variabili sorgente e la funzione. `resolve_direct` non le risolve (nessun
# candidato) e vengono aggiunte dopo la lettura diretta (vedi `derive_per_frame`
# per le derivate per-frame e l'accumulo stateful per precipitation_cum).
V2_RISK_VARS = [
    {"name": "temperature_2m",       "sources": ["temperature_2m"]},
    {"name": "dew_point_2m",         "sources": []},                          # DERIVATA (T2m + RH2m)
    {"name": "temperature_850hPa",   "sources": ["temperature_850hPa"]},      # FM_CACHE core
    {"name": "temperature_500hPa",   "sources": ["temperature_500hPa"]},      # FM_CACHE core
    {"name": "relative_humidity_2m", "sources": ["relative_humidity_2m"]},
    {"name": "relative_humidity_850hPa", "sources": ["relative_humidity_850hPa"]},  # FM_CACHE core
    {"name": "relative_humidity_700hPa", "sources": ["relative_humidity_700hPa"]},  # diretta (10/11 domini)
    {"name": "pressure_msl",         "sources": ["pressure_msl", "msl_pressure"]},
    {"name": "geopotential_height_500hPa", "sources": ["geopotential_height_500hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_850hPa", "sources": ["geopotential_height_850hPa"]},   # FM_CACHE core
    {"name": "geopotential_height_1000hPa", "sources": ["geopotential_height_1000hPa"]},  # FM_CACHE core
    {"name": "surface_pressure",     "sources": ["surface_pressure"]},        # spesso assente
    {"name": "wind_speed_10m",       "sources": []},                          # DERIVATA (u/v 10m, INPUT)
    {"name": "wind_direction_10m",   "sources": []},                          # DERIVATA (u/v 10m, INPUT)
    {"name": "wind_speed_850hPa",    "sources": []},                          # DERIVATA (u/v 850hPa, INPUT)
    {"name": "wind_direction_850hPa", "sources": []},                         # DERIVATA (u/v 850hPa, INPUT)
    {"name": "wind_speed_300hPa",    "sources": []},                          # DERIVATA (u/v 300hPa, INPUT)
    {"name": "wind_direction_300hPa", "sources": []},                         # DERIVATA (u/v 300hPa, INPUT)
    {"name": "wind_gusts_10m",       "sources": ["wind_gusts_10m"]},
    {"name": "cloud_cover",          "sources": ["cloud_cover"]},
    {"name": "cloud_cover_low",      "sources": ["cloud_cover_low"]},        # FM_CACHE core
    {"name": "cloud_cover_mid",      "sources": ["cloud_cover_mid"]},        # FM_CACHE core
    {"name": "cloud_cover_high",     "sources": ["cloud_cover_high"]},       # FM_CACHE core
    {"name": "cape",                 "sources": ["cape"]},
    {"name": "snow_depth",           "sources": ["snow_depth"]},
    {"name": "precipitation",        "sources": ["precipitation"]},
    {"name": "precipitation_cum",    "sources": []},                          # DERIVATA STATEFUL (somma precipitation)
    {"name": "rain",                 "sources": ["rain"]},
    {"name": "showers",              "sources": ["showers"]},
    {"name": "weather_code",         "sources": ["weather_code"]},
    {"name": "freezing_level_height", "sources": ["freezing_level_height"]},  # FM_CACHE core
]

# Variabili di INPUT: lette dal frame sorgente ma NON pubblicate. Sono gli input
# delle derivate wind_speed_*/wind_direction_* (componenti u/v a 10 m, 850 e
# 300 hPa). Le u/v a 10 m erano PUBBLICATE fino alla 1.2.4.10: dalla 1.2.4.11
# NON si pubblicano piu' (la libreria client vuole la COPPIA
# speed+direction). Devono essere lette ANCHE dal percorso a intervalli, quindi
# entrano nell'unione `V2_READ_VARS`.
V2_INPUT_VARS = [
    {"name": "wind_u_component_10m", "sources": ["wind_u_component_10m"]},
    {"name": "wind_v_component_10m", "sources": ["wind_v_component_10m"]},
    {"name": "wind_u_component_850hPa", "sources": ["wind_u_component_850hPa"]},
    {"name": "wind_v_component_850hPa", "sources": ["wind_v_component_850hPa"]},
    {"name": "wind_u_component_300hPa", "sources": ["wind_u_component_300hPa"]},
    {"name": "wind_v_component_300hPa", "sources": ["wind_v_component_300hPa"]},
]
V2_INPUT_VAR_NAMES = frozenset(v["name"] for v in V2_INPUT_VARS)

# Unione passata ai reader (`load_frame`, `read_frame_selective`): pubblicate +
# INPUT. Va passato QUESTO insieme ai reader, altrimenti la lettura a intervalli
# non legge le componenti u/v a 850/300 hPa e la derivazione fallisce solo su
# quel percorso.
V2_READ_VARS = V2_RISK_VARS + V2_INPUT_VARS

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
# Le wind_direction_* (direzione meteorologica in gradi, intervallo [0,360))
# usano passo 1 grado: e' il massimo di risoluzione utile (il valore e' un
# intero di gradi) e con scale_factor = 1/1 = 1 il massimo memorizzabile int16
# e' 32767 gradi, ampiamente sopra il range [0,360): nessun rischio di
# saturazione/NaN.
# ATTENZIONE (verificato su dati reali): la quantizzazione a 1 grado fa
# arrotondare 359.5..360.0 a 360, che e' FUORI da [0,360). Per non pubblicare
# mai 360 il writer pubblico passa da `wrap_wind_direction_for_step`, che mappa
# le direzioni >= 360 - passo/2 a 0 (360 equivale a 0).
WIND_DIRECTION_STEP = 1.0
VARIABLE_STEP = {
    "temperature_2m": 0.1,              # 0.1 degC
    "temperature_850hPa": 0.1,          # 0.1 degC
    "temperature_500hPa": 0.1,          # 0.1 degC
    "relative_humidity_2m": 1.0,        # 1 %
    "relative_humidity_850hPa": 1.0,    # 1 %
    "relative_humidity_700hPa": 1.0,    # 1 %
    "dew_point_2m": 0.1,                # 0.1 degC (DERIVATA, Magnus)
    "pressure_msl": 0.1,                # 0.1 hPa
    "geopotential_height_500hPa": 1.0,  # 1 m
    "geopotential_height_850hPa": 1.0,  # 1 m
    "geopotential_height_1000hPa": 1.0, # 1 m
    "surface_pressure": 0.1,            # 0.1 hPa
    # wind_u/v_component_* NON sono piu' pubblicate (INPUT): nessun passo qui.
    "wind_speed_10m": 0.1,              # 0.1 m/s (DERIVATA da u/v 10m, INPUT)
    "wind_direction_10m": WIND_DIRECTION_STEP,    # 1 grado (DERIVATA da u/v 10m, INPUT)
    "wind_speed_850hPa": 0.1,           # 0.1 m/s (DERIVATA da u/v 850hPa, INPUT)
    "wind_direction_850hPa": WIND_DIRECTION_STEP, # 1 grado (DERIVATA da u/v 850hPa, INPUT)
    "wind_speed_300hPa": 0.1,           # 0.1 m/s (DERIVATA da u/v 300hPa, INPUT)
    "wind_direction_300hPa": WIND_DIRECTION_STEP, # 1 grado (DERIVATA da u/v 300hPa, INPUT)
    "wind_gusts_10m": 0.1,              # 0.1 m/s
    "cloud_cover": 1.0,                 # 1 %
    "cloud_cover_low": 1.0,             # 1 %
    "cloud_cover_mid": 1.0,             # 1 %
    "cloud_cover_high": 1.0,            # 1 %
    "cape": 10.0,                       # 10 J/kg
    "snow_depth": 0.1,                  # 0.1 m
    "precipitation": 0.1,               # 0.1 mm
    # precipitation_cum (DERIVATA STATEFUL): cumulata lungo la serie dei
    # valid_times. LIMITE int16 (compressione pfor_delta_2d_int16): il writer
    # passa scale_factor = 1/passo = 10, quindi int = round(mm*10) e il massimo
    # int16 (32767) corrisponde a 3276.7 mm; OLTRE, omfiles produce NaN (non
    # wrap: comportamento MISURATO). Massimo osservato su dati reali: 1128.7 mm
    # (ciclone tropicale, dominio globale ECMWF) -> 11287, sotto il limite.
    "precipitation_cum": 0.1,           # 0.1 mm (DERIVATA STATEFUL)
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

# Tabella delle griglie NOTE dei 7 domini GEOGCRS. NON e' la fonte unica:
# la griglia di run e' DERIVATA da crs_wkt (BBOX) + shape del frame. Questa
# tabella serve SOLO come (a) FALLBACK nel --dry-run (dove il frame non si
# scarica, quindi nx/ny non e' derivabile) e (b) GUARDIA di coerenza sui dx/dy.
GRIDS = {
    "dwd_icon_eu":                  {"nx": 1377, "ny": 657,  "dx": 0.0625,    "dy": 0.0625},
    "ecmwf_ifs025":                 {"nx": 1440, "ny": 721,  "dx": 0.25,      "dy": 0.25},
    "ncep_gfs025":                  {"nx": 1440, "ny": 721,  "dx": 0.25,      "dy": 0.25},
    "ncep_gfs013":                  {"nx": 3072, "ny": 1536, "dx": 0.1171875, "dy": 0.1171493},
    "dwd_icon":                     {"nx": 2879, "ny": 1441, "dx": 0.125,     "dy": 0.125},
    "meteofrance_arpege_world025":  {"nx": 1440, "ny": 721,  "dx": 0.25,      "dy": 0.25},
    "dwd_icon_d2":                  {"nx": 1215, "ny": 746,  "dx": 0.02,      "dy": 0.02},
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


def _reader_shape(reader):
    """(ny, nx) dal PRIMO child array di un reader SYNC (o None)."""
    for i in range(reader.num_children):
        c = reader.get_child_by_index(i)
        if not c.is_scalar:
            shp = getattr(c, "shape", None)
            if shp is not None and len(shp) == 2:
                return int(shp[0]), int(shp[1])
    return None


def frame_shape(path):
    """(ny, nx) dal PRIMO child array del frame `.om` (o None)."""
    reader = OmFileReader.from_path(path)
    try:
        return _reader_shape(reader)
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


def _resample_window(window, jj, ii, jmin, imin):
    """Riduce una finestra alla griglia target (nearest). STESSA aritmetica per
    la lettura locale (sync) e per quella a intervalli (async)."""
    window = np.asarray(window, dtype=np.float32)
    return np.ascontiguousarray(window[np.ix_(jj - jmin, ii - imin)]).astype(np.float32)


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
    return _resample_window(window, jj, ii, jmin, imin)


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


# ---------------------------------------------------------------------------
# Derivazioni (1.2.4.11): variabili PUBBLICATE che NON esistono nella sorgente.
# ---------------------------------------------------------------------------
# Coefficienti Magnus (formula richiesta): T in degC, RH in %.
MAGNUS_A = 17.62
MAGNUS_B = 243.12


def dew_point_magnus(t, rh):
    """Punto di rugiada (Magnus). T in degC, RH in %.

    alpha = ln(RH/100) + a*T/(b+T);  Td = b*alpha/(a-alpha).
    Casi limite DICHIARATI:
      - RH <= 0: ln(0) = -inf -> RH viene CLIPPATA a 0.1% (il risultato resta
        finito, il valore piu' basso ottenibile);
      - RH > 100: RH viene CLIPPATA a 100 (saturazione, Td = T);
      - T/RH assenti: la derivata NON viene calcolata (gestito da `derive_per_frame`,
        che salta la variabile con warning).
    T fuori intervallo fisico non e' atteso (per T <= -b il denominatore
    degenera): non gestito, i dati reali stanno in [-90, 60] degC.
    """
    t = np.asarray(t, dtype=np.float64)
    rh = np.asarray(rh, dtype=np.float64)
    rh = np.clip(rh, 0.1, 100.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        alpha = np.log(rh / 100.0) + MAGNUS_A * t / (MAGNUS_B + t)
        td = MAGNUS_B * alpha / (MAGNUS_A - alpha)
    return td.astype(np.float32)


def wind_speed_from_uv(u, v):
    """Velocita' del vento = sqrt(u^2 + v^2) (il segno delle componenti non conta)."""
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    return np.sqrt(u * u + v * v).astype(np.float32)


def wind_direction_from_uv(u, v):
    """Direzione METEOROLOGICA del vento (gradi, [0,360)): quella DA cui soffia.

    Convenzione della libreria client `@openmeteo/weather-map-layer`
    (getSourceVars per i nomi `_(?:speed|direction)_`): la direzione e' la
    secondaria della velocita'

        direction = (degrees(atan2(u, v)) + 180) % 360

    dove u = componente zonale (positiva verso EST) e v = componente
    meridionale (positiva verso NORD). Risultato: 0 = vento da NORD,
    90 = da OVEST, 180 = da SUD, 270 = da EST.
    """
    u = np.asarray(u, dtype=np.float64)
    v = np.asarray(v, dtype=np.float64)
    return ((np.degrees(np.arctan2(u, v)) + 180.0) % 360.0).astype(np.float32)


def wrap_wind_direction_for_step(d):
    """Riporta a 0 le direzioni che il passo dichiarato quantizzerebbe a 360.

    La formula pura (`wind_direction_from_uv`) produce valori in [0,360), ma il
    writer memorizza int = round(valore / passo) ... nel nostro caso
    scale_factor = 1/passo, quindi valore = int/passo e una direzione in
    [360 - passo/2, 360) viene arrotondata a 360, FUORI dall'intervallo
    dichiarato. Poiche' 360 e 0 sono la stessa direzione, qui le si mappa a 0:
    il `.om` pubblicato resta sempre in [0,360). MISURATO su dwd_icon_eu reale:
    senza questo wrap 5 celle su 395569 contenevano 360.0.
    """
    d = np.asarray(d, dtype=np.float32)
    limit = np.float32(360.0 - 0.5 * WIND_DIRECTION_STEP)
    return np.where(d >= limit, np.float32(0.0), d).astype(np.float32)


def accumulate_precip_cum(cum, precip, shape):
    """Cumulata di precipitazione (STATEFUL): somma per cella in ordine di valid_time.

    `cum` = cumulata precedente (None al primo frame). `precip` = array del frame
    corrente o None se il frame NON espone `precipitation` (contribuisce 0: e' il
    caso del frame al reference_time). Ritorna la cumulata aggiornata np.float64.
    Il frame senza precipitazione inizializza comunque la cumulata a 0 (scelta
    DICHIARATA: precipitation_cum e' pubblicata su TUTTI i frame, cosi' l'insieme
    delle variabili resta coerente tra i frame).
    """
    if cum is None:
        cum = np.zeros(shape, dtype=np.float64)
    if precip is not None:
        cum = cum + np.asarray(precip, dtype=np.float64)
    return cum


def _derive_dew_point_2m(arrays):
    return dew_point_magnus(arrays["temperature_2m"],
                            arrays["relative_humidity_2m"])


def _derive_wind_speed_10m(arrays):
    return wind_speed_from_uv(arrays["wind_u_component_10m"],
                              arrays["wind_v_component_10m"])


def _derive_wind_speed_850hPa(arrays):
    return wind_speed_from_uv(arrays["wind_u_component_850hPa"],
                              arrays["wind_v_component_850hPa"])


def _derive_wind_speed_300hPa(arrays):
    return wind_speed_from_uv(arrays["wind_u_component_300hPa"],
                              arrays["wind_v_component_300hPa"])


def _derive_wind_direction_10m(arrays):
    return wrap_wind_direction_for_step(
        wind_direction_from_uv(arrays["wind_u_component_10m"],
                               arrays["wind_v_component_10m"]))


def _derive_wind_direction_850hPa(arrays):
    return wrap_wind_direction_for_step(
        wind_direction_from_uv(arrays["wind_u_component_850hPa"],
                               arrays["wind_v_component_850hPa"]))


def _derive_wind_direction_300hPa(arrays):
    return wrap_wind_direction_for_step(
        wind_direction_from_uv(arrays["wind_u_component_300hPa"],
                               arrays["wind_v_component_300hPa"]))


# Registro delle derivazioni: nome PUBBLICATO -> {sources, func, stateful}.
# `sources` = nomi (pubblicati o di INPUT) che devono essere presenti; `func`
# (arrays) = derivata per-frame; `stateful: True` = NON per-frame ma accumulata
# dal chiamante lungo la serie (`process_domain`).
# ATOMICITA' (velocita' + direzione): `wind_speed_X` e `wind_direction_X`
# dichiarano le STESSE due componenti u/v come `sources`, quindi
# `derive_per_frame` calcola la coppia o la OMETTE ENTRAMBI (se manca anche una
# sola componente, per entrambe `missing` e' non vuoto). La coppia e' percio'
# sempre o tutta presente o tutta assente nel `.om` pubblicato: nessuna
# variabile orfana che manderebbe in errore il reader del client (che pretende
# la secondaria `wind_direction_*` per ogni `wind_speed_*`).
DERIVED_VARS = {
    "dew_point_2m": {
        "sources": ["temperature_2m", "relative_humidity_2m"],
        "func": _derive_dew_point_2m,
    },
    "wind_speed_10m": {
        "sources": ["wind_u_component_10m", "wind_v_component_10m"],
        "func": _derive_wind_speed_10m,
    },
    "wind_direction_10m": {
        "sources": ["wind_u_component_10m", "wind_v_component_10m"],
        "func": _derive_wind_direction_10m,
    },
    "wind_speed_850hPa": {
        "sources": ["wind_u_component_850hPa", "wind_v_component_850hPa"],
        "func": _derive_wind_speed_850hPa,
    },
    "wind_direction_850hPa": {
        "sources": ["wind_u_component_850hPa", "wind_v_component_850hPa"],
        "func": _derive_wind_direction_850hPa,
    },
    "wind_speed_300hPa": {
        "sources": ["wind_u_component_300hPa", "wind_v_component_300hPa"],
        "func": _derive_wind_speed_300hPa,
    },
    "wind_direction_300hPa": {
        "sources": ["wind_u_component_300hPa", "wind_v_component_300hPa"],
        "func": _derive_wind_direction_300hPa,
    },
    "precipitation_cum": {
        "sources": ["precipitation"],
        "func": None,
        "stateful": True,
    },
}


def derive_per_frame(arrays, meta, domain):
    """Applica le derivazioni STATELESS e aggiorna `arrays`/`meta` (in place).

    Unica implementazione condivisa dai due percorsi di lettura (locale/reader e
    range): NON duplicata nei due punti. Ritorna i warning per le derivate non
    calcolabili (una sorgente assente). Le derivate stateful (precipitation_cum)
    sono escluse: le accumula `process_domain`.
    """
    warnings = []
    for name, spec in DERIVED_VARS.items():
        if spec.get("stateful"):
            continue
        srcs = spec["sources"]
        missing = [s for s in srcs if s not in arrays]
        if missing:
            warnings.append(
                "variabile derivata '%s' non calcolabile nel dominio '%s': "
                "manca %s" % (name, domain, ",".join(missing)))
            continue
        arrays[name] = spec["func"](arrays)
        meta[name] = {"source_domain": domain, "derived": True,
                      "derived_from": list(srcs)}
    return warnings


def missing_published_warnings(risk_vars, arrays, domain):
    """Warning per le variabili PUBBLICATE DIRETTE non risolte (file-per-file).

    Esclude gli INPUT (letti ma non pubblicati) e le derivate (gestite da
    `derive_per_frame` / dall'accumulo stateful).
    """
    warns = []
    for rv in risk_vars:
        name = rv["name"]
        if name in V2_INPUT_VAR_NAMES or name in DERIVED_VARS:
            continue
        if name not in arrays:
            warns.append("variabile '%s' assente nel dominio '%s' (%s): omessa"
                         % (name, domain, ",".join(rv["sources"])))
    return warns


def resolve_domain_frame(reader, grid, risk_vars, tgt_lats, tgt_lons, idx, domain):
    """Costruisce gli array di output del frame del dominio + metadati.

    Ritorna (arrays, variables_meta, warnings). `arrays` e' dict
    nome -> np.float32 crop: variabili PUBBLICATE (dirette + derivate) piu' gli
    INPUT (letti per le derivate, NON pubblicati e assenti da `meta`).
    `risk_vars` e' l'UNIONE pubblicate + input (`V2_READ_VARS`). Nessun dominio
    secondario: la griglia sorgente E' la griglia del dominio (crop esatto).
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
        if rv["name"] not in V2_INPUT_VAR_NAMES:
            meta[rv["name"]] = {"source_domain": domain, "source_var": src,
                                "derived": False}

    # 2) derivazione per-frame (stateless), condivisa coi due percorsi di lettura
    warnings.extend(derive_per_frame(arrays, meta, domain))

    # 3) variabili PUBBLICATE dirette richieste ma irrisolte -> warning
    warnings.extend(missing_published_warnings(risk_vars, arrays, domain))

    return arrays, meta, warnings


# ---------------------------------------------------------------------------
# Lettura SELETTIVA a intervalli (range GET) delle SOLE variabili pubblicate
# ---------------------------------------------------------------------------
# Ogni frame sorgente contiene molte piu' variabili di quelle che pubblichiamo
# (es. `ncep_gfs025`: ~315 array, ne pubblichiamo ~10; `ncep_gfs013`: 16 array,
# ne pubblichiamo ~5) e pesa decine/centinaia di MB. `omfiles` sa leggere un
# `.om` remoto a INTERVALLI via fsspec (HTTP Range): si apre il frame remoto e
# si leggono SOLO i child richiesti, senza scaricare l'intero file. `crs_wkt` e
# la shape della griglia (letture di header, piccole) vengono lette comunque.
#
# FALLBACK ESPLICITO: se fsspec/HTTP o il formato remoto non funzionano il
# motore RICADE sul download completo, con `[WARN]` inequivocabile (dominio/
# frame/motivo) e contatori registrati nel manifest (`download.mode`,
# `download.fallback_count`, ...). Il fallback NON e' mai silenzioso.
class RangeStats(object):
    """Byte e numero di richieste HTTP di un frame letto a intervalli."""

    __slots__ = ("requests", "bytes")

    def __init__(self):
        self.requests = 0
        self.bytes = 0


def range_supported():
    """True se la lettura a intervalli e' disponibile (fsspec + reader async)."""
    return _fsspec is not None and OmFileReaderAsync is not None


# Un SOLO event loop per processo. fsspec tiene in cache l'istanza del filesystem
# (e la sua sessione aiohttp): con `asyncio.run()` per-frame una sessione creata
# in un loop chiuso verrebbe riusata -> errore reale "Event loop is closed" gia'
# osservato. Qui il loop e' persistente e OGNI lettura apre un fs NUOVO (sessione
# fresca), chiuso a fine lettura: niente sessioni stantie tra frame.
_HTTP_LOOP = None
_ACTIVE_STATS = None


def _http_loop():
    global _HTTP_LOOP
    if _HTTP_LOOP is None or _HTTP_LOOP.is_closed():
        _HTTP_LOOP = asyncio.new_event_loop()
    return _HTTP_LOOP


def _new_http_fs():
    """fs HTTP asincrono NUOVO e strumentato (conta byte/richieste HTTP)."""
    fs = _fsspec.filesystem("http", asynchronous=True,
                            skip_instance_cache=True)
    orig = fs._cat_file

    async def _cat_file(path, start=None, end=None, **kwargs):
        data = await orig(path, start=start, end=end, **kwargs)
        if _ACTIVE_STATS is not None:
            _ACTIVE_STATS.requests += 1
            _ACTIVE_STATS.bytes += len(data)
        return data

    fs._cat_file = _cat_file
    return fs


async def _close_http_fs(fs):
    """Chiude la sessione aiohttp del fs (best-effort)."""
    try:
        sess = getattr(fs, "_session", None)
        if sess is not None:
            await sess.close()
    except Exception:  # noqa: BLE001
        pass


def _selective_run(coro, stats):
    """Esegue `coro` sul loop persistente, contando byte/richieste in `stats`."""
    global _ACTIVE_STATS
    _ACTIVE_STATS = stats
    try:
        return _http_loop().run_until_complete(coro)
    finally:
        _ACTIVE_STATS = None


def close_http():
    """Chiude il loop persistente (best-effort, a fine run)."""
    global _HTTP_LOOP
    try:
        if _HTTP_LOOP is not None and not _HTTP_LOOP.is_closed():
            _HTTP_LOOP.close()
    except Exception:  # noqa: BLE001
        pass
    _HTTP_LOOP = None


async def _areader_shape(reader):
    """(ny, nx) dal PRIMO child array di un reader ASYNC (o None)."""
    for i in range(reader.num_children):
        c = await reader.get_child_by_index(i)
        if not c.is_scalar:
            shp = getattr(c, "shape", None)
            if shp is not None and len(shp) == 2:
                return int(shp[0]), int(shp[1])
    return None


async def _selective_shape_async(url):
    fs = _new_http_fs()
    try:
        root = await OmFileReaderAsync.from_fsspec(fs, url)
        try:
            return await _areader_shape(root)
        finally:
            root.close()
    finally:
        await _close_http_fs(fs)


async def _selective_arrays_async(url, domain, risk_vars, grid, tgt_lats,
                                  tgt_lons):
    """Legge dal frame remoto SOLO le variabili di `risk_vars` presenti.

    Risoluzione nomi, finestra di crop e conversione sono IDENTICHE al percorso
    locale: `resolve_direct`/`nearest_indices`/`_resample_window` sono gli stessi
    (l'unica differenza e' l'I/O, che qui avviene a intervalli sul file remoto).
    """
    fs = _new_http_fs()
    try:
        root = await OmFileReaderAsync.from_fsspec(fs, url)
    except BaseException:  # noqa: BLE001
        await _close_http_fs(fs)
        raise
    try:
        shape = None
        names = set()
        for i in range(root.num_children):
            c = await root.get_child_by_index(i)
            if c.is_scalar:
                continue
            names.add(c.name)
            if shape is None:
                shp = getattr(c, "shape", None)
                if shp is not None and len(shp) == 2:
                    shape = (int(shp[0]), int(shp[1]))

        crs_frame = ""
        try:
            crs = await root.get_child_by_name("crs_wkt")
            if crs is not None:
                crs_frame = crs.read_scalar()
        except Exception:  # noqa: BLE001
            crs_frame = ""

        arrays = {}
        meta = {}
        warnings = []
        jj, ii = nearest_indices(grid, tgt_lats, tgt_lons)
        jmin, jmax = int(jj.min()), int(jj.max())
        imin, imax = int(ii.min()), int(ii.max())
        sel = (slice(jmin, jmax + 1), slice(imin, imax + 1))
        plan = []
        for rv in risk_vars:
            src = resolve_direct(rv, names)
            if src is not None:
                plan.append((rv["name"], src))

        async def _read_one(src):
            child = await root.get_child_by_name(src)
            window = await child.read_array(sel)
            return _resample_window(window, jj, ii, jmin, imin)

        # return_exceptions=True: TUTTE le letture della finestra arrivano a
        # termine prima di procedere, cosi' non restano task pendenti che
        # terrebbero aperta la sessione HTTP (chiusa nel finally).
        results = await asyncio.gather(*[_read_one(src) for _, src in plan],
                                       return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                raise r
        windows = results
        for (name, src), arr in zip(plan, windows):
            arrays[name] = arr
            if name not in V2_INPUT_VAR_NAMES:
                meta[name] = {"source_domain": domain, "source_var": src,
                              "derived": False}
        # derivazione per-frame (stateless) + warning delle PUBBLICATE irrisolte:
        # STESSA logica del percorso locale (`resolve_domain_frame`).
        warnings.extend(derive_per_frame(arrays, meta, domain))
        warnings.extend(missing_published_warnings(risk_vars, arrays, domain))
        return {"shape": shape, "crs_frame": crs_frame, "arrays": arrays,
                "meta": meta, "warnings": warnings}
    finally:
        root.close()
        await _close_http_fs(fs)


def probe_frame_shape_range(url, retries=3):
    """Shape del frame via lettura a intervalli. (shape, RangeStats, errore)."""
    stats = RangeStats()
    last = None
    for attempt in range(1, retries + 1):
        try:
            shape = _selective_run(_selective_shape_async(url), stats)
            return shape, stats, None
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return None, stats, last


def read_frame_selective(url, domain, risk_vars, grid, tgt_lats, tgt_lons,
                         retries=3):
    """Legge SOLO le variabili pubblicate via range GET.

    Ritorna (dict|None, RangeStats, errore). Un numero LIMITATO di tentativi
    (stesso pattern di `download`) assorbe disconnessioni transitorie; esauriti
    i tentativi il chiamante RICADE sul download completo (fallback esplicito).
    """
    stats = RangeStats()
    last = None
    for attempt in range(1, retries + 1):
        try:
            res = _selective_run(_selective_arrays_async(
                url, domain, risk_vars, grid, tgt_lats, tgt_lons), stats)
            return res, stats, None
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(1.5 * attempt)
    return None, stats, last


def _read_local_frame(path, domain, risk_vars, grid, tgt_lats, tgt_lons, idx):
    """Legge il frame da un file LOCALE (percorso sync invariato). Ritorna dict."""
    reader = OmFileReader.from_path(path)
    try:
        shape = _reader_shape(reader)
        crs_frame = ""
        try:
            crs = reader.get_child_by_name("crs_wkt")
            if crs is not None:
                crs_frame = crs.read_scalar()
        except Exception:  # noqa: BLE001
            crs_frame = ""
        arrays, meta, warnings = resolve_domain_frame(
            reader, grid, risk_vars, tgt_lats, tgt_lons, idx, domain)
        return {"shape": shape, "crs_frame": crs_frame, "arrays": arrays,
                "meta": meta, "warnings": warnings}
    finally:
        reader.close()


def _cached_path(url, domain, valid_name, frames_cache):
    """Percorso del frame in frames-cache se PRESENTE e aggiornato (nessuna GET)."""
    if not frames_cache:
        return None
    dst = os.path.join(frames_cache, domain, valid_name + ".om")
    if not os.path.isfile(dst):
        return None
    expected = http_content_length(url)
    local = os.path.getsize(dst)
    if expected is None or local == expected:
        print("%s   frame reuse (frames-cache %s, %d byte%s)"
              % (LOG, dst, local,
                 "" if expected is None else ", Content-Length ok"))
        return dst
    print("%s   frame cache stale (%d != %d byte, %s): non riusato"
          % (LOG, local, expected, dst))
    return None


def _record_fallback(dl, domain, valid_name, reason):
    """Registra un fallback da range a download completo (WARN + contatore)."""
    dl["fallback_count"] += 1
    detail = "dominio=%s frame=%s: %s" % (domain, valid_name, reason)
    if detail not in dl["fallback_reasons"]:
        dl["fallback_reasons"].append(detail)
    print("%s [WARN] lettura a intervalli fallita -> download completo (%s)"
          % (LOG, detail))


def probe_frame_shape(url, domain, valid_name, cache_dir, frames_cache, dl,
                      allow_range=True):
    """(shape, mode) del PRIMO frame: cache locale -> range -> download completo."""
    path = _cached_path(url, domain, valid_name, frames_cache)
    if path is not None:
        dl["grid_probe_mode"] = "cache"
        return frame_shape(path), "cache"
    if allow_range and range_supported():
        shape, stats, err = probe_frame_shape_range(url)
        dl["range_requests"] += stats.requests
        dl["range_bytes"] += stats.bytes
        if err is None and shape:
            dl["grid_probe_mode"] = "range"
            return shape, "range"
        if err is None:
            _record_fallback(dl, domain, valid_name, "shape non leggibile via range")
        else:
            _record_fallback(dl, domain, valid_name,
                             "%s: %s" % (type(err).__name__, err))
    elif allow_range:
        _record_fallback(dl, domain, valid_name,
                         "lettura a intervalli non disponibile "
                         "(fsspec/reader assente)")
    fpath, _ = fetch_frame(url, domain, valid_name, cache_dir, frames_cache,
                           label="griglia")
    if fpath is None:
        return None, "fail"
    dl["download_bytes"] += os.path.getsize(fpath)
    dl["grid_probe_mode"] = "download"
    return frame_shape(fpath), "download"


def load_frame(url, domain, valid_name, cache_dir, frames_cache, risk_vars,
               grid, tgt_lats, tgt_lons, idx, dl, allow_range=True):
    """Ottiene un frame sorgente con i dati pubblicati.

    Ordine: (1) frame locale in frames-cache (gratis), (2) lettura selettiva a
    intervalli, (3) FALLBACK download completo (con [WARN] e contatore).
    Ritorna un dict {shape, arrays, meta, warnings, crs_frame, mode} o None.
    """
    path = _cached_path(url, domain, valid_name, frames_cache)
    if path is not None:
        dl["cache_frames"] += 1
        d = _read_local_frame(path, domain, risk_vars, grid, tgt_lats, tgt_lons,
                              idx)
        d["mode"] = "cache"
        return d
    if allow_range and range_supported():
        d, stats, err = read_frame_selective(url, domain, risk_vars, grid,
                                             tgt_lats, tgt_lons)
        dl["range_requests"] += stats.requests
        dl["range_bytes"] += stats.bytes
        if err is None:
            dl["range_frames"] += 1
            d["mode"] = "range"
            return d
        _record_fallback(dl, domain, valid_name,
                         "%s: %s" % (type(err).__name__, err))
    elif allow_range:
        _record_fallback(dl, domain, valid_name,
                         "lettura a intervalli non disponibile "
                         "(fsspec/reader assente)")
    fpath, _ = fetch_frame(url, domain, valid_name, cache_dir, frames_cache,
                           label="frame")
    if fpath is None:
        return None
    dl["download_frames"] += 1
    dl["download_bytes"] += os.path.getsize(fpath)
    d = _read_local_frame(fpath, domain, risk_vars, grid, tgt_lats, tgt_lons, idx)
    d["mode"] = "download"
    return d


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

    # Contatori di provenienza dei frame (registrati nel manifest: vedi
    # `download` piu' sotto). `mode` = "range" | "mixed" | "download" |
    # "frames-cache"; `fallback_count` conta i frame che, per un guasto della
    # lettura a intervalli, sono stati scaricati per intero.
    dl = {
        "mode": "range",
        "grid_probe_mode": None,
        "range_frames": 0,
        "cache_frames": 0,
        "download_frames": 0,
        "fallback_count": 0,
        "fallback_reasons": [],
        "range_requests": 0,
        "range_bytes": 0,
        "download_bytes": 0,
    }
    res["download"] = dl

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
        shape, src_mode = probe_frame_shape(
            s3_url(domain, ref_time, first), domain,
            valid_time_to_filename(first), cache_dir, frames_cache, dl,
            allow_range=not args.no_range)
        if shape is None:
            res["reason"] = "shape del primo frame non leggibile (mode=%s)" % src_mode
            print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
            return res
        ny, nx = shape
        grid = derive_grid(crs_wkt, nx, ny)
        if grid is None:
            res["reason"] = "crs_wkt privo di BBOX o dimensioni non valide"
            print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
            return res
        print("%s   griglia dal PRIMO frame (%s)" % (LOG, src_mode))
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

    # Cumulata di precipitazione (STATE) sulla griglia di OUTPUT (dopo
    # crop/resample): accumulata lungo la serie ORDINATA dei valid_times.
    # precipitation_cum e' pubblicata su TUTTI i frame (frame 0 = 0.0).
    out_shape = (out_grid["ny"], out_grid["nx"])
    precip_cum = None
    precip_seen = False

    frames = []
    variables_map = {}
    sizes_map = {}
    all_warnings = []

    for vi, anchor in enumerate(anchors):
        print("%s   frame %d/%d valid_time=%s" % (LOG, vi + 1, len(anchors), anchor))
        vname = valid_time_to_filename(anchor)
        # `V2_READ_VARS` = pubblicate + INPUT: la lettura a intervalli deve
        # leggere ANCHE le componenti u/v a 850/300 hPa, altrimenti le derivate
        # wind_speed_850hPa/300hPa falliscono solo su quel percorso.
        frame = load_frame(s3_url(domain, ref_time, anchor), domain, vname,
                           cache_dir, frames_cache, V2_READ_VARS, grid,
                           tgt_lats, tgt_lons, idx, dl,
                           allow_range=not args.no_range)
        if frame is None:
            print("%s [WARN] salto frame %s (frame non ottenibile)." % (LOG, anchor))
            all_warnings.append("frame %s: frame non ottenibile" % anchor)
            continue

        crs_frame = frame["crs_frame"] or crs_wkt
        coordinates = "lat lon"
        arrays = frame["arrays"]
        vmeta = frame["meta"]
        warns = frame["warnings"]

        # Cumulata stateful: il frame senza `precipitation` (reference_time)
        # contribuisce 0 e NON interrompe l'accumulo.
        precip = arrays.get("precipitation")
        if precip is not None:
            precip_seen = True
        precip_cum = accumulate_precip_cum(precip_cum, precip, out_shape)
        arrays["precipitation_cum"] = precip_cum.astype(np.float32)
        vmeta["precipitation_cum"] = {
            "source_domain": domain, "derived": True,
            "derived_from": ["precipitation"]}

        vt_dt = parse_iso(anchor)
        ref_dt = parse_iso(ref_time)
        fname = vname + ".om"
        fpath_out = os.path.join(out_dir, fname)
        size, nvars = write_frame_om(
            fpath_out, ordered_names, arrays, crs_frame, coordinates,
            int(vt_dt.timestamp()), int(ref_dt.timestamp()))
        frames.append({"valid_time": anchor, "file": fname, "bytes": size,
                       "variables": nvars, "source_mode": frame["mode"]})
        variables_map[anchor] = vmeta
        sizes_map[anchor] = size
        all_warnings.extend(["%s: %s" % (anchor, w) for w in warns])
        print("%s   scritto %s (%d byte, %d var, src=%s)"
              % (LOG, fname, size, nvars, frame["mode"]))

    if not frames:
        res["reason"] = "nessun frame scritto (tutti i download falliti?)"
        print("%s [FAIL] dominio '%s': %s." % (LOG, domain, res["reason"]))
        return res

    if not precip_seen:
        all_warnings.append(
            "precipitation_cum: variabile 'precipitation' assente in TUTTI i "
            "frame del dominio '%s': la cumulata resta a 0" % domain)

    removed = []
    if not args.no_prune:
        keep_files = [valid_time_to_filename(a) + ".om" for a in anchors]
        removed = prune_frames(out_dir, keep_files, args.valid_frames)

    # Modalita' di download del dominio (registrata nel manifest/report).
    used_range = dl["range_frames"] > 0 or dl["grid_probe_mode"] == "range"
    used_dl = dl["download_frames"] > 0 or dl["grid_probe_mode"] == "download"
    if used_range and used_dl:
        dl["mode"] = "mixed"
    elif used_dl:
        dl["mode"] = "download"
    elif used_range:
        dl["mode"] = "range"
    elif dl["cache_frames"]:
        dl["mode"] = "frames-cache"
    else:
        dl["mode"] = "range"

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
        "download": dl,
    }
    write_json_atomic(latest_path, manifest)

    total_bytes = sum(f["bytes"] for f in frames)
    print("%s [OK] dominio '%s': %d frame, %d byte totali (%.1f KiB)."
          % (LOG, domain, len(frames), total_bytes, total_bytes / 1024.0))
    print("%s   download: mode=%s (griglia=%s) | range=%d frame (%d req, %d byte) | "
          "download=%d frame (%d byte) | cache=%d | fallback=%d"
          % (LOG, dl["mode"], dl["grid_probe_mode"], dl["range_frames"],
             dl["range_requests"], dl["range_bytes"], dl["download_frames"],
             dl["download_bytes"], dl["cache_frames"], dl["fallback_count"]))
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
    ap.add_argument("--no-range", action="store_true",
                    help="Disabilita la lettura a intervalli (range GET): scarica "
                         "sempre il frame COMPLETO. Usato per debug/confronto; "
                         "default = lettura selettiva con fallback esplicito.")
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
        close_http()
        return 1
    print("%s FATTO: %d/%d domini ok." % (LOG, len(ok), len(results)))
    close_http()
    return 0


if __name__ == "__main__":
    sys.exit(main())
