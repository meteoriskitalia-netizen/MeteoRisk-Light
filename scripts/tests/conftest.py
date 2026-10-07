# -*- coding: utf-8 -*-
"""Conftest di suite per i test Phase 2 (A2) della release.

Equivalente alla release del bootstrap di staging
(02_integrazione/tests/conftest_staging.py): in staging il path puntava alla
cartella 01_backend (copie A1); alla release gli stessi moduli, file-verbatim,
vivono in scripts/radar_engine/phase2. Questo conftest mette quella cartella su
sys.path (prepend) cosi' gli import top-level dei test A2 funzionano:
  import environment / hook / lightning / overshoot / aggregate /
  vertical_structure

Nessun nome condiviso con stdlib/pacchetti esistenti della suite. La suite
existing (scripts/tests) NON aveva un conftest alla root: viene creato ex novo,
nessuna fixture gia' presente da duplicare (le fixture engine vivono nel
conftest scoped scripts/tests/radar_engine/conftest.py e non si toccano).
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PHASE2 = os.path.join(os.path.dirname(_TESTS_DIR), "radar_engine", "phase2")

if not os.path.isdir(_PHASE2):
    raise ImportError(f"phase2 non trovato: {_PHASE2}")
if _PHASE2 not in sys.path:
    sys.path.insert(0, _PHASE2)