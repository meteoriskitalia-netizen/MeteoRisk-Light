#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2 SUPERCELL (morphology, vertical structure, environment NWP, lightning, overshooting top)

Package Fase 2 sperimentale: estende la firma radar Fase 1 (supercell.py) con
layer ADDITIVI che usano prodotti aggiuntivi (VIL/ETM/POH/CAPPI, satellite WV/IR,
fulmini WMS) e ambiente NWP (Open-Meteo). Nessun layer della Fase 1 viene
modificato: l'SSI v1 resta invariato, il SSI v2 (aggregate.aggregate_ssi_v2)
combinia i pesi espliciti documentati nel modulo aggregate.

Moduli:
  hook              - detection uncino (hook echo) da campo riflettivita'
  vertical_structure- strutture verticali (VIL, echo top, POH, overhang)
  environment       - indici ambientali NWP (SCP/STP/SHIP, SRH, CAPE/CIN)
  lightning         - frame fulmini via WMS DPC + trend Gatlin
  overshoot         - overshooting top da BTD WV-IR
  aggregate         - SSI v2 = combinazione pesata dei layer Fase 2

Tutte le funzioni di calcolo sono PURE (nessuna I/O, nessuno stato condiviso,
determinismo): l'I/O vive solo nei wrapper fetch_* e solleva eccezioni locali
allineabili a radar_engine.models.SourceError a integrazione.

EXPERIMENTAL: soglie e pesi non calibrati su dataset reali (v. report fase A1).

B2 — COMPONENTI PER CELLÀ (0.4.0): hook/struttura/ambiente/fulmini sono
calcolati SULLA FINESTRA LOCALE di ciascun candidato (footprint ±km) e non piu'
su una griglia globale condivisa; aggregate.aggregate_ssi_v2 ESCLUDE i
componenti None e RINORMALIZZA i pesi sui presenti (somma 1.00).
"""

PHASE2_VERSION = "0.4.0"   # B2: componenti per cella + rinormalizzazione pesi
