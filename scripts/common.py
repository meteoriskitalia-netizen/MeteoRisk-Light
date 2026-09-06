#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk Light — Centralized Derived Data Pipeline
Common module: configuration fedele all'app + client sorgenti + stato run.

Open-Meteo here is a DATA SOURCE (meteorological input data), not a dataset
to be copied and republished. The pipeline produces DERIVED MeteoRisk data.

The constants below mirror the application mri-light (APP_VERSION 1.0.0.3):
  - HOURLY_PARAMS / DAILY_PARAMS (the variables MeteoRisk actually consumes)
  - forecast_days=3, timezone=Europe/Rome
  - dual model: best_match,ecmwf_ifs
  - buildProvinceSamplesV1/V2 + orography tables (faithful port)
  - getHourOrDailyData day-collapse + scorePointForProvince (faithful port)
"""

import datetime as _dt
import hashlib
import json
import math
import os
import random
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_GEO = REPO_ROOT / "data" / "geography"
DATA_LATEST = REPO_ROOT / "data" / "latest"
DATA_STATE = REPO_ROOT / "data" / "state"
DATA_RAW = REPO_ROOT / "data" / "_raw"          # raw input, NEVER published (tmp)
DATA_STAGING = REPO_ROOT / "data" / "_staging"  # build target, validated before publish
DATA_WORK = REPO_ROOT / "data" / "_workdir"     # artifacts intermedi (non pubblicati)

LAST_MODEL_RUN = DATA_STATE / "last_model_run.json"
API_USAGE_JSON = DATA_STATE / "api_usage.json"
API_EFFICIENCY_DIR = DATA_WORK / "api_efficiency"   # report non pubblicati
METADATA_JSON = DATA_LATEST / "metadata.json"
POINTS_JSON = DATA_LATEST / "meteorisk-points.json"
PROVINCES_JSON = DATA_LATEST / "meteorisk-provinces.json"
VALIDATION_JSON = DATA_LATEST / "validation.json"

LEGACY_POINTS_JSON = DATA_LATEST / "points.json"    # not used by this design (kept for reference)
LEGACY_PROVINCES_JSON = DATA_LATEST / "provinces.json"

GEOJSON_PATH = DATA_GEO / "province_italiane.geojson"
REGIONS_PATH = DATA_GEO / "regions_data.json"

# ---------------------------------------------------------------------------
# Open-Meteo — source configuration (mirrors the app)
# ---------------------------------------------------------------------------
OPENMETEO_BASE = "https://api.open-meteo.com/v1/forecast"
OPENMETEO_META_BASE = "https://api.open-meteo.com/data/{model}/static/meta.json"
OPENMETEO_TIMEOUT_S = 30
# Open-Meteo batching: max N location per richiesta. Misurato sul campo con il
# set completo HOURLY+DAILY dual (best_match,ecmwf_ifs, 3 giorni):
#   10 coords -> URL 1.4 KB, resp 411 KB
#  100 coords -> URL 3.2 KB, resp 4.1 MB (URL resta sotto il limite ~8 KB)
# 257 coords in UNA richiesta -> 429 "Minutely API request limit exceeded".
BATCH_MAX_LOCATIONS = int(os.environ.get("METEO_RISK_BATCH_MAX_LOCATIONS", "100"))
# Pacing anti-minutely-limits: la soglia osservata è ~5 richieste/minuto. Piano
# reale (257 punti): 3 batch da 100 + 1 da 57 => 4 richieste con 30 s di
# spaziatura, ampiamente sotto la soglia e sotto il ceiling giornaliero.
API_MIN_REQUEST_INTERVAL_S = float(os.environ.get("METEO_RISK_API_MIN_INTERVAL_S", "30.0"))
# API USAGE GUARDRAILS (1.0.0.8 hardening) — unico luogo di definizione dei
# limiti: default qui + data/state/api_usage.json (config runtime centralizzata),
# MAI limite hardcodato nei singoli script.
#   - daily_safety_ceiling: HARD SAFETY LIMIT (free tier Open-Meteo 10.000/g).
#   - warn_threshold_fraction: soglia di OSSERVABILITA' (warning, NON blocca).
#   - hard_stop_enabled: oltre il ceiling il fetch è bloccato (safe skip) e il
#     canary è skippato; il rilevamento Metadata API resta solo osservato.
# NON è un razionamento preventivo "per risparmiare la riserva per dopo": il
# fetch reale (nuovo run ECMWF / Best Match cambiato / bootstrap) parte
# normalmente finché il tetto di sicurezza non è raggiunto.
API_DAILY_LIMIT = int(os.environ.get("METEO_RISK_API_DAILY_LIMIT", "10000"))
API_GUARDRAILS_DEFAULT = {
    "enabled": True,
    "daily_safety_ceiling": API_DAILY_LIMIT,
    "warn_threshold_fraction": float(os.environ.get("METEO_RISK_SAFETY_RESERVE_FRAC", "0.8")),
    "hard_stop_enabled": True,
}
# Retry: LIMITATI, esponenziali e selettivi. Al termine del primo giro vengono
# ritentati SOLO i batch falliti (mai una ripetizione integrale del piano).
RETRY_LIMIT = int(os.environ.get("METEO_RISK_RETRY_LIMIT", "3"))
RETRY_BACKOFF_BASE_S = float(os.environ.get("METEO_RISK_RETRY_BACKOFF_BASE_S", "5.0"))
# PARTE H (1.0.0.8) — ROBUSTEZZA RETE / Metadata API check (H1/H2/H3):
#   H2 timeout esplicito connect/read (ossia non ci si affida al solo timeout
#      implicito di urllib); la Metadata API non conteggiata nel ceiling resta
#      veloce a rispondere, ma il TLS/keepalive puo' bloccarsi -> timeout esplicito.
#   H1 retry AUTOMATICI per errori transienti (SSL handshake timeout, TimeoutError,
#      URLError transitorio, connection reset, HTTP 429, HTTP 5xx); tentativi
#      TOTALI METADATA_API_RETRIES (3-4): MAI retry infiniti.
#   H3 exponential backoff + jitter (RETRY_JITTER_MAX_S), evita di martellare
#      l'API durante un problema temporaneo.
OPENMETEO_METADATA_TIMEOUT_S = 15
METADATA_API_RETRIES = 4
METADATA_RETRY_BASE_S = 2.0
RETRY_JITTER_MAX_S = 1.5
# Grace period after run availability before downloading (eventual consistency).
GRACE_AFTER_AVAILABILITY_S = 10 * 60   # 10 minutes, configurable

HOURLY_PARAMS = (
    "temperature_2m,relativehumidity_2m,dew_point_2m,pressure_msl,cape,"
    "precipitation_probability,windspeed_10m,winddirection_10m,"
    "windspeed_100m,winddirection_100m,windgusts_10m,weathercode,"
    "precipitation,showers,freezing_level_height,"
    "wind_speed_1000hPa,wind_direction_1000hPa,"
    "wind_speed_975hPa,wind_direction_975hPa,"
    "wind_speed_950hPa,wind_direction_950hPa,"
    "wind_speed_925hPa,wind_direction_925hPa,"
    "wind_speed_900hPa,wind_direction_900hPa,"
    "wind_speed_850hPa,wind_direction_850hPa,"
    "wind_speed_800hPa,wind_direction_800hPa,"
    "wind_speed_700hPa,wind_direction_700hPa,"
    "wind_speed_600hPa,wind_direction_600hPa,"
    "wind_speed_500hPa,wind_direction_500hPa,"
    "temperature_850hPa,temperature_700hPa,temperature_500hPa,"
    "relative_humidity_850hPa,relative_humidity_700hPa,"
    "dew_point_850hPa,dew_point_700hPa,"
    "geopotential_height_850hPa,geopotential_height_700hPa,"
    "geopotential_height_500hPa,convective_inhibition,lifted_index,k_index"
)
DAILY_PARAMS = (
    "weathercode,temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,windspeed_10m_max,precipitation_probability_max"
)
FORECAST_DAYS = 3
TIMEZONE = "Europe/Rome"
# Leg modello scaricati in OGNI ciclo (coordinati col run driver ECMWF IFS).
# None di essi "comanda" il run: best_match è COORDINATO, non driver.
DUAL_MODELS = "best_match,ecmwf_ifs"

HOURLY_FIELDS = HOURLY_PARAMS.split(",")
DAILY_FIELDS = DAILY_PARAMS.split(",")

# COORDINATED SCHEDULING: ECMWF IFS e' il driver UNICO del ciclo.
# best_match NON ha un proprio run_key: e' un composito senza endpoint metadata
# e viene aggiornato COORDINATO con il run ECMWF nello stesso ciclo di fetch
# (ogni ciclo scarica entrambi i leg: best_match + ecmwf_ifs).
DRIVER_MODEL = "ecmwf_ifs"                     # run driver (metadata API)
METADATA_MODELS_TRACKED = ["ecmwf_ifs"]        # soli run rilevati

# B-MATCH CHANGE DETECTION (1.0.0.8): canary LIGHTWEIGHT su # 6 capoluoghi (= punti
# reali del dataset, coordIdx 0, coordinate identiche a quelle pubblicate) in UNA
# richiesta multi-location. Fingerprint indipendente dal generation-time: hash
# SHA-256 su day0 + weathercode/precipitation orarie best_match dei sentinel.
# Costo: 1 richiesta forecast/ciclo (96/giorno al peggio, ~1% del ceiling giornaliero).
BEST_MATCH_SENTINEL_SIGLAS = ("MI", "VE", "RM", "PE", "LE", "PA")
BEST_MATCH_SENTINEL_AREAS = {"MI": "NW-Po", "VE": "NE", "RM": "Tirreno-centro",
                             "PE": "Adriatico", "LE": "Sud", "PA": "Isole"}
# Campi minimi del canary (poco volume di risposta, determinismo fingerprint).
BEST_MATCH_CHECK_HOURLY = "weathercode,precipitation"

APP_NAME = "MeteoRisk Light"
APP_DATA_TYPE = "derived_meteorological_risk_data"
SOURCE_NAME = "Open-Meteo"
SOURCE_LICENSE = "CC BY 4.0"
SOURCE_ROLE = "meteorological input data"

# ---------------------------------------------------------------------------
# Orography tables (faithful port of the app, buildProvinceSamplesV2)
# ---------------------------------------------------------------------------
OROGRAPHY_FACTOR = {"M2": 1.3, "H": 1.6, "L": 0.7}
OROGRAPHY_SPACING_KM = {"H": 18, "M2": 20, "M": 26, "L": 34}
_ORO_H = "AO BZ TN SO BL CN VC VB UD PN BG BS PC PR GE IM SV AQ MC AP FM PG RI IS PZ CS CZ VV KR EN NU SS OT OR OG".split()
_ORO_M2 = "CO LC MB TO AT AL NO BI VA FC RN RA TR PG AR SI GR LI PI PT PO FI LU MS RM VT FR LT BN AV SA CB FG BA BT TA BR LE RC ME CT SR RG CL AG TP PA CA CI VS LE NU".split()
_ORO_L = "MI MB LO PV CR MN VE PD TV VR RO FE BO MO RE RA FC RN PU AN TE PE CH LE LT NA CE RM".split()
OROGRAPHY_CLASS = {}
for _s in _ORO_H:
    OROGRAPHY_CLASS[_s] = "H"
for _s in _ORO_M2:
    OROGRAPHY_CLASS[_s] = "M2"
for _s in _ORO_L:
    OROGRAPHY_CLASS[_s] = "L"


# ---------------------------------------------------------------------------
# Geometry ports (ringsOf / ringArea / ringCentroid / pointInRing / kmBetween)
# ---------------------------------------------------------------------------
def rings_of(feature):
    """faithful port of app ringsOf()."""
    g = feature.get("geometry")
    if not g:
        return []
    t = g.get("type")
    if t == "Polygon":
        return list(g.get("coordinates") or [])
    if t == "MultiPolygon":
        out = []
        for poly in (g.get("coordinates") or []):
            out.extend(list(poly))
        return out
    return []


def ring_area(ring):
    """faithful port of app ringArea() (shoelace, degrees^2)."""
    n = len(ring)
    a = 0.0
    for i in range(n - 1):
        a += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return abs(a) / 2.0


def ring_centroid(ring):
    """faithful port of app ringCentroid()."""
    n = len(ring) - 1
    a = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        cross = ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        a += cross
        cx += (ring[i][0] + ring[i + 1][0]) * cross
        cy += (ring[i][1] + ring[i + 1][1]) * cross
    a = a / 2.0
    if a == 0:
        return {"lat": ring[0][1], "lon": ring[0][0]}
    return {"lat": cy / (6 * a), "lon": cx / (6 * a)}


def point_in_ring(p, ring):
    """faithful port of app pointInRing() (ray casting)."""
    lat = p["lat"]
    lon = p["lon"]
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def km_between(a, b):
    """faithful port of app kmBetween() (equirectangular approx)."""
    d_lat = (b["lat"] - a["lat"]) * 111.32
    d_lon = (b["lon"] - a["lon"]) * 111.32 * math.cos((a["lat"] + b["lat"]) * math.pi / 360.0)
    return math.sqrt(d_lat * d_lat + d_lon * d_lon)


# ---------------------------------------------------------------------------
# Coordinate generation (faithful port of buildProvinceSamplesV1/V2)
# ---------------------------------------------------------------------------
def _load_regions():
    with open(REGIONS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # app order: idx is the array index (order preserved)
    return data


def _load_geojson():
    with open(GEOJSON_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_province_samples_v1(geojson, regions, sigla_map):
    """faithful port of app buildProvinceSamplesV1()."""
    out = []
    for feat in geojson.get("features", []):
        sigla = (feat.get("properties") or {}).get("SIGLA") or ""
        pidx = sigla_map.get(sigla)
        if pidx is None:
            continue
        cap = regions[pidx]
        rings = rings_of(feat)
        max_ring = None
        max_area = 0.0
        for r in rings:
            if len(r) >= 4:
                ar = ring_area(r)
                if ar > max_area:
                    max_area = ar
                    max_ring = r
        km2 = max_area * 9250.0 if max_ring else 0.0
        if sup_sampling() > 0:
            target = sup_sampling()
        elif not max_ring:
            target = 1
        elif km2 < 1000:
            target = 1
        elif km2 < 2500:
            target = 2
        elif km2 < 6000:
            target = 3
        else:
            target = 4
        candidates = [{"lat": cap["lat"], "lon": cap["lon"]}]
        if max_ring:
            c = ring_centroid(max_ring)
            candidates.append(c)
            lats = [p[1] for p in max_ring]
            lons = [p[0] for p in max_ring]
            min_lat, max_lat = min(lats), max(lats)
            min_lon, max_lon = min(lons), max(lons)
            h = max_lat - min_lat
            w = max_lon - min_lon
            q = {"lat": min_lat + 0.75 * h, "lon": min_lon + 0.5 * w}
            if not point_in_ring(q, max_ring):
                q = {"lat": (cap["lat"] + c["lat"]) / 2.0, "lon": (cap["lon"] + c["lon"]) / 2.0}
            candidates.append(q)
            r4 = {"lat": min_lat + 0.25 * h, "lon": min_lon + 0.5 * w}
            if not point_in_ring(r4, max_ring):
                r4 = {"lat": (cap["lat"] + min_lat + 0.75 * h) / 2.0,
                      "lon": (cap["lon"] + min_lon + 0.5 * w) / 2.0}
            candidates.append(r4)
        seen = []
        MIN_DIST = 0.03
        for p in candidates:
            dup = any(abs(s["lat"] - p["lat"]) < MIN_DIST and abs(s["lon"] - p["lon"]) < MIN_DIST for s in seen)
            if not dup and 33 <= p["lat"] <= 60 and -20 <= p["lon"] <= 30:
                seen.append(p)
        for s in range(min(len(seen), target)):
            out.append({"index": len(out), "provinceIdx": pidx, "sigla": sigla,
                        "coordIdx": s, "lat": seen[s]["lat"], "lon": seen[s]["lon"]})
    provinces_with = {p["provinceIdx"] for p in out}
    for idx, r in enumerate(regions):
        if idx not in provinces_with:
            out.append({"index": len(out), "provinceIdx": idx, "sigla": r["sigla"],
                        "coordIdx": 0, "lat": r["lat"], "lon": r["lon"]})
    return out


def build_province_samples_v2(geojson, regions, sigla_map, budget):
    """faithful port of app buildProvinceSamplesV2()."""
    out = []
    if not geojson or not geojson.get("features"):
        return out
    provs = []
    for feat in geojson.get("features", []):
        sigla = (feat.get("properties") or {}).get("SIGLA") or ""
        pidx = sigla_map.get(sigla)
        if pidx is None:
            continue
        rings = rings_of(feat)
        max_ring = None
        max_area = 0.0
        for r in rings:
            if len(r) >= 4:
                ar = ring_area(r)
                if ar > max_area:
                    max_area = ar
                    max_ring = r
        km2 = max_area * 9250.0
        if sup_sampling() > 0:
            base = sup_sampling()
        elif not max_ring:
            base = 1
        elif km2 < 1000:
            base = 1
        elif km2 < 2500:
            base = 2
        elif km2 < 6000:
            base = 3
        else:
            base = 4
        cls = OROGRAPHY_CLASS.get(sigla, "M")
        factor = OROGRAPHY_FACTOR.get(cls, 1)
        cap = regions[pidx]
        provs.append({"pidx": pidx, "sigla": sigla, "cap": cap, "maxRing": max_ring,
                      "cls": cls, "base": base, "factor": factor, "placed": 0, "coordOrder": 1,
                      "target": 0, "frac": 0.0, "cands": []})
    w_sum = sum(p["base"] * p["factor"] for p in provs)
    scale = (budget / max(1.0, w_sum)) if budget > 0 else 1.0
    for p in provs:
        p["frac"] = p["base"] * p["factor"] * scale
        p["target"] = max(1, int(round(p["frac"])))
    diff = (budget if budget > 0 else len(provs)) - sum(p["target"] for p in provs)

    def sort_by_frac(desc):
        # Array.sort stabile dell'app: tie (frac-target) == ordine features geojson
        provs.sort(key=lambda p: (p["frac"] - p["target"]), reverse=desc)

    if diff > 0:
        sort_by_frac(True)
        for di in range(diff):
            provs[di % len(provs)]["target"] += 1
    elif diff < 0:
        sort_by_frac(False)
        need = -diff
        for p in provs:
            if need <= 0:
                break
            if p["target"] > 1:
                p["target"] -= 1
                need -= 1
    sort_by_frac(True)
    for p in provs:
        p["cands"] = []
        ring = p["maxRing"]
        if ring:
            c = ring_centroid(ring)
            if point_in_ring(c, ring):
                p["cands"].append({"lat": c["lat"], "lon": c["lon"]})
            lats = [q[1] for q in ring]
            lons = [q[0] for q in ring]
            mn_lat, mx_lat = min(lats), max(lats)
            mn_lon, mx_lon = min(lons), max(lons)
            for gx in range(1, 12, 2):
                for gy in range(1, 12, 2):
                    q2 = {"lat": mn_lat + (mx_lat - mn_lat) * gx / 12.0,
                          "lon": mn_lon + (mx_lon - mn_lon) * gy / 12.0}
                    if point_in_ring(q2, ring):
                        p["cands"].append(q2)
        if not p["cands"] and p["cap"]:
            p["cands"].append({"lat": p["cap"]["lat"], "lon": p["cap"]["lon"]})
    for p in provs:
        out.append({"index": len(out), "provinceIdx": p["pidx"], "sigla": p["sigla"],
                    "coordIdx": 0, "lat": p["cap"]["lat"], "lon": p["cap"]["lon"]})
        p["placed"] = 1
    for _pass in range(12):
        any_added = False
        for p in provs:
            if p["placed"] >= p["target"]:
                continue
            sp_min = OROGRAPHY_SPACING_KM.get(p["cls"], 26)
            best = None
            best_min = -1.0
            for cand in p["cands"]:
                m = float("inf")
                for k in range(len(out)):
                    dk = km_between(cand, out[k])
                    if dk < m:
                        m = dk
                if m > best_min:
                    best_min = m
                    best = cand
            if best is not None and best_min >= sp_min:
                out.append({"index": len(out), "provinceIdx": p["pidx"], "sigla": p["sigla"],
                            "coordIdx": p["coordOrder"], "lat": best["lat"], "lon": best["lon"]})
                p["coordOrder"] += 1
                p["placed"] += 1
                any_added = True
            else:
                p["target"] = p["placed"]
        if not any_added:
            break
    return out


_sampling_override = 0  # Sviluppo-only in the app; pipeline uses 0 (auto)


def sup_sampling():
    return _sampling_override


def set_sampling_override(v):
    global _sampling_override
    _sampling_override = int(v)


def generate_real_points():
    """Returns the list of REAL sample points the app would fetch (V2 default)."""
    regions = _load_regions()
    geojson = _load_geojson()
    sigla_map = {r["sigla"]: r["idx"] for r in regions}
    v1 = build_province_samples_v1(geojson, regions, sigla_map)
    v2 = build_province_samples_v2(geojson, regions, sigla_map, len(v1)) if v1 else []
    return v2 if v2 else v1


# ---------------------------------------------------------------------------
# Day collapse (faithful port of getHourOrDailyData(...,'all'))
# ---------------------------------------------------------------------------
def collapse_day(hourly, day_index):
    """Day-wide collapse for day_index (0..2), port of getHourOrDailyData(hourly, dayIndex, 'all')."""
    s = day_index * 24

    def avg(arr):
        vals = [x for x in arr[s:s + 24] if x is not None]
        return (sum(vals) / len(vals)) if vals else None

    def mx(arr):
        vals = [x for x in arr[s:s + 24] if x is not None]
        return max(vals) if vals else None

    def mode(arr):
        vals = [x for x in arr[s:s + 24] if x is not None]
        if not vals:
            return None
        freq = {}
        for v in vals:
            freq[v] = freq.get(v, 0) + 1
        best = None
        best_count = 0
        for k, v in freq.items():
            if v > best_count:
                best_count = v
                best = k
        return int(best) if best is not None else None

    def ssum(arr):
        vals = [x for x in arr[s:s + 24] if x is not None]
        return sum(vals) if vals else None

    return {
        "humidity": avg(hourly.get("relativehumidity_2m") or []),
        "pressure": avg(hourly.get("pressure_msl") or []),
        "prob": mx(hourly.get("precipitation_probability") or []),
        "wind": avg(hourly.get("windspeed_10m") or []),
        "wind100": mx(hourly.get("windspeed_100m") or []),
        "gusts": mx(hourly.get("windgusts_10m") or []),
        "code": mode(hourly.get("weathercode") or []),
        "precip": ssum(hourly.get("precipitation") or []),
        "showers": ssum(hourly.get("showers") or []),
        "cape": mx(hourly.get("cape") or []),
        "freezingLevel": mx(hourly.get("freezing_level_height") or []),
    }


# ---------------------------------------------------------------------------
# scorePointForProvince (faithful port) — worst (most convective) point
# ---------------------------------------------------------------------------
def score_point_for_province(rec):
    """Port of app scorePointForProvince(): CAPE max + storm/hail hour bonuses."""
    max_cape = 0.0
    storm_hours = 0
    hail_hours = 0
    capes = rec.get("hourly", {}).get("cape") or []
    codes = rec.get("hourly", {}).get("weathercode") or []
    for h in range(max(len(capes), len(codes))):
        cv = capes[h] if h < len(capes) else 0
        cv = cv or 0
        wc = codes[h] if h < len(codes) else 0
        wc = wc or 0
        if cv > max_cape:
            max_cape = cv
        if wc >= 95:
            storm_hours += 1
        if wc in (96, 99):
            hail_hours += 1
    return max_cape + storm_hours * 150 + hail_hours * 300


# ---------------------------------------------------------------------------
# Open-Meteo clients
# ---------------------------------------------------------------------------
def _http_json(url, timeout_s=OPENMETEO_TIMEOUT_S):
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME + "/centralized-pipeline (non-commercial)"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_transient_error(exc):
    """Errori TRANSIENTI (rettentabili): SSL/keepalive handshake timeout,
    TimeoutError, connessione resettata/chiusa, URLError di rete/TLS/DNS,
    HTTP 429 e HTTP 5xx. NON transienti (raise immediata): HTTP 4xx."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    if isinstance(exc, (TimeoutError, ssl.SSLError, ConnectionResetError,
                        ConnectionError, BrokenPipeError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return True
    return False


