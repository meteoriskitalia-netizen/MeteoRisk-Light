#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — config.py

Configurazione centralizzata del motore. Le soglie e i pesi sono espliciti e
marcati come EXPERIMENTAL DEFAULTS (non calibrati su dataset reali): vanno
validati retrospettivamente prima di qualunque uso operativo.

Il file può essere sovrascritto parzialmente con un JSON (opzione --config /
variabile RADAR_ENGINE_CONFIG): l'override è un merge 1-livello sui singoli
blocchi (source, preprocess, detect, tracking, scoring, phase2, output).
"""

import json
import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults — EXPERIMENTAL (valori iniziali di progetto, nessuna calibrazione)
# ---------------------------------------------------------------------------
EXPERIMENTAL = "EXPERIMENTAL DEFAULT — non calibrato su dati reali"

CONFIG = {
    "source": {
        # Unico punto a conoscenza degli endpoint DPC (v. source_dpc.py).
        "api_base": "https://radar-api.protezionecivile.it",
        "origin_header": "https://radar.protezionecivile.it",
        "product_type": "VMI",
        "history_frames": 6,        # finestra candidata: T-25m..T0 (VMI PT5M)
        "http_timeout_s": 30,
        "download_timeout_s": 60,
        "max_download_retries": 2,
        "cache_dir": None,          # None -> scripts/radar_engine/temp
        "keep_raw_frames": False,   # esclude sempre il raw dal repo (gitignore)
    },
    "preprocess": {
        # DATA VALIDITY (nodata/espliciti) — distinta dalla soglia meteorologica.
        "nodata_values": [-9999.0, -9998.0],
        "declared_nodata_from_tiff": True,   # aggiunge ds.nodata del GeoTIFF
        # Plausibility check geografica (FAIL SAFE): fuori bbox Italia -> errore.
        "geo_plausible_bbox": {"lon": [4.0, 21.0], "lat": [34.0, 48.0]},
    },
    "detect": {
        # METEOROLOGICAL THRESHOLD — separato dalla validità del dato.
        "dbz_threshold": 20.0,             # EXPERIMENTAL
        "min_area_km2": 30.0,              # EXPERIMENTAL (celle piccole scartate)
        "max_area_km2": 40000.0,           # EXPERIMENTAL (cap anti-mosaico)
        "morph_radius_px": 1,              # EXPERIMENTAL (SE quadrato 2r+1)
        "connectivity_8": True,            # labelling 8-connesso (np.ones(3,3))
    },
    "tracking": {
        # Cost function combinata (Hungarian / linear_sum_assignment):
        #   cost = w_distance*d[km] + w_area*Δarea_norm + w_intensity*ΔdBZ
        #          + w_overlap*(1 - IoU_circle)
        "w_distance": 1.0,                 # EXPERIMENTAL
        "w_area": 6.0,                     # EXPERIMENTAL (Δarea normalizzata 0-1)
        "w_intensity": 0.25,               # EXPERIMENTAL (ΔdBZ)
        "w_overlap": 8.0,                  # EXPERIMENTAL (1-IoU in 0-1)
        "max_assignment_cost": 400.0,      # EXPERIMENTAL (sopra soglia -> birth)
        "min_frames_track": 2,             # track minima (2+ frame)
        "full_confidence_frames": 5,       # >=5 frame -> tracking_confidence=full
        # ---- Fase 1.6 (STABILIZATION) — EXPERIMENTAL ----
        "cost_formula": "legacy",          # 'legacy' (Fase 1) | 'improved' (1.6)
        "w_prediction": 1.0,               # peso errore predizione (solo improved)
        "distance_ref_km": 100.0,          # normalizzazione distanza (0-1)
        "prediction_ref_km": 50.0,         # normalizzazione errore predizione
        "intensity_ref_dbz_cost": 35.0,    # normalizzazione ΔdBZ (0-1)
        "memory_frames": 3,                # media recente area/intensità (matching)
        "max_storm_speed_kmh": None,       # GATE fisico (NON hardcodato; None=off
                                           #  = default di produzione, vedi Fase1.6)
        "prediction_gate_km": None,        # None -> = step gate da max_storm_speed
        "diagnostics": False,              # per-match diag (Mai nel dataset pubbl.)
    },
    "storm": {
        # Fase 1.7 — LAYER MULTISCALA (CELL -> STORM OBJECT -> STORM SYSTEM).
        # Il CellTracker NON viene sostituito: resta il layer locale. Sotto il
        # tracker, lo storm object è l'unità di identità principale.
        "enabled": True,               # abilita aggregazione + storm tracking
        "aggregation": {
            # Metodo di raggruppamento celle in storm object (EXPERIMENTAL):
            #   'distance_cc'     - componenti connesse su distanza centroide
            #   'dbscan'          - DBSCAN di riferimento (cell centroids)
            #   'dilation_overlap'- (DEFAULT) unione se gap-cerchio <= merge_gap
            #                       OPPURE cerchi dilatati di dilate_km si sovrapp.
            "method": "dilation_overlap",
            "merge_gap_km": 5.0,       # gap (d - r1 - r2) per l'unione (>=0)
            "dilate_km": 8.0,          # raggio di dilatazione per l'overlap test
            "dbscan_eps_km": 15.0,     # riferimento: eps DBSCAN (centroids)
            "dbscan_min_cells": 2,     # riferimento: min_samples DBSCAN
            "min_cells": 1,            # storm object con >= n celle
            "hull": True,              # incluso convex hull centroids nei props
            "density_radius_km": 25.0, # raggio per la local cell density
        },
        "tracking": {
            # Cost function NORMALIZZATA (0-1 per termine), pesi configurabili:
            #   cost = w_distance * d/distance_ref + w_iou * (1-IoU_circle)
            #        + w_area * |ΔA|/max(A) + w_intensity * |ΔdBZ|/int_ref
            #        + w_cells * min(1, |Δcount|/cell_count_ref)
            # PRIORITÀ: geometry overlap (w_iou) > centroid continuity
            # (w_distance) > motion continuity (predizione, NON Kalman).
            "w_distance": 1.0,
            "w_iou": 6.0,
            "w_area": 2.0,
            "w_intensity": 0.5,
            "w_cells": 1.0,
            "cell_count_ref": 10.0,
            "distance_ref_km": 100.0,
            "intensity_ref_dbz": 35.0,
            "w_prediction": 1.0,       # errore predizione (come tracking 1.6)
            "prediction_ref_km": 50.0,
            "max_assignment_cost": 400.0,
            "min_frames_track": 2,
            "full_confidence_frames": 5,
            "max_storm_speed_kmh": None,    # gate fisico (None = off Fase 1.6)
            "prediction_gate_km": None,
            "diagnostics": False,
        },
        "ambiguity": {
            # metrica DIAGNOSTICA di qualità dell'identificazione (non meteo):
            # competing candidates + cell density locale + merge/split + cost sep.
            "density_radius_km": 25.0,
            "gate_radius_km": 50.0,       # candidati competitor entro il raggio
            "density_medium": 5,          # celle entro raggio -> soglie
            "density_high": 10,
            "competing_high": 3,          # n candidati finiti -> soglia high
            "cost_sep_high": 0.12,        # sep_ratio sotto -> high
            "cost_sep_medium": 0.30,      # sotto -> medium
        },
        "motion": {
            # Risoluzione motion_confidence (low/medium/high): age, osservazioni,
            # continuità geometrica, coerenza spostamento, ambiguità residua.
            "min_frames_clock": 3,       # frame per motion (oltre il minimo 2)
            "geom_iou_high": 0.35,       # media IoU consecutiva
            "geom_iou_medium": 0.20,
            "cv_speed_high": 0.40,       # CV velocità tra segmenti
            "turn_high_deg": 15.0,       # std circolare heading tra segmenti
            "max_validated_velocity_kmh": 200.0,  # GATE di VALIDAZIONE cinematica
                                 # (Fase 1.9, Parte B): rifiuta la velocity_kmh
                                 # pubblicata se il robust median è impossibile
                                 # (>200 km/h). CALIBRATO su 25 casi reali
                                 # (PHASE19 report): p99 velocità vera ~181 km/h,
                                 # tracking invariato (continuità 0.585). NON
                                 # tocca il matching (max_storm_speed_kmh resta
                                 # il solo step-gate Fase 1.6, default None).
        },
    },
    "scoring": {
        # Organization Score (0-100), pesi in somma 1.0. EXPERIMENTAL.
        "weights": {
            "persistence": 0.20,
            "intensity": 0.20,
            "intensity_consistency": 0.15,
            "spatial_coherence": 0.15,
            "motion_consistency": 0.15,
            "growth_sustained": 0.15,
        },
        # Scale di riferimento per la normalizzazione (valori fisici plausibili).
        "intensity_ref_dbz": [20.0, 55.0], # floor/ceiling normalizzazione dBZ
        "persistence_ref_frames": 5,       # track "piena" a 5 frame (T0..T-20)
        "bands": [
            (0.0, 30.0, "Weak Convective Cell"),
            (31.0, 55.0, "Convective Cell"),
            (56.0, 75.0, "Organized Convective Cell"),
            (76.0, 100.0, "Highly Organized Convective Cell"),
        ],
    },
    "supercell": {
        # Fase 1 SUPERCELL (EXPERIMENTAL): Supercell Signature Index.
        # Layer ADDITIVO: stessi dati Fase 1 (celle+track+Organization Score),
        # nessuna richiesta rete aggiuntiva. Nessun Doppler nel prodotto VMI:
        # mesocyclone/hook-echo NON vengono MAI dichiarati.
        "enabled": True,
        "weights": {
            "intensity_core": 0.25,
            "organization": 0.30,
            "core_morphology": 0.10,
            "persistence": 0.15,
            "growth_sustained": 0.20,
        },
        "intensity_core_dbz": 45.0,   # GATE: core convettivo reale per il claim
        "intensity_ref_dbz": [35.0, 55.0],
        "persistence_ref_frames": 5,
        "compactness_ref": 8.0,
        "candidate_ssi": 65.0,        # SSI per il livello 'possible'
        "birth_frames": 3,            # candidate con <=n frame nella finestra -> 'birth'
        "min_frames": 2,
        "bands": [
            (0.0, 49.0, "non_supercell"),
            (50.0, 64.0, "weak"),
            (65.0, 79.0, "possible"),
            (80.0, 100.0, "marked"),
        ],
    },
    "phase2": {
        # Fase 2 SUPERCELL (EXPERIMENTAL): layer additivi A1 -> SSI v2.
        # Policy: ogni sotto-layer e' OPZIONALE; errore/dato assente ->
        # warning + sub-score None (mai crash del run, stessa politica dei
        # layer storm/supercell della Fase 1). Nessun dato inventato.
        "enabled": True,
        "hook": {
            # Hook echo morfologico da griglia VMI (nessun Doppler):
            # soglia dBZ del core + persistenza sulle ultime N griglie.
            "dbz_threshold": 45.0,     # = hook.HOOK_DBZ_THRESHOLD (A1)
            "history_frames": 3,       # frame per hook.filter_persistence
        },
        "structure": {
            # Prodotti DPC addizionali (griglie devono allinearsi alla VMI).
            # None = product type non configurato -> descrittore ASSENTE
            # (membership 0, mai un valore fabbricato).
            "product_vil": "VIL",      # prodotto DPC verificato (A0)
            "product_etm": "ETM",      # prodotto DPC verificato (A0)
            "product_poh": "POH",      # prodotto DPC verificato (A0)
            "product_low": None,       # CAPPI 2 km: product type DPC ignoto
            "product_high": None,      # CAPPI 6 km: idem -> overhang assente
        },
        "environment": {
            # Open-Meteo sul punto del PRIMO candidato (urllib diretto,
            # nessuna dipendenza extra): SCP/STP/SHIP + env_score 0-100.
            "enabled": True,
            "timeout_s": 30,           # = environment.ENV_HTTP_TIMEOUT_S
        },
        "lightning": {
            # Blitz v2 S3 DPC: finestra del rate in slot da 5 minuti.
            "window_slots": 4,         # = lightning.LIGHTNING_TREND_WINDOW
        },
        "ot": {
            # EUMETSAT WV/IR: DN->K NON calibrato (evidence A0: PNG
            # grayscale senza taratura) -> dn_to_kelvin=None implica
            # layer OT = None con warning ot_unavailable:dn_to_kelvin_non_configurato.
            "dn_to_kelvin": None,      # None | {"offset": K, "scale": K/DN}
            "btd_threshold_k": 12.0,   # = overshoot.OT_BTD_THRESHOLD_K
            "ir_threshold_k": 215.0,   # = overshoot.OT_IR_THRESHOLD_K
        },
        "aggregate": {
            # Pesi SSI v2: SOMMA ESATTAMENTE 1.00 (aggregate.DEFAULT_WEIGHTS).
            # Somma > 1.0 -> ValueError -> warning + nessun ssi_v2 (v. A2-2).
            "weights": {
                "base": 0.60,          # SSI Fase 1 (supercell.py) — fondamento
                "hook": 0.15,
                "structure": 0.10,
                "env": 0.08,
                "ot": 0.04,
                "lightning": 0.03,
            },
        },
    },
    "output": {
        "out_dir": "data/radar",
        "files": ["latest.json", "storms.geojson", "tracks.json",
                  "storm_objects.geojson", "storm_tracks.json",
                  "supercells.json"],
    },
}


def merge_config(base, overrides):
    """Merge 1-livello: overrides (dict) aggiorna i blocchi esistenti di base."""
    merged = dict(base)
    for key, value in (overrides or {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            section = dict(merged[key])
            section.update(value)
            merged[key] = section
        else:
            merged[key] = value
    return merged


def load_config(path=None):
    """Carica la configurazione con eventuale override JSON (merge 1-livello)."""
    cfg = merge_config(CONFIG, {})
    path = path or os.environ.get("RADAR_ENGINE_CONFIG")
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = merge_config(CONFIG, json.load(fh))
    if cfg["source"].get("cache_dir") is None:
        cfg["source"]["cache_dir"] = str(
            Path(__file__).resolve().parent / "temp"
        )
    return cfg