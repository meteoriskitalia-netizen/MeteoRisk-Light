#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 2b (check_best_match.py): rilevatore LEGGERO e INDIPENDENTE di aggiornamento
Best Match (1.0.0.8). Best Match NON ha un proprio endpoint metadata: il canary
usa 6 capoluoghi-sentinella (coordinate IDENTICHE ai punti reali pubblicati) in
UNA richiesta forecast multi-location con SOLO weathercode+precipitation orarie.

  - fingerprint = SHA-256 deterministico su day0 + dati orari sentinella best_match.
    INDIPENDENTE dal generation-time: riflette SOLO il contenuto del forecast.
  - confronto con data/state/last_model_run.json (last_model_runs.best_match):
    - cambiato   -> rc 0  (il decision engine autorizza il refresh best_match)
    - invariato  -> rc 10 (clean success, zero lavoro)
    - errore     -> rc 1  (errore tecnico VIStibile, mai silenziato)
  - il canary NON scrive la fingerprint (viene registrata da publish_dataset.py
    sul dataset VALIDATO, atomicamente: stato==dataset sempre).
  - costo: 1 richiesta forecast per ciclo (~144/giorno al peggio con cron */10,
    ~1,4% del ceiling giornaliero). Hard safety ceiling (guardrail PRE-FLIGHT):
    se oggi è
    esaurito il canary NON parte (zero richieste oltre il tetto) -> rc 10 con
    motivo esplicito. NIENTE altro blocca il canary: è già leggero.

  G5 / G3: se lo stato è INITIAL DATASET BOOTSTRAP (state/dataset/fingerprint
  assenti) il canary è saltato (zero richieste): il primo ciclo effettua il
  fetch reale completo. MAI interpretato come 'no change' o 'already processed'.

Exit codes (contratto 1.0.0.8):
   0 = Best Match CAMBIATO (refresh necessario)
  10 = Best Match INVARIATO (clean success)
   1 = errore tecnico (workflow FAIL, mai confuso con 'nessuna modifica')
"""

import argparse
import re
import sys

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def response_payload(data, sentinels):
    """Estrae {day0, sentinels:{sigla:{weathercode,precipitation}}} dalla risposta
    multi-location (l'ordine degli elementi coincide con l'ordine di richiesta)."""
    els = common.response_locations(data)
    if len(els) != len(sentinels):
        raise ValueError("risposta %d sentinelle (attese %d)" % (len(els), len(sentinels)))
    first = els[0]
    time_arr = ((first.get("hourly") or {}).get("time")) or []
    day0 = (time_arr[0].split("T")[0]) if time_arr else None
    out = {}
    for sent, el in zip(sentinels, els):
        h = el.get("hourly") or {}
        out[sent["sigla"]] = {
            "weathercode": list(h.get("weathercode") or []),
            "precipitation": list(h.get("precipitation") or []),
        }
    return {"day0": day0, "sentinels": out}


def main():
    parser = argparse.ArgumentParser(description="Canary Best Match (sentinelle, 1 richiesta).")
    parser.add_argument("--dry-run", action="store_true", help="Non scrivere last_checked_at.")
    args = parser.parse_args()

    if common.is_bootstrap_pending():
        print("[check_best_match] INITIAL DATASET BOOTSTRAP pending (state/dataset/fingerprint "
              "assenti): canary saltato, zero richieste — il primo ciclo effettua il fetch reale "
              "completo (MAI interpretato come 'no change').")
        return 0

    sentinels = common.best_match_sentinels()
    if not sentinels:
        print("[check_best_match] ERRORE: nessuna sentinella definita (regions_data).")
        return 1

    available = common.available_today()
    if available < 1:
        print("[check_best_match] HARD SAFETY CEILING esaurito (disponibile oggi=%d): canary CHECK "
              "saltato, zero richieste oltre il tetto. Retry al prossimo ciclo."
              % available)
        return 10

    try:
        data = common.fetch_best_match_check([s["lat"] for s in sentinels],
                                             [s["lon"] for s in sentinels])
    except Exception as exc:  # noqa: BLE001
        print("[check_best_match] ERRORE canary Best Match: %s" % exc)
        return 1

    payload = response_payload(data, sentinels)
    fp = common.fingerprint_best_match(payload)
    common.record_api_usage(requests=1, canary=1, batches=1, locations=len(sentinels))
    print("[check_best_match] Canary: %d sentinelle · day0=%s · %d valori orari · fingerprint=%s"
          % (len(sentinels), payload.get("day0"),
             sum(len(v["weathercode"]) for v in payload["sentinels"].values()), fp))

    state = common.load_run_state()
    bm = (state.get("last_model_runs") or {}).get("best_match") or {}
    prev_fp = bm.get("last_fingerprint")
    if not args.dry_run:
        state.setdefault("last_model_runs", {})["best_match"] = {
            "model": "best_match",
            "last_checked_at": common.now_iso(),
            "last_changed_at": bm.get("last_changed_at"),
            "last_fingerprint": bm.get("last_fingerprint"),
            "fingerprint_source": bm.get("fingerprint_source"),
        }
        common.save_run_state(state)

    changed = not prev_fp or prev_fp != fp
    if changed:
        print("[check_best_match] BEST MATCH CAMBIATO (fingerprint %s -> %s): refresh autorizzato."
              % (prev_fp, fp))
        return 0
    print("[check_best_match] Best Match INVARIATO (fingerprint %s): clean exit, nessun refresh."
          % prev_fp)
    return 10


if __name__ == "__main__":
    sys.exit(main())