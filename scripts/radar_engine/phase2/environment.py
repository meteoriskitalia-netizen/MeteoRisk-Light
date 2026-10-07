#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — environment.py (AMBIENTE NWP, EXPERIMENTAL)

Indici ambientali da Open-Meteo per il punto (lat, lon): CAPE/CIN/LI, venti a
livelli di pressione, SRH 0-3 km e 0-1 km, SCP / STP / SHIP (formule SPC).

Sorgente: Open-Meteo forecast API (stessa fonte della Fase 1/centrale dati).
Il modulo NON dipende da `requests` (non presente nel venv): l'helper di rete
usa urllib (stessa convenzione di radar_engine/source_dpc.py). Se il chiamante
passa un `openmeteo_client` (callabile url->dict o oggetto con get_json/
fetch_json), quello viene usato al posto della rete diretta — l'I/O resta
INJETTABILE e testabile.

EVIDENZA (lettera A0 + verifica live 2026-10-07):
  - `storm_relative_helicity` NON esiste nell'API Open-Meteo (400 su tutti i
    modelli, lettera A0): da qui il calcolo lato nostro del SRH dai venti di
    livello (10m/1000/925/850/700/500 hPa, tutti verificati 200);
  - tutte e 23 le variabili di HOURLY_VARS rispondono HTTP 200, 24 slot
    orari, 0 nulli (verifica live del 2026-10-07); `specific_humidity_2m` e'
    stata rifiutata (400 "invalid String value") e rimossa: q si calcola con
    Bolton da dew point + pressure_msl (flags["q_source"]).

