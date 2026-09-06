#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 4 (decide_cycle.py) — DECISION ENGINE + PRE-FLIGHT GUARDRAIL (1.0.0.8).

Combina i due rilevatori INDIPENDENTI e decide l'azione del ciclo:
  ecmwf_new    = nuovo run ECMWF IFS disponibile (check_model_runs, Metadata)
  best_changed = Best Match cambiato (canary sentinelle, check_best_match)
  bootstrap    = INITIAL DATASET BOOTSTRAP (state/dataset/fingerprint assenti, G3/G5)

Matrice decisionale (STATELESS 1.0.0.8 · FIX PIPELINE — NESSUNA dipendenza dal
raw di un ciclo precedente; ogni fetch riscarica SEMPRE entrambi i leg):
  ecmwf_new | best_changed | azione
  ----------+--------------+--------------------------------------------------
    true    |   true       | coordinated  = fetch completo entrambi i leg
    true    |   false      | coordinated  = fetch completo entrambi i leg
    false   |   true       | coordinated  = fetch completo entrambi i leg
    false   |   false      | none         = clean exit, zero fetch/build/commit
    (bootstrap)            | bootstrap    = fetch reale completo del primo ciclo

  La modalità best_match_only (refresh parziale con merge del raw precedente) è
  RIMOSSA: richiedeva data/_raw/source_raw.json del ciclo PRECEDENTE, che non
  sopravvive sui runner GitHub ephemeral. Quando il canary Best Match cambia e
  l'ECMWF è invariato, il ciclo esegue comunque un fetch completo coordinato
  (Best Match + ECMWF insieme): pipeline stateless rispetto a data/_raw.

