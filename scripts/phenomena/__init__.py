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
    CORROBORATED  — gate primario (riflettivita'/POH) E almeno UN corroboratore
                    multi-fonte (struttura verticale forte, fulmini, H0 basso);
                    mai promosso da una sola evidenza debole.
    VERIFIED      — SOLO report esterni (ESWD): previsto nello schema ma MAI
                    popolato da questo motore (nessun report inventato).

Vortici: SOLO proxy da riflettivita' (organization_score / morfologia) + fulmini.
NESSUN dato Doppler -> mai dichiarato mesociclone o tornado.
"""

PHENOMENA_VERSION = "0.5.0"
# Versione delle SOGLIE dei tier: da bumpare quando cambia una soglia in
# hail.py/vortex.py (il campo thresholds_version degli eventi la espone).
# pheno-1.1.0: gate fulmini per-sorgente (lightning_corroborates) al posto
# del conteggio assoluto DPC.
# pheno-1.2.0: gate morfologia hook PER-FRAME con coerenza temporale
# (firma hook che reggerebbe tutte e tre le condizioni nello stesso frame in
# >= vortex.MORPH_HOOK_MIN_FRAMES frame distinti, invece degli aggregati
# max/min su frame indipendenti).
# pheno-1.3.0: tier HAIL MULTI-FONTE (struttura verticale POH/ETM/VIL/overhang
# dal candidato + H0 per-cella come corroboratore di una base forte) e gate
# MLI ristretto (densita'/rate del disco, non piu' 0.01).
# pheno-1.4.0: VORTEX anti-falsi-positivi. Il badge VORTEX richiede ora
# EVIDENZA OSSERVATA SOSTENUTA: gate di organizzazione alzato a 76 (banda
# "Highly Organized", prima 60 ~= soglia del candidato 56 -> ogni cella
# organizzata emetteva), hook morfologico coerente su >= 3 frame (era 2),
# core convettivo reale (max_dbz >= 45), >= 3 frame osservati, severita' ALTA
# solo con corroborazione indipendente (fulmini forti O struttura verticale
# forte). Inoltre gli eventi sono AGGREGATI per tempesta nella finestra
# (events.aggregate_events): una cella ri-identificata con id diversi a ogni
# run resta UN evento/badge con durata/conteggio frame, non N.
THRESHOLDS_VERSION = "pheno-1.5.0"
# pheno-1.5.0: corroboratore OVERSHOOTING TOP da IR_108 (satellite_ot) nei
# tier HAIL e VORTEX: ot_flag promuove a CORROBORATED una base primaria forte
# (HAIL: POH o riflettivita' forte; VORTEX: organization alta o hook sostenuto).
# Evidenza SATELLITE indipendente; fail-closed (nessun OT -> comportamento
# invariato). Sorgente: radar_engine.phase2.satellite_ot (prodotto DPC IR_108).
# Finestra di storia events.json (prune per last_seen piu' vecchio di window).
WINDOW_HOURS = 24

# Gate fulmini PER SORGENTE (provider-agnostico: phenomena.lightning.
# lightning_corroborates usa strength_min quando il provider espone
# `strength` (0..1), altrimenti count_min come fallback — DPC e mock legacy
# senza strength restano sul gate storico):
#   dpc — Blitz v2 puntuale: strength = min(1, count_near/30); 0.33 == 10
#         fulmini nel raggio (gate storico HAIL_LTG_MIN_STRIKES, invariato).
#   mli — proxy AFA (pixel attivi alpha>0 nel disco di 30 km): strength e' la
#         DENSITA'/RATE del disco (count_near / count_disk, ~2134 px alla
#         risoluzione Italia 1024px, bbox 6.6/36.5/18.8/47.2), cioe' la
#         frazione di flash area accumulata nel raggio: usando il rate si
#         svincola il gate dalla risoluzione/num_scan. In 1.3.0 il gate e'
#         stato RI-STRETTO (era 0.01, ~1% ~= 21 px, troppo vicino al rumore):
#         strength_min 0.05 == ~5% del disco (>= ~107 px / ~140 km² di AFA),
#         coerente con una supercella in sviluppo reale; count_min documenta
#         lo stesso valore in pixel (0.05 * ~2134). Resta sperimentale (come
#         tutte le soglie di questo modulo, NON calibrate su report esterni).
LIGHTNING_SOURCE_THRESHOLDS = {
    "dpc": {"strength_min": 0.33, "count_min": 10},
    "mli": {"strength_min": 0.05, "count_min": 107},
}
DEFAULT_LIGHTNING_THRESHOLD = LIGHTNING_SOURCE_THRESHOLDS["dpc"]
