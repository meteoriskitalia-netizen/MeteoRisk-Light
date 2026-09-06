#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflow_gate.py — GITHUB PRODUCTION HARDENING (1.0.0.6) · COORDINATED SCHEDULING (1.0.0.8)
                    · BEST MATCH CANARY + INITIAL BOOTSTRAP (1.0.0.8).

Classificazione OBBLIGATORIA e log canonici dei punti di controllo della GitHub
Action. Fonte unica (testabile) della separazione richiesta dall'addendum:

  A) NEW RUN AVAILABLE   -> la pipeline continua
  B) NO NEW RUN          -> clean success: NESSUN fetch/build/commit/deploy
  C) TECHNICAL ERROR     -> workflow FAIL: errore visibile, mai silenziato

Scheduling 1.0.0.8: il run check è su ECMWF IFS (DRIVER_MODEL, via Metadata API,
LEGGERO); best_match è COORDINATO e scaricato con ECMWF nello stesso ciclo.

1.0.0.8: canary Best Match indipendente (rilevatore sentinelle,
rc best: 0=cambiato · 10=invariato · 1+=errore), DECISION ENGINE decide_cycle
(rc plan: 0=ok · 2=HARD SAFETY CEILING safe skip · 3=HARD SAFETY CEILING in
BOOTSTRAP — senza dataset da preservare è workflow FAIL) e fetch FATAL in
bootstrap (rc 4). API USAGE GUARDRAILS: hard ceiling (mai oltre il tetto),
osservabilità separata (nessun razionamento preventivo).

Stati del Request Planner / Decision Engine:
  PLAN VALID                          -> continua
  PLAN BLOCKED BY HARD SAFETY CEILING -> success safe-skip, last known good preservato
  PLAN BLOCKED BY CEILING (BOOTSTRAP) -> workflow FAIL (nessun dataset da preservare)
  PLAN ERROR                          -> workflow FAIL, mai trattato come "no new run"

Classificazione exit code (contratto stabile):
  check (ecmwf): 0 = nuovo run · 10 = nessun nuovo run · 1+ = errore tecnico
  best  (canary): 0 = cambiato · 10 = invariato · 1+ = errore tecnico
  plan:          0 = piano ok · 2 = ceiling raggiunto (safe skip) ·
                 3 = ceiling in BOOTSTRAP (FAIL) · 1+ = errore
  fetch:         0 = ok · 2 = ceiling (safe) · 3 = capoluoghi mancanti (safe) ·
                 4 = BOOTSTRAP FATAL (ceiling/capoluoghi) · 1+ = errore
  build:         0 = ok · 1+ = errore
  validate:      0 = ok · 1+ = errore (PUBLISH SKIP, last known good intatto)
  publish:       0 = ok (nuovo dataset) · 1+ = errore

Ogni funzione restituisce un dict {stage, rc, decision, message, outputs, exit_code}
e la CLI, quando l'ambiente GitHub è presente ($GITHUB_OUTPUT), scrive le variabili
di output usate dai passi condizionali (`if: steps.X.outputs.Y`).

Uso nella Action (ogni step termina con `exit $?` del gate):
  python scripts/workflow_gate.py <stage> <rc>
