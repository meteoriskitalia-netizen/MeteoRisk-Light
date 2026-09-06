#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 6 (fetch_source_data.py): scarica i dati RAW dei modelli sorgente nel
directorio TEMPORANEO data/_raw (MAI pubblicato: escluso dal repository e da
qualunque publish). Open-Meteo è una FONTE (input meteorologico): i payload
costituenti non vengono ripubblicati, specchiati o incapsulati.

1.0.0.5 — PRODUCTION PIPELINE (1.0.0.6: hardening invariato; 1.0.0.8: il ciclo è
  comandato dal run ECMWF IFS; BEST MATCH viene scaricato COORDINATO nello stesso
  ciclo — stesso fetched_at, dataset temporalmente coerente):
  - REQUEST PLANNER integrato: coordinate DEDUPLICATE + BATCH (100/richiesta) +
    pre-flight GUARDRAIL (api_usage.json, hard safety ceiling).
  - Piano reale (257 punti, 107 province): 3 blocchi (100/100/57) x 2 leg =
    6 richieste (era 1 a punto: 257). Spaziatura 30 s anti minutely-limit
    (~5/min osservato sul campo).
  - RETRY selettivi e limitati: al primo giro falliscono solo alcuni batch;
    vengono ritentati SOLO quelli falliti; l'exponential backoff limitato vive
    nel client interno (RETRY_LIMIT, mai ripetizioni integrali del piano).
  - OSSERVABILITA' tracciata: data/state/api_usage.json (richieste/fallite/
    riuscite/batch/località/byte/checks/canary/fetch/retry per giorno — mai
    usata per blocchi se non l'hard ceiling). Report efficienza in
    data/_workdir/api_efficiency/ (non pubblicato).
  - Exit codes: 0 = raw scritto · 2 = pre-flight HARD SAFETY CEILING · 3 =
    capoluoghi mancanti (abort: nessuna pubblicazione parziale) · 1 = errore
    tecnico (coordinate assenti o fallimento residuo).