Approssimazioni documentate (mai nascoste):
  - muCAPE: Open-Meteo espone la CAPE di dominio (MLCAPE); e' usata come
    proxy di MUCAPE (stessa scelta dell'app, audit C2 "ML-SHIP").
  - SRH_0_3: integrale discrete (Davies-Jones) sul profilo [superficie 10m,
    1000, 925, 850 hPa] con storm-motion Bunkers right-mover dal profilo
    profondo disponibile (10m + 925/850/700/500). I livelli di pressione sono
    quote variabili (ASL, non AGL fissi): l'errore sullo spessore di strato
    e' lo stesso rilevato dall'audit C1 della release e va validato su
    radiosonde; qui e' DICHIARATO, non corretto a oltraggio.
  - SRH_1km: proxy = integrale sul solo strato [10m, 1000, 925] (925 hPa
    ~0.7-1.0 km AGL in pianura), NON un vero SRH 0-1km AGL.
  - EBWD/BWD_0_6: |V(500hPa) - V(10m)| (~5.5 km): proxy del bulk 0-6 km.
  - LCL surface-based: approssimazione Lawrence 125*(T2m - Td2m) [m]
    (l'app espone la stessa: Open-Meteo non serve LCL di livello).
  - LR 700-500: spessore geometrico da geopotenziale con la conversione
    H_geo = R*G/(1.00122*R - G) della release (geoToMetres).

Formule (come da specifica Fase 2, note SPC/Thompson/Gropp-Davenport/NOAA):
  SCP  = (muCAPE/1000) * (ESRH/50) * (EBWD/20) * (-40/muCIN)
  STP  = (sbCAPE/1500) * ((2000-sbLCL)/1000) * (SRH1/150) * (BWD06/20)
         * ((200+sbCIN)/150)          [ogni termine clampato a >=0]
  SHIP = (MUCAPE*qMU*LR700500*(-T500)*BWD06)/42000000 con clamp
         (BWD in [7,27] m/s, q in [11,13.6] g/kg, T500 max -5.5 C,
          correzioni: CAPE<1300 -> *CAPE/1300; LR<5.8 -> *LR/5.8;
          freezing<2400 m -> *freezing/2400)
  NOTA: la release applica SHIP/44000000 (fix 1.0.0.11): qui vale il
  denominatore 42000000 della specifica Fase 2, esplicito in
  SHIP_DENOMINATOR per rendere il disallineamento ispezionabile.

env_score = 100 * tanh(SCP/3) * clamp01(completeness): fuzzy su SCP con
soglie operative 1 (debole) / 3 (forte); completeness = frazione di
variabili obbligatorie disponibili -> score PARZIALE + flag se mancano dati.
"""

import math
import urllib.error
import urllib.request

# ---------------------------------------------------------------------------
# Costanti — EXPERIMENTAL DEFAULTS
# ---------------------------------------------------------------------------
OPENMETEO_URL = "https://api.open-meteo.com/v1/forecast"
ENV_HTTP_TIMEOUT_S = 30
ENV_SCP_TANH_SCALE = 3.0          # tanh(SCP/3): SCP=1 -> ~31, SCP=3 -> ~76
SHIP_DENOMINATOR = 42000000.0     # specifica Fase 2 (release usa 44e6)
BUNKERS_DEV_MS = 7.5              # deviazione standard Bunkers right-mover
SRH_LOW_LEVELS_HPA = (1000, 925, 850)   # nodi per SRH_0_3
SRH1_LEVELS_HPA = (1000, 925)           # nodi per proxy SRH_0_1km
DEEP_LEVELS_HPA = (1000, 925, 850, 700, 500)  # profilo profondo/EBWD
CAPE_GATE_JKG = 100.0             # sotto questa CAPE l'ambiente e' secco
CIN_TERM_CAP = 4.0                # cap del termine -40/muCIN (SCP)
SHIP_Q_CLAMP = (11.0, 13.6)       # g/kg (caps US della specifica)
SHIP_BWD_CLAMP = (7.0, 27.0)      # m/s (caps US della specifica)
SHIP_T500_MAX_C = -5.5            # T500 usata = min(T500, -5.5)
SHIP_CAPE_CORR_JKG = 1300.0
SHIP_LR_CORR_KKM = 5.8
SHIP_FREEZING_CORR_M = 2400.0
LCL_LAWRENCE_M = 125.0            # LCL = 125 * (T2m - Td2m)

# Variabili hourly richieste (Open-Meteo; unit esplicite nella URL).
# VERIFICATE LIVE 2026-10-07: tutte e 23 rispondono HTTP 200, 24 slot, 0 nulli.
# `specific_humidity_2m` e' stata RIFIUTATA dall'API (400 "invalid String
# value", stesso errore di storm_relative_helicity nella lettera A0) ->
# rimossa: l'umidita' superficiale (q) si calcola con Bolton da dew point e
# pressione (mixing_ratio_gkg), con flags["q_source"] esplicito.
HOURLY_VARS = (
    "temperature_2m", "dew_point_2m", "pressure_msl",
    "cape", "convective_inhibition", "lifted_index",
    "wind_speed_10m", "wind_direction_10m",
    "wind_speed_1000hPa", "wind_direction_1000hPa",
    "wind_speed_925hPa", "wind_direction_925hPa",
    "wind_speed_850hPa", "wind_direction_850hPa",
    "wind_speed_700hPa", "wind_direction_700hPa",
    "wind_speed_500hPa", "wind_direction_500hPa",
    "temperature_700hPa", "temperature_500hPa",
    "geopotential_height_700hPa", "geopotential_height_500hPa",
    "freezing_level_height",
)

REQUIRED_INPUTS = (
    "cape", "cin", "wind_10m", "wind_500hPa", "wind_low_levels",
    "temperature_2m", "dew_point_2m", "surface_humidity",
    "temperature_700hPa", "temperature_500hPa",
    "geopotential_height_700hPa", "geopotential_height_500hPa",
)


class EnvironmentFetchError(Exception):
    """Rete/Open-Meteo non raggiungibile o risposta non interpretabile.

    Allineabile a radar_engine.models.SourceError a integrazione (stessa
    politica: l'ambiente degrada, mai crash del run)."""


# ---------------------------------------------------------------------------
# Helper locali
# ---------------------------------------------------------------------------
def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def _term(x):
    """Termine di prodotto non negativo (0 se invalido/negativo)."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    return x if math.isfinite(x) and x > 0.0 else 0.0


def _http_json(url, timeout_s=ENV_HTTP_TIMEOUT_S):
    """GET JSON via urllib (stessa pattern di source_dpc._http_json)."""
    req = urllib.request.Request(url, method="GET",
                                 headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise EnvironmentFetchError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise EnvironmentFetchError(
            f"network:{exc.__class__.__name__}") from exc
    import json
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise EnvironmentFetchError("invalid_json") from exc


def geo_to_metres(gph_metres):
    """Geopotential -> quota geometrica ASL (m), conversione della release.

    H_geo = R*G / (1.00122*R - G), R = 6371000 m (diff <0.3% troposfera).
    Ritorna None se il input e' None/non finito (mai quote inventate)."""
    if gph_metres is None:
        return None
    try:
        g = float(gph_metres)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(g):
        return None
    r = 6371000.0
    ratio = 1.00122
    denom = ratio * r - g
    if denom <= 0:
        return None
    return r * g / denom


def wind_to_uv(speed_ms, direction_deg):
    """Vento (m/s, direzione meteorologica DA cui, gradi) -> (u, v) m/s."""
    s = float(speed_ms)
    d = float(direction_deg)
    rad = math.radians(d)
    return -s * math.sin(rad), -s * math.cos(rad)


def mixing_ratio_gkg(dew_point_c, pressure_hpa):
    """Mixing ratio (g/kg) da dewpoint e pressione (Bolton, esatto per e).

    r = 622*e/(p-e) con e = 6.11*10^(7.5*td/(237.7+td)) hPa. Ritorna None se
    gli input non sono validi (nessuna stima fittizia)."""
    if dew_point_c is None or pressure_hpa is None:
        return None
    try:
        td = float(dew_point_c)
        p = float(pressure_hpa)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(td) and math.isfinite(p)) or p <= 0:
        return None
    e = 6.11 * 10.0 ** (7.5 * td / (237.7 + td))
    if e >= p:
        return None
    return 622.0 * e / (p - e)


def build_openmeteo_url(lat, lon):
    """URL Open-Meteo deterministica per il punto (unit esplicite)."""
    q = (
        f"{OPENMETEO_URL}?latitude={float(lat):.5f}&longitude={float(lon):.5f}"
        f"&hourly={','.join(HOURLY_VARS)}"
        "&temperature_unit=celsius&wind_speed_unit=ms"
        "&pressure_unit=hPa&timezone=GMT&forecast_days=1"
    )
    return q


def pick_current_index(times_iso):
    """Indice dello slot orario corrente nella serie hourly.time (ISO UTC).

    Primo slot >= ora (UTC); se tutta la serie e' passata, l'ultimo slot.
    Ritorna None per serie vuota (dato assente: nessun indice inventato)."""
    import datetime as _dt
    if not times_iso:
        return None
    now = _dt.datetime.now(_dt.timezone.utc)
    last_idx = None
    for i, t in enumerate(times_iso):
        try:
            dt = _dt.datetime.strptime(str(t), "%Y-%m-%dT%H:%M").replace(
                tzinfo=_dt.timezone.utc)
        except ValueError:
            try:
                dt = _dt.datetime.strptime(str(t)[:16],
                                            "%Y-%m-%dT%H:%M").replace(
                    tzinfo=_dt.timezone.utc)
            except ValueError:
                continue
        last_idx = i
        if dt >= now:
            return i
    return last_idx


def srh_discrete(levels_uv, storm_motion_uv):
    """SRH discrete (m^2/s^2) all' integrale di Davies-Jones usato dall'app.

    srh += (u2-Cx)*(v1-Cy) - (u1-Cx)*(v2-Cy) per strato consecutivo, con
    (Cx,Cy) = storm motion. levels_uv: lista [(u,v)] in ordine di quota."""
    if len(levels_uv) < 2:
        return 0.0
    cx, cy = storm_motion_uv
    srh = 0.0
    for i in range(len(levels_uv) - 1):
        u1, v1 = levels_uv[i]
        u2, v2 = levels_uv[i + 1]
        srh += (u2 - cx) * (v1 - cy) - (u1 - cx) * (v2 - cy)
    return float(srh)


def bunkers_right_mover(levels_uv):
    """Storm motion Bunkers right-mover (emisfero nord), m/s.

    mean wind aritmetico del profilo + deviazione 7.5 m/s a destra del vettore
    shear (superficie -> top del profilo). Se lo shear e' nullo ritorna il
    mean wind. Approssimazione: media ARITMETICA (i geopotenziali Open-Meteo
    coprono solo 850/700/500, la media pesata per spessore della release non
    e' ricostruibile a livello di punto senza tutti i livelli)."""
    if not levels_uv:
        return (0.0, 0.0)
    mu = sum(p[0] for p in levels_uv) / len(levels_uv)
    mv = sum(p[1] for p in levels_uv) / len(levels_uv)
    if len(levels_uv) < 2:
        return (mu, mv)
    u0, v0 = levels_uv[0]
    u1, v1 = levels_uv[-1]
    shx, shy = u1 - u0, v1 - v0
    shmag = math.hypot(shx, shy)
    if shmag < 0.1:
        return (mu, mv)
    return (mu + BUNKERS_DEV_MS * shy / shmag,
            mv + BUNKERS_DEV_MS * (-shx) / shmag)


# ---------------------------------------------------------------------------
# Indici (funzioni pure sui valori estratti)
# ---------------------------------------------------------------------------
def scp_env(mu_cape, esrh, ebwd_ms, mu_cin):
    """SCP = (muCAPE/1000)*(ESRH/50)*(EBWD/20)*(-40/muCIN), termini >=0.

    mu_cin: CIN (J/kg, valore NEGATIVO come da convenzione Open-Meteo); il
    termine vale 40/|CIN| con cap CIN_TERM_CAP (|CIN| -> 0 = termine esploso,
    troncato: nessun SCP infinito). CIN mancante -> termine neutro 1.0 (il
    chiamante marca il flag). CAPE mancante -> None (score parziale)."""
    if mu_cape is None:
        return None
    try:
        cape = float(mu_cape)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cape):
        return None
    t_cape = _term(cape) / 1000.0
    t_srh = _term(esrh) / 50.0
    t_bwd = _term(ebwd_ms) / 20.0
    if mu_cin is None:
        t_cin = 1.0
    else:
        try:
            cin = float(mu_cin)
        except (TypeError, ValueError):
            cin = None
        if cin is None or not math.isfinite(cin):
            t_cin = 1.0
        else:
            abs_cin = abs(cin)
            t_cin = (min(40.0 / abs_cin, CIN_TERM_CAP)
                     if abs_cin > 0 else CIN_TERM_CAP)
    return t_cape * t_srh * t_bwd * t_cin


def stp_env(sb_cape, sb_lcl_m, srh1, bwd06_ms, sb_cin):
    """STP (SPL) = (sbCAPE/1500)*((2000-sbLCL)/1000)*(SRH1/150)*(BWD06/20)
    *((200+sbCIN)/150), ogni termine clampato a >=0 (0 se non valido o
    negativo: LCL >= 2000 m -> termine LCL 0; CIN <= -200 -> termine CIN 0).

    Ritorna None se sb_cape manca (dato assente, mai fabbricato)."""
    if sb_cape is None:
        return None
    try:
        lcl_term = _term((2000.0 - float(sb_lcl_m)) / 1000.0) \
            if sb_lcl_m is not None else 0.0
    except (TypeError, ValueError):
        lcl_term = 0.0
    try:
        cin_term = _term((200.0 + float(sb_cin)) / 150.0) \
            if sb_cin is not None else 0.0
    except (TypeError, ValueError):
        cin_term = 0.0
    terms = (
        _term(sb_cape) / 1500.0,
        lcl_term,
        _term(srh1) / 150.0,
        _term(bwd06_ms) / 20.0,
        cin_term,
    )
    out = 1.0
    for t in terms:
        out *= t
    return out


def ship_env(mu_cape, q_gkg, lr_700_500, t500_c, bwd06_ms, freezing_m):
    """SHIP (NOAA/NSHARP, specifica Fase 2: denominatore 42000000).

    Clamp: q -> [11,13.6] g/kg, BWD -> [7,27] m/s, T500 = min(T500,-5.5)
    (cioe' |T500| con floor 5.5, come da fix "T500 max -5.5" della release).
    Correzioni secondarie: CAPE<1300 -> *CAPE/1300; LR<5.8 -> *LR/5.8;
    freezing<2400 m -> *freezing/2400 (freeze mancante -> correzione saltata,
    flag a valle). Ritorna None se uno degli input obbligatori manca."""
    if mu_cape is None or q_gkg is None or lr_700_500 is None \
            or t500_c is None or bwd06_ms is None:
        return None
    try:
        cape = float(mu_cape)
        q = float(q_gkg)
        lr = float(lr_700_500)
        t500 = float(t500_c)
        bwd = float(bwd06_ms)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (cape, q, lr, t500, bwd)):
        return None

    q_c = max(SHIP_Q_CLAMP[0], min(q, SHIP_Q_CLAMP[1]))
    bwd_c = max(SHIP_BWD_CLAMP[0], min(bwd, SHIP_BWD_CLAMP[1]))
    t500_used = min(t500, SHIP_T500_MAX_C)      # floor su |T500| = 5.5
    t500_abs = abs(t500_used)

    ship = (cape * q_c * lr * t500_abs * bwd_c) / SHIP_DENOMINATOR
    if cape < SHIP_CAPE_CORR_JKG:
        ship *= cape / SHIP_CAPE_CORR_JKG
    if lr < SHIP_LR_CORR_KKM:
        ship *= lr / SHIP_LR_CORR_KKM
    if freezing_m is not None:
        try:
            fz = float(freezing_m)
        except (TypeError, ValueError):
            fz = None
        if fz is not None and math.isfinite(fz) and fz < SHIP_FREEZING_CORR_M:
            ship *= fz / SHIP_FREEZING_CORR_M
    return max(ship, 0.0)


