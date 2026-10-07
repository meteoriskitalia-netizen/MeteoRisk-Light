#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — aggregate.py (SSI v2, EXPERIMENTAL)

Combinazione pesata dei layer Fase 2 nello Supercell Signature Index v2:

  ssi_v2 = min(100, round( w_base*base + w_hook*hook + w_structure*structure
                           + w_env*env + w_ot*ot + w_lightning*lightning ))

Pesi DEFAULT (somma ESATTAMENTE 1.00, nessun iperparametro):
  base      0.60   SSI Fase 1 (supercell.py) — resta il fondamento
  hook      0.15   morphologia uncino (hook.py)
  structure 0.10   struttura verticale VIL/ETM/POH/overhang (vertical_structure)
  env       0.08   ambiente NWP SCP/STP/SHIP (environment)
  ot        0.04   overshooting top (overshoot)
  lightning 0.03   tasso/jump fulmini (lightning)

Validazioni (ValueError, input malformati):
  - chiavi pesi sconosciute o negative;
  - SOMMA PESI > 1.0 (divieto esplicito: nessuna normalizzazione fittizia);
  - somma < 1.0 e' AMMESSA (il residuo non viene redistribuito: un layer
    assente non gonfia gli altri).

Un componente None (layer non calcolato / dato mancante) contribuisce 0.0 ed
e' elencato in 'missing' con partial=True: nessun valore fabbricato.
Funzione PURA (nessuna I/O).
"""

# ---------------------------------------------------------------------------
# Costanti — pesi di default (somma 1.00, verificata in _validate_weights)
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS = {
    "base": 0.60,
    "hook": 0.15,
    "structure": 0.10,
    "env": 0.08,
    "ot": 0.04,
    "lightning": 0.03,
}
WEIGHTS_MAX_SUM = 1.0
COMPONENT_KEYS = ("base", "hook", "structure", "env", "ot", "lightning")


def _clamp100(x):
    """Valore componente in [0,100]; None/non finito -> 0.0."""
    if x is None:
        return 0.0
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v != v or v in (float("inf"), float("-inf")):
        return 0.0
    return max(0.0, min(100.0, v))


def _validate_weights(weights):
    """Valida i pesi: chiavi note, valori >=0, somma <= 1.0. Ritorna dict."""
    w = dict(weights)
    unknown = set(w) - set(COMPONENT_KEYS)
    if unknown:
        raise ValueError(f"unknown_weight_keys:{sorted(unknown)}")
    missing = set(COMPONENT_KEYS) - set(w)
    if missing:
        raise ValueError(f"missing_weight_keys:{sorted(missing)}")
    total = 0.0
    for k, v in w.items():
        try:
            fv = float(v)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"weight_not_numeric:{k}") from exc
        if fv < 0:
            raise ValueError(f"weight_negative:{k}")
        if fv != fv:
            raise ValueError(f"weight_nan:{k}")
        w[k] = fv
        total += fv
    if total > WEIGHTS_MAX_SUM + 1e-9:
        raise ValueError(f"weights_sum_gt_1:{round(total, 6)}")
    return w, round(total, 6)


def aggregate_ssi_v2(base_ssi, hook, structure, env, ot, lightning,
                     weights=None):
    """Combina i layer in SSI v2 (0-100 intero).

    Ritorna dict con le singole voci (valori clamped 0..100, 1 decimale),
    'ssi_v2' (int), 'weights', 'weights_sum', 'missing' (voci None) e
    'partial' (True se almeno una voce mancante). Funzione pura."""
    w, wsum = _validate_weights(DEFAULT_WEIGHTS if weights is None
                                else weights)
    raw = {
        "base": base_ssi,
        "hook": hook,
        "structure": structure,
        "env": env,
        "ot": ot,
        "lightning": lightning,
    }
    missing = [k for k in COMPONENT_KEYS if raw[k] is None]
    values = {k: _clamp100(raw[k]) for k in COMPONENT_KEYS}

    acc = 0.0
    for k in COMPONENT_KEYS:
        acc += w[k] * values[k]
    ssi_v2 = int(min(100, round(acc)))

    out = {k: round(values[k], 1) for k in COMPONENT_KEYS}
    out["ssi_v2"] = ssi_v2
    out["weights"] = dict(w)
    out["weights_sum"] = wsum
    out["missing"] = missing
    out["partial"] = bool(missing)
    return out
