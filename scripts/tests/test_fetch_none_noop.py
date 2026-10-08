"""TEST HOTFIX 1.1.0.8 — FALSO ALLARME `budget_blocked` nel ciclo idle (mode=none).

Diagnosi: nel ciclo "nessun lavoro" (decide_cycle -> cycle_mode=none) lo step
fetch partiva lo stesso perche' la condizione era solo
`plan_state == 'ok'` (il gate riscrive plan_state=ok su rc 0, anche per none).
Il fetch veniva invocato con `--mode none` -> argparse (choices=[coordinated])
usciva con rc 2 -> workflow_gate.classify_fetch(2) lo classificava come HARD
SAFETY CEILING -> `Fetch: false budget_blocked` spurio a ogni ciclo idle.
I dati NON erano fermi.

Copertura:
  1. FIX PRIMARIO: la condizione `if` dello step fetch esige
     fetch_mode == 'coordinated' (update-weather-data.yml).
  2. decide_cycle, nel ramo none, esporta fetch_mode=none; il gate riscrive
     comunque plan_state=ok -> e' il secondo termine a fermare il fetch.
  3. DIFENSIVA: fetch_source_data.py --mode none -> rc DEDICATO 5 (NO-OP),
     mai rc 2, senza leggere coordinate ne' fare richieste.
  4. workflow_gate.classify_fetch(5) -> clean exit (fetch_ok=false,
     fetch_reason=mode_none), MAI budget_blocked; rc 2 resta il ceiling.
  5. Errore di uso argparse -> rc 1 (mai 2).
Nessun test esistente e' stato modificato.
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import common  # noqa: E402
import decide_cycle  # noqa: E402
import fetch_source_data  # noqa: E402
import workflow_gate as wg  # noqa: E402

_RELEASE_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _RELEASE_ROOT / ".github" / "workflows" / "update-weather-data.yml"


def _run_fetch_main(argv):
    with mock.patch.object(sys, "argv", ["fetch_source_data"] + argv):
        return fetch_source_data.main()


def _gate_outputs(stdout):
    """Estrae le GATE_OUTPUT k=v stampate quando GITHUB_OUTPUT e' assente."""
    out = {}
    for line in stdout.splitlines():
        if line.startswith("GATE_OUTPUT "):
            k, _, v = line[len("GATE_OUTPUT "):].partition("=")
            out[k] = v
    return out


def _ensure_points_json():
    """Prerequisito offline (stesso generatore di produzione di test_decide_cycle)."""
    p = os.path.join(common.REPO_ROOT, "data", "_workdir", "real_points.json")
    if not os.path.exists(p):
        pts = common.generate_real_points()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(pts, fh, ensure_ascii=False)
    return p


def test_yml_fetch_if_requires_fetch_mode_coordinated():
    """FIX PRIMARIO: lo step fetch parte SOLO con fetch_mode == coordinated."""
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert ("if: steps.plan.outputs.plan_state == 'ok' "
            "&& steps.plan.outputs.fetch_mode == 'coordinated'") in text
    # il mode passato allo step e' l'output del decision engine
    assert '--mode "${{ steps.plan.outputs.fetch_mode }}"' in text


def test_decide_cycle_none_exports_fetch_mode_gate_rewrites_plan_state():
    """Ramo none: fetch_mode=none esportato; il gate riscrive plan_state=ok
    (causa radice) -> il secondo termine della condizione `if` ferma il fetch."""
    args = ["decide_cycle", "--ecmwf-new", "false", "--best-changed", "false",
            "--no-write", "--points-json", _ensure_points_json()]
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", args), \
         mock.patch.dict(os.environ, {"GITHUB_OUTPUT": ""}), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=False), \
         redirect_stdout(buf):
        rc = decide_cycle.main()
    assert rc == 0
    outs = _gate_outputs(buf.getvalue())
    assert outs.get("fetch_mode") == "none"
    assert outs.get("cycle_mode") == "none"
    # causa radice del falso allarme: gate plan rc0 -> plan_state=ok (append)
    plan_state = wg.classify_plan(0)["outputs"]["plan_state"]
    assert plan_state == "ok"
    # simulazione della condizione `if` dello step fetch -> DEVE essere False
    assert not (plan_state == "ok" and outs.get("fetch_mode") == "coordinated")


def test_fetch_mode_none_noop_rc5_never_rc2():
    """DIFENSIVA: --mode none -> rc dedicato 5 (≠2), no-op PRIMA di ogni
    lettura file / chiamata di rete (anche con points-json inesistente)."""
    missing = os.path.join(tempfile.mkdtemp(prefix="mri_none_"), "missing.json")
    buf = io.StringIO()
    with mock.patch.object(common, "fetch_source_batch",
                            side_effect=AssertionError("nessuna chiamata di rete attesa")), \
         redirect_stdout(buf):
        rc = _run_fetch_main(["--mode", "none", "--points-json", missing])
    assert rc == fetch_source_data.RC_MODE_NONE
    assert rc == 5
    assert rc != 2
    assert "NO-OP" in buf.getvalue()


def test_gate_fetch_rc5_clean_exit_never_budget_blocked():
    """Il gate classifica il no-op come clean exit, MAI come ceiling; il rc 2
    resta il vero hard safety ceiling (nessuna regressione del guardrail)."""
    d = wg.classify_fetch(fetch_source_data.RC_MODE_NONE)
    assert d["decision"] == "clean_exit"
    assert d["exit_code"] == 0
    assert d["outputs"]["fetch_ok"] == "false"
    assert d["outputs"]["fetch_reason"] == "mode_none"
    assert d["outputs"]["fetch_reason"] != "budget_blocked"
    ceiling = wg.classify_fetch(2)
    assert ceiling["outputs"]["fetch_reason"] == "budget_blocked"
    assert ceiling["decision"] == "safe_skip"
    assert ceiling["exit_code"] == 0


def test_plan_ok_plus_fetch_mode_none_not_budget_blocked():
    """Scenario del bug: plan_state=ok (riscritto dal gate) + fetch_mode=none
    -> l'esito del fetch non puo' essere budget_blocked."""
    assert wg.classify_plan(0)["outputs"]["plan_state"] == "ok"
    assert fetch_source_data.RC_MODE_NONE != 2
    d = wg.classify_fetch(fetch_source_data.RC_MODE_NONE)
    assert d["outputs"].get("fetch_reason") != "budget_blocked"
    assert d["exit_code"] == 0


def test_argparse_usage_error_exits_1_never_2():
    """Errore di uso CLI -> rc 1: rc 2 esclusivamente per il vero ceiling."""
    try:
        _run_fetch_main(["--mode", "best_match_only", "--dry-run",
                         "--points-json", _ensure_points_json()])
        raise AssertionError("SystemExit atteso per --mode best_match_only")
    except SystemExit as exc:
        assert exc.code == 1
        assert exc.code != 2


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (hotfix falso allarme budget_blocked ciclo idle)")
