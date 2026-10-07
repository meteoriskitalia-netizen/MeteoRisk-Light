#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — vertical_structure.py (STRUTTURE VERTICALI, EXPERIMENTAL)

Strutture verticali della convezione dai prodotti DPC addizionali, tutti
allineati alla stessa griglia del frame (capiri ESA-live / VMI):

  VIL     - Volumetric Liquidated Index (kg/m^2)         prodotto DPC "VIL"
  ETM     - Echo Top (km)                                 prodotto DPC "ETM"
  POH     - Probability Of Hail (%)                       prodotto DPC "POH"
  CAPPI   - riflettivita' a quota fissa (2 km low, 6 km high) prodotti "CAPPI"
            (in assenza dei CAPPI dedicati si usano i campi gia' disponibili
             alla quota indicata dal chiamante: qui si consumano GRIGLIE)

Funzioni PURE su ndarray (nessuna I/O, nessuno stato, determinismo): il
download/intercalazione dei prodotti e' compito del chiamante (fetch Fase 1
generalizzato), qui entrano solo array numpy allineati per shape.

Fuzzy a tre soglie (basso/medio/alto) per ciascun descrittore, combinato con
STRUCTURE_WEIGHTS (somma 1.00) nello structure_score 0-100.
"""

import math

import numpy as np

# ---------------------------------------------------------------------------
# Costanti — EXPERIMENTAL DEFAULTS (non calibrati su casi reali)
# ---------------------------------------------------------------------------
STRUCTURE_VIL_TH = (20.0, 35.0, 50.0)        # kg/m^2  (basso/medio/alto)
STRUCTURE_ETM_TH = (6.0, 9.0, 12.0)          # km
STRUCTURE_POH_TH = (30.0, 50.0, 70.0)        # %
STRUCTURE_OVERHANG_TH = (0.15, 0.30, 0.50)   # frazione [0,1]
STRUCTURE_DENSITY_TH = (0.30, 0.55, 0.85)    # kg/m^2 per km di echo top
STRUCTURE_DEFAULT_DBZ = 40.0                 # soglia dBZ per l'overhang
STRUCTURE_WEIGHTS = {                        # somma 1.00
    "vil": 0.35,
    "etm": 0.25,
    "poh": 0.25,
    "overhang": 0.15,
}


# ---------------------------------------------------------------------------
# Helper locali (puri)
# ---------------------------------------------------------------------------
def _clamp01(x):
    return max(0.0, min(1.0, float(x)))


def _as_grid(a, name):
    g = np.asarray(a, dtype="float64")
    if g.ndim != 2:
        raise ValueError(f"{name}_2d_required")
    return g


def _same_shape(*grids):
    ref = grids[0].shape
    for i, g in enumerate(grids[1:], start=1):
        if g.shape != ref:
            raise ValueError(f"shape_mismatch:grid_{i}")


def _finite_max(g):
    """Massimo su celle finite; -inf -> None (dato assente, mai inventato)."""
    v = g[np.isfinite(g)]
    return float(v.max()) if v.size else None


def _finite_mean(g):
    v = g[np.isfinite(g)]
    return float(v.mean()) if v.size else None


def _fuzzy3(x, th):
    """Membership a 3 soglie: 0 sotto th[0], rampa a 0.5 su th[0..1],
    rampa a 1.0 su th[1..2], 1.0 sopra th[2]. th = (basso, medio, alto)."""
    if x is None or not math.isfinite(float(x)):
        return 0.0
    x = float(x)
    lo, mid, hi = float(th[0]), float(th[1]), float(th[2])
    if x <= lo:
        return 0.0
    if x <= mid:
        return 0.5 * _clamp01((x - lo) / max(mid - lo, 1e-9))
    if x <= hi:
        return 0.5 + 0.5 * _clamp01((x - mid) / max(hi - mid, 1e-9))
    return 1.0


# ---------------------------------------------------------------------------
# API pure
# ---------------------------------------------------------------------------
def vil_density(vil_grid, etm_grid):
    """Densita' VIL/echo-top (kg/m^2 per km) cella per cella.

    vil_grid:  VIL (kg/m^2), etm_grid: echo top (km). Dove ETM <= 0 o non
    finito il risultato e' 0.0 (cella invalida: nessuna divisione per zero).
    Ritorna ndarray float64 della stessa forma."""
    vil = _as_grid(vil_grid, "vil_grid")
    etm = _as_grid(etm_grid, "etm_grid")
    _same_shape(vil, etm)
    out = np.zeros(vil.shape, dtype="float64")
    valid = np.isfinite(vil) & np.isfinite(etm) & (etm > 0.0)
    out[valid] = vil[valid] / etm[valid]
    return out


def overhang_index(low_grid_2, high_grid_6, threshold=None):
    """Indice di overhang (weak echo region proxy) fra CAPPI 2 km e 6 km.

    Frazione dell'eco ALTA quota (>= threshold dBZ) che NON ha eco bassa
    corrispondente: sum(high & ~low) / sum(high). Valori [0,1]; 0.0 se nessuna
    eco alta (dato assente -> 0, con struttura verificabile dal chiamante).
    threshold: soglia dBZ (default STRUCTURE_DEFAULT_DBZ)."""
    low = _as_grid(low_grid_2, "low_grid_2")
    high = _as_grid(high_grid_6, "high_grid_6")
    _same_shape(low, high)
    thr = float(STRUCTURE_DEFAULT_DBZ if threshold is None else threshold)
    hi = np.isfinite(high) & (high >= thr)
    lo = np.isfinite(low) & (low >= thr)
    n_hi = int(hi.sum())
    if n_hi == 0:
        return 0.0
    return round(float((hi & ~lo).sum()) / n_hi, 4)


def poh_etm_scalars(poh_grid, etm_grid):
    """Scalari POH (%) e ETM (km) da griglie prodotto.

    Ritorna dict: poh_max, poh_mean, etm_max, etm_mean, n_valid. Le chiavi
    sono None quando la griglia non contiene celle finite (dato mancante:
    MAI sostituito con un valore inventato)."""
    poh = _as_grid(poh_grid, "poh_grid")
    etm = _as_grid(etm_grid, "etm_grid")
    _same_shape(poh, etm)
    both = np.isfinite(poh) & np.isfinite(etm)
    return {
        "poh_max": _finite_max(poh),
        "poh_mean": _finite_mean(poh),
        "etm_max": _finite_max(etm),
        "etm_mean": _finite_mean(etm),
        "n_valid": int(both.sum()),
    }


def structure_features(vil_grid, etm_grid, poh_grid, low_grid_2, high_grid_6,
                       threshold=None, vil_density_grid=None):
    """Descrittori + membership fuzzy dei 4 componenti (per audit/trasparenza).

    Ritorna dict con gli scalari grezzi e le membership 0..1 (key 'm_*')."""
    vil = _as_grid(vil_grid, "vil_grid")
    etm = _as_grid(etm_grid, "etm_grid")
    poh = _as_grid(poh_grid, "poh_grid")
    low = _as_grid(low_grid_2, "low_grid_2")
    high = _as_grid(high_grid_6, "high_grid_6")
    _same_shape(vil, etm, poh, low, high)

    vil_max = _finite_max(vil)
    etm_max = _finite_max(etm)
    poh_max = _finite_max(poh)
    overhang = overhang_index(low, high, threshold)

    dens_max = None
    if vil_density_grid is not None:
        dens_max = _finite_max(_as_grid(vil_density_grid, "vil_density_grid"))

    return {
        "vil_max": vil_max,
        "etm_max": etm_max,
        "poh_max": poh_max,
        "overhang": overhang,
        "density_max": dens_max,
        "m_vil": _fuzzy3(vil_max, STRUCTURE_VIL_TH),
        "m_etm": _fuzzy3(etm_max, STRUCTURE_ETM_TH),
        "m_poh": _fuzzy3(poh_max, STRUCTURE_POH_TH),
        "m_overhang": _fuzzy3(overhang, STRUCTURE_OVERHANG_TH),
    }


def structure_score(vil_grid, etm_grid, poh_grid, low_grid_2, high_grid_6,
                    threshold=None, weights=None):
    """Score struttura verticale 0-100 (float, 1 decimale), fuzzy 3 soglie.

    Combina le membership dei descrittori (VIL, ETM, POH, overhang) con i pesi
    STRUCTURE_WEIGHTS (somma 1.00). Un descrittore non disponibile (None) vale
    membership 0 e NON viene rinormalizzato: dati mancanti abbassano lo score
    (nessun iperparametro). weights: dict opzionale che sovrascrive i pesi
    (stesse chiavi; somma <= 1.0 verificata)."""
    feats = structure_features(vil_grid, etm_grid, poh_grid, low_grid_2,
                               high_grid_6, threshold)
    w = dict(STRUCTURE_WEIGHTS) if weights is None else dict(weights)
    unknown = set(w) - set(STRUCTURE_WEIGHTS)
    if unknown:
        raise ValueError(f"unknown_weight_keys:{sorted(unknown)}")
    if sum(float(v) for v in w.values()) > 1.0 + 1e-9:
        raise ValueError("weights_sum_gt_1")

    total = 0.0
    for key, wkey in (("m_vil", "vil"), ("m_etm", "etm"), ("m_poh", "poh"),
                      ("m_overhang", "overhang")):
        total += float(feats[key]) * float(w.get(wkey, 0.0))
    return round(100.0 * _clamp01(total), 1)
