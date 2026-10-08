#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conftest dei test phenomena-verify (PARTE 4a).

Mette `scripts/` su sys.path (prepend) per importare `radar_engine` e il
package `phenomena`, specchio di scripts/tests/radar_engine/conftest.py.

Nessuna fixture dati reali: i dati di data/radar della release sono letti SOLO
dal test e2e su release reale (skip se assente), tutti gli altri test girano su
input sintetici scritti in tmp_path. NESSUN test tocca la rete.
"""

import os
import sys

SCRIPTS_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)