def env_score(scp, completeness=1.0):
    """Ambiente score 0-100 = 100*tanh(SCP/ENV_SCP_TANH_SCALE)*completeness.

    SCP=None (variabili obbligatorie mancanti) -> 0.0 con score parziale
    segnalato dal chiamante tramite completeness/missing. completeness in [0,1]
    = frazione di variabili obbligatorie disponibili (0.7 -> score al 70%)."""
    if scp is None:
        return 0.0
    try:
        s = float(scp)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(s) or s <= 0.0:
        return 0.0
    base = 100.0 * math.tanh(s / ENV_SCP_TANH_SCALE)
    return round(base * _clamp01(completeness), 1)


# ---------------------------------------------------------------------------
# Parse + orchestrazione (wrapper I/O)
# ---------------------------------------------------------------------------
def _hourly_get(hourly, key, idx):
    seq = (hourly or {}).get(key)
    if not isinstance(seq, (list, tuple)) or idx is None \
            or idx >= len(seq):
        return None
    v = seq[idx]
    if v is None:
        return None
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return None
    return fv if math.isfinite(fv) else None


def parse_environment(payload, index=None):
    """Estrae i valori ambiente dalla risposta Open-Meteo (slot corrente).

    Ritorna dict con i valori grezzi, l'indice usato e i livelli vento
    (u,v) in lista ordinata di quota. Nessun valore mancante viene riempito:
    le chiavi assenti valgono None."""
    hourly = (payload or {}).get("hourly") or {}
    idx = index if index is not None else pick_current_index(
        hourly.get("time") or [])
    out = {
        "index": idx,
        "time_iso": (hourly.get("time") or [None])[idx]
        if (hourly.get("time") and idx is not None
            and idx < len(hourly["time"])) else None,
        "cape": _hourly_get(hourly, "cape", idx),
        "cin": _hourly_get(hourly, "convective_inhibition", idx),
        "lifted_index": _hourly_get(hourly, "lifted_index", idx),
        "temperature_2m": _hourly_get(hourly, "temperature_2m", idx),
        "dew_point_2m": _hourly_get(hourly, "dew_point_2m", idx),
        "pressure_msl": _hourly_get(hourly, "pressure_msl", idx),
        "freezing_level_height": _hourly_get(hourly,
                                             "freezing_level_height", idx),
        "temperature_700hPa": _hourly_get(hourly, "temperature_700hPa", idx),
        "temperature_500hPa": _hourly_get(hourly, "temperature_500hPa", idx),
        "geopotential_height_700hPa": _hourly_get(
            hourly, "geopotential_height_700hPa", idx),
        "geopotential_height_500hPa": _hourly_get(
            hourly, "geopotential_height_500hPa", idx),
        "winds": {},
    }
    winds = out["winds"]
    ws10 = _hourly_get(hourly, "wind_speed_10m", idx)
    wd10 = _hourly_get(hourly, "wind_direction_10m", idx)
    if ws10 is not None and wd10 is not None:
        winds["10m"] = wind_to_uv(ws10, wd10)
    for pl in DEEP_LEVELS_HPA:
        ws = _hourly_get(hourly, f"wind_speed_{pl}hPa", idx)
        wd = _hourly_get(hourly, f"wind_direction_{pl}hPa", idx)
        if ws is not None and wd is not None:
            winds[pl] = wind_to_uv(ws, wd)
    return out


