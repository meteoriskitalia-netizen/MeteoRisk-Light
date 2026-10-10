#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — vortex.py: logica VORTICI (proxy da RIFLETTIVITA', NO Doppler).

Tier (THRESHOLDS_VERSION in phenomena/__init__.py):
  SUSPECT       — evidenza OSSERVATA SOSTENUTA, tutte richieste:
                  (i) persistenza (>= VORTEX_MIN_POINTS punti recenti o
                      duration_min >= VORTEX_MIN_DURATION_MIN per i target
                      senza punti);
                  (ii) core convettivo reale (max_dbz >= VORTEX_MIN_DBZ);
                  (iii) >= VORTEX_MIN_FRAMES frame osservati (un track di 1-2
                        frame e' uno scan isolato, non evidenza sostenuta);
                  (iv) gate primario: hook-proxy morfologico coerente su
                       >= MORPH_HOOK_MIN_FRAMES frame OPPURE organization_score
                       >= VORTEX_ORG_SUSPECT (76 = inizio della banda "Highly
                       Organized Convective Cell"; NON la soglia del candidato
                       supercella organization_organized = 56, che promuoveva
                       quasi ogni cella organizzata -> falsi positivi).
                  L'evidenza fulmini (gate provider-agnostico
                  lightning_corroborates: strength/soglia della sorgente con
                  fallback count_near per DPC, OPPURE trend in salita) viene
                  COMBINATA in labels+score quando il provider e' disponibile.
  CORROBORATED  — gate primario E almeno UN corroboratore INDIPENDENTE:
                  organization_score >= VORTEX_ORG_CORROBORATED (80) con
                  fulmini forti, OPPURE hook-proxy sostenuto con struttura
                  verticale forte (composito VIL/ETM/POH/overhang) o con
                  overshooting top da IR_108 (satellite_ot.ot_flag: evidenza
                  satellite indipendente della cima convettiva). Senza
                  corroborazione l'evento resta SUSPECT (severita' bassa).
  VERIFIED      — NON popolato (arrivera' con report esterni ESWD).

Sorgente non puntuale (MLI/AFA): evidence.lightning.note = "AFA proxy (no
flash puntuali)" + attribution EUMETSAT, count_near in PIXEL (label esplicita).

