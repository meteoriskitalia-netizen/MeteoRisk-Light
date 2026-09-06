#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_negative_validation.py — test NEGATIVI della validazione dataset:
una validazione deve FALLIRE su dataset corrotti e il dataset protetto
(last known good) deve rimanere intatto.

PARTE G (1.0.0.8): il rilascio NON contiene dataset live in data/latest. Il
baseline "noto-buono" da proteggere viene quindi generato OFFLINE dal fixture
sintetico (gen_fixture_raw + build) in data/_staging; se invece data/latest
contiene un dataset reale (snapshot successivo) si protegge quello.

Copre 3 manomissioni:
  1. array hourly troncato (72 -> 70)           -> FAIL
  2. lat e selected_point alterati              -> FAIL
  3. valore non numerico in un array hourly     -> FAIL

Uso:  py -3 scripts/tests/test_negative_validation.py
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(SCRIPTS, "validate_dataset.py")
BUILD = os.path.join(SCRIPTS, "build_meteorisk_dataset.py")
FIXTURE = os.path.join(SCRIPTS, "tests", "gen_fixture_raw.py")
POINTS_FILE = "meteorisk-points.json"
PROTECTED_DIR = None  # risolto in ensure_baseline()


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def ensure_baseline():
    """Sceglie il baseline da proteggere:
    - data/latest reale presente -> lo protegge;
    - altrimenti (G1) genera il baseline OFFLINE dal fixture in _staging."""
    global PROTECTED_DIR
    if (common.DATA_LATEST / POINTS_FILE).exists():
        PROTECTED_DIR = common.DATA_LATEST
        return PROTECTED_DIR
    PROTECTED_DIR = common.DATA_STAGING
    if not (PROTECTED_DIR / POINTS_FILE).exists():
        subprocess.run([sys.executable, FIXTURE], check=True, env=_env(), capture_output=True, text=True)
        raw = common.DATA_WORK / "fixture_raw.json"
        subprocess.run([sys.executable, BUILD, "--raw-json", str(raw)],
                       check=True, env=_env(), capture_output=True, text=True)
        print("[baseline] dataset protetto (fixture offline): data/_staging (last known good)")
        return PROTECTED_DIR
    print("[baseline] dataset reale protetto: data/latest")
    return PROTECTED_DIR


def clone(tag):
    dst = common.DATA_WORK / tag
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(PROTECTED_DIR, dst)
    return dst


def read_points(dst):
    return json.loads((dst / POINTS_FILE).read_text(encoding="utf-8"))


def write_points(dst, points):
    (dst / POINTS_FILE).write_text(json.dumps(points), encoding="utf-8")


def run_validate(dst):
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--dir", str(dst)],
        capture_output=True, text=True, env=_env(),
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def protected_sha():
    return (PROTECTED_DIR / POINTS_FILE).read_bytes()


def main():
    ensure_baseline()
    before = protected_sha()
    failures = 0

    d1 = clone("_t_neg_len")
    p = read_points(d1)
    p["points"][0]["models"]["best_match"]["hourly"]["temperature_2m"] = \
        p["points"][0]["models"]["best_match"]["hourly"]["temperature_2m"][:70]
    write_points(d1, p)
    rc, out = run_validate(d1)
    ok = rc == 1 and "FAIL" in out
    print("[%s] array hourly troncato -> rc=%d (atteso 1)" % ("PASS" if ok else "FAIL", rc))
    if not ok:
        failures += 1

    d2 = clone("_t_neg_sel")
    p = read_points(d2)
    p["points"][1]["lat"] = 99.0
    write_points(d2, p)
    prov = json.loads((d2 / "meteorisk-provinces.json").read_text(encoding="utf-8"))
    prov["provinces"][1]["selected_point"]["lat"] = 99.0
    (d2 / "meteorisk-provinces.json").write_text(json.dumps(prov), encoding="utf-8")
    rc, out = run_validate(d2)
    ok = rc == 1 and "FAIL" in out
    print("[%s] lat/selected_point alterati -> rc=%d (atteso 1)" % ("PASS" if ok else "FAIL", rc))
    if not ok:
        failures += 1

    d3 = clone("_t_neg_val")
    p = read_points(d3)
    p["points"][2]["models"]["best_match"]["hourly"]["windspeed_10m"][0] = "abc"
    write_points(d3, p)
    rc, out = run_validate(d3)
    ok = rc == 1 and "FAIL" in out
    print("[%s] valore non numerico -> rc=%d (atteso 1)" % ("PASS" if ok else "FAIL", rc))
    if not ok:
        failures += 1

    after = protected_sha()
    ok = before == after
    print("[%s] dataset protetto intatto (last known good preservato)" % ("PASS" if ok else "FAIL"))
    if not ok:
        failures += 1

    for tag in ("_t_neg_len", "_t_neg_sel", "_t_neg_val"):
        d = common.DATA_WORK / tag
        if d.exists():
            shutil.rmtree(d)

    print("RESULT: %s (%d errori)" % ("PASS" if failures == 0 else "FAIL", failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())