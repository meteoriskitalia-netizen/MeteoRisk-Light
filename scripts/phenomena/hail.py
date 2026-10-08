#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — hail.py: logica GRANDINE (HAIL).

Tier (THRESHOLDS_VERSION in phenomena/__init__.py):
  SUSPECT       — max_dbz >= 55 in >= 2 punti della finestra recente (<=15 min)
                  su tracks.json: persistenza REALE, mai uno scan isolato;
                  per i target senza punti per-frame (es. storm_object) la
                  persistenza e' duration_min >= 10 (>= 2 scan da 5 min) +
                  on_latest_frame.
  CORROBORATED  — (max_dbz >= 60, oppure >= 55 con trend dBZ positivo) E
                  gate fulmini provider-agnostico (lightning_corroborates:
                  strength/soglia della sorgente, fallback count_near per DPC)
                  OPPURE trend fulmini in salita. Se il provider fulmini e'
                  unavailable -> RESTA SUSPECT (nessuna promozione senza
                  evidenza).
  VERIFIED      — NON popolato: arrivera' solo con report esterni (ESWD);
                  nessun report e' inventato qui.

H0 (freezing level, Open-Meteo) entra SOLO come evidence/label addizionale:
se manca o la rete e' spenta il tier NON cambia (non blocca mai).

Sorgente non puntuale (MLI/AFA): evidence.lightning.note = "AFA proxy (no
flash puntuali)" + attribution EUMETSAT, e count_near e' in PIXEL (label
esplicita), mai presentato come numero di fulmini.

Osservazione in ingresso (dict normalizzato, costruito da engine.build_observations):
  {"anchor": "cell-3", "type": "cell", "position": [lon, lat],
   "first_seen": iso, "last_seen": iso, "persistence_source": "points"|"duration",
   "recent_points": [{"timestamp_ms", "lonlat", "max_dbz"}], "max_dbz": float|None,
   "duration_min": float|None, "on_latest_frame": bool, "organization_score": int|None,
   "eccentricity_max"|"solidity_min"|"compactness_max": float|None,
   "classification": str|None, "n_frames": int|None}
"""

import json
import math
import urllib.error
import urllib.request

from radar_engine.phase2 import environment as _env

from . import LIGHTNING_SOURCE_THRESHOLDS, THRESHOLDS_VERSION
from .lightning import lightning_corroborates, lightning_strength

HAIL_DBZ_SUSPECT = 55.0        # gate intensita' (dBZ)
HAIL_DBZ_CORROBORATED = 60.0   # gate intensita' forte (senza fulmini non basta)
HAIL_WINDOW_MIN = 15           # finestra di persistenza recente (minuti)
HAIL_MIN_POINTS = 2            # punti >= soglia richiesti per SUSPECT
HAIL_MIN_DURATION_MIN = 10.0   # persistenza minima per target senza punti
# Gate fulmini DPC legacy (oggi in LIGHTNING_SOURCE_THRESHOLDS["dpc"]):
HAIL_LTG_MIN_STRIKES = LIGHTNING_SOURCE_THRESHOLDS["dpc"]["count_min"]
FZL_LOW_M = 3000.0             # H0 sotto questa quota rafforza l'evidenza
FZL_URL = "https://api.open-meteo.com/v1/forecast"
FZL_HTTP_TIMEOUT_S = 15


def _num(value):
    """float finite oppure None (mai NaN/inf nel payload)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def recent_points(points, radar_timestamp_ms, window_min=HAIL_WINDOW_MIN):
    """Punti dentro la finestra recente [radar_ts - window_min, radar_ts].

    Normalizza in {"timestamp_ms", "lonlat", "max_dbz"}; punti senza
    timestamp_ms valido o FUTURI rispetto al radar sono scartati (nessun
    punto inventato). radar_timestamp_ms None -> lista vuota."""
    if not points or radar_timestamp_ms is None:
        return []
    try:
        ref = int(radar_timestamp_ms)
        span = int(window_min * 60000)
    except (TypeError, ValueError):
        return []
    out = []
    for p in points:
        if not isinstance(p, dict):
            continue
        try:
            ts = int(p.get("timestamp_ms"))
        except (TypeError, ValueError):
            continue
        if ts > ref or ref - ts > span:
            continue
        out.append({"timestamp_ms": ts,
                    "lonlat": list(p.get("lonlat") or []),
                    "max_dbz": _num(p.get("max_dbz"))})
    return out