def _backoff_delay(base_s, attempt):
    """Backoff esponenziale con jitter (Parte H3)."""
    return base_s * (2 ** attempt) + random.uniform(0.0, RETRY_JITTER_MAX_S)


_last_metadata_attempts = 1


def metadata_attempts():
    """Tentativi usati dall'ultima get_model_metadata (telemetria guardrails)."""
    return max(1, int(_last_metadata_attempts))


def get_model_metadata(model_id):
    """Official Metadata API (NOT counted toward API limits). PARTE H:
    - H2: timeout esplicito connect/read (OPENMETEO_METADATA_TIMEOUT_S);
    - H1: retry automatici per errori transienti, al piu' METADATA_API_RETRIES
      tentativi totali (mai infiniti);
    - H3: exponential backoff + jitter tra i tentativi;
    Al termine dei retry alza RuntimeError esplicito (il chiamante NON aggiorna
    lo state: NETWORK ERROR != NO NEW RUN, requisito H4)."""
    global _last_metadata_attempts
    url = OPENMETEO_META_BASE.format(model=urllib.parse.quote(model_id))
    last = None
    for attempt in range(METADATA_API_RETRIES):
        _last_metadata_attempts = attempt + 1
        try:
            return _http_json(url, timeout_s=OPENMETEO_METADATA_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if not _is_transient_error(exc):
                raise  # errore NON transitorio (4xx/altro): propagare SUBITO
            if attempt + 1 >= METADATA_API_RETRIES:
                break
            delay = _backoff_delay(METADATA_RETRY_BASE_S, attempt)
            print("[common] Metadata API retry %d/%d in %.1fs: %s"
                  % (attempt + 1, METADATA_API_RETRIES, delay, exc))
            time.sleep(delay)
    raise RuntimeError("Metadata API unavailable after %d retries: %s"
                       % (METADATA_API_RETRIES, last))


# ---------------------------------------------------------------------------
# Best Match sentinel canary + fingerprint (1.0.0.8) + INITIAL BOOTSTRAP (G)
# ---------------------------------------------------------------------------
def best_match_sentinels():
    """6 capoluoghi-sentinella (aree orografiche/regime diverse). Coordinate
    IDENTICHE ai punti reali pubblicati (capoluogo, coordIdx 0 di regions_data):
    la fingerprint pubblicata e quella del canary si confrontano su stessa base."""
    regions = {r["sigla"]: r for r in _load_regions()}
    out = []
    for s in BEST_MATCH_SENTINEL_SIGLAS:
        r = regions.get(s)
        if r is None:
            continue
        out.append({"sigla": s, "area": BEST_MATCH_SENTINEL_AREAS.get(s, "?"),
                    "lat": r["lat"], "lon": r["lon"]})
    return out


def best_match_sentinel_payload(points_data, day0):
    """Estrae dal dataset derivato (o dal raw) i dati best_match SOLO dei
    capoluoghi-sentinella (coordIdx 0): base condivisa per la fingerprint."""
    by_sigla = {s: {"weathercode": [], "precipitation": []} for s in BEST_MATCH_SENTINEL_SIGLAS}
    sigla_set = set(BEST_MATCH_SENTINEL_SIGLAS)
    for p in (points_data or []):
        if not isinstance(p, dict) or p.get("coordIdx") != 0 or p.get("sigla") not in sigla_set:
            continue
        bm = (p.get("models") or {}).get("best_match") or {}
        h = bm.get("hourly") or {}
        by_sigla[p["sigla"]] = {
            "weathercode": list(h.get("weathercode") or []),
            "precipitation": list(h.get("precipitation") or []),
        }
    return {"day0": day0, "sentinels": by_sigla}


def fingerprint_best_match(payload):
    """SHA-256 deterministico sul payload sentinella (chiavi ordinate, JSON
    canonico). Indipendente da generation-time: riflette SOLO il contenuto."""

    def canon(v):
        if isinstance(v, dict):
            return {k: canon(v[k]) for k in sorted(v.keys())}
        if isinstance(v, list):
            return [canon(x) for x in v]
        return v

    blob = json.dumps(canon(payload), separators=(",", ":"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def is_bootstrap_pending():
    """PARTE G (1.0.0.8) — stato iniziale FIRST RUN / NO DATASET. Vero quando:
    state assente (no run ecmwf processato) O dataset live assente (data/latest)
    O fingerprint Best Match assente. MAI interpretato come 'no change' o
    'already processed': attiva l'INITIAL DATASET BOOTSTRAP (fetch reale)."""
    state = load_run_state()
    ecmwf = (state.get("last_model_runs") or {}).get("ecmwf_ifs") or {}
    bm = (state.get("last_model_runs") or {}).get("best_match") or {}
    has_processed = bool(state.get("last_processed_key")) and state.get("status") == "live"
    has_dataset = METADATA_JSON.exists() and POINTS_JSON.exists() and PROVINCES_JSON.exists()
    has_ecmwf_run = ecmwf.get("last_run_initialisation_time") is not None
    has_bm_fp = bool(bm.get("last_fingerprint"))
    return not (has_processed and has_dataset and has_ecmwf_run and has_bm_fp)


# ---------------------------------------------------------------------------
# API USAGE GUARDRAILS (data/state/api_usage.json) — OSSERVABILITA' + protezioni
# ---------------------------------------------------------------------------
_last_request_at = 0.0


def usage_day_key(dt=None):
    """Chiave giorno (UTC) per il conteggio del consumo API."""
    d = dt or _dt.datetime.now(_dt.timezone.utc)
    return d.strftime("%Y-%m-%d")


def load_usage_state():
    if API_USAGE_JSON.exists():
        try:
            st = json.loads(API_USAGE_JSON.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            st = {}
    else:
        st = {}
    st.setdefault("last_update", None)
    gr = st.setdefault("api_usage_guardrails", dict(API_GUARDRAILS_DEFAULT))
    for k, v in API_GUARDRAILS_DEFAULT.items():
        gr.setdefault(k, v)
    migrated = "daily_limit" in st
    if migrated:
        gr["daily_safety_ceiling"] = int(st.pop("daily_limit"))
    st.setdefault("days", {})
    if migrated:
        save_usage_state(st)
    return st


def save_usage_state(st):
    DATA_STATE.mkdir(parents=True, exist_ok=True)
    st["last_update"] = now_iso()
    with open(API_USAGE_JSON, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def guardrails():
    """Config guardrails attiva (dallo stato; default da API_GUARDRAILS_DEFAULT)."""
    return load_usage_state().get("api_usage_guardrails", dict(API_GUARDRAILS_DEFAULT))


def effective_budget():
    """Ceiling di sicurezza giornaliero (guardrails). Alias compatibile."""
    return int(guardrails().get("daily_safety_ceiling", API_DAILY_LIMIT))


def usage_today(day=None):
    key = day or usage_day_key()
    d = load_usage_state().get("days", {}).get(key, {})
    return {
        "requests": d.get("requests", 0),
        "successful": d.get("successful", 0),
        "failed": d.get("failed", 0),
        "checks": d.get("checks", 0),
        "canary_requests": d.get("canary_requests", 0),
        "forecast_requests": d.get("forecast_requests", 0),
        "retries": d.get("retries", 0),
        "batches": d.get("batches", 0),
        "locations": d.get("locations", 0),
        "bytes": d.get("bytes", 0),
    }


def available_today(day=None):
    """Richieste ancora sotto il ceiling (usata SOLO come guardia del canary)."""
    return max(0, effective_budget() - usage_today(day)["requests"])


def record_api_usage(requests=0, failed=0, successful=0, batches=0, locations=0,
                     bytes_=0, checks=0, canary=0, forecast=0, retries=0, day=None):
    """OSSERVABILITA' guardrails: contatori telemetrici giornalieri separati per
    tipo (checks leggeri Metadata, canary Best Match, fetch full forecast, retry,
    failed, successful). Registra e basta — NON blocca (il blocco è del solo
    hard ceiling in guard_planned_requests)."""
    st = load_usage_state()
    key = day or usage_day_key()
    d = st.setdefault("days", {}).setdefault(
        key, {"requests": 0, "successful": 0, "failed": 0, "checks": 0,
              "canary_requests": 0, "forecast_requests": 0, "retries": 0,
              "batches": 0, "locations": 0, "bytes": 0})
    d["requests"] += requests
    d["successful"] += successful
    d["failed"] += failed
    d["checks"] += checks
    d["canary_requests"] += canary
    d["forecast_requests"] += forecast
    d["retries"] += retries
    d["batches"] += batches
    d["locations"] += locations
    d["bytes"] += bytes_
    save_usage_state(st)


def guard_planned_requests(planned_requests):
    """PRE-FLIGHT GUARDRAIL (anti loop/retry infiniti/fetch duplicati/runaway/
    richieste massive accidentali): un piano parte se il tetto di sicurezza NON
    verrebbe superato (usate + pianificate <= ceiling). NON blocca per
    razionamento preventivo: consumi alti sotto il tetto producono solo WARN
    (osservabilità), il fetch reale prosegue. Schema ritornato (contratto
    stabile): {ok, planned, available, worst, reason}."""
    g = guardrails()
    used = usage_today()["requests"]
    ceiling = effective_budget()
    avail = max(0, ceiling - used)
    if not g.get("enabled", True):
        return {"ok": True, "planned": planned_requests, "available": avail,
                "worst": planned_requests * RETRY_LIMIT, "warned": False, "reason": None}
    projected = used + planned_requests
    frac = g.get("warn_threshold_fraction", 0.8) or 0.0
    warned = frac > 0.0 and used >= ceiling * frac
    if warned:
        print("[guardrails] OSSERVAZIONE (nessun blocco): usate oggi=%d >= %.0f%% "
              "del ceiling %d." % (used, frac * 100, ceiling))
    blocked = g.get("hard_stop_enabled", True) and projected > ceiling
    reason = None if not blocked else (
        "HARD SAFETY CEILING: usate oggi=%d, richieste pianificate=%d, tetto=%d. "
        "Safe skip (niente fetch oltre il tetto); retry al ciclo successivo." % (
            used, planned_requests, ceiling))
    return {"ok": not blocked, "planned": planned_requests, "available": avail,
            "worst": planned_requests * RETRY_LIMIT, "warned": warned, "reason": reason}


def ensure_api_budget(planned_requests):
    """Alias retro-compatibile di guard_planned_requests (contratto invariato)."""
    return guard_planned_requests(planned_requests)


def unique_coordinates(points, digits=4):
    """Dedup delle coordinate reali (round ~11 m). Ritorna liste
    [lat, lon, [punti indices]] ordinati per primo indice (ordine dell'app)."""
    out = []
    seen = {}
    for p in sorted(points, key=lambda x: x["index"]):
        key = (round(p["lat"], digits), round(p["lon"], digits))
        if key in seen:
            seen[key][2].append(p["index"])
        else:
            e = [key[0], key[1], [p["index"]]]
            seen[key] = e
            out.append(e)
    return out


def _pace_next_request():
    """Spaziatura minima tra richieste (anti minutely-limit)."""
    global _last_request_at
    now = time.monotonic()
    wait = (_last_request_at + API_MIN_REQUEST_INTERVAL_S) - now
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def fetch_source_batch(lats, lons, models=DUAL_MODELS, forecast_days=FORECAST_DAYS):
    """One batched multi-location request (<=BATCH_MAX_LOCATIONS locations),
    pace-limited, with LIMITED exponential backoff. Only THIS batch is retried.

    Returns the parsed response. Raises RuntimeError with the API reason on
    hard failures (e.g. 429 after retries, 5xx after retries)."""
    params = {
        "latitude": ",".join(str(x) for x in lats),
        "longitude": ",".join(str(x) for x in lons),
        "hourly": HOURLY_PARAMS,
        "daily": DAILY_PARAMS,
        "timezone": "Europe/Rome",
        "forecast_days": str(forecast_days),
    }
    if models:
        params["models"] = models
    url = OPENMETEO_BASE + "?" + urllib.parse.urlencode(params)
    last_err = None
    delay = 0.0
    for attempt in range(RETRY_LIMIT):
        if delay > 0:
            time.sleep(delay)
        _pace_next_request()
        try:
            data = _http_json(url)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError("Open-Meteo API error: " + str(data.get("reason")))
            return data
        except urllib.error.HTTPError as exc:  # HTTP status error
            last_err = exc
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                pass
            msg = body.decode("utf-8", "replace")
            if exc.code == 429 or ("limit" in msg.lower() and "minutely" in msg.lower()):
                delay = 60.0  # Open-Meteo: "try again in one minute"
            elif exc.code >= 500:
                delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
            else:
                raise
        except RuntimeError as exc:  # in-body API error (transient possible)
            last_err = exc
            delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
        except Exception as exc:  # noqa: BLE001  (network/JSON/timeout)
            last_err = exc
            delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
    raise RuntimeError("Open-Meteo source fetch failed after %d tentativi: %s" % (RETRY_LIMIT, last_err))


def fetch_best_match_check(lats, lons):
    """Canary LEGGERO Best Match: UNA richiesta multi-location (sentinelle) con
    SOLO weathercode+precipitation orarie. Piccolo volume, stesso client con
    pacing e backoff limitato. Conteggiata 1 richiesta forecast nel ceiling."""
    params = {
        "latitude": ",".join(str(x) for x in lats),
        "longitude": ",".join(str(x) for x in lons),
        "hourly": BEST_MATCH_CHECK_HOURLY,
        "timezone": TIMEZONE,
        "forecast_days": str(FORECAST_DAYS),
        "models": "best_match",
    }
    url = OPENMETEO_BASE + "?" + urllib.parse.urlencode(params)
    last_err = None
    delay = 0.0
    for attempt in range(RETRY_LIMIT):
        if delay > 0:
            time.sleep(delay)
        _pace_next_request()
        try:
            data = _http_json(url)
            if isinstance(data, dict) and data.get("error"):
                raise RuntimeError("Open-Meteo API error: " + str(data.get("reason")))
            return data
        except urllib.error.HTTPError as exc:  # HTTP status error
            last_err = exc
            body = b""
            try:
                body = exc.read()
            except Exception:  # noqa: BLE001
                pass
            msg = body.decode("utf-8", "replace")
            if exc.code == 429 or ("limit" in msg.lower() and "minutely" in msg.lower()):
                delay = 60.0  # Open-Meteo: "try again in one minute"
            elif exc.code >= 500:
                delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
            else:
                raise
        except RuntimeError as exc:  # in-body API error (transient possible)
            last_err = exc
            delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
        except Exception as exc:  # noqa: BLE001  (network/JSON/timeout)
            last_err = exc
            delay = _backoff_delay(RETRY_BACKOFF_BASE_S, attempt)
    raise RuntimeError("Best Match sentinel check failed after %d tentativi: %s" % (RETRY_LIMIT, last_err))


def split_dual_response_element(el):
    """Extract per-model records {daily, hourly, elevation} from one response element.

    Element shapes supported:
      - element has 'best_match'/'ecmwf_ifs' sub-objects (dual, per location)
      - element has daily/hourly directly (single model)
    Returns dict model_id -> {'daily':..., 'hourly':..., 'elevation':...}
    """
    out = {}
    models = ["best_match", "ecmwf_ifs"]
    if el.get(models[0]) or el.get(models[1]):
        for m in models:
            sub = el.get(m)
            if sub and sub.get("daily") and sub.get("hourly"):
                out[m] = {
                    "daily": sub["daily"],
                    "hourly": sub["hourly"],
                    "elevation": sub.get("elevation"),
                }
    elif el.get("daily") and el.get("hourly"):
        out["best_match"] = {"daily": el["daily"], "hourly": el["hourly"],
                             "elevation": el.get("elevation")}
    return out


def response_locations(data):
    """Multiple locations -> list of elements; single location -> [element]."""
    if isinstance(data, list):
        return data
    return [data]


# ---------------------------------------------------------------------------
# Run state
# ---------------------------------------------------------------------------
def load_run_state():
    if LAST_MODEL_RUN.exists():
        try:
            return json.loads(LAST_MODEL_RUN.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_run_state(state):
    DATA_STATE.mkdir(parents=True, exist_ok=True)
    with open(LAST_MODEL_RUN, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def unix_to_iso_utc(ts):
    if ts is None:
        return None
    try:
        return _dt.datetime.fromtimestamp(int(ts), _dt.timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return None


def mkdirs(*paths):
    for p in paths:
        Path(p).mkdir(parents=True, exist_ok=True)


def r2(x, nd=2):
    """Round helper; keep ints int when the source value is integral."""
    if x is None:
        return x
    try:
        if isinstance(x, int):
            return x
        r = round(x, nd)
        if r == int(r):
            return int(r)
        return r
    except Exception:  # noqa: BLE001
        return x