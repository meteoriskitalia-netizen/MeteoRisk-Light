#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 6 (fetch_source_data.py): scarica i dati RAW dei modelli sorgente nel
directorio TEMPORANEO data/_raw (MAI pubblicato: escluso dal repository e da
qualunque publish). Open-Meteo è una FONTE (input meteorologico): i payload
costituenti non vengono ripubblicati, specchiati o incapsulati.

1.0.0.5 — PRODUCTION PIPELINE (1.0.0.6: hardening invariato):
  - REQUEST PLANNER integrato: coordinate DEDUPLICATE + BATCH (100/richiesta) +
    pre-flight di BUDGET bloccante (api_usage.json, riserva di sicurezza).
  - Piano reale (257 punti, 107 province): 3 blocchi (100/100/57) x 2 leg =
    6 richieste (era 1 a punto: 257). Spaziatura 30 s anti minutely-limit
    (~5/min osservato sul campo).
  - RETRY selettivi e limitati: al primo giro falliscono solo alcuni batch;
    vengono ritentati SOLO quelli falliti; l'exponential backoff limitato vive
    nel client interno (RETRY_LIMIT, mai ripetizioni integrali del piano).
  - CONSUMO tracciato: data/state/api_usage.json (richieste/fallite/batch/
    località/byte per giorno). Report efficienza in data/_workdir/api_efficiency/
    (non pubblicato).
  - Exit codes: 0 = raw scritto · 2 = pre-flight BUDGET BLOCKED · 3 = capoluoghi
    mancanti (abort: nessuna pubblicazione parziale) · 1 = errore
    tecnico (coordinate assenti o fallimento residuo).
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def fetch_batch(batch):
    """Una richiesta batched PER MODELLO leg (le risposte multi-location di
    Open-Meteo non annidano i modelli: arrays piatti per leg). Ricostruisce il
    record dual punto-punto nel formato del builder
    {best_match:{daily,hourly}, ecmwf_ifs:{daily,hourly}, elevation}.
    Ritorna (records, error)."""
    records = {}
    n_models = len(common.DUAL_MODELS.split(","))
    for model in common.DUAL_MODELS.split(","):
        data = common.fetch_source_batch(batch["lats"], batch["lons"],
                                         models=model,
                                         forecast_days=common.FORECAST_DAYS)
        els = common.response_locations(data)
        if len(els) != batch["locations"]:
            return None, "risposta %d località (attese %d)" % (len(els), batch["locations"])
        for k, el in enumerate(els):
            h = el.get("hourly") or {}
            d = el.get("daily") or {}
            rec = records.setdefault(k, {"elevation": el.get("elevation")})
            rec[model] = {
                "daily": {f: d[f] for f in common.DAILY_FIELDS if f in d},
                "hourly": {f: h[f] for f in common.HOURLY_FIELDS if f in h},
            }
            if rec["elevation"] is None:
                rec["elevation"] = el.get("elevation")
    return [records[k] for k in sorted(records)], None


