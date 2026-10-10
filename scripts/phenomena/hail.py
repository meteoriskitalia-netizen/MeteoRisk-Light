#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — hail.py: logica GRANDINE (HAIL) MULTI-FONTE.

Tier (THRESHOLDS_VERSION in phenomena/__init__.py):
  SUSPECT       — gate di riflettivita' (max_dbz >= 55 in >= 2 punti della
                  finestra recente <=15 min su tracks.json: persistenza REALE,
                  mai uno scan isolato). Per i target senza punti per-frame
                  (es. storm_object) la persistenza e' duration_min >= 10
                  (>= 2 scan da 5 min) + on_latest_frame.
  CORROBORATED  — gate di riflettivita' E corroborazione MULTI-FONTE. La
                  base primaria puo' essere:
                    * POH (Probability Of Hail) >= POH_PRIMARY_MIN (50%);
                    * riflettivita' forte (max_dbz >= 60, oppure >= 55 con
                      trend dBZ positivo) — percorso legacy invariato.
                  Il corroboratore puo' essere ALMENO UNO fra:
                    * struttura verticale FORTE (composito fuzzy VIL/ETM/POH/
                      overhang >= STRUCTURE_STRONG, stesse soglie/pesi di
                      radar_engine.phase2.vertical_structure);
                    * gate fulmini provider-agnostico (lightning_corroborates:
                      strength/densita' per-sorgente, fallback count_near per
                      DPC) OPPURE trend fulmini in salita;
                    * freezing level basso (H0 < FZL_LOW_M) — SOLO come
                      corroboratore aggiuntivo di una base forte, mai da solo.
                    * overshooting top da IR_108 (satellite_ot.ot_flag) —
                      evidenza SATELLITE indipendente della cima convettiva
                      che sovrasta l'anvillo (radar_engine.phase2.satellite_ot).
                  FAIL-CLOSED: se il segnale primario (POH) manca, la sola
                  struttura o l'H0 non promuovono senza il percorso legacy
                  (riflettivita' forte) o il gate fulmini.
  VERIFIED      — NON popolato: arrivera' solo con report esterni (ESWD);
                  nessun report e' inventato qui.

MULTI-FONTE (dai derivati radar, additivi/retro-compatibili):
  - POH / ETM / VIL / overhang arrivano per-candidato da
    supercells.json > candidates[*].structure_features (prodotti DPC VIL/ETM/
    POH + CAPPI_2/CAPPI_6 esposti da radar_engine main.py → output.py); per i
    track cell il valore e' agganciato per track_id dal candidato omonimo.
  - l'overhang (CAPPI 2 km vs 6 km) e' la coerenza bassa/alta del profilo.
  - unita': POH normalizzato a % (il prodotto DPC e' una frazione [0,1]),
    ETM in km, VIL in kg/m^2 (fallback: se il campo e' assente -> membership 0,
    mai un valore inventato).

H0 (freezing level, Open-Meteo) e' calcolato PER-CELLA (griglia cache
FZL_CACHE_GRID_DEG): con grader di freezing basso entra come corroboratore del
tier (oltre che in evidence/label). Se manca o la rete e' spenta il tier NON
viene forzato (l'H0 non blocca mai la valutazione).

Sorgente non puntuale (MLI/AFA): evidence.lightning.note = "AFA proxy (no
flash puntuali)" + attribution EUMETSAT, e count_near e' in PIXEL (label
esplicita), mai presentato come numero di fulmini.

Osservazione in ingresso (dict normalizzato, costruito da engine.build_observations):
  {"anchor": "cell-3", "type": "cell", "position": [lon, lat],
   "first_seen": iso, "last_seen": iso, "persistence_source": "points"|"duration",
   "recent_points": [{"timestamp_ms", "lonlat", "max_dbz"}], "max_dbz": float|None,
   "duration_min": float|None, "on_latest_frame": bool, "organization_score": int|None,
   "eccentricity_max"|"solidity_min"|"compactness_max": float|None,
   "classification": str|None, "n_frames": int|None,
   "structure_score": float|None,
   "vertical": {"poh_percent": float|None, "etm_km": float|None,
                "vil_kg_m2": float|None, "overhang": float|None}|None,
   "satellite_ot": {"score": float|None, "ot_flag": bool, "cold_top": bool,
                    "ctt_min_c": float|None, "n_flags": int|None}|None}
