#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — satellite_ot.py (OVERSHOOTING TOP da IR_108 DPC)

Rilevamento Overshooting Top (OT) dalla TEMPERATURA DI SOMMITA' NUvolosa (CTT)
del canale IR 10.8 um MSG SEVIRI, prodotto DPC `IR_108` (GeoTIFF Float32, °C,
griglia 1200x1400 Transverse Mercator Italia, passo 1 km, STESSA griglia della
VMI). Evidenza file reale (verificata in questa sessione):
  IR_108 10-10-2026-13-35.tif -> GTiff, 1 banda, float32, 1200x1400, res 1000 m,
  CRS TM custom (lat0=42, lon0=12.5) identico alla VMI, valori °C -70.5..+32.5,
  nodata non dichiarato, 100% pixel finiti, periodo PT5M.

METODO — proxy IRW-texture (Bedka et al. 2010, J. Appl. Meteor. Climatol., 49,
181-202; GOES-R AWG "Overshooting Top and Enhanced-V" ATBD, 2015):
un OT e' un piccolo cluster di pixel MOLTO piu' freddi dell'anvillo circostante
(diametro <= ~15 km). Per ogni pixel "freddo" candidato (CTT <= 215 K) si
campiona la BT dell'anvillo a un raggio di ~8 km in 16 direzioni; l'anvillo
valido e' BT <= 225 K e servono >= 5 campioni validi su 16; il candidato e'
OT se e' almeno 6.5 K piu' freddo della media anvillo. I pixel cosi' marcati
vengono erosi (3x3) per eliminare gli isolati e lo score deriva dall'AREA
(delega a overshoot.ot_score_from_flags, unica fonte di verita' sulle soglie
d'area).

LIMITI DICHIARATI (nessun artifatto):
  - il BTD WV-IR (overshoot.py, canale WV 6.2) NON e' calcolabile: il canale WV
    non e' tra i prodotti DPC esposti. Qui si usa SOLO l'IR 10.8.
  - la temperatura di tropopausa NWP usata da Bedka come gate NON e'
    disponibile: il gate d'anomalia locale rispetto all'anvillo la sostituisce
    (maggiore sensibilita', nessuna verifica esplicita di superamento della
    tropopausa).
  - il pixel e' 1 km ma la risoluzione EFFETTIVA del canale SEVIRI e' ~3 km:
    gli OT piu' piccoli possono risultare smussati/piu' caldi.
  - soglie EXPERIMENTAL: non calibrate su casi reali italiani.

Funzioni PURE su ndarray (nessuna I/O, nessuno stato, determinismo). Celle non
finite -> mai un OT costruito sul vuoto (fail-closed).
"""

import math

import numpy as np
from scipy import ndimage

from . import overshoot as _overshoot

# ---------------------------------------------------------------------------
# Costanti — EXPERIMENTAL DEFAULTS (Bedka et al. 2010 / GOES-R ATBD)
# ---------------------------------------------------------------------------
IR_CTT_THRESHOLD_C = -58.0      # 215 K: soglia "pixel freddo" (Bedka 2010)
IR_ANVIL_THRESHOLD_C = -48.0    # 225 K: BT anvillo ammissibile nel campione
IR_ANOMALY_THRESHOLD_C = 6.5    # OT >= 6.5 K piu' freddo dell'anvillo (B10)
IR_RING_RADIUS_PX = 8           # ~8 km a 1 km/pixel (raggio di campionamento)
IR_RING_DIRECTIONS = 16         # 16 direzioni (Bedka 2010)
IR_MIN_ANVIL_SAMPLES = 5        # >= 5/16 campioni d'anvillo validi (B10)
IR_EROSION_KERNEL = 3           # erosione NxN (1/None = nessuna)
IR_PIXEL_AREA_DEFAULT_KM2 = 1.0  # se il chiamante non passa pixel_area_km2


# ---------------------------------------------------------------------------
# Helper locali (puri)
# ---------------------------------------------------------------------------
def _as_grid(a, name):
    g = np.asarray(a, dtype="float64")
    if g.ndim != 2:
        raise ValueError(f"{name}_2d_required")
    return g


def _ring_offsets(radius_px, n_dirs):
    """Offset interi (row, col) dei campioni d'anvillo sul ring.

    n_dirs direzioni equispaziate a raggio radius_px; gli offset sono
    arrotondati al pixel piu' vicino e DEDUPLICATI (a raggi piccoli alcune
    direzioni possono collassare sullo stesso pixel)."""
    r = float(radius_px)
    seen = set()
    offsets = []
    for k in range(int(n_dirs)):
        ang = 2.0 * math.pi * k / float(n_dirs)
        dr = int(round(r * math.sin(ang)))
        dc = int(round(r * math.cos(ang)))
        if (dr, dc) in seen:
            continue
        seen.add((dr, dc))
        offsets.append((dr, dc))
    return offsets


def _sample_offsets(grid, offsets):
    """Campiona `grid` a ogni offset (forma risultante (n_off, R, C)).

    Le posizioni che escono dalla griglia restano NaN (nessun wrap-around):
    bordo = campione non valido, come nel sampling reale a raggio 8 km."""
    rows, cols = grid.shape
    out = np.full((len(offsets), rows, cols), np.nan, dtype="float64")
    for i, (dr, dc) in enumerate(offsets):
        src_r0, src_r1 = max(0, -dr), rows - max(0, dr)
        src_c0, src_c1 = max(0, -dc), cols - max(0, dc)
        if src_r0 >= src_r1 or src_c0 >= src_c1:
            continue
        dst_r0, dst_c0 = max(0, dr), max(0, dc)
        out[i, dst_r0:dst_r0 + (src_r1 - src_r0),
            dst_c0:dst_c0 + (src_c1 - src_c0)] = grid[src_r0:src_r1,
                                                      src_c0:src_c1]
    return out


# ---------------------------------------------------------------------------
# API pure
# ---------------------------------------------------------------------------
def cold_cloud_top_mask(ctt_grid, threshold=None, valid_mask=None):
    """Maschera dei pixel "freddi" (cima convettiva): CTT <= threshold.

    threshold: override (default IR_CTT_THRESHOLD_C, in °C). valid_mask:
    maschera di validita' opzionale (bool, DATA VALIDITY dal RasterData);
    None = tutte le celle finite sono valide. Celle non finite o non valide
    -> False. Ritorna ndarray bool della stessa forma."""
    ctt = _as_grid(ctt_grid, "ctt_grid")
    th = float(IR_CTT_THRESHOLD_C if threshold is None else threshold)
    ok = np.isfinite(ctt)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != ctt.shape:
            raise ValueError("shape_mismatch:valid_mask")
        ok = ok & vm
    return ok & (ctt <= th)


def anvil_mean_grid(ctt_grid, valid_mask=None,
                    anvil_threshold_c=None, ring_radius_px=None,
                    ring_directions=None, min_anvil_samples=None):
    """BT media dell'anvillo per ogni pixel (NaN se campioni insufficienti).

    Campiona `ctt_grid` a raggio ring_radius_px in ring_directions direzioni;
    i campioni validi sono finiti (e dentro valid_mask, se data) e con
    CTT <= anvil_threshold_c (anvillo otticamente spesso). Media calcolata
    solo se il numero di campioni validi >= min_anvil_samples, altrimenti NaN
    (nessun valore inventato). Ritorna ndarray float64 della stessa forma."""
    ctt = _as_grid(ctt_grid, "ctt_grid")
    ath = float(IR_ANVIL_THRESHOLD_C if anvil_threshold_c is None
                else anvil_threshold_c)
    radius = int(IR_RING_RADIUS_PX if ring_radius_px is None else ring_radius_px)
    ndirs = int(IR_RING_DIRECTIONS if ring_directions is None
                else ring_directions)
    min_s = int(IR_MIN_ANVIL_SAMPLES if min_anvil_samples is None
                else min_anvil_samples)
    if radius <= 0 or ndirs <= 0 or min_s <= 0:
        return np.full(ctt.shape, np.nan, dtype="float64")

    samples = _sample_offsets(ctt, _ring_offsets(radius, ndirs))
    usable = np.isfinite(samples) & (samples <= ath)
    if valid_mask is not None:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != ctt.shape:
            raise ValueError("shape_mismatch:valid_mask")
        # un campione fuori dalla maschera di validita' non e' anvillo valido
        vm_samples = _sample_offsets(vm.astype("float64"), _ring_offsets(
            radius, ndirs)) >= 0.5
        usable = usable & vm_samples
    count = usable.sum(axis=0)
    total = np.where(usable, samples, 0.0).sum(axis=0)
    return np.where(count >= min_s, total / np.maximum(count, 1), np.nan)


def cold_anomaly_grid(ctt_grid, **kwargs):
    """Anomalia di freddo = anvil_mean - CTT (positiva se il pixel e' piu'
    freddo dell'anvillo circostante). NaN dove anvil_mean e' NaN. Accetta gli
    stessi kwargs di anvil_mean_grid (soglie/raggio)."""
    ctt = _as_grid(ctt_grid, "ctt_grid")
    has_vm = kwargs.pop("valid_mask", None)
    anvil = anvil_mean_grid(ctt, valid_mask=has_vm, **kwargs)
    out = np.full(ctt.shape, np.nan, dtype="float64")
    valid = np.isfinite(anvil) & np.isfinite(ctt)
    out[valid] = anvil[valid] - ctt[valid]
    return out


def ot_flags(ctt_grid, valid_mask=None, ctt_threshold_c=None,
             anvil_threshold_c=None, anomaly_threshold_c=None,
             ring_radius_px=None, ring_directions=None,
             min_anvil_samples=None, erosion_kernel=None):
    """Flag OT booleano (maschera 2D) dalla sola CTT IR 10.8.

    flag = freddo (CTT <= ctt_threshold_c) E anomalia >= anomaly_threshold_c,
    poi erosione NxN (default 3x3) per eliminare i pixel isolati. Celle non
    finite/invalide -> False. Ritorna ndarray bool."""
    ctt = _as_grid(ctt_grid, "ctt_grid")
    ath = IR_ANOMALY_THRESHOLD_C if anomaly_threshold_c is None \
        else anomaly_threshold_c
    k = int(IR_EROSION_KERNEL if erosion_kernel is None else erosion_kernel)

    cold = cold_cloud_top_mask(ctt, threshold=ctt_threshold_c,
                               valid_mask=valid_mask)
    anomaly = cold_anomaly_grid(
        ctt, valid_mask=valid_mask, anvil_threshold_c=anvil_threshold_c,
        ring_radius_px=ring_radius_px, ring_directions=ring_directions,
        min_anvil_samples=min_anvil_samples)
    flags = cold & np.isfinite(anomaly) & (anomaly >= float(ath))
    if k > 1:
        structure = np.ones((k, k), dtype=bool)
        flags = ndimage.binary_erosion(flags, structure=structure,
                                       border_value=0)
    return flags


def evaluate_ir_ot(ctt_grid, valid_mask=None, pixel_area_km2=None,
                   ctt_threshold_c=None, anvil_threshold_c=None,
                   anomaly_threshold_c=None, ring_radius_px=None,
                   ring_directions=None, min_anvil_samples=None,
                   erosion_kernel=None):
    """Valuta l'OT IR-only sulla finestra del candidato -> dict di risultato.

    Ritorna dict con:
      score             0-100 (area dei flag OT, overshoot.ot_score_from_flags)
      ctt_min_c         CTT minima (°C) sulle celle valide (None se nessuna)
      cold_top          True se ESISTE almeno un pixel freddo (candidate OT)
      ot_flag           True se ESISTE almeno un flag OT (dopo erosione)
      n_flags           numero di pixel flag
      valid_pixels      celle finite e valide nella finestra
      ctt_threshold_c / anomaly_threshold_c / anvil_threshold_c /
      ring_radius_px / erosion_kernel   (soglie REALMENTE usate, per audit)
    """
    ctt = _as_grid(ctt_grid, "ctt_grid")
    cth = float(IR_CTT_THRESHOLD_C if ctt_threshold_c is None
                else ctt_threshold_c)
    ath = float(IR_ANOMALY_THRESHOLD_C if anomaly_threshold_c is None
                else anomaly_threshold_c)
    vth = float(IR_ANVIL_THRESHOLD_C if anvil_threshold_c is None
                else anvil_threshold_c)
    radius = int(IR_RING_RADIUS_PX if ring_radius_px is None else ring_radius_px)
    ndirs = int(IR_RING_DIRECTIONS if ring_directions is None
                else ring_directions)
    min_s = int(IR_MIN_ANVIL_SAMPLES if min_anvil_samples is None
                else min_anvil_samples)
    k = int(IR_EROSION_KERNEL if erosion_kernel is None else erosion_kernel)

    if valid_mask is None:
        valid = np.isfinite(ctt)
    else:
        vm = np.asarray(valid_mask, dtype=bool)
        if vm.shape != ctt.shape:
            raise ValueError("shape_mismatch:valid_mask")
        valid = vm & np.isfinite(ctt)

    n_valid = int(valid.sum())
    ctt_min = float(ctt[valid].min()) if n_valid else None

    flags = ot_flags(
        ctt, valid_mask=valid_mask, ctt_threshold_c=cth,
        anvil_threshold_c=vth, anomaly_threshold_c=ath,
        ring_radius_px=radius, ring_directions=ndirs,
        min_anvil_samples=min_s, erosion_kernel=k)
    cold = cold_cloud_top_mask(ctt, threshold=cth, valid_mask=valid_mask)
    n_flags = int(flags.sum())
    score = _overshoot.ot_score_from_flags(
        flags, pixel_area_km2=IR_PIXEL_AREA_DEFAULT_KM2
        if pixel_area_km2 is None else pixel_area_km2)

    return {
        "score": score,
        "ctt_min_c": ctt_min,
        "cold_top": bool(cold.any()),
        "ot_flag": bool(n_flags > 0),
        "n_flags": n_flags,
        "valid_pixels": n_valid,
        "ctt_threshold_c": cth,
        "anvil_threshold_c": vth,
        "anomaly_threshold_c": ath,
        "ring_radius_px": radius,
        "ring_directions": ndirs,
        "min_anvil_samples": min_s,
        "erosion_kernel": k,
    }
