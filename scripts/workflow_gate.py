#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
workflow_gate.py — GITHUB PRODUCTION HARDENING (1.0.0.6).

Classificazione OBBLIGATORIA e log canonici dei punti di controllo della GitHub
Action. Fonte unica (testabile) della separazione richiesta dall'addendum:

  A) NEW RUN AVAILABLE   -> la pipeline continua
  B) NO NEW RUN          -> clean success: NESSUN fetch/build/commit/deploy
  C) TECHNICAL ERROR     -> workflow FAIL: errore visibile, mai silenziato

Stati del Request Planner:
  PLAN VALID            -> continua
  PLAN BLOCKED BY BUDGET-> success safe-skip, last known good preservato
  PLAN ERROR            -> workflow FAIL, mai trattato come "no new run"

Classificazione exit code (contratto stabile):
  check:      0 = nuovo run · 10 = nessun nuovo run · 1+ = errore tecnico
  planner:    0 = piano ok · 2 = budget bloccato · 1+ = errore
  fetch:      0 = ok · 2 = budget (pre-flight) · 3 = capoluoghi mancanti · 1+ = errore
  build:      0 = ok · 1+ = errore
  validate:   0 = ok · 1+ = errore (PUBLISH SKIP, last known good intatto)
  publish:    0 = ok (nuovo dataset) · 1+ = errore

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


def classify_plan(rc):
    if rc == 0:
        return {"stage": "plan", "rc": rc, "decision": "continue",
                "message": "[INFO] Plan valid — pipeline continues",
                "outputs": {"plan_state": "ok"}, "exit_code": 0}
    if rc == 2:
        return {"stage": "plan", "rc": rc, "decision": "safe_skip",
                "message": "[INFO] Budget blocked — preserving last known good dataset",
                "outputs": {"plan_state": "budget_blocked"}, "exit_code": 0}
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
                "message": "[INFO] Budget blocked — preserving last known good dataset",
                "outputs": {"fetch_ok": "false", "fetch_reason": "budget_blocked"}, "exit_code": 0}
    if rc == 3:
        return {"stage": "fetch", "rc": rc, "decision": "safe_skip",
                "message": "[INFO] Capoluoghi mancanti — no partial publish (last known good preserved)",
                "outputs": {"fetch_ok": "false", "fetch_reason": "capoluoghi_mancanti"}, "exit_code": 0}
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