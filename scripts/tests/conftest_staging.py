# -*- coding: utf-8 -*-
"""Compat con il bootstrap di staging per i test A2 (02_integrazione/tests).

In staging questo modulo era importato come PRIMA riga di ogni test A2
(`import conftest_staging`) e metteva 01_backend/ su sys.path. Alla release i
moduli A1 (copie file-verbatim) stanno in scripts/radar_engine/phase2: lo
stesso file, riadattato a quel path, continua a funzionare identico per gli
stessi test copiati verbatim (import top-level: hook, vertical_structure,
environment, lightning, overshoot, aggregate). Il path viene inserito in testa.
E' mantenuto SOLO per compatibilita' con la riga di import dei test A2 copiati
verbatim: il bootstrap effettivo di suite e' in conftest.py (stesso lavoro,
idempotente grazie al guard su sys.path).
"""
import os
import sys

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_PHASE2 = os.path.join(os.path.dirname(_TESTS_DIR), "radar_engine", "phase2")

if not os.path.isdir(_PHASE2):
    raise ImportError(f"phase2 non trovato: {_PHASE2}")
if _PHASE2 not in sys.path:
    sys.path.insert(0, _PHASE2)