Priority guardrails (NON un razionamento preventivo: i consumi sotto il tetto
non bloccano mai; solo oltre l'hard safety ceiling il fetch si ferma):
  1. INITIAL BOOTSTRAP            (senza dataset il safe-skip non ha senso -> FATAL)
  2. coordinated (entrambi i leg)
  3. canary sentinelle            (già eseguito in check_best_match, 1 richiesta/ciclo)
  4. retry selettivi              (controllo budget PRIMA di ogni retry, senza
                                   prenotazione preventiva per retry ipotetici)

Hard safety ceiling (PRE-FLIGHT guardrail, mai oltre il tetto):
  - steady-state -> rc 2: SAFE SKIP, ultimo dataset valido preservato, retry
    al ciclo successivo (MAI un fetch destinato a superare il tetto).
  - bootstrap    -> rc 3: nessun dataset da preservare -> workflow FAIL visibile
    (nessun file falso/parziale, requisito G6).

Exit codes (contratto 1.0.0.8, workflow_gate `plan`):
  0 = PIANO VALID (fetch autorizzato) · 2 = HARD SAFETY CEILING (safe skip) ·
  3 = HARD SAFETY CEILING (BOOTSTRAP, FAIL) · 1 = ERRORE TECNICO (FAIL)

Outputs (GITHUB_OUTPUT): cycle_mode · fetch_mode · plan_state · bootstrap_pending
"""

import argparse
import json
import os
import sys

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common  # noqa: E402
import request_planner  # noqa: E402


def cycle_mode(ecmwf_new, best_changed, bootstrap):
    """Decisione pura del ciclo (testabile). Stateless: qualunque cambiamento
    reale -> fetch completo coordinato. Nessun best_match_only (refresh parziale)."""
    if bootstrap:
        return "bootstrap"
    if ecmwf_new:
        return "coordinated"
    if best_changed:
        return "coordinated"
    return "none"


def fetch_mode_for(mode):
    """Modalità di fetch associata alla decisione del ciclo (pura). Il fetch è
    SEMPRE completo coordinato quando c'è lavoro (nessun refresh parziale)."""
    if mode == "none":
        return "none"
    return "coordinated"


def fetch_request_count(mode, plan):
    if mode in ("none",):
        return 0
    return len(plan["batches"]) * plan["n_model_legs"]


def write_outputs(outputs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in outputs.items():
            print("GATE_OUTPUT %s=%s" % (k, v))
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in outputs.items():
            fh.write("%s=%s\n" % (k, v))


def main():
    parser = argparse.ArgumentParser(description="Decision engine + pre-flight guardrails del ciclo.")
    parser.add_argument("--ecmwf-new", choices=["true", "false"], default="true",
                        help="Nuovo run ECMWF rilevato (output del gate check).")
    parser.add_argument("--best-changed", choices=["true", "false"], default="true",
                        help="Best Match cambiato (output del gate best).")
    parser.add_argument("--points-json",
                        default=str(common.REPO_ROOT / "data" / "_workdir" / "real_points.json"))
    parser.add_argument("--no-write", action="store_true", help="Solo print, nessun file report.")
    args = parser.parse_args()

    ecmwf_new = args.ecmwf_new == "true"
    best_changed = args.best_changed == "true"
    bootstrap = bool(common.is_bootstrap_pending())

    if not os.path.exists(args.points_json):
        print("[decide_cycle] Coordinate non trovate: %s (errore tecnico)." % args.points_json)
        return 1
    points = json.load(open(args.points_json, encoding="utf-8"))
    if not points:
        print("[decide_cycle] Nessuna coordinata (errore tecnico).")
        return 1

    plan = request_planner.build_plan(points)
    mode = cycle_mode(ecmwf_new, best_changed, bootstrap)
    fetch_mode = fetch_mode_for(mode)
    needed = fetch_request_count(mode, plan)
    budget = {"ok": True, "available": common.available_today(), "planned": 0}
    if mode != "none":
        budget = common.guard_planned_requests(needed)
        planned_print = budget["planned"]
        avail_print = budget["available"]
        print("[decide_cycle] PRE-FLIGHT per mode=%s: richieste=%d · guardrail disponibile=%d"
              % (mode, planned_print, avail_print))

    print("[decide_cycle] ecmwf_new=%s best_changed=%s bootstrap=%s -> MODE=%s (fetch_mode=%s, coord=%d, leg=%d)"
          % (ecmwf_new, best_changed, bootstrap, mode, fetch_mode, len(plan["batches"]), plan["n_model_legs"]))

    if mode == "none":
        print("[decide_cycle] NESSUN lavoro richiesto: clean exit (zero fetch/build/commit/deploy).")
        write_outputs({"cycle_mode": "none", "fetch_mode": "none", "plan_state": "none",
                       "bootstrap_pending": "true" if bootstrap else "false"})
        return 0

    if not budget["ok"]:
        if bootstrap:
            print("[decide_cycle] HARD SAFETY CEILING IN BOOTSTRAP (nessun dataset da preservare): "
                  "workflow FAIL richiesto. %s" % budget["reason"])
            write_outputs({"cycle_mode": mode, "fetch_mode": fetch_mode,
                           "plan_state": "bootstrap_budget_blocked",
                           "bootstrap_pending": "true"})
            return 3
        print("[decide_cycle] HARD SAFETY CEILING (safe skip): %s. Retry al prossimo ciclo; ultimo "
              "dataset valido preservato." % budget["reason"])
        write_outputs({"cycle_mode": mode, "fetch_mode": fetch_mode,
                       "plan_state": "budget_blocked",
                       "bootstrap_pending": "false"})
        return 2

    print("[decide_cycle] PIANO VALID: %d richieste <= %d disponibili. Fetch autorizzato (%s)."
          % (budget["planned"], budget["available"], fetch_mode))
    if not args.no_write:
        common.API_EFFICIENCY_DIR.mkdir(parents=True, exist_ok=True)
        report = {
            "cycle_mode": mode, "fetch_mode": fetch_mode,
            "ecmwf_new": ecmwf_new, "best_changed": best_changed, "bootstrap": bootstrap,
            "plan": plan, "budget": budget, "generated_at": common.now_iso(),
        }
        out = common.API_EFFICIENCY_DIR / "decide_cycle.json"
        out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        print("[decide_cycle] Report decisione -> %s" % out)
    write_outputs({"cycle_mode": mode, "fetch_mode": fetch_mode, "plan_state": "ok",
                   "bootstrap_pending": "true" if bootstrap else "false"})
    return 0


if __name__ == "__main__":
    sys.exit(main())