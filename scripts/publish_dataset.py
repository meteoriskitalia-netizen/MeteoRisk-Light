#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 9 (publish_dataset.py): PUBBLICAZIONE ATOMICA del dataset derivato.

Passa SOLO dataset validati (validate_dataset.py). Sequenza:
  1. legge validation.json da data/_workdir (prodotto dall'ultima validazione)
  2. se dataset non valido => EXIT 1, data/latest NON viene toccato
  3. swap atomico: copia staging -> latest (gli old "latest" vengono sostituiti
     in blocco; in caso di errore a metà, i file vengono salvati come .bak
     e ripristinati -> ultimo valido conosciuto preservato)
  4. aggiorna data/state/last_model_run.json (status live)

Non esegue MAI git/commit: la GitHub Action gestisce il commit esclusivamente
quando questo script ritorna 0 (NUOVO dataset valido).

Uso nelle Action:
  python scripts/validate_dataset.py --staging && python scripts/publish_dataset.py
"""

import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def main():
    validation_path = common.DATA_WORK / "validation.json"
    if not validation_path.exists():
        print("[publish] validation.json assente (eseguire validate_dataset.py prima). FERMO.")
        return 1
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    if not validation.get("valid"):
        print("[publish] Dataset NON valido (%s): data/latest NON viene toccato (last known good)."
              % validation.get("outcome"))
        return 1

    files = ["metadata.json", "meteorisk-points.json", "meteorisk-provinces.json", "validation.json"]
    staging = common.DATA_STAGING
    latest = common.DATA_LATEST
    latest.mkdir(parents=True, exist_ok=True)

    # swap atomico con backup di sicurezza
    backup = common.DATA_WORK / "_latest_backup"
    if backup.exists():
        shutil.rmtree(backup)
    backup.mkdir(parents=True, exist_ok=True)
    for f in files:
        if (latest / f).exists():
            shutil.copy2(latest / f, backup / f)
    for f in files:
        src = staging / f
        if not src.exists():
            print("[publish] manca %s in staging. ABORT, ripristino backup." % f)
            rollback(latest, backup, files)
            return 1
        try:
            shutil.copy2(src, latest / f)
        except Exception as exc:  # noqa: BLE001
            print("[publish] errore copiando %s: %s. Ripristino backup." % (f, exc))
            rollback(latest, backup, files)
            return 1

    # aggiorna stato (status live + run processato per il dedup)
    state = common.load_run_state()
    state["status"] = "live"
    state["published_at"] = common.now_iso()
    state["last_processed_key"] = state.get("run_key")
    state["last_processed_at"] = common.now_iso()
    state["dataset"] = {
        "point_count": validation.get("dataset", {}).get("point_count")
                       or (json.loads((latest / "metadata.json").read_text(encoding="utf-8")).get("point_count")),
        "validation_outcome": validation.get("outcome"),
    }

    # Fingerprint Best Match (canary sentinelle 1.0.0.8) DAL DATASET VALIDATO
    # pubblicato: atomicita' stato==dataset. last_changed_at solo su variazione.
    # ecmwf_ifs resta tracciata dal run_key (Metadata API): stato separato.
    points_pub = json.loads((latest / "meteorisk-points.json").read_text(encoding="utf-8"))
    meta_pub = json.loads((latest / "metadata.json").read_text(encoding="utf-8"))
    payload = common.best_match_sentinel_payload(points_pub.get("points", []), meta_pub.get("day0"))
    fp = common.fingerprint_best_match(payload)
    lmr = state.setdefault("last_model_runs", {})
    bm_prev = lmr.get("best_match") or {}
    bm_prev_fp = bm_prev.get("last_fingerprint")
    pub_ts = state["published_at"]
    lmr["best_match"] = {
        "model": "best_match",
        "last_checked_at": pub_ts,
        "last_changed_at": pub_ts if (not bm_prev_fp or bm_prev_fp != fp) else bm_prev.get("last_changed_at"),
        "last_fingerprint": fp,
        "fingerprint_source": "sentinel",
    }
    common.save_run_state(state)

    print("[publish] Pubblicato data/latest (nuovo dataset valido). State aggiornato (status=live).")
    for f in files:
        p = latest / f
        print("[publish]   %s (%d byte)" % (f, p.stat().st_size))
    return 0


def rollback(latest, backup, files):
    for f in files:
        bf = backup / f
        if bf.exists():
            try:
                shutil.copy2(bf, latest / f)
            except Exception:  # noqa: BLE001
                pass
    print("[publish] RIPRISTINATO data/latest dal last known good.")


if __name__ == "__main__":
    sys.exit(main())