1.0.0.8 — STATELESS FULL COORDINATED FETCH (FIX PIPELINE):
  - La modalità `best_match_only` (refresh parziale del solo leg best_match con
    merge dell'ecmwf_ifs dal raw PRECEDENTE) è RIMOSSA: data/_raw non persiste
    tra i run (runner GitHub ephemeral), quindi quel merge non era riproducibile
    e rendeva la pipeline dipendente da stato residuale. OGGI ogni ciclo di
    lavoro scarica SEMPRE entrambi i leg (best_match + ecmwf_ifs) in un fetch
    completo coordinato: il raw è scritto e consumato DENTRO lo stesso run,
    MAI riletto da un ciclo successivo (STATELESS rispetto a data/_raw).
  - BOOTSTRAP (G5): se non esiste ancora un dataset live, gli esiti che in
    steady-state sono "safe skip" diventano FATAL (rc 4): hard ceiling o
    capoluoghi mancanti in bootstrap non hanno alcun last-known-good da
    preservare → workflow FAIL visibile (nessun file falso/parziale, G6).
  - RETRY + BUDGET (FIX 1.0.0.8): ECCETTO il pre-flight, anche ogni re-tentativo
    selettivo verifica il budget residuo PRIMA di partire (richiesta → errore
    retryable → controllo budget → retry/stop). Nessuna prenotazione preventiva
    per retry ipotetici: si controlla SOLO il momento del retry.
  - Exit codes 1.0.0.8: 0 = raw scritto · 2 = HARD SAFETY CEILING (safe skip) ·
    3 = capoluoghi mancanti (safe skip) · 4 = BOOTSTRAP FATAL (ceiling o
    capoluoghi su primo dataset: FAIL) · 1 = errore tecnico.
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
    Il fetch è SEMPRE completo (entrambi i leg, coordinated). Ritorna
    (records, error)."""
    records = {}
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


def retry_budget_available(need):
    """FIX PIPELINE — budget residuo PRIMA di un re-tentativo selettivo
    (richiesta -> errore retryable -> controllo budget -> retry o stop).
    Nessuna prenotazione preventiva: si verifica solo al momento del retry."""
    return common.budget_ok_for(need)


def main():
    parser = argparse.ArgumentParser(description="Fetch source data (raw, temporaneo).")
    parser.add_argument("--points-json", default=str(common.REPO_ROOT / "data" / "_workdir" / "real_points.json"),
                        help="File coordinate reali generate dal port (default data/_workdir/real_points.json).")
    parser.add_argument("--workers", type=int, default=1,
                        help="Compat. legacy: i batch sono sequenziali (pacing anti minutely-limit).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Stampa il PIANO OTTIMIZZATO (dedup+batch+preflight) senza scaricare.")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Salta il blocco da hard safety ceiling (uso diagnostico, MAI nelle Action).")
    parser.add_argument("--mode", choices=["coordinated"], default="coordinated",
                        help="coordinated (default e UNICO): fetch completo di entrambi i leg "
                             "(best_match + ecmwf_ifs). Niente refresh parziale best_match_only "
                             "(rimosso: dipendeva dal raw di un ciclo precedente).")
    args = parser.parse_args()

    if not os.path.exists(args.points_json):
        print("[fetch_source_data] Coordinate non trovate: %s (errore tecnico)" % args.points_json)
        return 1
    points = json.load(open(args.points_json, encoding="utf-8"))
    if not points:
        print("[fetch_source_data] Nessuna coordinata (errore tecnico).")
        return 1

    bootstrap = common.is_bootstrap_pending()

    coords = common.unique_coordinates(points)
    n_models = len(common.DUAL_MODELS.split(","))
    batches = [{"batch": i, "global_start": i * common.BATCH_MAX_LOCATIONS,
                "locations": len(chunk),
                "lats": [c[0] for c in chunk], "lons": [c[1] for c in chunk]}
               for i, chunk in enumerate(
                   [coords[j:j + common.BATCH_MAX_LOCATIONS] for j in range(0, len(coords), common.BATCH_MAX_LOCATIONS)])]
    planned = len(batches) * n_models
    budget = common.guard_planned_requests(planned)

    print("[fetch_source_data] Real points=%d · coordinate uniche=%d (dedup) · batch=%dx%d · %d leg → richieste ottimizzate=%d (era %d) · mode=coordinated%s"
          % (len(points), len(coords), len(batches), common.BATCH_MAX_LOCATIONS, n_models, planned, len(coords),
             " · BOOTSTRAP" if bootstrap else ""))
    usage = common.usage_today()
    print("[fetch_source_data] Guardrails: ceiling=%d usato-oggi=%d disponibile=%d"
          % (common.effective_budget(), usage["requests"], budget["available"]))

    if args.dry_run:
        print("[fetch_source_data] DRY-RUN: batch=%s (x%d leg), risparmio richieste=%d, efficienza=%+.2f%%"
              % (", ".join("%dx%d" % (b["batch"] + 1, b["locations"]) for b in batches),
                 n_models,
                 len(coords) - planned,
                 0.0 if not coords else 100.0 * (len(coords) - planned) / len(coords)))
        return 0

    if not budget["ok"]:
        print("[fetch_source_data] PRE-FLIGHT HARD SAFETY CEILING: %s" % budget["reason"])
        if not args.skip_preflight:
            if bootstrap:
                print("[fetch_source_data] BOOTSTRAP FATAL: nessun dataset da preservare (G6) "
                      "-> workflow FAIL, nessun file falso/parziale.")
                return 4
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
                                successful=(reqs - bads) * n_models,
                                forecast=reqs * n_models,
                                batches=reqs,
                                locations=sum(b["locations"] for b in batch_list))

    # primo giro: tutti i batch
    results, failures = run_pass(batches)
    record_usage(len(batches), sum(1 for b in batches if any(
        pidx in failures for pidx in [coords[b["global_start"] + k][2][0]
                                      for k in range(b["locations"])])), batches)

    # retry SELETTIVO: un solo re-tentativo dei SOLI batch falliti (l'exponential
    # backoff interno a fetch_source_batch copre già i transienti su quel batch).
    # BUDGET PRIMA DEL RETRY (FIX 1.0.0.8): se il tetto giornaliero sarebbe
    # superato dai retry, NON si ritenta (nessuna prenotazione preventiva: il
    # controllo avviene solo a questo punto, dopo i consumi del primo giro).
    failed_batches = [b for b in batches if any(
        pidx in failures for pidx in [coords[b["global_start"] + k][2][0]
                                      for k in range(b["locations"])])]
    if failed_batches:
        retry_need = len(failed_batches) * n_models
        if not retry_budget_available(retry_need):
            print("[fetch_source_data] RETRY SKIPPED (budget): serve %d richieste di re-tentativo, ma il "
                  "tetto giornaliero non lo consente (FIX 1.0.0.8: nessun retry oltre il tetto, nessuna "
                  "prenotazione preventiva). I batch falliti restano falliti."
                  % retry_need)
        else:
            print("[fetch_source_data] retry selettivo di %d batch falliti (budget residuo OK per %d richieste)..."
                  % (len(failed_batches), retry_need))
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
        if bootstrap:
            print("[fetch_source_data] BOOTSTRAP FATAL: nessun dataset da preservare (G6) "
                  "-> workflow FAIL, nessun file falso/parziale.")
            return 4
        return 3

    fetched_ts = common.now_iso()
    sorted_points = sorted(results.items())
    leg_ts = {"best_match_fetched_at": fetched_ts, "ecmwf_fetched_at": fetched_ts}
    cycle_mode = "coordinated"

    payload = {
        "fetched_at": fetched_ts,
        "forecast_days": common.FORECAST_DAYS,
        "timezone": common.TIMEZONE,
        "driver_model": common.DRIVER_MODEL,
        "models": common.DUAL_MODELS.split(","),   # dataset SEMPRE dual (best_match + ecmwf_ifs)
        "cycle_mode": cycle_mode,
        "leg_timestamps": leg_ts,
        "points": sorted_points,
    }
    tmp_path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp_path, out_path)
    size_kb = out_path.stat().st_size / 1024.0
    common.record_api_usage(requests=0, bytes_=out_path.stat().st_size)  # solo volume raw generato

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