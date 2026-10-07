#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — overshoot.py (OVERSHOOTING TOP, EXPERIMENTAL)

Rilevamento Overshooting Top (OT) dal canale WV e IR allineati (stessa griglia
del frame satellite/ESA): BTD = WV - IR (K), flag OT dove IR molto freddo
(cima dell'anvillo) E BTD positivo marcato (la cima che sporge sopra l'anvillo
rende il WV piu' caldo dell'IR sottostante).

Regola operativa (soglie EXPERIMENTAL, allineate alla pratica EUMETSAT):
  IR  <= OT_IR_THRESHOLD_K   (215 K)
  BTD >= OT_BTD_THRESHOLD_K  (12 K)
  + erosione morphologica con kernel NxN (default 3x3) per eliminare pixel
    isolati (scipy.ndimage.binary_erosion, no dipendenze extra).

Funzioni PURE su ndarray (nessuna I/O, nessuno stato, determinismo). I grid
arrivano gia' scaricati dal chiamante (fetch satellite separato); celle non
finite -> False (mai un OT costruito sul vuoto).

Sorgente dati dichiarata (lettera A0, 00_ricerca/lettera_a0.md, EVIDENZA):
canali WV 6.2 = `msg_fes:wv062` e IR 10.8 = `msg_fes:ir108` su
`https://view.eumetsat.int/geoserver/ows` (WMS 1.1.1, FORMAT=image/png,
TIME ISO onorato, ANONIMO nessuna chiave; verificati live HTTP 200 in A0).
L'endpoint storico `eumetview.eumetsat.int/geoserver/wms` e' MORTO (redirect
SPA): non usarlo. Frame di esempio salvati in 00_ricerca/eum_msg_fes_*.png
(RGBA 8 bit, 512x384) — esito controllo: interamente opachi, idonei come
regressione del decoder PNG della utility in lightning.py.
"""

import math

import numpy as np
from scipy import ndimage

# ---------------------------------------------------------------------------
# Costanti — EXPERIMENTAL DEFAULTS (non calibrati su casi reali)
# ---------------------------------------------------------------------------
OT_BTD_THRESHOLD_K = 12.0
OT_IR_THRESHOLD_K = 215.0
OT_EROSION_KERNEL = 3            # kernel quadrato NxN (3 = 3x3)
OT_AREA_TH_KM2 = (1.0, 4.0, 9.0)  # area OT (km^2) basso/medio/alto
OT_PIXEL_AREA_DEFAULT_KM2 = 1.0   # se il chiamante non passa pixel_area_km2


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


def _fuzzy3(x, th):
    """Membership a 3 soglie (basso/medio/alto): 0 -> 0.5 -> 1.0."""
    if x is None or not math.isfinite(float(x)):
        return 0.0
    x = float(x)
    lo, mid, hi = th
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
def btd_wv_ir(wv_grid, ir_grid):
    """BTD = WV - IR (K), cella per cella (float64).

    Celle non finite in uno dei due canali -> NaN nel risultato (il flag OT
    le trattera' come False). ValueError solo su shape/misura errate."""
    wv = _as_grid(wv_grid, "wv_grid")
    ir = _as_grid(ir_grid, "ir_grid")
    if wv.shape != ir.shape:
        raise ValueError("shape_mismatch:ir_grid")
    out = np.full(wv.shape, np.nan, dtype="float64")
    valid = np.isfinite(wv) & np.isfinite(ir)
    out[valid] = wv[valid] - ir[valid]
    return out


def ot_flag(btd_grid, ir_grid, btd_th=None, ir_th=None, kernel=None):
    """Flag OT booleano: IR <= ir_th E BTD >= btd_th, poi erosione NxN.

    btd_th / ir_th: override soglie (default OT_BTD_THRESHOLD_K /
    OT_IR_THRESHOLD_K). kernel: lato del kernel quadrato di erosione
    (default OT_EROSION_KERNEL; 1 o None = nessuna erosione).
    Ritorna ndarray bool. Celle non finite -> False."""
    btd = _as_grid(btd_grid, "btd_grid")
    ir = _as_grid(ir_grid, "ir_grid")
    if btd.shape != ir.shape:
        raise ValueError("shape_mismatch:ir_grid")
    bt = float(OT_BTD_THRESHOLD_K if btd_th is None else btd_th)
    it = float(OT_IR_THRESHOLD_K if ir_th is None else ir_th)
    k = int(OT_EROSION_KERNEL if kernel is None else kernel)

    mask = (np.isfinite(btd) & np.isfinite(ir)
            & (ir <= it) & (btd >= bt))
    if k > 1:
        structure = np.ones((k, k), dtype=bool)
        mask = ndimage.binary_erosion(mask, structure=structure,
                                      border_value=0)
    return mask


def ot_score_from_flags(flags, pixel_area_km2=None):
    """Score OT 0-100 (float, 1 decimale) dai flag (dopo erosione).

    Normalizzazione fuzzy 3 sull'AREA totale dei flag
    (OT_AREA_TH_KM2 = 1 / 4 / 9 km^2). pixel_area_km2: area di un pixel
    (RasterData.pixel_area_km2 della Fase 1); default
    OT_PIXEL_AREA_DEFAULT_KM2 (1 px = 1 km^2) se non passato.
    Nessun flag -> 0.0."""
    f = np.asarray(flags, dtype=bool)
    if f.ndim != 2:
        raise ValueError("flags_2d_required")
    n = int(f.sum())
    if n == 0:
        return 0.0
    area_px = float(OT_PIXEL_AREA_DEFAULT_KM2 if pixel_area_km2 is None
                    else float(pixel_area_km2))
    if not math.isfinite(area_px) or area_px <= 0:
        area_px = OT_PIXEL_AREA_DEFAULT_KM2
    area_km2 = n * area_px
    return round(100.0 * _fuzzy3(area_km2, OT_AREA_TH_KM2), 1)