def hail_score(state, max_dbz, lightning, freezing_level_m):
    """Score 0-100: base dal tier + contributi (dBZ, fulmini, H0 basso).

    La somma dei contributi e' limitata a 29 punti -> SUSPECT <= 69,
    CORROBORATED <= 99, VERIFIED = 100 (mai prodotto in questo step)."""
    if state == "VERIFIED":
        return 100.0
    base = {"SUSPECT": 40.0, "CORROBORATED": 70.0}.get(state, 40.0)
    dbz = _num(max_dbz)
    dbz_t = _clamp01((dbz - HAIL_DBZ_SUSPECT) / 15.0) if dbz else 0.0
    ltg = lightning if isinstance(lightning, dict) else {}
    ltg_t = lightning_strength(ltg)        # strength 0..1, fallback count/30
    fzl = _num(freezing_level_m)
    fzl_t = 1.0 if (fzl is not None and fzl < FZL_LOW_M) else 0.0
    raw = base + 29.0 * (0.50 * dbz_t + 0.35 * ltg_t + 0.15 * fzl_t)
    return round(min(99.9, raw), 1)


def evaluate_hail(obs, lightning=None, freezing_level_m=None):
    """Valuta HAIL su un'osservazione normalizzata -> evento dict oppure None."""
    if not isinstance(obs, dict) or not obs.get("anchor"):
        return None
    source = obs.get("persistence_source") or "points"
    pts = [p for p in (obs.get("recent_points") or []) if isinstance(p, dict)]
    dbz_vals = [v for v in (_num(p.get("max_dbz")) for p in pts)
                if v is not None]
    duration = _num(obs.get("duration_min"))
    if source == "duration":
        max_dbz = _num(obs.get("max_dbz"))
        n_above = 1 if (max_dbz is not None
                        and max_dbz >= HAIL_DBZ_SUSPECT) else 0
        persistent = bool(obs.get("on_latest_frame")) \
            and duration is not None and duration >= HAIL_MIN_DURATION_MIN
        n_recent = len(pts)
    else:
        max_dbz = max(dbz_vals) if dbz_vals else None
        n_above = sum(1 for v in dbz_vals if v >= HAIL_DBZ_SUSPECT)
        persistent = n_above >= HAIL_MIN_POINTS
        n_recent = len(pts)
    if max_dbz is None or max_dbz < HAIL_DBZ_SUSPECT or not persistent:
        return None

    dbz_trend = None
    if len(dbz_vals) >= 2:
        dbz_trend = (dbz_vals[-1] - dbz_vals[0]) / (len(dbz_vals) - 1)
    strong = max_dbz >= HAIL_DBZ_CORROBORATED or (
        max_dbz >= HAIL_DBZ_SUSPECT and (dbz_trend or 0.0) > 0.0)

    ltg = lightning if isinstance(lightning, dict) else {"available": False}
    ltg_avail = bool(ltg.get("available"))
    count_near = int(ltg.get("count_near") or 0) if ltg_avail else 0
    strength = lightning_strength(ltg) if ltg_avail else None
    trend_dir = None
    if ltg_avail and isinstance(ltg.get("trend"), dict):
        trend_dir = ltg["trend"].get("direction")
    ltg_ok = lightning_corroborates(ltg) or (ltg_avail and trend_dir == "up")
    state = "CORROBORATED" if (strong and ltg_ok) else "SUSPECT"

    fzl = _num(freezing_level_m)
    fzl_low = None if fzl is None else bool(fzl < FZL_LOW_M)
    evidence = {
        "anchor": obs.get("anchor"),
        "track_type": obs.get("type"),
        "classification": obs.get("classification"),
        "persistence_source": source,
        "window_minutes": HAIL_WINDOW_MIN,
        "recent_points": n_recent,
        "points_ge_55": n_above,
        "max_dbz": round(max_dbz, 1),
        "dbz_trend_per_frame": round(dbz_trend, 2) if dbz_trend is not None
        else None,
        "duration_min": duration,
        "on_latest_frame": bool(obs.get("on_latest_frame")),
        "lightning": {
            "available": ltg_avail,
            "source": ltg.get("source"),
            "count_near": count_near,
            "count_total": int(ltg.get("count_total") or 0),
            "radius_km": ltg.get("radius_km"),
            "direction": trend_dir,
            "score": ltg.get("score"),
            "reason": ltg.get("reason"),
            "strength": strength,
            "note": ltg.get("note"),
            "attribution": ltg.get("attribution"),
        },
        "freezing_level_m": fzl,
        "freezing_level_low": fzl_low,
        "labels": [],
    }

    labels = [f"tier {state}"]
    if source == "duration":
        labels.append(
            f"max_dbz {max_dbz:.1f} >= {HAIL_DBZ_SUSPECT:.0f} dBZ, "
            f"persistenza {duration:.0f} min (>= {HAIL_MIN_DURATION_MIN:.0f})")
    else:
        labels.append(
            f"max_dbz {max_dbz:.1f} >= {HAIL_DBZ_SUSPECT:.0f} dBZ su "
            f"{n_above}/{n_recent} punti (finestra {HAIL_WINDOW_MIN} min)")
    if ltg_avail:
        radius = ltg.get("radius_km")
        note = ltg.get("note")
        if note:
            # sorgente non puntuale (MLI): count_near e' in PIXEL, mai
            # presentato come numero di fulmini
            labels.append(f"{count_near} px AFA nel raggio {radius} km "
                          f"(trend {trend_dir}) — {note}")
        else:
            labels.append(f"fulmini {count_near} in {radius} km "
                          f"(trend {trend_dir})")
        if ltg.get("attribution"):
            labels.append(str(ltg["attribution"]))
    else:
        labels.append("fulmini non disponibili")
    if fzl_low:
        labels.append(f"H0 {fzl:.0f} m < {FZL_LOW_M:.0f} m (rafforza)")
    evidence["labels"] = labels

    return {
        "id": f"HAIL-{obs['anchor']}",
        "type": "HAIL",
        "state": state,
        "score": hail_score(state, max_dbz, ltg, fzl),
        "first_seen": obs.get("first_seen"),
        "last_seen": obs.get("last_seen"),
        "position": list(obs.get("position") or []),
        "evidence": evidence,
        "thresholds_version": THRESHOLDS_VERSION,
    }