def fetch_environment(lat, lon, openmeteo_client=None, timeout_s=None):
    """WRAPPER DI RETE: scarica l'ambiente Open-Meteo per (lat, lon).

    openmeteo_client:
      None            -> urllib diretto (default, nessuna dipendenza extra)
      callable(url)   -> deve ritornare il dict JSON
      oggetto         -> con metodo get_json/fetch_json/get (url)
    Ritorna il payload JSON. Solleva EnvironmentFetchError su errori di
    rete/risposta (il chiamante degrada, NON crasha il run)."""
    url = build_openmeteo_url(lat, lon)
    to = ENV_HTTP_TIMEOUT_S if timeout_s is None else float(timeout_s)
    if openmeteo_client is None:
        return _http_json(url, to)
    if callable(openmeteo_client):
        fn = openmeteo_client
    else:
        fn = None
        for attr in ("get_json", "fetch_json", "get"):
            cand = getattr(openmeteo_client, attr, None)
            if callable(cand):
                fn = cand
                break
        if fn is None:
            raise EnvironmentFetchError("unsupported_client")
    try:
        return fn(url)
    except EnvironmentFetchError:
        raise
    except Exception as exc:  # il client puo' sollevare di tutto
        raise EnvironmentFetchError(
            f"client_failed:{exc.__class__.__name__}") from exc