"""

import json
import math
import urllib.error
import urllib.request

from radar_engine.phase2 import environment as _env
from radar_engine.phase2 import vertical_structure as _vs

from . import LIGHTNING_SOURCE_THRESHOLDS, THRESHOLDS_VERSION
from .lightning import lightning_corroborates, lightning_strength

HAIL_DBZ_SUSPECT = 55.0        # gate intensita' (dBZ)
HAIL_DBZ_CORROBORATED = 60.0   # gate intensita' forte (percorso legacy)
HAIL_WINDOW_MIN = 15           # finestra di persistenza recente (minuti)
HAIL_MIN_POINTS = 2            # punti >= soglia richiesti per SUSPECT
HAIL_MIN_DURATION_MIN = 10.0   # persistenza minima per target senza punti
# Gate fulmini DPC legacy (oggi in LIGHTNING_SOURCE_THRESHOLDS["dpc"]):
HAIL_LTG_MIN_STRIKES = LIGHTNING_SOURCE_THRESHOLDS["dpc"]["count_min"]
FZL_LOW_M = 3000.0             # H0 sotto questa quota rafforza l'evidenza
FZL_URL = "https://api.open-meteo.com/v1/forecast"
FZL_HTTP_TIMEOUT_S = 15
# Griglia di cache per l'H0 per-cella: nodo piu' vicino entro ~0.5 gradi
# (~55 km, passo sensato per la variabilita' orografica alpina) cosi' i
# bersagli vicini condividono un solo fetch Open-Meteo.
FZL_CACHE_GRID_DEG = 0.5

# Soglie/pesi della struttura verticale: UNA SOLA FONTE DI VERITA' in
# radar_engine.phase2.vertical_structure (stesse soglie usate per l'SSI v2).
POH_TH = _vs.STRUCTURE_POH_TH            # (30, 50, 70)  %
ETM_TH = _vs.STRUCTURE_ETM_TH            # (6, 9, 12)    km
VIL_TH = _vs.STRUCTURE_VIL_TH            # (20, 35, 50)  kg/m^2
OVERHANG_TH = _vs.STRUCTURE_OVERHANG_TH  # (0.15, 0.30, 0.50)
STRUCTURE_WEIGHTS = _vs.STRUCTURE_WEIGHTS
HAIL_POH_PRIMARY_MIN = 50.0    # POH (%) minimo come segnale primario (== POH_TH medio)
HAIL_STRUCTURE_STRONG = 0.55   # composito fuzzy VIL/ETM/POH/overhang "forte"


def _num(value):
    """float finite oppure None (mai NaN/inf nel payload)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def _fuzzy3(x, thresholds):
    """Membership 0..1 su soglie (low, mid, high): replica esatta di
    vertical_structure._fuzzy3 (stessa curva trapezoidale a due tratti)."""
    v = _num(x)
    if v is None:
        return 0.0
    lo, mid, hi = (float(thresholds[0]), float(thresholds[1]),
                   float(thresholds[2]))
    if v <= lo:
        return 0.0
    if v <= mid:
        return 0.5 * _clamp01((v - lo) / max(mid - lo, 1e-9))
    if v <= hi:
        return 0.5 + 0.5 * _clamp01((v - mid) / max(hi - mid, 1e-9))
    return 1.0


def _poh_percent(value):
    """POH normalizzato a %: il prodotto DPC POH e' una FRAZIONE [0,1]
    (vertical_structure lo converte x100), ma snapshot piu' vecchi possono
    esporre ancora la frazione grezza non convertita. Valori <= 1.0 sono
    trattati come frazione, altrimenti come percentuale. None/NaN -> None."""
    v = _num(value)
    if v is None:
        return None
    return v * 100.0 if v <= 1.0 else v


