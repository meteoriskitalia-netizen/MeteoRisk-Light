#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — Fase 1 (MVP Storm Detection & Tracking).

Motore quantitativo di rilevamento e tracciamento di celle convettive a partire
dal prodotto Radar-DPC VMI (GeoTIFF Float32, dBZ) scaricato via REST API ufficiale
(OPTION_A, Fase 0 — docs/RADAR_SOURCE_VERIFICATION.md).

Sorgente dati: Radar-DPC (Dipartimento della Protezione Civile), licenza
CC-BY-SA 4.0. Il motore pubblica SOLO output derivati (coordinate di celle,
geometrie, metriche, score, classificazioni) e mai immagini/tile/raw radar.

Classificazione Fase 1 (probabilistica, NON "supercell"):
  - Weak Convective Cell [0-30]
  - Convective Cell [31-55]
  - Organized Convective Cell [56-75]
  - Highly Organized Convective Cell [76-100]
Nessun hook-echo/mesocyclone/rotation in Fase 1 (riferimenti Fase 2+).

Fase 1 SUPERCELL (0.2.0, EXPERIMENTAL): il modulo supercell.py combina i
descrittori Fase 1 (celle + tracking + Organization Score) in un Supercell
Signature Index 0-100. Il layer resta ad ESPLICITO GATE di intensità e NON
dichiara mesocyclone/hook-echo (prodotto VMI a singola riflettività, nessun
Doppler). Output: data/radar/supercells.json (additivo, mai sostitutivo).
"""

ENGINE_NAME = "Meteorisk Radar Engine"
ENGINE_VERSION = "0.2.0"
ENGINE_PHASE = 1