def evaluate_environment(lat, lon, openmeteo_client=None, timeout_s=None):
    """Ambiente completo per il punto: fetch + parse + SCP/STP/SHIP + score.

    Ritorna dict con: valori grezzi, srh_0_3, srh_1km, ebwd_ms, sb_lcl_m,
    q_gkg, lr_700_500, scp, stp, ship, env_score (0-100), completeness,
    partial (bool), missing (lista), flags (dict). Fetch fallito -> solleva
    EnvironmentFetchError (nessun dato ambiente inventato)."""
    payload = fetch_environment(lat, lon, openmeteo_client, timeout_s)
    env = parse_environment(payload)

    winds = env["winds"]
    sfc = winds.get("10m")
    deep = [winds[pl] for pl in DEEP_LEVELS_HPA if pl in winds]
    low = [winds[pl] for pl in SRH_LOW_LEVELS_HPA if pl in winds]
    low1 = [winds[pl] for pl in SRH1_LEVELS_HPA if pl in winds]

    flags = {
        "srh_1km_approx": True,
        "srh_low_levels_used": [pl for pl in SRH_LOW_LEVELS_HPA
                                if pl in winds],
        "storm_motion": None,
        "cin_term_neutral": False,
        "q_source": None,
        "freezing_correction_applied": False,
    }

    # --- SRH con storm-motion Bunkers dal profilo profondo ---
    srh_0_3 = None
    srh_1km = None
    storm = None
    deep_full = ([sfc] if sfc is not None else []) + deep
    if len(deep_full) >= 2:
        storm = bunkers_right_mover(deep_full)
        flags["storm_motion"] = "bunkers_rm_mean_arith"
    elif len(deep_full) >= 1:
        storm = deep_full[0]
        flags["storm_motion"] = "single_level"
    if storm is not None:
        prof03 = ([sfc] if sfc is not None else []) + low
        if len(prof03) >= 2:
            srh_0_3 = srh_discrete(prof03, storm)
        prof01 = ([sfc] if sfc is not None else []) + low1
        if len(prof01) >= 2:
            srh_1km = srh_discrete(prof01, storm)

    # --- EBWD proxy 0-6 km (sfc -> 500 hPa) ---
    ebwd = None
    if sfc is not None and 500 in winds:
        u0, v0 = sfc
        u1, v1 = winds[500]
        ebwd = math.hypot(u1 - u0, v1 - v0)

    # --- LCL surface-based (Lawrence) ---
    t2m = env["temperature_2m"]
    td2m = env["dew_point_2m"]
    sb_lcl = None
    if t2m is not None and td2m is not None:
        sb_lcl = max(LCL_LAWRENCE_M * (t2m - td2m), 0.0)

    # --- mixing ratio superficiale: SOLO Bolton da dew point + pressione
    #     (specific_humidity_2m rifiutata dall'API, v. HOURLY_VARS) ---
    q = mixing_ratio_gkg(td2m, env["pressure_msl"])
    flags["q_source"] = "dewpoint_pressure" if q is not None else "missing"

    # --- lapse rate 700-500 geometrico ---
    lr = None
    z700 = geo_to_metres(env["geopotential_height_700hPa"])
    z500 = geo_to_metres(env["geopotential_height_500hPa"])
    t700 = env["temperature_700hPa"]
    t500 = env["temperature_500hPa"]
    if t700 is not None and t500 is not None and z700 is not None \
            and z500 is not None:
        dh_km = (z500 - z700) / 1000.0
        if dh_km > 0.1:
            lr = (t700 - t500) / dh_km
        elif env["geopotential_height_700hPa"] is not None \
                and env["geopotential_height_500hPa"] is not None:
            dh_raw = (env["geopotential_height_500hPa"]
                      - env["geopotential_height_700hPa"]) / 1000.0
            if dh_raw > 0.1:
                lr = (t700 - t500) / dh_raw

    # --- indici compositi ---
    if env["cin"] is None:
        flags["cin_term_neutral"] = True
    scp = scp_env(env["cape"], srh_0_3, ebwd, env["cin"])
    stp = stp_env(env["cape"], sb_lcl, srh_1km, ebwd, env["cin"])
    freezing = env["freezing_level_height"]
    if freezing is not None and scp is not None:
        flags["freezing_correction_applied"] = freezing \
            < SHIP_FREEZING_CORR_M
    ship = ship_env(env["cape"], q, lr, t500, ebwd, freezing)

    # --- completeness / missing / score ---
    present = {
        "cape": env["cape"] is not None,
        "cin": env["cin"] is not None,
        "wind_10m": sfc is not None,
        "wind_500hPa": 500 in winds,
        "wind_low_levels": bool(low),
        "temperature_2m": t2m is not None,
        "dew_point_2m": td2m is not None,
        "surface_humidity": q is not None,
        "temperature_700hPa": t700 is not None,
        "temperature_500hPa": t500 is not None,
        "geopotential_height_700hPa": env["geopotential_height_700hPa"]
        is not None,
        "geopotential_height_500hPa": env["geopotential_height_500hPa"]
        is not None,
    }
    missing = [k for k in REQUIRED_INPUTS if not present.get(k, False)]
    completeness = round(len(present) - len(missing), 4) / len(REQUIRED_INPUTS)
    completeness = round(completeness, 4)
    score = env_score(scp, completeness)

    return {
        "lat": float(lat),
        "lon": float(lon),
        "time_iso": env["time_iso"],
        "cape": env["cape"],
        "cin": env["cin"],
        "lifted_index": env["lifted_index"],
        "sb_lcl_m": round(sb_lcl, 1) if sb_lcl is not None else None,
        "q_gkg": round(q, 3) if q is not None else None,
        "lr_700_500": round(lr, 3) if lr is not None else None,
        "t500_c": t500,
        "freezing_level_m": freezing,
        "srh_0_3": round(srh_0_3, 1) if srh_0_3 is not None else None,
        "srh_1km": round(srh_1km, 1) if srh_1km is not None else None,
        "ebwd_ms": round(ebwd, 2) if ebwd is not None else None,
        "scp": round(scp, 3) if scp is not None else None,
        "stp": round(stp, 3) if stp is not None else None,
        "ship": round(ship, 3) if ship is not None else None,
        "env_score": score,
        "completeness": completeness,
        "partial": bool(missing),
        "missing": missing,
        "flags": flags,
    }