def structure_evidence(vertical):
    """Descrittori verticali + membership fuzzy + composito (0..1).

    `vertical` e' un dict con chiavi poh_percent/etm_km/vil_kg_m2/overhang
    (oppure None). Un campo assente -> membership 0 (fail-closed), mai un
    valore inventato. Il composito usa gli stessi pesi di
    vertical_structure.STRUCTURE_WEIGHTS."""
    v = vertical if isinstance(vertical, dict) else {}
    poh = _poh_percent(v.get("poh_percent"))
    etm = _num(v.get("etm_km"))
    vil = _num(v.get("vil_kg_m2"))
    ovh = _num(v.get("overhang"))
    m = {
        "vil": _fuzzy3(vil, VIL_TH),
        "etm": _fuzzy3(etm, ETM_TH),
        "poh": _fuzzy3(poh, POH_TH),
        "overhang": _fuzzy3(ovh, OVERHANG_TH),
    }
    composite = (STRUCTURE_WEIGHTS["vil"] * m["vil"]
                 + STRUCTURE_WEIGHTS["etm"] * m["etm"]
                 + STRUCTURE_WEIGHTS["poh"] * m["poh"]
                 + STRUCTURE_WEIGHTS["overhang"] * m["overhang"])
    return {
        "available": any(x is not None for x in (poh, etm, vil, ovh)),
        "poh_percent": None if poh is None else round(poh, 1),
        "etm_km": etm,
        "vil_kg_m2": vil,
        "overhang": ovh,
        "m_poh": round(m["poh"], 4),
        "m_etm": round(m["etm"], 4),
        "m_vil": round(m["vil"], 4),
        "m_overhang": round(m["overhang"], 4),
        "composite": round(composite, 4),
    }


def fzl_bucket(lat, lon, grid_deg=FZL_CACHE_GRID_DEG):
    """Nodo di griglia (lat, lon) piu' vicino per la cache H0 per-cella."""
    g = float(grid_deg)
    return (round(float(lat) / g) * g, round(float(lon) / g) * g)


def recent_points(points, radar_timestamp_ms, window_min=HAIL_WINDOW_MIN):
    """Punti dentro la finestra recente [radar_ts - window_min, radar_ts].

    Normalizza in {"timestamp_ms", "timestamp", "lonlat", "max_dbz",
    "eccentricity", "solidity", "compactness"}; le tre grandezze di forma
    sono propagate quando il punto le porta (tracks.json per-frame) e sono
    None altrimenti (schema legacy), cosi' la morfologia resta agganciata
    alla finestra senza inventare frame. Punti senza timestamp_ms valido o
    FUTURI rispetto al radar sono scartati (nessun punto inventato).
    radar_timestamp_ms None -> lista vuota."""
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
                    "timestamp": p.get("timestamp"),
                    "lonlat": list(p.get("lonlat") or []),
                    "max_dbz": _num(p.get("max_dbz")),
                    "eccentricity": _num(p.get("eccentricity")),
                    "solidity": _num(p.get("solidity")),
                    "compactness": _num(p.get("compactness"))})
    return out


