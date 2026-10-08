#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MeteoRisk phenomena-verify — verifica eventi GRANDINE (HAIL) e VORTICI (VORTEX).

Motore ENGINE-ONLY (nessun lato client in questo step): legge i derivati radar
(data/radar: latest.json, supercells.json, tracks.json, storms.geojson), combina
persistenza di riflettivita', morfologia e fulmini (provider PLUGGABLE) e
scrive in modo atomico:
    data/phenomena/events.json   — evento rolling con finestra 24h (merge per id)
    data/phenomena/badges.json   — stato corrente, SOLO eventi attivi

Tier (soglie sperimentali, NON calibrate su report esterni):
    SUSPECT       — gate di intensita'/morfologia + persistenza (riflettivita')
    CORROBORATED  — gate piu' forte E evidenza fulmini; mai senza fulmini
                    (provider unavailable -> resta SUSPECT, nessuna promozione)
    VERIFIED      — SOLO report esterni (ESWD): previsto nello schema ma MAI
                    popolato da questo motore (nessun report inventato).

Vortici: SOLO proxy da riflettivita' (organization_score / morfologia) + fulmini.
NESSUN dato Doppler -> mai dichiarato mesociclone o tornado.
"""

PHENOMENA_VERSION = "0.2.0"
# Versione delle SOGLIE dei tier: da bumpare quando cambia una soglia in
# hail.py/vortex.py (il campo thresholds_version degli eventi la espone).
# pheno-1.1.0: gate fulmini per-sorgente (lightning_corroborates) al posto
# del conteggio assoluto DPC.
THRESHOLDS_VERSION = "pheno-1.1.0"
# Finestra di storia events.json (prune per last_seen piu' vecchio di window).
WINDOW_HOURS = 24

# Gate fulmini PER SORGENTE (provider-agnostico: phenomena.lightning.
# lightning_corroborates usa strength_min quando il provider espone
# `strength` (0..1), altrimenti count_min come fallback — DPC e mock legacy
# senza strength restano sul gate storico):
#   dpc — Blitz v2 puntuale: strength = min(1, count_near/30); 0.33 == 10
#         fulmini nel raggio (gate storico HAIL_LTG_MIN_STRIKES, invariato).
#   mli — proxy AFA (pixel attivi alpha>0 nel disco di 30 km): 0.01 ~= 1%
#         del disco (~21 px alla risoluzione Italia 1024px, bbox
#         6.6/36.5/18.8/47.2, ~28 km² di flash area accumulata nel raggio). Sperimentale
#         (come tutte le soglie di questo modulo, NON calibrate su report
#         esterni): abbastanza bassa da cogliere un nucleo convettivo piccolo,
#         alta da non promuovere con un pugno di pixel di rumore; count_min
#         documenta lo stesso valore in pixel (0.01 * ~2140 px di disco).
LIGHTNING_SOURCE_THRESHOLDS = {
    "dpc": {"strength_min": 0.33, "count_min": 10},
    "mli": {"strength_min": 0.01, "count_min": 21},
}
DEFAULT_LIGHTNING_THRESHOLD = LIGHTNING_SOURCE_THRESHOLDS["dpc"]
