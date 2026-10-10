#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — fusion (PROTOTIPO ISOLATO, release 1.2.2.0)

Pacchetto sperimentale di fusione multi-sorgente. NON fa parte della pipeline di
produzione: nessun modulo di `radar_engine` (main.py, config.py, ...) importa
questo pacchetto. Serve a progettare e validare dal vivo l'aggiunta del
composito OPERA CIRRUS (EUMETNET) come seconda fonte a supporto dei candidati
supercella rilevati sulla VMI Radar-DPC.

Sotto-moduli:
  - opera_cirrus : adapter S3/ORD per OPERA CIRRUS + primitive di allineamento
                   griglia e la logica PURA di consenso `source_agreement`.
"""

from . import opera_cirrus

__all__ = ["opera_cirrus"]
