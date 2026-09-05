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
import json
import math
import os
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
# spaziatura, ampiamente sotto la soglia e sotto il budget giornaliero.
API_MIN_REQUEST_INTERVAL_S = float(os.environ.get("METEO_RISK_API_MIN_INTERVAL_S", "30.0"))
# Budget giornaliero API forecast (free tier Open-Meteo 10.000/giorno, default
# conservativo e configurabile). La Metadata API (run detection) NON è conteggiata.
API_DAILY_LIMIT = int(os.environ.get("METEO_RISK_API_DAILY_LIMIT", "10000"))
# Riserva di sicurezza: frazione del limite mai consumata dal piano ordinario,
# copre retry selettivi e slittamenti di meteo dati. Default 10%.
API_SAFETY_RESERVE_FRAC = float(os.environ.get("METEO_RISK_SAFETY_RESERVE_FRAC", "0.1"))
# Retry: LIMITATI, esponenziali e selettivi. Al termine del primo giro vengono
# ritentati SOLO i batch falliti (mai una ripetizione integrale del piano).
RETRY_LIMIT = int(os.environ.get("METEO_RISK_RETRY_LIMIT", "3"))
RETRY_BACKOFF_BASE_S = float(os.environ.get("METEO_RISK_RETRY_BACKOFF_BASE_S", "5.0"))
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
DUAL_MODELS = "best_match,ecmwf_ifs"

HOURLY_FIELDS = HOURLY_PARAMS.split(",")
DAILY_FIELDS = DAILY_PARAMS.split(",")

# Models consumed by the app (source identifiers used for run detection).
# best_match is a COMPOSITE (no metadata endpoint): its Italian leading segment
# for 3-day forecasts is ARPAE ICON-2I -> used as the run driver, together with
# ECMWF IFS for the dual second leg.
MODEL_RUN_DRIVER_MAP = [
    # (app model id, metadata model id for run detection)
    ("italia_meteo_arpae_icon_2i", "italia_meteo_arpae_icon_2i"),  # best_match driver
    ("ecmwf_ifs", "ecmwf_ifs"),
]
METADATA_MODELS_TRACKED = ["italia_meteo_arpae_icon_2i", "ecmwf_ifs"]

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


def get_model_metadata(model_id):
    """Official Metadata API (not counted toward API limits)."""
    url = OPENMETEO_META_BASE.format(model=urllib.parse.quote(model_id))
    return _http_json(url)


# ---------------------------------------------------------------------------
# API budget management (data/state/api_usage.json) — consumo giornaliero
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
    st.setdefault("daily_limit", API_DAILY_LIMIT)
    st.setdefault("safety_reserve_fraction", API_SAFETY_RESERVE_FRAC)
    st.setdefault("days", {})
    return st


def save_usage_state(st):
    DATA_STATE.mkdir(parents=True, exist_ok=True)
    st["last_update"] = now_iso()
    with open(API_USAGE_JSON, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


def effective_budget():
    """Budget effettivamente pianificabile: limite giornaliero meno la riserva."""
    return int(API_DAILY_LIMIT * (1.0 - API_SAFETY_RESERVE_FRAC))


def usage_today(day=None):
    key = day or usage_day_key()
    d = load_usage_state().get("days", {}).get(key, {})
    return {
        "requests": d.get("requests", 0),
        "failed": d.get("failed", 0),
        "batches": d.get("batches", 0),
        "locations": d.get("locations", 0),
        "bytes": d.get("bytes", 0),
    }


def available_today(day=None):
    return max(0, effective_budget() - usage_today(day)["requests"])


def record_api_usage(requests=1, failed=0, batches=0, locations=0, bytes_=0, day=None):
    st = load_usage_state()
    key = day or usage_day_key()
    d = st.setdefault("days", {}).setdefault(
        key, {"requests": 0, "failed": 0, "batches": 0, "locations": 0, "bytes": 0})
    d["requests"] += requests
    d["failed"] += failed
    d["batches"] += batches
    d["locations"] += locations
    d["bytes"] += bytes_
    save_usage_state(st)


def ensure_api_budget(planned_requests):
    """PRE-FLIGHT: il piano ordinario (senza retry) deve stare sotto il budget
    effettivo del giorno. Restituisce {ok, planned, available, worst, reason}.
    NB: il worst-case (planned*RETRY_LIMIT) può eccedere il budget effettivo ma
    resta coperto dalla riserva di sicurezza: il blocco scatta SOLO se il piano
    ordinario non rientra (niente avvio di un fetch destinato a fallire)."""
    avail = available_today()
    worst = planned_requests * RETRY_LIMIT
    ok = avail >= planned_requests
    return {
        "ok": ok,
        "planned": planned_requests,
        "available": avail,
        "worst": worst,
        "reason": None if ok else (
            "budget effettivo insufficiente: piani=%d, disponibili oggi=%d (limite=%d, riserva=%d%%). "
            "Attendere domani o alzare METEO_RISK_API_DAILY_LIMIT." % (
                planned_requests, avail, API_DAILY_LIMIT, int(API_SAFETY_RESERVE_FRAC * 100))),
    }


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
                delay = RETRY_BACKOFF_BASE_S * (2 ** attempt)
            else:
                raise
        except RuntimeError as exc:  # in-body API error (transient possible)
            last_err = exc
            delay = RETRY_BACKOFF_BASE_S * (2 ** attempt)
        except Exception as exc:  # noqa: BLE001  (network/JSON/timeout)
            last_err = exc
            delay = RETRY_BACKOFF_BASE_S * (2 ** attempt)
    raise RuntimeError("Open-Meteo source fetch failed after %d tentativi: %s" % (RETRY_LIMIT, last_err))


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