# ---------------------------------------------------------------------------
# H0 (freezing level) — evidenza addizionale, MAI bloccante
# ---------------------------------------------------------------------------
def build_fzl_url(lat, lon):
    """URL Open-Meteo minimale per freezing_level_height (pattern environment.py)."""
    return (f"{FZL_URL}?latitude={float(lat):.5f}&longitude={float(lon):.5f}"
            f"&hourly=freezing_level_height&timezone=GMT&forecast_days=1")


def _http_json(url, timeout_s):
    """GET JSON via urllib (stessa pattern di environment._http_json)."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


def _call_client(url, client, timeout_s):
    """Client iniettabile (callable / oggetto con get_json|fetch_json|get)."""
    if client is None:
        return _http_json(url, timeout_s)
    if callable(client):
        return client(url)
    for attr in ("get_json", "fetch_json", "get"):
        fn = getattr(client, attr, None)
        if callable(fn):
            return fn(url)
    raise ValueError("unsupported_client")


def fetch_freezing_level(lat, lon, client=None, timeout_s=None):
    """H0 (freezing_level_height) per il punto -> metri, oppure None.

    NON solleva MAI: rete assente, risposta invalida o client rotto -> None
    (l'evidenza H0 e' opzionale per definizione). `client` iniettabile per i
    test (callable url -> dict)."""
    try:
        url = build_fzl_url(lat, lon)
        to = FZL_HTTP_TIMEOUT_S if timeout_s is None else float(timeout_s)
        payload = _call_client(url, client, to)
        hourly = (payload or {}).get("hourly") or {}
        times = hourly.get("time") or []
        seq = hourly.get("freezing_level_height") or []
        idx = _env.pick_current_index(times)
        if idx is None or idx >= len(seq):
            return None
        return _num(seq[idx])
    except Exception:
        return None
