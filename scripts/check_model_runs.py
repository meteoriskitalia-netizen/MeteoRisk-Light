#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2 (check_model_runs.py): verifica se è disponibile un NUOVO run dei modelli
sorgente senza consumare il budget dell'API forecast (si usa la Metadata API,
che NON è conteggiata nei limiti Open-Meteo: 600/min · 5.000/h · 10.000/g · 300.000/mese).

Regole:
  - per ogni modello tracciato vengono letti last_run_initialisation_time e
    available_time dalla Metadata API;
  - un nuovo run vale dopo un grace period configurato (default 10 min) per
    evitare fetch di dati non ancora completamente propagati;
  - UNO SOLO dei modelli tracciati (il driver: ARPAE ICON-2I, che è il segmento
    leader di best_match a 3 giorni) può comandare l'avvio; ECMWF IFS è il
    secondo leg del dual;
  - lo stato viene salvato in data/state/last_model_run.json; se la coppia
    run-timestamp risulta identica allo stato precedente => "run già processato".

Exit codes (contratto stabile, HARDENING 1.0.0.6):
  0  = nuovo model run disponibile (la pipeline continua)
  10 = nessun nuovo model run (clean SUCCESS nella Action: zero lavoro)
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
    parser = argparse.ArgumentParser(description="Rilevamento nuovi run dei modelli sorgente (Metadata API).")
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
    results = {}
    try:
        for app_model, meta_model in common.MODEL_RUN_DRIVER_MAP:
            results[app_model] = query_model_metadata(meta_model)
    except Exception as exc:  # noqa: BLE001
        print("[check_model_runs] ERRORE metadata API: %s" % exc)
        return 1

    driver = common.MODEL_RUN_DRIVER_MAP[0][0]
    driver_meta = results[driver]
    init_ts = driver_meta["last_run_initialisation_time"]
    avail_ts = driver_meta["available_time"] or (init_ts + (driver_meta.get("update_interval_seconds") or 0) if init_ts else None)

    now = int(time.time())
    provisioned_locally = False
    if init_ts is not None and avail_ts is not None and now >= (int(avail_ts) + args.grace_seconds):
        run_key = str(init_ts)
        print("[check_model_runs] Nuovo run driver %s: init=%s available=%s (grace %ds ok)" %
              (driver, init_ts, avail_ts, args.grace_seconds))
    elif args.provision_local and init_ts is not None:
        run_key = str(init_ts)
        provisioned_locally = True
        print("[check_model_runs] PROVISION-LOCAL: run driver %s init=%s registrato (grace %ds non ancora scaduto; "
              "la API forecast serve già questo run)" % (driver, init_ts, args.grace_seconds))
    else:
        print("[check_model_runs] Nessun nuovo run usabile per il driver %s (init=%s avail=%s, grace %ds, now=%d)"
              % (driver, init_ts, avail_ts, args.grace_seconds, now))
        return 10

    prev_key = prev_processed
    if not args.force_update and prev_key == run_key and state.get("status") == "live":
        print("[check_model_runs] Run %s già processato (nessuna modifica)." % run_key)
        return 10

    new_state = {
        "last_model_runs": results,
        "driver_model": driver,
        "run_key": run_key,
        "run_init_ts": init_ts,
        "run_available_ts": avail_ts,
        "last_processed_key": prev_key,
        "status": "pending",
        "checked_at": common.now_iso(),
        "note": ("PROVISION-LOCALE (dev, fuori dalla cadenza dell'Action): run %s non ancora in grace, "
                 "registrato per la generazione del dataset reale." % run_key
                 if provisioned_locally else
                 "I run non vanno copiati/ripubblicati: Open-Meteo è SOLO fonte dati. "
                 "Vengono pubblicati dati DERIVATI MeteoRisk."),
    }
    if not args.dry_run:
        common.save_run_state(new_state)
        print("[check_model_runs] Stato salvato in data/state/last_model_run.json")
    else:
        print("[check_model_runs] DRY-RUN: stato non scritto")
    return 0


if __name__ == "__main__":
    sys.exit(main())