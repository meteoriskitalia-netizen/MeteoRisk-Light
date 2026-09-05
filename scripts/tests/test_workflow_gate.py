"""Test HARDENING 1.0.0.6: classificazione workflow_gate + catena pipeline (TEST A-F).

Verifica la separazione OBBLIGATORIA:
  A) check 10 (no new run)  -> clean success, zero fetch/build/commit/deploy
  B) check 0 (+ catena ok)  -> planner, budget check, fetch, build, validate, commit, deploy
  C) check 1+ (errore)      -> workflow FAIL, nessuna pipeline successiva
  D) planner 1 (errore)     -> workflow FAIL, nessun fetch/pubblicazione
  E) planner 2 (budget bloccato) -> workflow SUCCESS, safe skip, last known good
  F) validate 1             -> workflow FAIL, publish/commit/deploy mai eseguiti
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import workflow_gate as wg  # noqa: E402


def run_chain(check_rc, plan_rc=None, fetch_rc=None, build_rc=None,
              validate_rc=None, publish_rc=None):
    """Simula la catena del workflow (if: sui gate) e riporta quali passi girano."""
    used = {"plan": False, "fetch": False, "build": False, "validate": False,
            "publish": False, "commit": False, "deploy": False}
    check = wg.classify_check(check_rc)
    plan = fetch = build = validate = publish = None
    if check["decision"] in ("continue",):
        plan = wg.classify_plan(plan_rc)
        used["plan"] = True
        if plan["decision"] == "continue":
            fetch = wg.classify_fetch(fetch_rc)
            used["fetch"] = True
            if fetch["decision"] == "continue":
                build = wg.classify_build(build_rc)
                used["build"] = True
                if build["decision"] == "continue":
                    validate = wg.classify_validate(validate_rc)
                    used["validate"] = True
                    if validate["decision"] == "continue":
                        publish = wg.classify_publish(publish_rc)
                        used["publish"] = True
                        if publish["decision"] == "continue":
                            used["commit"] = True
                            used["deploy"] = True
    last = publish or validate or build or fetch or plan or check
    return used, check, last


def assert_no_heavy(used):
    for s in ("fetch", "build", "validate", "publish", "commit", "deploy"):
        assert used[s] is False, "passo %s non deve eseguire" % s


def test_a_no_new_run_clean_exit():
    d = wg.classify_check(10)
    assert d["exit_code"] == 0
    assert d["decision"] == "clean_exit"
    assert d["outputs"]["new_run"] == "false"
    assert "[INFO] No new model run — clean exit" in d["message"]
    used, _, _ = run_chain(10)
    assert used["plan"] is False
    assert_no_heavy(used)


def test_b_new_run_full_chain():
    d = wg.classify_check(0)
    assert d["exit_code"] == 0
    assert d["decision"] == "continue"
    assert d["outputs"]["new_run"] == "true"
    assert "[INFO] New model run detected" in d["message"]
    used, _, pub = run_chain(0, plan_rc=0, fetch_rc=0, build_rc=0,
                             validate_rc=0, publish_rc=0)
    for s in ("plan", "fetch", "build", "validate", "publish", "commit", "deploy"):
        assert used[s] is True, "passo %s deve eseguire nella catena completa" % s
    assert "[INFO] Dataset successfully published" in pub["message"]


def test_c_model_check_error_fails():
    d = wg.classify_check(1)
    assert d["exit_code"] == 1
    assert d["decision"] == "fail"
    assert "[ERROR]" in d["message"]
    used, _, _ = run_chain(1)
    assert_no_heavy(used)


def test_d_planner_error_fails():
    d = wg.classify_plan(1)
    assert d["exit_code"] == 1
    assert d["decision"] == "fail"
    assert "[ERROR] Request planner failed" in d["message"]
    used, _, _ = run_chain(0, plan_rc=1)
    assert used["fetch"] is False
    assert_no_heavy(used)


def test_e_budget_blocked_safe_success():
    d = wg.classify_plan(2)
    assert d["exit_code"] == 0
    assert d["decision"] == "safe_skip"
    assert "[INFO] Budget blocked — preserving last known good dataset" in d["message"]
    used, _, _ = run_chain(0, plan_rc=2)
    assert staged_fetch_not_run(used)
    assert_no_heavy(used)


def staged_fetch_not_run(used):
    # plan gira ma fetch e successivi non devono eseguire
    return used["fetch"] is False and used["publish"] is False


def test_f_validation_failure_blocks_publish():
    d = wg.classify_validate(1)
    assert d["exit_code"] == 1
    assert d["decision"] == "fail"
    assert "[ERROR] Dataset validation failed" in d["message"]
    used, _, _ = run_chain(0, plan_rc=0, fetch_rc=0, build_rc=0, validate_rc=1)
    assert used["publish"] is False
    assert used["commit"] is False
    assert used["deploy"] is False


def test_fetch_budget_and_nopartial_safe():
    d2 = wg.classify_fetch(2)
    assert d2["exit_code"] == 0 and d2["decision"] == "safe_skip"
    assert d2["outputs"]["fetch_reason"] == "budget_blocked"
    d3 = wg.classify_fetch(3)
    assert d3["exit_code"] == 0 and d3["decision"] == "safe_skip"
    assert d3["outputs"]["fetch_reason"] == "capoluoghi_mancanti"
    assert "no partial publish" in d3["message"]
    df = wg.classify_fetch(1)
    assert df["exit_code"] == 1 and df["decision"] == "fail"
    assert "[ERROR] Source data fetch failed" in df["message"]


def test_build_publish_errors_fail():
    assert wg.classify_build(1)["exit_code"] == 1
    assert wg.classify_publish(1)["exit_code"] == 1
    assert wg.classify_publish(0)["outputs"]["new_dataset"] == "true"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (TEST A-F workflow gate)")