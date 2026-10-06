#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — Validation (Fase 1.5)

Retrospective validation & calibration. TUTTO parte da campioni VMI reali
mantenuti FUORI dal repository (il raw DPC non si committa mai); nel repo
restano SOLO: codice validation/, metadata dei casi (cases/), e output derivati
(data/validation/summary.json, docs/*.md).

Sotto-moduli:
  cases.py        — definizione e risoluzione dei casi (metadata-only nel repo)
  metrics.py      — metriche detection / tracking / score (definizioni operative)
  calibration.py  — sensitivity sweep + grid search limitata su un caso
  report.py       — scrittura data/validation/summary.json + tabelle markdown
"""

VALIDATION_VERSION = "0.1.0"
VALIDATION_PHASE = "1.5"

# Bande di classificazione usate per l'analisi distributiva (da config engine).
BAND_FALSE_HIGH_NAMES = ("Organized Convective Cell", "Highly Organized Convective Cell")