Ogni label esplicita: "proxy riflettività, nessun dato Doppler" — il motore
NON dichiara mai mesociclone/tornado.
"""

from . import THRESHOLDS_VERSION
from .hail import structure_evidence
from .lightning import lightning_corroborates, lightning_strength

# Organization Score richiesto per l'EMISSIONE del badge (SUSPECT): 76 e' la
# soglia della banda "Highly Organized Convective Cell" (config.py:
# 56-75 Organized / 76-100 Highly Organized). Il gate del CANDIDATO supercella
# e' organization_organized (Organization Score >= 56): emettere VORTEX a 60
# promuoveva praticamente ogni cella organizzata (banda 56-75) -> ~166
# badge/24h, in gran parte falsi positivi. Il badge VORTEX (proxy di rotazione
# da riflettivita') richiede organizzazione ALTA, non la soglia del candidato.
VORTEX_ORG_SUSPECT = 76         # organization_score per SUSPECT
VORTEX_ORG_CORROBORATED = 80    # organization_score per CORROBORATED
VORTEX_MIN_POINTS = 2           # punti recenti di persistenza
VORTEX_MIN_DURATION_MIN = 10.0  # persistenza per target senza punti
# Evidenza OSSERVATA SOSTENUTA: almeno VORTEX_MIN_FRAMES frame distinti
# (frame morfologici, punti recenti o n_frames del track). Un track visto su
# 1-2 frame e' uno scan isolato: non giustifica un badge.
VORTEX_MIN_FRAMES = 3
# Guardia di riflettivita': un proxy di vortice richiede un core convettivo
# reale (stessa soglia del gate supercella intensity_core_dbz = 45). Esclude
# echi deboli/stratiformi e osservazioni senza dato radar.
VORTEX_MIN_DBZ = 45.0
# Corroboratore strutturale per la severita' ALTA: composito verticale forte
# (stesse soglie/pesi di HAIL e di radar_engine.phase2.vertical_structure).
VORTEX_STRUCTURE_STRONG = 0.55
# Proxy morfologico "hook-ish" (soglie sperimentali sulle celle di storms.geojson)
MORPH_ECC_MIN = 0.90
MORPH_SOLIDITY_MAX = 0.85
MORPH_COMPACTNESS_MIN = 3.0
# Coerenza temporale della firma hook: le TRE condizioni devono reggere nello
# STESSO frame e in almeno K frame distinti. Massimo eccentricity + minimo
# solidity + massima compattita' presi su frame DIVERSI non bastano piu'
# (celle allungate in un frame e frastagliate in un altro -> falsi positivi
# su linee/squall line/bow echo/merger/anvil). K alzato da 2 a 3 (>= 15 min).
MORPH_HOOK_MIN_FRAMES = 3       # K: frame hook coerenti richiesti

DOPPLER_DISCLAIMER = "proxy riflettività, nessun dato Doppler"


def _num(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v == v else None


def _frame_passes(frame):
    """True se UN frame soddisfa insieme ecc>=MIN, solidity<=MAX, comp>=MIN.

    Nessuna combinazione fra valori di frame diversi: e' il test di forma
    COERENTE nel tempo (stesso frame, tutte e tre le condizioni)."""
    if not isinstance(frame, dict):
        return False
    ecc = _num(frame.get("eccentricity"))
    sol = _num(frame.get("solidity"))
    comp = _num(frame.get("compactness"))
    if ecc is None or sol is None or comp is None:
        return False
    return (ecc >= MORPH_ECC_MIN and sol <= MORPH_SOLIDITY_MAX
            and comp >= MORPH_COMPACTNESS_MIN)


def _frame_groups(obs):
    """{chiave_frame: [campioni]} da obs["morph_frames"].

    Un campione con lo stesso timestamp e' LO STESSO frame (le chiavi sono
    timestamp_ms, poi timestamp, poi l'indice se il frame non ha tempo).
    Observazione assente/non-lista -> {} (nessun dato per-frame)."""
    if not isinstance(obs, dict):
        return {}
    frames = obs.get("morph_frames")
    if not isinstance(frames, list):
        return {}
    grouped = {}
    for idx, frame in enumerate(frames):
        if not isinstance(frame, dict):
            continue
        key = frame.get("timestamp_ms")
        if key is None:
            key = frame.get("timestamp")
        if key is None:
            key = ("no-timestamp", idx)
        grouped.setdefault(key, []).append(frame)
    return grouped


def hook_frame_count(obs):
    """Frame distinti in cui la cella mostra la firma hook COMPLETA.

    Conta i frame (timestamp distinti) in cui ecc/solidity/compattita'
    reggono insieme; campioni discordanti dello stesso timestamp fanno
    fallire quel frame (fail-closed: stesso frame = una sola verita').
    Nessun dato per-frame -> 0: la coerenza temporale non e' verificabile,
    quindi nessuna promozione dalla sola morfologia."""
    return sum(1 for samples in _frame_groups(obs).values()
               if all(_frame_passes(sample) for sample in samples))


def is_hookish(obs):
    """True se la cella e' "hook-ish" con coerenza temporale.

    Richiede la firma completa (eccentricita' alta + solidity bassa +
    compattita' alta) NELLO STESSO frame e in >= MORPH_HOOK_MIN_FRAMES
    frame distinti (hook_frame_count). Il vecchio test su
    eccentricity_max/solidity_min/compactness_max calcolati su TUTTI i
    frame in modo indipendente promuoveva celle che superavano una
    condizione per frame diverso -> falsi positivi: qui non si combinano
    mai valori di frame diversi. Tutte e tre le condizioni devono reggere
    (i valori medi delle celle sono gia' eccentrici: con una sola
    condizione il gate non discrimina nulla)."""
    if not isinstance(obs, dict):
        return False
    return hook_frame_count(obs) >= MORPH_HOOK_MIN_FRAMES


def observed_frame_count(obs):
    """Frame distinti osservati per la cella (evidenza sostenuta).

    Massimo fra: frame morfologici distinti (morph_frames), punti recenti del
    track e n_frames dichiarato dal track. Nessun dato -> 0: la sostenibilita'
    non e' verificabile, quindi il solo organization_score non giustifica il
    badge (fail-closed)."""
    if not isinstance(obs, dict):
        return 0
    counts = [len(_frame_groups(obs))]
    pts = obs.get("recent_points")
    if isinstance(pts, list):
        counts.append(len(pts))
    n = _num(obs.get("n_frames"))
    if n is not None and n > 0:
        counts.append(int(n))
    return max(counts) if counts else 0


def vortex_score(state, organization_score, lightning):
    """Score 0-100: base dal tier + contributi (organization, fulmini, trend).

    Contributi limitati a 29 punti -> SUSPECT <= 69, CORROBORATED <= 99."""
    if state == "VERIFIED":
        return 100.0
    base = {"SUSPECT": 40.0, "CORROBORATED": 70.0}.get(state, 40.0)
    org = _num(organization_score)
    org_t = max(0.0, min(1.0, (org - VORTEX_ORG_SUSPECT) / 20.0)) \
        if org is not None else 0.0
    ltg = lightning if isinstance(lightning, dict) else {}
    ltg_t = lightning_strength(ltg)        # strength 0..1, fallback count/30
    direction = (ltg.get("trend") or {}).get("direction") \
        if isinstance(ltg.get("trend"), dict) else None
    trend_t = 1.0 if direction == "up" else 0.0
    raw = base + 29.0 * (0.50 * org_t + 0.30 * ltg_t + 0.20 * trend_t)
    return round(min(99.9, raw), 1)


def evaluate_vortex(obs, lightning=None):
    """Valuta VORTEX su un'osservazione normalizzata -> evento dict oppure None.

    Emissione SOLO con evidenza osservata sostenuta:
      1. persistenza (>= VORTEX_MIN_POINTS punti recenti o durata >=
         VORTEX_MIN_DURATION_MIN per i target senza punti);
      2. core convettivo reale (max_dbz >= VORTEX_MIN_DBZ);
      3. sostenibilita' (>= VORTEX_MIN_FRAMES frame osservati, oppure durata
         sufficiente per i target "duration");
      4. gate primario: hook morfologico coerente su >= MORPH_HOOK_MIN_FRAMES
         frame OPPURE organization_score >= VORTEX_ORG_SUSPECT.
    Severita': CORROBORATED solo se il gate primario E' corroborato da almeno
    una fonte INDIPENDENTE (organization_score >= VORTEX_ORG_CORROBORATED con
    fulmini forti, OPPURE hook sostenuto con struttura verticale forte). Senza
    corroborazione l'evento resta SUSPECT (severita' bassa)."""
    if not isinstance(obs, dict) or not obs.get("anchor"):
        return None
    source = obs.get("persistence_source") or "points"
    pts = [p for p in (obs.get("recent_points") or []) if isinstance(p, dict)]
    duration = _num(obs.get("duration_min"))
    if source == "duration":
        persistent = bool(obs.get("on_latest_frame")) \
            and duration is not None and duration >= VORTEX_MIN_DURATION_MIN
    else:
        persistent = len(pts) >= VORTEX_MIN_POINTS \
            or (duration is not None and duration >= VORTEX_MIN_DURATION_MIN)
    if not persistent:
        return None

    max_dbz = _num(obs.get("max_dbz"))
    if max_dbz is None or max_dbz < VORTEX_MIN_DBZ:
        return None                       # nessun core convettivo: nessun badge

    org = _num(obs.get("organization_score"))
    hook_frames = hook_frame_count(obs)
    hookish = hook_frames >= MORPH_HOOK_MIN_FRAMES   # == is_hookish(obs)
    org_gate = org is not None and org >= VORTEX_ORG_SUSPECT
    frames_observed = observed_frame_count(obs)
    sustained = (hookish
                 or frames_observed >= VORTEX_MIN_FRAMES
                 or (source == "duration" and duration is not None
                     and duration >= VORTEX_MIN_DURATION_MIN))
    if not (sustained and (org_gate or hookish)):
        return None

    ltg = lightning if isinstance(lightning, dict) else {"available": False}
    ltg_avail = bool(ltg.get("available"))
    count_near = int(ltg.get("count_near") or 0) if ltg_avail else 0
    strength = lightning_strength(ltg) if ltg_avail else None
    trend_dir = None
    if ltg_avail and isinstance(ltg.get("trend"), dict):
        trend_dir = ltg["trend"].get("direction")
    ltg_score = ltg.get("score") if ltg_avail else None
    ltg_strong = ltg_avail and (trend_dir == "up"
                                or lightning_corroborates(ltg))
    se = structure_evidence(obs.get("vertical"))
    structure_strong = se["composite"] >= VORTEX_STRUCTURE_STRONG
    # Overshooting top (IR_108) -> corroboratore SATELLITE indipendente.
    # Fail-closed: nessun OT -> ot_ok False (comportamento invariato).
    ot_info = obs.get("satellite_ot")
    ot_info = ot_info if isinstance(ot_info, dict) else {}
    ot_ok = bool(ot_info.get("ot_flag"))
    corroboration = bool(ltg_strong or structure_strong)
    strong_org = org is not None and org >= VORTEX_ORG_CORROBORATED
    # Severita' ALTA solo con corroborazione indipendente: org forte + fulmini/
    # OT, oppure hook sostenuto + struttura verticale forte/OT.
    corroborated = (strong_org and (corroboration or ot_ok)) or (
        hookish and (structure_strong or ot_ok))
    state = "CORROBORATED" if corroborated else "SUSPECT"

    evidence = {
        "anchor": obs.get("anchor"),
        "track_type": obs.get("type"),
        "classification": obs.get("classification"),
        "persistence_source": source,
        "organization_score": int(org) if org is not None else None,
        "hookish_morphology": bool(hookish),
        "morphology": {
            "eccentricity_max": _num(obs.get("eccentricity_max")),
            "solidity_min": _num(obs.get("solidity_min")),
            "compactness_max": _num(obs.get("compactness_max")),
            "hook_frames": hook_frames,
            "hook_frames_required": MORPH_HOOK_MIN_FRAMES,
            "frames_observed": len(_frame_groups(obs)),
        },
        "recent_points": len(pts),
        "duration_min": duration,
        "max_dbz": max_dbz,
        "observed_frames": frames_observed,
        "frames_required": VORTEX_MIN_FRAMES,
        "sustained": bool(sustained),
        "frame_count": frames_observed,
        "structure": dict(se, strong=bool(structure_strong)),
        "satellite_ot": {
            "available": bool(ot_info),
            "flag": bool(ot_ok),
            "score": _num(ot_info.get("score")),
            "ctt_min_c": _num(ot_info.get("ctt_min_c")),
            "n_flags": ot_info.get("n_flags"),
            "cold_top": bool(ot_info.get("cold_top")),
        },
        "corroboration": {"lightning": bool(ltg_strong),
                          "structure": bool(structure_strong),
                          "ot": bool(ot_ok)},
        "n_frames": obs.get("n_frames"),
        "lightning": {
            "available": ltg_avail,
            "source": ltg.get("source"),
            "count_near": count_near,
            "count_total": int(ltg.get("count_total") or 0),
            "radius_km": ltg.get("radius_km"),
            "direction": trend_dir,
            "score": ltg_score,
            "reason": ltg.get("reason"),
            "strength": strength,
            "note": ltg.get("note"),
            "attribution": ltg.get("attribution"),
        },
        "doppler": False,
        "labels": [],
    }

    labels = [f"tier {state}"]
    labels.append(f"evidenza sostenuta: {frames_observed} frame osservati "
                  f"(>= {VORTEX_MIN_FRAMES}), max_dbz {max_dbz:.0f} "
                  f"(>= {VORTEX_MIN_DBZ:.0f})")
    if org is not None:
        labels.append(f"organization_score {org:.0f} "
                      f"(>= {VORTEX_ORG_SUSPECT} per suspect, "
                      f">= {VORTEX_ORG_CORROBORATED} per corroborated)")
    if hookish:
        labels.append(f"morfologia hook-proxy coerente su {hook_frames} frame "
                      f"(>= {MORPH_HOOK_MIN_FRAMES}; ecc alta / solidity bassa "
                      f"nello stesso frame)")
    if source == "duration":
        labels.append(f"persistenza {duration:.0f} min "
                      f"(>= {VORTEX_MIN_DURATION_MIN:.0f})")
    else:
        labels.append(f"persistenza {len(pts)} punti recenti")
    if structure_strong:
        labels.append(f"struttura verticale forte (composito "
                      f"{se['composite']:.2f} >= {VORTEX_STRUCTURE_STRONG})")
    if ot_ok:
        ctt = _num(ot_info.get("ctt_min_c"))
        ot_label = "overshooting top (IR_108)"
        if ctt is not None:
            ot_label += f": CTT min {ctt:.1f} °C"
        labels.append(ot_label)
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
                          f"(trend {trend_dir}, score {ltg_score})")
        if ltg.get("attribution"):
            labels.append(str(ltg["attribution"]))
    else:
        labels.append("fulmini non disponibili")
    labels.append(DOPPLER_DISCLAIMER)
    evidence["labels"] = labels

    return {
        "id": f"VORTEX-{obs['anchor']}",
        "type": "VORTEX",
        "state": state,
        "score": vortex_score(state, org, ltg),
        "first_seen": obs.get("first_seen"),
        "last_seen": obs.get("last_seen"),
        "position": list(obs.get("position") or []),
        "evidence": evidence,
        "thresholds_version": THRESHOLDS_VERSION,
    }
