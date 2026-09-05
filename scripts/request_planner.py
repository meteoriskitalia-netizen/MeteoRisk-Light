#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
REQUEST PLANNER (1.0.0.5 · 1.0.0.6 hardening) — pianificazione e pre-flight del fetch sorgente.

Principi (ADDENDUM obbligatorio, sezioni 9-16):
  - BUDGET: il consumo giornaliero è tracciato in data/state/api_usage.json e il
    piano ordinario NON consuma mai la riserva di sicurezza; pre-flight BLOCANTE
    se il piano non rientra nel budget effettivo del giorno.
  - EFFICIENZA: dati derivati consumati a stesso-dataset -> il fetch è eseguito
    UNA volta per dataset; le coordinate sono DEDUPLICATE (round 1e-4 ~11 m) e le
    richieste sono BATCH-ate (100 coordinati/richiesta; misurato: URL 3.2 KB,
    resp 4.1 MB, limite URL ~8 KB). 257 punti reali => 257 richieste naive
    diventano 3+1=4 richieste (risparmio ~98,4%).
  - Report non pubblicati: data/_workdir/request_plan_<ts>.json e
    data/_workdir/api_efficiency_<ts>.json (esclusi da .gitignore e dal publish).

Exit codes (contratto HARDENING 1.0.0.6):
  0 = PLAN VALID (preflight ok) · 2 = PLAN BLOCKED BY BUDGET (safe success,
  ultimo dataset valido preservato) · 1 = PLAN TECHNICAL ERROR (nessuna
  coordinata / errore; la Action FALLA, mai confuso con "no new run").
"""

import argparse
import json
import math
import sys

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def plan_batches(coords, batch_size=common.BATCH_MAX_LOCATIONS):
    """Suddivide le coordinate uniche in batch di dimensione <= batch_size."""
    batches = []
    for i in range(0, len(coords), batch_size):
        chunk = coords[i:i + batch_size]
        global_start = i
        batches.append({
            "batch": len(batches),
            "global_start": global_start,
            "locations": len(chunk),
            "lats": [c[0] for c in chunk],
            "lons": [c[1] for c in chunk],
        })
    return batches


def build_plan(points, batch_size=common.BATCH_MAX_LOCATIONS):
    coords = common.unique_coordinates(points)
    batches = plan_batches(coords, batch_size)
    n_models = len(common.DUAL_MODELS.split(","))
    naive_requests = len(coords)
    optimized_requests = len(batches) * n_models  # una richiesta per leg modello
    saved = naive_requests - optimized_requests
    efficiency = (math.inf if naive_requests == 0
                  else 100.0 * saved / naive_requests if naive_requests else 0.0)
    dup_observations = len(points) - len(coords)
    return {
        "points": len(points),
        "unique_coordinates": len(coords),
        "duplicate_observations": dup_observations,
        "naive_requests": naive_requests,
        "batch_size": batch_size,
        "n_model_legs": n_models,
        "optimized_requests": optimized_requests,
        "requests_saved": saved,
        "efficiency_gain_pct": round(efficiency, 2),
        "batches": batches,
        "estimated_locations_total": sum(b["locations"] for b in batches),
    }


def main():
    parser = argparse.ArgumentParser(description="Pianificazione richieste fetch (dedup + batching + preflight).")
    parser.add_argument("--points-json", default=str(common.REPO_ROOT / "data" / "_workdir" / "real_points.json"))
    parser.add_argument("--output", default=str(common.API_EFFICIENCY_DIR / "request_plan.json"),
                        help="File report pianificazione (default data/_workdir/api_efficiency/request_plan.json).")
    parser.add_argument("--no-write", action="store_true", help="Solo print, nessun file.")
    args = parser.parse_args()

    points = json.load(open(args.points_json, encoding="utf-8")) if __import__("os").path.exists(args.points_json) else None
    if not points:
        print("[request_planner] Nessuna coordinata in %s." % args.points_json)
        return 1

    plan = build_plan(points)
    budget = common.ensure_api_budget(plan["optimized_requests"])
    usage = common.usage_today()

    print("[request_planner] punti=%d · coordinate uniche=%d · osservazioni duplicate=%d"
          % (plan["points"], plan["unique_coordinates"], plan["duplicate_observations"]))
    print("[request_planner] richieste naive=%d → ottimizzate=%d (batch=%d x %d leg, risparmio=%d, efficienza=%+.2f%%)"
          % (plan["naive_requests"], plan["optimized_requests"], plan["batch_size"],
             plan["n_model_legs"], plan["requests_saved"], plan["efficiency_gain_pct"]))
    print("[request_planner] batch: %s" % ", ".join("%dx%d" % (b["batch"] + 1, b["locations"]) for b in plan["batches"]))
    print("[request_planner] budget: limite=%d riserva=%.0f%% effettivo=%d · usato oggi=%d · disponibile=%d"
          % (common.API_DAILY_LIMIT, common.API_SAFETY_RESERVE_FRAC * 100,
             common.effective_budget(), usage["requests"], budget["available"]))
    if not budget["ok"]:
        print("[request_planner] PRE-FLIGHT BLOCKED: %s" % budget["reason"])
        return 2
    print("[request_planner] PRE-FLIGHT OK: %d richieste pianificate <= %d disponibili."
          % (budget["planned"], budget["available"]))

    if not args.no_write:
        common.API_EFFICIENCY_DIR.mkdir(parents=True, exist_ok=True)
        report = {
            "plan": plan,
            "budget": budget,
            "usage_today": usage,
            "config": {"daily_limit": common.API_DAILY_LIMIT,
                       "safety_reserve_fraction": common.API_SAFETY_RESERVE_FRAC,
                       "effective_budget": common.effective_budget(),
                       "batch_size": common.BATCH_MAX_LOCATIONS,
                       "min_request_interval_s": common.API_MIN_REQUEST_INTERVAL_S},
            "generated_at": common.now_iso(),
        }
        common.API_EFFICIENCY_DIR.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
        print("[request_planner] Report pianificazione -> %s" % args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())