"""TEST 1.0.0.8 — Decision engine + pre-flight guardrails (decide_cycle.py).

Matrice 2x2 (+ bootstrap, priorita' ASSOLUTA) e contratti rc per workflow_gate:
  0 = PIANO VALID · 2 = HARD SAFETY CEILING (safe skip) · 3 = HARD SAFETY
  CEILING IN BOOTSTRAP (FAIL: nessun dataset da preservare, G6) · 1 = ERRORE
  TECNICO. I guardrails bloccano SOLO oltre l'hard safety ceiling (nessun
  razionamento preventivo). SELF-SUFFICIENT: se data/_workdir/real_points.json
  non esiste viene generato offline con common.generate_real_points()
  (stesso generatore della pipeline, nessuna rete, nessun dato fake).
"""

import importlib.util
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import common  # noqa: E402
import decide_cycle  # noqa: E402


def _ensure_points_json():
    """Prerequisito auto-generato: punti reali (offline, dal generatore di
    produzione). MAI dati fake: e' il generatore usato dalla GitHub Action."""
    p = os.path.join(common.REPO_ROOT, "data", "_workdir", "real_points.json")
    if not os.path.exists(p):
        pts = common.generate_real_points()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(pts, fh, ensure_ascii=False)
    return p


_ensure_points_json()


def test_decision_matrix_2x2():
    assert decide_cycle.cycle_mode(True, True, False) == "coordinated"
    assert decide_cycle.cycle_mode(True, False, False) == "coordinated"
    assert decide_cycle.cycle_mode(False, True, False) == "best_match_only"
    assert decide_cycle.cycle_mode(False, False, False) == "none"


def test_bootstrap_overrides_all():
    # in stato INITIAL BOOTSTRAP la decisione e' SEMPRE bootstrap (fetch reale
    # coordnato), qualunque sia l'esito dei rilevatori; MAI 'no change'.
    assert decide_cycle.cycle_mode(False, False, True) == "bootstrap"
    assert decide_cycle.cycle_mode(True, False, True) == "bootstrap"
    assert decide_cycle.cycle_mode(False, True, True) == "bootstrap"


def test_fetch_mode_mapping():
    assert decide_cycle.fetch_mode_for("bootstrap") == "coordinated"
    assert decide_cycle.fetch_mode_for("coordinated") == "coordinated"
    assert decide_cycle.fetch_mode_for("best_match_only") == "best_match_only"
    assert decide_cycle.fetch_mode_for("none") == "none"


def test_fetch_request_count():
    plan = {"batches": [1, 2, 3], "n_model_legs": 2}
    assert decide_cycle.fetch_request_count("none", plan) == 0
    assert decide_cycle.fetch_request_count("coordinated", plan) == 6
    assert decide_cycle.fetch_request_count("bootstrap", plan) == 6
    assert decide_cycle.fetch_request_count("best_match_only", plan) == 3


def _points_json():
    return _ensure_points_json()


def test_main_none_clean_exit():
    args = ["--ecmwf-new", "false", "--best-changed", "false", "--no-write",
            "--points-json", _points_json()]
    with mock.patch.object(sys, "argv", ["decide_cycle"] + args), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=False):
        assert decide_cycle.main() == 0


def test_main_ceiling_blocked_steady_rc2():
    blocked = {"ok": False, "available": 0, "planned": 6, "reason": "HARD SAFETY CEILING (test)"}
    args = ["--ecmwf-new", "true", "--best-changed", "false", "--no-write",
            "--points-json", _points_json()]
    with mock.patch.object(sys, "argv", ["decide_cycle"] + args), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=False), \
         mock.patch.object(common, "guard_planned_requests", return_value=blocked):
        assert decide_cycle.main() == 2  # safe skip, ultimo dataset valido preservato


def test_main_ceiling_blocked_bootstrap_rc3():
    blocked = {"ok": False, "available": 0, "planned": 6, "reason": "HARD SAFETY CEILING (test)"}
    args = ["--ecmwf-new", "false", "--best-changed", "false", "--no-write",
            "--points-json", _points_json()]
    with mock.patch.object(sys, "argv", ["decide_cycle"] + args), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=True), \
         mock.patch.object(common, "guard_planned_requests", return_value=blocked):
        assert decide_cycle.main() == 3  # bootstrap: NESSUN safe-skip, workflow FAIL


def test_main_plan_valid_rc0_bootstrap():
    ok = {"ok": True, "available": 8994, "planned": 6}
    args = ["--ecmwf-new", "false", "--best-changed", "false", "--no-write",
            "--points-json", _points_json()]
    with mock.patch.object(sys, "argv", ["decide_cycle"] + args), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=True), \
         mock.patch.object(common, "guard_planned_requests", return_value=ok):
        assert decide_cycle.main() == 0  # bootstrap valido: fetch reale autorizzato


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (decision engine + pre-flight guardrails)")