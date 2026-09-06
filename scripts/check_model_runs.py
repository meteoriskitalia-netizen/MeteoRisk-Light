#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2 (check_model_runs.py): verifica se è disponibile un NUOVO run ECMWF IFS
senza consumare quota forecast (si usa la Metadata API, che NON è conteggiata
nei limiti Open-Meteo: 600/min · 5.000/h · 10.000/g · 300.000/mese; i relativi
counters vivono SOLO nell'osservabilità guardrails).

COORDINATED SCHEDULING (1.0.0.8):
  - driver UNICO: ECMWF IFS (common.DRIVER_MODEL). best_match NON ha un proprio
    run_key: è un composito senza endpoint metadata e viene aggiornato in modo
    COORDINATO con il run ECMWF nello stesso ciclo di fetch.
  - dalla Metadata API vengono letti last_run_initialisation_time e
    available_time (fallback: init + update_interval_seconds) di ecmwf_ifs;
  - grace period prudenziale (default 600 s dopo available_time) per evitare
    fetch prematuri / dataset parziali / falsi positivi;
  - confronto run_key (= init ECMWF) con l'ultimo run processato: identico e
    status live => "run già processato" => NO NEW ECMWF RUN.
  - CHECK = leggero (solo Metadata API). FETCH = costoso (avviato SOLO con un
    run ECMWF nuovo e disponibile).

ROBUSTEZZA RETE / METADATA API (1.0.0.8, PARTE H):
  - H2 timeout esplicito connect/read (OPENMETEO_METADATA_TIMEOUT_S = 15s);
  - H1 retry automatici per errori transienti (SSL/keepalive handshake timeout,
    TimeoutError, URLError transitorio, connection reset, HTTP 429, HTTP 5xx),
    tentativi totali METADATA_API_RETRIES = 4 (MAI infiniti);
  - H3 exponential backoff + jitter tra i tentativi;
  - H4 NETWORK ERROR != NO NEW RUN: lo state/fingerprint/last_processed_key si
    aggiornano SOLO dopo un check riuscito (un errore API non tocca mai nulla);
  - H5 dopo i retry esauriti -> CHECK FAILED: il workflow fallisce esplicitamente
    e il summary riporta "Metadata API unavailable after retries",
    "No data fetch performed", "Last dataset unchanged".

Exit codes (contratto stabile · Parte H invariato, 1.0.0.8):
  0  = nuovo run ECMWF disponibile (la pipeline continua)
  10 = nessun nuovo run ECMWF (clean SUCCESS nella Action: zero lavoro)
  1  = errore tecnico Metadata API: la Action FALLA con errore visibile,
       MAI interpretato come "nessun nuovo run" o silenziato.
"""

import argparse
import json
import sys
import time

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def query_model_metadata(model_id):
    meta = common.get_model_metadata(model_id)
    return {
        "model": model_id,
        "last_run_initialisation_time": meta.get("last_run_initialisation_time"),
        "available_time": meta.get("available_time"),
        "update_interval_seconds": meta.get("update_interval_seconds"),
        "not_available_before_this_time": meta.get("not_available_before_this_time"),
    }


def main():
    parser = argparse.ArgumentParser(description="Rilevamento nuovi run ECMWF IFS (Metadata API).")
    parser.add_argument("--grace-seconds", type=int, default=common.GRACE_AFTER_AVAILABILITY_S,
                        help="Attesa minima dopo l'available_time prima di considerare il run usabile.")
    parser.add_argument("--force-update", action="store_true",
                        help="Ignora il check 'run già processato' (MAI la validazione).")
    parser.add_argument("--provision-local", action="store_true",
                        help="DEV/LOCALE SOLO: registra il run anche se il grace non è ancora scaduto "
                             "(la API forecast lo serve già). MAI usato dalla GitHub Action.")
    parser.add_argument("--dry-run", action="store_true", help="Non scrivere lo stato.")
    args = parser.parse_args()

    state = common.load_run_state()
    prev_processed = state.get("last_processed_key")
    bootstrap = common.is_bootstrap_pending()
    if bootstrap:
        print("[check_model_runs] INITIAL DATASET BOOTSTRAP pending (G3/G5): nessun run processato "
              "o dataset assente o fingerprint Best Match assente. Il run ECMWF corrente sara' "
              "considerato NUOVO (mai 'already processed'/'no change').")
    results = {}
    try:
        model_id = common.DRIVER_MODEL
        results[model_id] = query_model_metadata(model_id)
        # osservabilità guardrails SOLO a check riuscito (H4: su errore lo stato
        # non è toccato — né run state né telemetria; l'esito arriva da rc+summary).
        common.record_api_usage(checks=1, retries=max(0, common.metadata_attempts() - 1))
    except Exception as exc:  # noqa: BLE001
        print("[check_model_runs] METADATA API UNAVAILABLE AFTER RETRIES (Parte H5): %s" % exc)
        print("[check_model_runs] NETWORK ERROR != NO NEW RUN (H4): stato NON aggiornato, "
              "nessun fetch eseguito, ultimo dataset invariato.")
        return 1

    driver_meta = results[common.DRIVER_MODEL]
    init_ts = driver_meta["last_run_initialisation_time"]
    avail_ts = driver_meta["available_time"] or (
        (init_ts + (driver_meta.get("update_interval_seconds") or 0)) if init_ts else None)

    if init_ts is None and bootstrap:
        print("[check_model_runs] ERRORE IN BOOTSTRAP: nessun run ECMWF risulta dalla Metadata API "
              "(init=None). Senza un run di riferimento il primo dataset non puo' nascere: workflow "
              "FAIL (nessun file falso/parziale, requisito G6).")
        return 1

    now = int(time.time())
    provisioned_locally = False
    if init_ts is not None and avail_ts is not None and now >= (int(avail_ts) + args.grace_seconds):
        run_key = str(init_ts)
        print("[check_model_runs] Nuovo run ECMWF IFS: init=%s available=%s (grace %ds ok)" %
              (init_ts, avail_ts, args.grace_seconds))
    elif args.provision_local and init_ts is not None:
        run_key = str(init_ts)
        provisioned_locally = True
        print("[check_model_runs] PROVISION-LOCAL: run ECMWF IFS init=%s registrato (grace %ds non ancora "
              "scaduto; la API forecast serve gia' questo run)" % (init_ts, args.grace_seconds))
    else:
        if bootstrap:
            print("[check_model_runs] ERRORE IN BOOTSTRAP: run ECMWF non ancora usabile (init=%s "
                  "avail=%s, grace %ds, now=%d). Workflow FAIL: il primo dataset richiede un run reale."
                  % (init_ts, avail_ts, args.grace_seconds, now))
            return 1
        print("[check_model_runs] Nessun nuovo run usabile per il driver %s (init=%s avail=%s, grace %ds, now=%d)"
              % (common.DRIVER_MODEL, init_ts, avail_ts, args.grace_seconds, now))
        return 10

    prev_key = prev_processed
    if not args.force_update and not bootstrap and prev_key == run_key and state.get("status") == "live":
        print("[check_model_runs] Run ECMWF %s gia' processato (nessuna modifica)." % run_key)
        return 10

    new_state = {
        "last_model_runs": results,
        "driver_model": common.DRIVER_MODEL,
        "run_key": run_key,
        "run_init_ts": init_ts,
        "run_available_ts": avail_ts,
        "last_processed_key": prev_key,
        "status": "bootstrap_pending" if bootstrap else "pending",
        "bootstrap_pending": bootstrap,
        "checked_at": common.now_iso(),
        "last_processed_at": state.get("last_processed_at"),
        "dataset": state.get("dataset"),
        "note": ("INITIAL DATASET BOOTSTRAP (1.0.0.8): primo ciclo senza dataset live; "
                 "sara' eseguito il fetch reale del primo dataset (guardrail ceiling -> fetch -> build -> "
                 "validate -> publish atomico)." if bootstrap else
                 "Ciclo coordinato: il run ECMWF comanda l'aggiornamento; best_match viene "
                 "scaricato nello stesso ciclo di fetch. I run non vanno copiati/ripubblicati: "
                 "Open-Meteo e' SOLO fonte dati. Vengono pubblicati dati DERIVATI MeteoRisk."),
    }
    if not args.dry_run:
        common.save_run_state(new_state)
        print("[check_model_runs] Stato salvato in data/state/last_model_run.json")
    else:
        print("[check_model_runs] DRY-RUN: stato non scritto")
    return 0


if __name__ == "__main__":
    sys.exit(main())