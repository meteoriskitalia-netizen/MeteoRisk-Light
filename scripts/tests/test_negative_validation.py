#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_negative_validation.py — test NEGATIVI della validazione dataset:
una validazione deve FALLIRE su dataset corrotti e data/latest deve
rimanere intatto (last known good).

Copre 3 manomissioni:
  1. array hourly troncato (72 -> 70)           -> FAIL
  2. lat e selected_point alterati              -> FAIL
  3. valore non numerico in un array hourly     -> FAIL
In ogni caso data/latest NON viene toccato.

Uso:  py -3 scripts/tests/test_negative_validation.py
"""

import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "validate_dataset.py")
POINTS_FILE = "meteorisk-points.json"


def clone(tag):
    dst = common.DATA_WORK / tag
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(common.DATA_LATEST, dst)
    return dst


def read_points(dst):
    return json.loads((dst / POINTS_FILE).read_text(encoding="utf-8"))


def write_points(dst, points):
    (dst / POINTS_FILE).write_text(json.dumps(points), encoding="utf-8")


def run_validate(dst):
    proc = subprocess.run(
        [sys.executable, SCRIPT, "--dir", str(dst)],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return proc.returncode, out


def latest_sha():
    return (common.DATA_LATEST / POINTS_FILE).read_bytes()


def main():
    before = latest_sha()
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

    after = latest_sha()
    ok = before == after
    print("[%s] data/latest intatto (last known good preservato)" % ("PASS" if ok else "FAIL"))
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