"""

import os
import sys


def classify_check(rc):
    if rc == 0:
        return {"stage": "check", "rc": rc, "decision": "continue",
                "message": "[INFO] New model run detected — starting pipeline",
                "outputs": {"new_run": "true", "check_state": "new"}, "exit_code": 0}
    if rc == 10:
        return {"stage": "check", "rc": rc, "decision": "clean_exit",
                "message": "[INFO] No new model run — clean exit",
                "outputs": {"new_run": "false", "check_state": "none"}, "exit_code": 0}
    return {"stage": "check", "rc": rc, "decision": "fail",
            "message": "[ERROR] Model run check failed (rc=%d)" % rc,
            "outputs": {"new_run": "false", "check_state": "failed"}, "exit_code": 1}


def classify_best(rc):
    if rc == 0:
        return {"stage": "best", "rc": rc, "decision": "continue",
                "message": "[INFO] Best Match changed (canary) — refresh authorized",
                "outputs": {"best_changed": "true"}, "exit_code": 0}
    if rc == 10:
        return {"stage": "best", "rc": rc, "decision": "clean_exit",
                "message": "[INFO] Best Match unchanged — no refresh needed",
                "outputs": {"best_changed": "false"}, "exit_code": 0}
    return {"stage": "best", "rc": rc, "decision": "fail",
            "message": "[ERROR] Best Match canary check failed (rc=%d)" % rc,
            "outputs": {"best_changed": "false"}, "exit_code": 1}


def classify_plan(rc):
    if rc == 0:
        return {"stage": "plan", "rc": rc, "decision": "continue",
                "message": "[INFO] Plan valid — pipeline continues",
                "outputs": {"plan_state": "ok"}, "exit_code": 0}
    if rc == 2:
        return {"stage": "plan", "rc": rc, "decision": "safe_skip",
                "message": "[INFO] Hard safety ceiling reached — safe skip, last known good dataset preserved",
                "outputs": {"plan_state": "budget_blocked"}, "exit_code": 0}
    if rc == 3:
        return {"stage": "plan", "rc": rc, "decision": "fail",
                "message": "[ERROR] INITIAL DATASET BOOTSTRAP blocked by hard safety ceiling — no dataset exists, workflow FAIL",
                "outputs": {"plan_state": "bootstrap_budget_blocked"}, "exit_code": 1}
    return {"stage": "plan", "rc": rc, "decision": "fail",
            "message": "[ERROR] Request planner failed (rc=%d)" % rc,
            "outputs": {"plan_state": "error"}, "exit_code": 1}


def classify_fetch(rc):
    if rc == 0:
        return {"stage": "fetch", "rc": rc, "decision": "continue",
                "message": "[INFO] Source data fetched (raw temporaneo)",
                "outputs": {"fetch_ok": "true"}, "exit_code": 0}
    if rc == 2:
        return {"stage": "fetch", "rc": rc, "decision": "safe_skip",
                "message": "[INFO] Hard safety ceiling reached — safe skip, last known good dataset preserved",
                "outputs": {"fetch_ok": "false", "fetch_reason": "budget_blocked"}, "exit_code": 0}
    if rc == 3:
        return {"stage": "fetch", "rc": rc, "decision": "safe_skip",
                "message": "[INFO] Capoluoghi mancanti — no partial publish (last known good preserved)",
                "outputs": {"fetch_ok": "false", "fetch_reason": "capoluoghi_mancanti"}, "exit_code": 0}
    if rc == 4:
        return {"stage": "fetch", "rc": rc, "decision": "fail",
                "message": "[ERROR] INITIAL DATASET BOOTSTRAP cannot complete (ceiling/capoluoghi) — no dataset exists, workflow FAIL",
                "outputs": {"fetch_ok": "false", "fetch_reason": "bootstrap_fatal"}, "exit_code": 1}
    return {"stage": "fetch", "rc": rc, "decision": "fail",
            "message": "[ERROR] Source data fetch failed (rc=%d)" % rc,
            "outputs": {"fetch_ok": "false", "fetch_reason": "fetch_error"}, "exit_code": 1}


def classify_build(rc):
    if rc == 0:
        return {"stage": "build", "rc": rc, "decision": "continue",
                "message": "[INFO] Dataset built (staging)",
                "outputs": {"build_ok": "true"}, "exit_code": 0}
    return {"stage": "build", "rc": rc, "decision": "fail",
            "message": "[ERROR] Dataset build failed (rc=%d)" % rc,
            "outputs": {"build_ok": "false"}, "exit_code": 1}


def classify_validate(rc):
    if rc == 0:
        return {"stage": "validate", "rc": rc, "decision": "continue",
                "message": "[INFO] Dataset validation passed",
                "outputs": {"validate_ok": "true"}, "exit_code": 0}
    return {"stage": "validate", "rc": rc, "decision": "fail",
            "message": "[ERROR] Dataset validation failed (rc=%d)" % rc,
            "outputs": {"validate_ok": "false"}, "exit_code": 1}


def classify_publish(rc):
    if rc == 0:
        return {"stage": "publish", "rc": rc, "decision": "continue",
                "message": "[INFO] Dataset successfully published",
                "outputs": {"new_dataset": "true"}, "exit_code": 0}
    return {"stage": "publish", "rc": rc, "decision": "fail",
            "message": "[ERROR] Dataset publish failed (rc=%d)" % rc,
            "outputs": {"new_dataset": "false"}, "exit_code": 1}


def classify_points(rc):
    if rc == 0:
        return {"stage": "points", "rc": rc, "decision": "continue",
                "message": "[INFO] Required sample points ready",
                "outputs": {}, "exit_code": 0}
    return {"stage": "points", "rc": rc, "decision": "fail",
            "message": "[ERROR] Sample points generation failed (rc=%d)" % rc,
            "outputs": {}, "exit_code": 1}


_STAGES = {
    "check": classify_check,
    "best": classify_best,
    "plan": classify_plan,
    "fetch": classify_fetch,
    "build": classify_build,
    "validate": classify_validate,
    "publish": classify_publish,
    "points": classify_points,
}


def classify(stage, rc):
    fn = _STAGES.get(stage)
    if fn is None:
        raise ValueError("stage sconosciuto: %r (attesi: %s)" % (stage, ", ".join(sorted(_STAGES))))
    return fn(int(rc))


def _write_outputs(outputs):
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        for k, v in outputs.items():
            print("GATE_OUTPUT %s=%s" % (k, v))
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in outputs.items():
            fh.write("%s=%s\n" % (k, v))


def main():
    if len(sys.argv) != 3:
        print("uso: workflow_gate.py <stage> <rc>")
        print("stages: %s" % ", ".join(sorted(_STAGES)))
        return 2
    stage, rc = sys.argv[1], sys.argv[2]
    try:
        d = classify(stage, rc)
    except ValueError as exc:
        print("[ERROR] %s" % exc)
        return 2
    print(d["message"])
    _write_outputs(d["outputs"])
    return d["exit_code"]


if __name__ == "__main__":
    sys.exit(main())