def hail_score(state, max_dbz, lightning, freezing_level_m, structure_t=None):
    """Score 0-100: base dal tier + contributi (dBZ, struttura, fulmini, H0).

    La somma dei contributi e' limitata a 29 punti -> SUSPECT <= 69,
    CORROBORATED <= 99, VERIFIED = 100 (mai prodotto in questo step).
    `structure_t` e' il composito verticale 0..1 (None -> 0)."""
    if state == "VERIFIED":
        return 100.0
    base = {"SUSPECT": 40.0, "CORROBORATED": 70.0}.get(state, 40.0)
    dbz = _num(max_dbz)
    dbz_t = _clamp01((dbz - HAIL_DBZ_SUSPECT) / 15.0) if dbz else 0.0
    ltg = lightning if isinstance(lightning, dict) else {}
    ltg_t = lightning_strength(ltg)        # strength 0..1, fallback count/30
    fzl = _num(freezing_level_m)
    fzl_t = 1.0 if (fzl is not None and fzl < FZL_LOW_M) else 0.0
    st_t = _clamp01(_num(structure_t) or 0.0)
    raw = base + 29.0 * (0.40 * dbz_t + 0.30 * st_t
                         + 0.20 * ltg_t + 0.10 * fzl_t)
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

    # --- evidenza strutturale verticale multi-fonte (POH/ETM/VIL/overhang) ---
    se = structure_evidence(obs.get("vertical"))
    poh_primary = (se["poh_percent"] is not None
                   and se["poh_percent"] >= HAIL_POH_PRIMARY_MIN)
    structure_strong = se["composite"] >= HAIL_STRUCTURE_STRONG

    # Overshooting top (IR_108, radar_engine.phase2.satellite_ot): cima
    # convettiva che sovrasta l'anvillo -> corroboratore INDIPENDENTE (satellite)
    # di una base primaria. Fail-closed: osservazione senza OT -> ot_ok False,
    # nessun effetto (comportamento invariato).
    ot_info = obs.get("satellite_ot")
    ot_info = ot_info if isinstance(ot_info, dict) else {}
    ot_ok = bool(ot_info.get("ot_flag"))

    ltg = lightning if isinstance(lightning, dict) else {"available": False}
    ltg_avail = bool(ltg.get("available"))
    count_near = int(ltg.get("count_near") or 0) if ltg_avail else 0
    strength = lightning_strength(ltg) if ltg_avail else None
    trend_dir = None
    if ltg_avail and isinstance(ltg.get("trend"), dict):
        trend_dir = ltg["trend"].get("direction")
    ltg_ok = lightning_corroborates(ltg) or (ltg_avail and trend_dir == "up")

    fzl = _num(freezing_level_m)
    fzl_low = None if fzl is None else bool(fzl < FZL_LOW_M)
    h0_ok = bool(fzl_low)

    # --- tier MULTI-FONTE (fail-closed) -------------------------------------
    # Base primaria: POH >= 50% oppure riflettivita' forte (percorso legacy).
    # Corroboratore: struttura verticale forte, gate fulmini (o trend up) e,
    # SOLO a supporto di una base forte, H0 basso. Se la base e' debole o
    # assente la sola struttura/H0 non promuove -> resta SUSPECT.
    primary_ok = poh_primary or strong
    corroborated = primary_ok and (
        (poh_primary and structure_strong)
        or (poh_primary and ltg_ok)
        or (strong and (ltg_ok or structure_strong or h0_ok))
        or ot_ok
    )
    state = "CORROBORATED" if corroborated else "SUSPECT"

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
        "structure": {
            "available": se["available"],
            "score": _num(obs.get("structure_score")),
            "composite": se["composite"],
            "poh_percent": se["poh_percent"],
            "etm_km": se["etm_km"],
            "vil_kg_m2": se["vil_kg_m2"],
            "overhang": se["overhang"],
            "membership": {"vil": se["m_vil"], "etm": se["m_etm"],
                           "poh": se["m_poh"], "overhang": se["m_overhang"]},
            "poh_primary": poh_primary,
            "strong": structure_strong,
        },
        "freezing_level_m": fzl,
        "freezing_level_low": fzl_low,
        "satellite_ot": {
            "available": bool(ot_info),
            "flag": bool(ot_ok),
            "score": _num(ot_info.get("score")),
            "ctt_min_c": _num(ot_info.get("ctt_min_c")),
            "n_flags": ot_info.get("n_flags"),
            "cold_top": bool(ot_info.get("cold_top")),
        },
        "corroboration": {
            "primary": "poh" if poh_primary else ("dbz" if strong else None),
            "lightning": bool(ltg_ok),
            "structure": bool(structure_strong),
            "h0_low": bool(h0_ok),
            "ot": bool(ot_ok),
        },
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
    if se["available"]:
        parts = []
        if se["poh_percent"] is not None:
            parts.append(f"POH {se['poh_percent']:.0f}%")
        if se["etm_km"] is not None:
            parts.append(f"ETM {se['etm_km']:.1f} km")
        if se["vil_kg_m2"] is not None:
            parts.append(f"VIL {se['vil_kg_m2']:.0f} kg/m2")
        if se["overhang"] is not None:
            parts.append(f"overhang {se['overhang']:.2f}")
        labels.append("struttura verticale " + ", ".join(parts)
                      + f" (composito {se['composite']:.2f})")
    else:
        labels.append("struttura verticale non disponibile")
    if poh_primary:
        labels.append(f"POH >= {HAIL_POH_PRIMARY_MIN:.0f}% (segnale primario)")
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
    if ot_ok:
        ctt = _num(ot_info.get("ctt_min_c"))
        ot_label = "overshooting top (IR_108)"
        if ctt is not None:
            ot_label += f": CTT min {ctt:.1f} °C"
        labels.append(ot_label)
    evidence["labels"] = labels

    return {
        "id": f"HAIL-{obs['anchor']}",
        "type": "HAIL",
        "state": state,
        "score": hail_score(state, max_dbz, ltg, fzl,
                            structure_t=se["composite"]),
        "first_seen": obs.get("first_seen"),
        "last_seen": obs.get("last_seen"),
        "position": list(obs.get("position") or []),
        "evidence": evidence,
        "thresholds_version": THRESHOLDS_VERSION,
    }


# ---------------------------------------------------------------------------
# H0 (freezing level) — valutata PER-CELLA (engine.run con cache a griglia);
# entra in evidence/label e come corroboratore di una base forte, ma da sola
# non promuove e non blocca mai la valutazione.
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
