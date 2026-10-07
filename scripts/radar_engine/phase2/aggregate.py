#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — aggregate.py (SSI v2, EXPERIMENTAL)

Combinazione pesata dei layer Fase 2 nello Supercell Signature Index v2:

  ssi_v2 = min(100, round( sum_k w_eff[k] * value[k] ))   k sui componenti PRESENTI

Pesi DEFAULT (somma ESATTAMENTE 1.00, nessun iperparametro):
  base      0.60   SSI Fase 1 (supercell.py) — resta il fondamento
  hook      0.15   morphologia uncino (hook.py)
  structure 0.10   struttura verticale VIL/ETM/POH/overhang (vertical_structure)
  env       0.08   ambiente NWP SCP/STP/SHIP (environment)
  ot        0.04   overshooting top (overshoot)
  lightning 0.03   tasso/jump fulmini (lightning)

Politica RINORMALIZZAZIONE PESI (B2, PHASE2_VERSION 0.4.0 — componenti per
candidato): i componenti None (layer non calcolato / dato mancante per
QUEL candidato) vengono ESCLUSI e i pesi dei componenti PRESENTI vengono
ridistribuiti in modo che la somma dei pesi usati sia ESATTAMENTE 1.00
(w_eff[k] = w[k] / sum(w[j] per j presenti)). Conseguenze esplicite:
  - un layer assente NON abbassa piu' lo score degli altri (prima: contribuiva
    0.0 a peso pieno, sottostimando sistematicamente i candidati con dati
    parziali);
  - tutti i componenti None -> ssi_v2 = None (nessun punteggio fabbricato da
    nessun dato);
  - componenti presenti ma tutti a peso 0 -> ssi_v2 = None (nessuna base per
    una media pesata);
  - 'weights' resta il dict RICHIESTO (validato: somma <= 1.0) e
    'weights_effective' documenta i pesi REALMENTE usati sui presenti.

Validazioni (ValueError, input malformati):
  - chiavi pesi sconosciute, mancanti o negative;
  - SOMMA PESI RICHIESTI > 1.0 (divieto esplicito: nessuna normalizzazione
    fittizia sui pesi di default).

Un componente None resta None nel dict di output (MAI convertito in 0.0: 0.0
sarebbe un valore fabbricato) ed e' elencato in 'missing' con partial=True.
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
    """Valore componente in [0,100]; None/non finito -> 0.0 (solo per il
    calcolo interno dei componenti PRESENTI: i None restano None in output)."""
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


def effective_weights(weights, present):
    """Pesi RINORMALIZZATI sui componenti presenti (somma esattamente 1.00).

    present: iterable di chiavi componente. Ritorna dict {chiave: w/sum} SOLO
    per le chiavi presenti (ordine COMPONENT_KEYS). Se la somma dei pesi dei
    presenti e' 0 -> dict vuoto (nessuna media pesata possibile, nessun
    valore inventato). ValueError per chiavi sconosciute (input malformati)."""
    unknown = set(present) - set(COMPONENT_KEYS)
    if unknown:
        raise ValueError(f"unknown_weight_keys:{sorted(unknown)}")
    w, _ = _validate_weights(weights)
    keys = [k for k in COMPONENT_KEYS if k in set(present)]
    total = sum(w[k] for k in keys)
    if total <= 0.0:
        return {}
    return {k: w[k] / total for k in keys}


def aggregate_ssi_v2(base_ssi, hook, structure, env, ot, lightning,
                     weights=None):
    """Combina i layer in SSI v2 (0-100 intero) con RINORMALIZZAZIONE PESI.

    Componenti None ESCLUSI e pesi ridistribuiti sui presenti (somma 1.00);
    tutti None -> ssi_v2 None. Ritorna dict con le singole voci (componenti
    presenti clamped 0..100 con 1 decimale, componenti assenti None),
    'ssi_v2' (int | None), 'weights' (richiesti), 'weights_effective' (usati),
    'weights_sum', 'present', 'missing' (voci None) e 'partial' (True se
    almeno una voce mancante). Funzione pura."""
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
    present = [k for k in COMPONENT_KEYS if raw[k] is not None]
    missing = [k for k in COMPONENT_KEYS if raw[k] is None]

    out = {k: None for k in COMPONENT_KEYS}
    for k in present:
        out[k] = round(_clamp100(raw[k]), 1)

    eff = effective_weights(w, present)
    ssi_v2 = None
    if eff:
        acc = sum(eff[k] * _clamp100(raw[k]) for k in present)
        ssi_v2 = int(min(100, round(acc)))

    out["ssi_v2"] = ssi_v2
    out["weights"] = dict(w)
    out["weights_effective"] = {k: round(v, 6) for k, v in eff.items()}
    out["weights_effective_sum"] = round(sum(eff.values()), 6)
    out["weights_sum"] = wsum
    out["present"] = present
    out["missing"] = missing
    out["partial"] = bool(missing)
    return out