def main():
    parser = argparse.ArgumentParser(description="Fetch source data (raw, temporaneo).")
    parser.add_argument("--points-json", default=str(common.REPO_ROOT / "data" / "_workdir" / "real_points.json"),
                        help="File coordinate reali generate dal port (default data/_workdir/real_points.json).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Compat. legacy: i batch sono sequenziali (pacing anti minutely-limit).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stampa il PIANO OTTIMIZZATO (dedup+batch+preflight) senza scaricare.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Salta il blocco di budget (uso diagnostico, MAI nelle Action).")
    args = parser.parse_args()

    if not os.path.exists(args.points_json):
        print("[fetch_source_data] Coordinate non trovate: %s (errore tecnico)" % args.points_json)
        return 1
    points = json.load(open(args.points_json, encoding="utf-8"))
    if not points:
        print("[fetch_source_data] Nessuna coordinata (errore tecnico).")
        return 1

    coords = common.unique_coordinates(points)
    n_models = len(common.DUAL_MODELS.split(","))
    batches = [{"batch": i, "global_start": i * common.BATCH_MAX_LOCATIONS,
                "locations": len(chunk),
                "lats": [c[0] for c in chunk], "lons": [c[1] for c in chunk]}
               for i, chunk in enumerate(
                   [coords[j:j + common.BATCH_MAX_LOCATIONS] for j in range(0, len(coords), common.BATCH_MAX_LOCATIONS)])]
    planned = len(batches) * n_models
    budget = common.ensure_api_budget(planned)

    print("[fetch_source_data] Real points=%d · coordinate uniche=%d (dedup) · batch=%dx%d · 2 leg modello → richieste ottimizzate=%d (era %d)"
          % (len(points), len(coords), len(batches), common.BATCH_MAX_LOCATIONS, planned, len(coords)))
    usage = common.usage_today()
    print("[fetch_source_data] Budget: limite=%d riserva=%.0f%% effettivo=%d usato-oggi=%d disponibile=%d"
          % (common.API_DAILY_LIMIT, common.API_SAFETY_RESERVE_FRAC * 100,
             common.effective_budget(), usage["requests"], budget["available"]))

    if args.dry_run:
        print("[fetch_source_data] DRY-RUN: batch=%s (x2 leg), risparmio richieste=%d, efficienza=%+.2f%%"
              % (", ".join("%dx%d" % (b["batch"] + 1, b["locations"]) for b in batches),
                 len(coords) - planned,
                 0.0 if not coords else 100.0 * (len(coords) - planned) / len(coords)))
        return 0

    if not budget["ok"]:
        print("[fetch_source_data] PRE-FLIGHT BUDGET BLOCKED: %s" % budget["reason"])
        if not args.skip_preflight:
            return 2
        print("[fetch_source_data] (--skip-preflight: esecuzione forzata, uso diagnostico)")

    common.mkdirs(common.DATA_RAW)
    out_path = common.DATA_RAW / "source_raw.json"
    tmp_path = common.DATA_RAW / "source_raw.json.tmp"

    results = {}
    failures = {}
    t0 = time.time()

    def request_policy(batch):
        """Ritorna ({punto->record}, {punto->errore}) per un batch."""
        try:
            records, err = fetch_batch(batch)
        except Exception as exc:  # noqa: BLE001
            records, err = None, str(exc)
        if err:
            return {}, {coords[batch["global_start"] + k][2][0]: err for k in range(batch["locations"])}
        ok = {}
        start = batch["global_start"]
        for k, rec in enumerate(records):
            for pidx in coords[start + k][2]:
                ok[pidx] = rec
        return ok, {}

    def run_pass(batch_list):
        ok_here = {}
        bad_here = {}
        for b in batch_list:
            mm, ee = request_policy(b)
            ok_here.update(mm)
            bad_here.update(ee)
            print("[fetch_source_data] batch %d/%d · %d location · %.1fs"
                  % (b["batch"] + 1, len(batches), b["locations"], time.time() - t0))
        return ok_here, bad_here

    def record_usage(reqs, bads, batch_list):
        # ogni batch = UNA richiesta per leg modello (best_match + ecmwf_ifs)
        common.record_api_usage(requests=reqs * n_models, failed=bads * n_models,
                                batches=reqs,
                                locations=sum(b["locations"] for b in batch_list))

    # primo giro: tutti i batch
    results, failures = run_pass(batches)
    record_usage(len(batches), sum(1 for b in batches if any(
        pidx in failures for pidx in [coords[b["global_start"] + k][2][0]
                                      for k in range(b["locations"])])), batches)

    # retry SELETTIVO: un solo re-tentativo dei SOLI batch falliti (l'exponential
    # backoff interno a fetch_source_batch copre già i transienti su quel batch)
    failed_batches = [b for b in batches if any(
        pidx in failures for pidx in [coords[b["global_start"] + k][2][0]
                                      for k in range(b["locations"])])]
    if failed_batches:
        print("[fetch_source_data] retry selettivo di %d batch falliti (primo giro ok per gli altri)..."
              % len(failed_batches))
        mm, ee = run_pass(failed_batches)
        results.update(mm)
        for k in list(failures.keys()):
            if k in mm:
                del failures[k]
        record_usage(len(failed_batches), sum(1 for b in failed_batches if any(
            pidx in ee for pidx in [coords[b["global_start"] + j][2][0]
                                    for j in range(b["locations"])])), failed_batches)

    print("[fetch_source_data] Completato: ok=%d fail=%d (%.1fs)"
          % (len(results), len(failures), time.time() - t0))

    # Capoluoghi (coordIdx 0) MAI mancanti → altrimenti nessuna pubblicazione
    stuck = []
    if failures:
        print("[fetch_source_data] Punti falliti: %s" % (
            ", ".join("%s:%s" % (k, v) for k, v in list(failures.items())[:20])))
        for idx in failures:
            for p in points:
                if p["index"] == idx and p.get("coordIdx") == 0:
                    stuck.append(idx)
    if stuck:
        print("[fetch_source_data] FALLIMENTO: capoluoghi mancanti %s → condizione di no-pubblicazione attiva."
              % sorted(stuck)[:20])
        return 3

    payload = {
        "fetched_at": common.now_iso(),
        "forecast_days": common.FORECAST_DAYS,
        "timezone": common.TIMEZONE,
        "models": [m for m in common.MODEL_RUN_DRIVER_MAP],
        "points": sorted(results.items()),
    }
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_path, out_path)
    size_kb = out_path.stat().st_size / 1024.0
    common.record_api_usage(bytes_=out_path.stat().st_size)  # volume raw generato

    # Report efficienza (non pubblicato)
    common.API_EFFICIENCY_DIR.mkdir(parents=True, exist_ok=True)
    eff = {
        "generated_at": common.now_iso(),
        "points": len(points),
        "unique_coordinates": len(coords),
        "naive_requests": len(coords),
        "optimized_requests": planned,
        "requests_saved": len(coords) - planned,
        "efficiency_gain_pct": round(100.0 * (len(coords) - planned) / len(coords), 2) if coords else 0.0,
        "batch_size": common.BATCH_MAX_LOCATIONS,
        "n_model_legs": n_models,
        "elapsed_s": round(time.time() - t0, 1),
        "ok_points": len(results),
        "failed_points": len(failures),
        "raw_bytes": out_path.stat().st_size,
        "usage_after": common.usage_today(),
        "note": "Open-Meteo e' SOLO fonte dati: i raw non vengono pubblicati.",
    }
    eff_path = common.API_EFFICIENCY_DIR / ("fetch_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    eff_path.write_text(json.dumps(eff, indent=2, ensure_ascii=False), encoding="utf-8")
    print("[fetch_source_data] RAW scritto in data/_raw/source_raw.json (%.0f KB). MAI pubblicato." % size_kb)
    print("[fetch_source_data] REPORT efficienza: %d richieste (era %d) -> %s" % (planned, len(coords), eff_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())