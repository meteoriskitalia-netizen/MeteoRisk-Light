#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — forecast.py (tracking 2h + nowcast, PARTE 3)

Forecast cinematico <=2h per i candidati supercell: estrapolazione lineare
PESSATA dai centroidi della track associata (TITAN/SCIT-style, NO fit
quadratico), proiezione haversine (riuso di tracking.dest_from), cono di
incertezza che cresce nel tempo e intensita' che decade esponenzialmente
verso la media regionale con gate sul trend recente (dBZ/scansione clampato).

Layer ADDITIVO ed ENGINE-ONLY: la chiave `forecast` viene aggiunta ai dict
candidato di supercells.json. Candidato non proiettabile (non sull'ultimo
frame, basis < 2 punti, qualsiasi errore) -> forecast = None: nessun errore
del modulo fa fallire il run.
"""

import datetime as _dt
import math

from . import models
from .tracking import bearing_deg, dest_from, haversine_km

HORIZON_MIN = 120
STEP_MIN = 30
OFFSETS_MIN = (30, 60, 90, 120)
METHOD = "linear_weighted_haversine_decay"

_ALPHA = 0.5                  # peso 0.5^(step di distanza dall'ultimo punto)
_BASIS_MAX = 8                # ultimi N punti della track come basis
_FIT_MIN_POINTS = 4           # >=4 punti -> fit lineare, altrimenti vector-avg
_OUTLIER_SPEED_KMH = 50.0     # segmento > 50 km/h equiv. -> candidato outlier
_ENDPOINT_MAD_K = 2.5         # soglia estremi: max(50, k*mediana, mediana+k*MAD)
_ENDPOINT_MIN_KEEP = 5        # potatura estremi solo se restano >=4 punti
_CONE_MIN_KM = 6.0            # floor del cono di incertezza
_CONE_DEFAULT_KM = 8.0        # cone_0 senza fit (residui non calcolabili)
_CONE_GROWTH_KM_MIN = 0.08    # ~4.8 km/h di espansione del cono
_YOUNG_MINUTES = 20.0         # durata < 20 min -> cella giovane
_INTENSITY_LAMBDA_MIN = 30.0  # tau del decadimento esponenziale dBZ
_TREND_SLOPE_MIN = -2.5       # dBZ/scansione, gate trend decadimento
_TREND_SLOPE_MAX = 1.5        # dBZ/scansione, gate trend crescita
_DBZ_RANGE = (20.0, 75.0)     # clamp intensita' proiettata
_MEAN_DBZ_RANGE = (10.0, 60.0)
_SPEED_RANGE_KMH = (1.0, 110.0)  # velocita' di fit plausibile
_REGION_DEFAULT_DBZ = 30.0    # media regionale se nessun punto nel bundle
_KM_PER_DEG = 111.1949        # km per grado (sfera media IUGG)
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _iso_from_ms(ms):
    """Epoch ms -> ISO UTC '...Z'."""
    return _dt.datetime.fromtimestamp(ms / 1000.0, _dt.timezone.utc).strftime(_ISO_FMT)


def _ms_from_iso(iso):
    """ISO UTC '...Z' -> epoch ms (None se non interpretabile)."""
    try:
        dt = _dt.datetime.strptime(iso, _ISO_FMT).replace(tzinfo=_dt.timezone.utc)
    except (TypeError, ValueError):
        return None
    return int(dt.timestamp() * 1000.0)


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _steps_weights(n):
    """Pesi 0.5^(distanza in step dall'ultimo punto), basis ordinata per tempo."""
    return [_ALPHA ** (n - 1 - i) for i in range(n)]


def _weighted_fit(t_min, values, weights):
    """Regressione lineare pesata y = a + b*t. Ritorna (a, b)."""
    sw = sum(weights)
    if sw <= 0.0:
        return sum(values) / max(len(values), 1), 0.0
    t_bar = sum(w * t for w, t in zip(weights, t_min)) / sw
    y_bar = sum(w * y for w, y in zip(weights, values)) / sw
    sxx = sum(w * (t - t_bar) ** 2 for w, t in zip(weights, t_min))
    sxy = sum(w * (t - t_bar) * (y - y_bar)
              for w, t, y in zip(weights, t_min, values))
    b = sxy / sxx if sxx > 1e-9 else 0.0
    return y_bar - b * t_bar, b


def _median(values):
    """Mediana robusta (0.0 su input vuoto)."""
    ordered = sorted(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _remove_outliers(points):
    """Scarta i punti-spike dalla basis (interni e agli estremi).

    Punto interno: outlier se ENTRAMBI i segmenti adiacenti superano i 50
    km/h equivalenti E rimuovendolo il percorso si accorcia (salto isolato,
    non traiettoria realmente veloce; per un moto rettilineo il chord ~=
    somma dei segmenti e il punto resta anche a >50 km/h). Estremi: il
    primo/ultimo punto viene scartato se la sua velocita' di segmento supera
    la soglia robusta max(_OUTLIER_SPEED_KMH, k*mediana,
    mediana + k*MAD) stimata sui segmenti correnti, cosi' un singolo spike
    di coda (o di testa) non trascina fit e fallback. La potatura degli
    estremi parte solo se restano >= _ENDPOINT_MIN_KEEP punti (protegge le
    tracce brevi); l'invariante assoluta e' comunque >= 2 punti."""
    keep = list(points)
    removed = 0
    i = 1
    while i < len(keep) - 1:
        p0, p1, p2 = keep[i - 1], keep[i], keep[i + 1]
        d_in = haversine_km(p0.lonlat, p1.lonlat)
        d_out = haversine_km(p1.lonlat, p2.lonlat)
        dt_in = (p1.timestamp_ms - p0.timestamp_ms) / 60000.0
        dt_out = (p2.timestamp_ms - p1.timestamp_ms) / 60000.0
        s_in = d_in / dt_in * 60.0 if dt_in > 0.0 else math.inf
        s_out = d_out / dt_out * 60.0 if dt_out > 0.0 else math.inf
        if s_in > _OUTLIER_SPEED_KMH and s_out > _OUTLIER_SPEED_KMH:
            d_short = haversine_km(p0.lonlat, p2.lonlat)
            if d_short < 0.5 * (d_in + d_out):
                del keep[i]
                removed += 1
                continue
        i += 1
    if len(keep) >= _ENDPOINT_MIN_KEEP:
        speeds = []
        for a, b in zip(keep, keep[1:]):
            dt = (b.timestamp_ms - a.timestamp_ms) / 60000.0
            d = haversine_km(a.lonlat, b.lonlat)
            speeds.append(d / dt * 60.0 if dt > 0.0 else math.inf)
        med = _median(speeds)
        mad = _median([abs(s - med) for s in speeds])
        thr = max(_OUTLIER_SPEED_KMH, _ENDPOINT_MAD_K * med,
                  med + _ENDPOINT_MAD_K * mad)
        if speeds[-1] > thr:
            del keep[-1]
            removed += 1
        if len(keep) >= _ENDPOINT_MIN_KEEP:
            dt0 = (keep[1].timestamp_ms - keep[0].timestamp_ms) / 60000.0
            d0 = haversine_km(keep[0].lonlat, keep[1].lonlat)
            s0 = d0 / dt0 * 60.0 if dt0 > 0.0 else math.inf
            if s0 > thr:
                del keep[0]
                removed += 1
    return keep, removed


def fit_motion(points):
    """Fit cinematico pesato sui centroidi della track.

    Basis = ultimi min(8, n) punti ordinati per tempo meno gli outlier
    (interni ed estremi). Con >=4 punti: regressione lineare separata su
    lon/lat vs tempo (pesi 0.5^step); con meno punti o velocita' fuori range:
    fallback sulla media vettoriale dell'ultimo segmento, con velocita'
    clampata in _SPEED_RANGE_KMH (clamped=True). Ritorna dict con
    basis_frames, n_outliers, fit_used, clamped, velocity_kmh,
    velocity_km_min, direction_toward_deg, resid_std_km, cone0_km."""
    pts = sorted(points, key=lambda p: p.timestamp_ms)
    basis, n_outliers = _remove_outliers(pts[-_BASIS_MAX:])
    n = len(basis)
    if n < 2:
        return {
            "basis_frames": n,
            "n_outliers": n_outliers,
            "fit_used": False,
            "clamped": False,
            "velocity_kmh": 0.0,
            "velocity_km_min": 0.0,
            "direction_toward_deg": 0.0,
            "resid_std_km": _CONE_DEFAULT_KM,
            "cone0_km": _CONE_DEFAULT_KM,
        }

    last_ms = basis[-1].timestamp_ms
    t_min = [(p.timestamp_ms - last_ms) / 60000.0 for p in basis]
    lons = [p.lonlat[0] for p in basis]
    lats = [p.lonlat[1] for p in basis]
    weights = _steps_weights(n)

    fit_used = False
    clamped = False
    vel_km_min = 0.0
    vel_kmh = 0.0
    brg = 0.0
    resid_km = _CONE_DEFAULT_KM
    lo, hi = _SPEED_RANGE_KMH
    if n >= _FIT_MIN_POINTS:
        a_lon, b_lon = _weighted_fit(t_min, lons, weights)
        a_lat, b_lat = _weighted_fit(t_min, lats, weights)
        lat_ref = math.radians(sum(lats) / n)
        vx = b_lon * _KM_PER_DEG * math.cos(lat_ref)   # km/min verso est
        vy = b_lat * _KM_PER_DEG                        # km/min verso nord
        cand_km_min = math.hypot(vx, vy)
        if lo <= cand_km_min * 60.0 <= hi:
            fit_used = True
            vel_km_min = cand_km_min
            vel_kmh = cand_km_min * 60.0
            brg = math.degrees(math.atan2(vx, vy)) % 360.0
            sw = sum(weights)
            acc = 0.0
            for idx, p in enumerate(basis):
                fitted = (a_lon + b_lon * t_min[idx], a_lat + b_lat * t_min[idx])
                acc += weights[idx] * haversine_km(p.lonlat, fitted) ** 2
            resid_km = math.sqrt(acc / sw)

    if not fit_used:
        p0, p1 = basis[-2], basis[-1]
        seg = haversine_km(p0.lonlat, p1.lonlat)
        dt = (p1.timestamp_ms - p0.timestamp_ms) / 60000.0
        vel_km_min = seg / dt if dt > 0.0 else 0.0
        vel_kmh = vel_km_min * 60.0
        brg = bearing_deg(p0.lonlat, p1.lonlat) if seg > 0.0 else 0.0
        if vel_kmh > hi:
            vel_kmh = hi
            clamped = True
        elif vel_kmh < lo:
            vel_kmh = lo
            clamped = True
        vel_km_min = vel_kmh / 60.0
        resid_km = _CONE_DEFAULT_KM

    return {
        "basis_frames": n,
        "n_outliers": n_outliers,
        "fit_used": fit_used,
        "clamped": clamped,
        "velocity_kmh": vel_kmh,
        "velocity_km_min": vel_km_min,
        "direction_toward_deg": brg,
        "resid_std_km": resid_km,
        "cone0_km": resid_km,
    }


def _region_dbz_means(bundle):
    """Media regionale di max/mean dBZ su TUTTI i punti di tutte le track
    del bundle (cella + storm object); default 30.0 dBZ se nessun punto."""
    maxes, means = [], []
    pools = list(getattr(bundle, "tracks", None) or []) + \
        list(getattr(bundle, "storm_tracks", None) or [])
    for track in pools:
        for p in track.points:
            maxes.append(float(p.max_dbz))
            means.append(float(p.mean_dbz))
    if not maxes:
        return _REGION_DEFAULT_DBZ, _REGION_DEFAULT_DBZ
    return sum(maxes) / len(maxes), sum(means) / len(means)


def _dbz_trend(points):
    """Slope OLS (dBZ per scansione) del max_dbz sugli ultimi min(5, n) punti."""
    ys = [float(p.max_dbz) for p in points[-5:]]
    n = len(ys)
    if n < 2:
        return 0.0
    x_bar = (n - 1) / 2.0
    y_bar = sum(ys) / n
    sxx = sum((i - x_bar) ** 2 for i in range(n))
    sxy = sum((i - x_bar) * (y - y_bar) for i, y in enumerate(ys))
    return sxy / sxx if sxx > 1e-9 else 0.0


def _confidence(fit, young):
    """high: basis >=6 + nessun outlier + residui <6 km; medium: basis >=4;
    low: basis <4, cella giovane (<20 min), fit NON usato (fallback),
    velocita' clampata o qualsiasi outlier scartato (basis non pulita). Solo
    fit realmente accettati su basis pulite possono essere medium/high."""
    n = fit["basis_frames"]
    if (n < _FIT_MIN_POINTS or young or not fit["fit_used"]
            or fit["clamped"] or fit["n_outliers"] > 0):
        return "low"
    if n >= 6 and fit["n_outliers"] == 0 and fit["resid_std_km"] < 6.0:
        return "high"
    return "medium"


def _resolve_track(candidate_ctx, bundle):
    """Track associata al candidato: dalla chiave 'track' del ctx, oppure da
    track_id/track_type risolti sul bundle."""
    if isinstance(candidate_ctx, dict) and candidate_ctx.get("track") is not None:
        return candidate_ctx["track"]
    track_id = candidate_ctx.get("track_id") if isinstance(candidate_ctx, dict) else None
    if track_id is None:
        return None
    cell_pool = list(getattr(bundle, "tracks", None) or [])
    storm_pool = list(getattr(bundle, "storm_tracks", None) or [])
    track_type = candidate_ctx.get("track_type")
    pools = (storm_pool, cell_pool) if track_type == "storm_object" else \
        (cell_pool, storm_pool)
    for pool in pools:
        for track in pool:
            if track.track_id == track_id:
                return track
    return None


def _on_latest_frame(candidate_ctx, track, bundle):
    """True se il candidato e' vivo sull'ultimo frame (chiave del ctx, oppure
    derivata da frame_index == ultimo frame con celle nel bundle)."""
    if isinstance(candidate_ctx, dict) and "on_latest_frame" in candidate_ctx:
        return bool(candidate_ctx["on_latest_frame"])
    cells_by_frame = getattr(bundle, "cells_by_frame", None) or []
    n_frames = sum(1 for f in cells_by_frame if f)
    if not n_frames:
        return False
    return track.points[-1].frame_index == n_frames - 1


def _anchor_ms(bundle, last):
    """Ancora temporale: radar_timestamp del bundle (ms o ISO), fallback
    sull'ultimo punto della track."""
    ms = getattr(bundle, "radar_timestamp_ms", None)
    if ms is not None:
        return int(ms)
    ms = _ms_from_iso(getattr(bundle, "radar_timestamp_iso", None))
    if ms is not None:
        return ms
    return int(last.timestamp_ms)


def _build(candidate_ctx, bundle):
    """Costruisce il blocco forecast (dict) o None se non computabile."""
    track = _resolve_track(candidate_ctx, bundle)
    if track is None or len(track.points) < 2:
        return None
    if not _on_latest_frame(candidate_ctx, track, bundle):
        return None

    points = sorted(track.points, key=lambda p: p.timestamp_ms)
    fit = fit_motion(points)
    if fit["basis_frames"] < 2:
        return None

    last = points[-1]
    duration_min = (last.timestamp_ms - points[0].timestamp_ms) / 60000.0
    young = duration_min < _YOUNG_MINUTES
    anchor_ms = _anchor_ms(bundle, last)
    generated_at = getattr(bundle, "generated_at_iso", None) or models.utcnow_iso()
    mean_max, mean_mean = _region_dbz_means(bundle)
    i0 = float(last.max_dbz)
    m0 = float(last.mean_dbz)
    trend = _clamp(_dbz_trend(points), _TREND_SLOPE_MIN, _TREND_SLOPE_MAX)
    growth = _CONE_GROWTH_KM_MIN * (1.5 if young else 1.0)

    steps = []
    for offset_min in OFFSETS_MIN:
        lon, lat = dest_from(last.lonlat, fit["direction_toward_deg"],
                             fit["velocity_km_min"] * offset_min)
        decay = math.exp(-offset_min / _INTENSITY_LAMBDA_MIN)
        i_dec = mean_max + (i0 - mean_max) * decay
        max_dbz = _clamp(i_dec + trend * (offset_min / 5.0), *_DBZ_RANGE)
        mean_dbz = _clamp(mean_mean + (m0 - mean_mean) * decay, *_MEAN_DBZ_RANGE)
        cone = max(_CONE_MIN_KM, fit["cone0_km"] + growth * offset_min)
        steps.append({
            "offset_min": offset_min,
            "at": _iso_from_ms(anchor_ms + offset_min * 60000),
            "position": [round(float(lon), 5), round(float(lat), 5)],
            "cone_km": round(cone, 1),
            "max_dbz": round(max_dbz, 1),
            "mean_dbz": round(mean_dbz, 1),
            "area_km2": round(float(last.area_km2), 1),
        })

    return {
        "horizon_min": HORIZON_MIN,
        "step_min": STEP_MIN,
        "generated_at": generated_at,
        "method": METHOD,
        "basis_frames": int(fit["basis_frames"]),
        "confidence": _confidence(fit, young),
        "fit_used": bool(fit["fit_used"]),
        "clamped": bool(fit["clamped"]),
        "direction_toward_deg": round(float(fit["direction_toward_deg"]), 1),
        "velocity_kmh": round(float(fit["velocity_kmh"]), 1),
        "steps": steps,
    }


def build_forecast(candidate_ctx, bundle):
    """Blocco `forecast` per un candidato supercell, None se non computabile.

    candidate_ctx = dict candidato (on_latest_frame/track_id/track_type) con
    eventuale track associata sotto la chiave 'track'; senza 'track' la track
    viene risolta da track_id/track_type sul bundle. Qualsiasi errore -> None
    (layer additivo: mai rompere il run)."""
    try:
        return _build(candidate_ctx, bundle)
    except Exception:
        return None


def attach_forecasts(bundle):
    """Aggiunge la chiave `forecast` a ogni candidato di bundle.supercells.

    Ritorna il numero di candidati con forecast calcolato (0 su bundle senza
    candidati)."""
    count = 0
    for candidate in (getattr(bundle, "supercells", None) or []):
        block = build_forecast(candidate, bundle)
        candidate["forecast"] = block
        if block is not None:
            count += 1
    return count
