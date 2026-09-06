"""TEST 1.0.0.8 — PARTE G: INITIAL DATASET BOOTSTRAP (G1/G3/G5/G6).

G3/G5: is_bootstrap_pending() = vero quando state assente OR dataset assente OR
fingerprint Best Match assente; MAI interpretato come 'no change'/'already
processed'. G1: il rilascio NON contiene dataset live in data/latest (solo
.gitkeep) e lo stato parte bootstrap_pending. G6: senza dataset da preservare i
fallimenti del primo ciclo sono workflow FAIL (contratti rc 3/4 in decide_cycle
/fetch_source_data, coperti da test_decide_cycle.py e test_workflow_gate.py).
"""

import json
import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import common  # noqa: E402


class _FakePath:
    def exists(self):
        return True


class _FakePathMissing:
    def exists(self):
        return False


def _full_state():
    return {
        "status": "live",
        "last_processed_key": "1788609600",
        "dataset": {"point_count": 257},
        "last_model_runs": {
            "ecmwf_ifs": {"last_run_initialisation_time": 1788609600},
            "best_match": {"last_fingerprint": "abc123"},
        },
    }


def test_g5_full_state_not_bootstrap():
    with mock.patch.object(common, "load_run_state", return_value=_full_state()), \
         mock.patch("pathlib.Path.exists", _FakePath.exists):
        assert common.is_bootstrap_pending() is False


def test_g5_missing_state_is_bootstrap():
    with mock.patch.object(common, "load_run_state", return_value={}), \
         mock.patch("pathlib.Path.exists", _FakePath.exists):
        assert common.is_bootstrap_pending() is True


def test_g5_missing_dataset_is_bootstrap():
    with mock.patch.object(common, "load_run_state", return_value=_full_state()), \
         mock.patch("pathlib.Path.exists", _FakePathMissing.exists):
        assert common.is_bootstrap_pending() is True


def test_g5_missing_best_match_fingerprint_is_bootstrap():
    st = _full_state()
    st["last_model_runs"]["best_match"] = {}
    with mock.patch.object(common, "load_run_state", return_value=st), \
         mock.patch("pathlib.Path.exists", _FakePath.exists):
        assert common.is_bootstrap_pending() is True


def test_g5_never_treated_as_no_change():
    # stato presente ma senza fingerprint Best Match (1.0.0.7 -> 1.0.0.8):
    # NON e' "already processed" ne' "no change" -> bootstrap attivo.
    st = _full_state()
    st["last_model_runs"].pop("best_match", None)
    with mock.patch.object(common, "load_run_state", return_value=st), \
         mock.patch("pathlib.Path.exists", _FakePath.exists):
        assert common.is_bootstrap_pending() is True


def test_g1_release_has_no_live_dataset():
    latest = common.REPO_ROOT / "data" / "latest"
    json_files = list(latest.glob("*.json"))
    assert json_files == [], "G1: data/latest non deve contenere dataset locale: %s" % json_files
    assert (latest / ".gitkeep").exists()


def test_g1_state_is_bootstrap_pending():
    state = common.load_run_state()
    assert state.get("status") == "bootstrap_pending"
    assert state.get("bootstrap_pending") is True
    assert (state.get("last_model_runs") or {}).get("best_match", {}).get("last_fingerprint") is None
    assert state.get("dataset") is None


def test_g1_api_usage_config_only():
    # configurazione guardrails pristine su file temporaneo: non dipende dallo
    # stato di esecuzioni precedenti e non tocca il file di release.
    real = common.API_USAGE_JSON
    try:
        common.API_USAGE_JSON = Path(__file__).resolve().parent / "_tmp_g1_api_usage.json"
        common.API_USAGE_JSON.write_text(
            (common.REPO_ROOT / "data" / "state" / "api_usage.json").read_text(encoding="utf-8"),
            encoding="utf-8")
        usage = common.load_usage_state()
        assert usage.get("days") == {}
        assert "daily_limit" not in usage
        gr = usage.get("api_usage_guardrails", {})
        assert gr.get("daily_safety_ceiling") == 10000
        assert gr.get("warn_threshold_fraction") == 0.8
        assert gr.get("hard_stop_enabled") is True
    finally:
        common.API_USAGE_JSON = real
        p = Path(__file__).resolve().parent / "_tmp_g1_api_usage.json"
        if p.exists():
            p.unlink()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (Parte G — INITIAL DATASET BOOTSTRAP G1/G3/G5/G6)")