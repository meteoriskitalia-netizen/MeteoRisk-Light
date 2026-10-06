#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_day0_alignment.py — REGRESSIONE FIX 1.0.1.4 "GIORNI SCORRETTI" (scarto +1).
Le serie orarie/giornaliere Open-Meteo partono SEMPRE dall'00:00 del giorno
Europe/Rome del FETCH (daily[0] = stesso giorno), NON dal giorno di INIT del
run driver (ecmwf_ifs). Se metadata.day0 veniva calcolato da run_init_ts e il
run partiva la sera/notte del giorno PRIMA, day0 restava indietro di 1 →
in applicazione "Oggi" mostrava i dati di Domani (dopodomani = vuoto).

Il test simula ESATTAMENTE questo scenario offline:
  - run_state.json con run_init_ts = 2025-12-31T20:00:00Z  →  giorno Europe/Rome 2025-12-31
  - raw sintetico con fetched_at = 2026-01-01T00:00:00Z e _time {hourly0: "2026-01-01T00:00", daily0: "2026-01-01"}
Checks:
  1. build produce metadata.day0 == "2026-01-01" (OVERRIDE data, NON 2025-12-31 da init)
  2. metadata.time_base == "2026-01-01T00:00" (primo timestamp orario)
  3. validate --staging PASS (invariante day0 == time_base[:10] soddisfatta)
  4. NEGATIVO: day0/points sfalsati MAI validi (validate FAIL) → un dataset
     allineato male non puo' essere pubblicato.

Uso:  py -3 scripts/tests/test_day0_alignment.py
"""

import datetime as _dt
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import common

SCRIPTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VA = os.path.join(SCRIPTS, "validate_dataset.py")
BUILD = os.path.join(SCRIPTS, "build_meteorisk_dataset.py")
FIXTURE = os.path.join(SCRIPTS, "tests", "gen_fixture_raw.py")
STATE_JSON = common.DATA_STATE / "last_model_run.json"
EXPECT_DAY0 = "2026-01-01"      # giorno Europe/Rome esatto del FETCH
EXPECT_BASE = "2026-01-01T00:00"  # primo timestamp orario (daily[0] = 2026-01-01)
INIT_DAY = "2025-12-31"         # giorno di init del run (il trap: il giorno PRIMA)

# run_init_ts = 2025-12-31T20:00:00Z → in Europe/Rome (UTC+1) = 2025-12-31T21:00
INIT_TS = int(_dt.datetime(2025, 12, 31, 20, 0, tzinfo=_dt.timezone.utc).timestamp())
CHECKED_AT = "2026-01-01T00:05:00Z"


def _env():
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


def run(*args):
    proc = subprocess.run([sys.executable] + list(args), capture_output=True, text=True, env=_env())
    if proc.returncode != 0:
        print(proc.stdout[-4000:])
        print(proc.stderr[-4000:])
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def read_json(p):
    return json.loads(common.Path(p).read_text(encoding="utf-8"))


def load_state():
    return json.loads(STATE_JSON.read_text(encoding="utf-8")) if STATE_JSON.exists() else None


def main():
    failures = 0
    saved_state = None
    try:
        saved_state = load_state()

        # --- setup: run state con init del giorno PRIMA (il caso del bug) ---
        common.DATA_STATE.mkdir(parents=True, exist_ok=True)
        STATE_JSON.write_text(json.dumps({
            "run_key": "test_day0_alignment",
            "driver_model": "ecmwf_ifs",
            "run_init_ts": INIT_TS,
            "run_available_ts": INIT_TS + 3600,
            "checked_at": CHECKED_AT,
            "last_checked_at": CHECKED_AT,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        rc, out = run(FIXTURE)
        ok = rc == 0
        print("[%s] gen_fixture_raw (raw con _time 2026-01-01)" % ("PASS" if ok else "FAIL"))
        failures += 0 if ok else 1

        rc, out = run(BUILD, "--raw-json", str(common.DATA_WORK / "fixture_raw.json"))
        ok = rc == 0
        print("[%s] build da raw del 2026-01-01 con run_init del 2025-12-31" % ("PASS" if ok else "FAIL"))
        failures += 0 if ok else 1
        if not ok:
            return 1

        meta = read_json(common.DATA_STAGING / "metadata.json")
        points = read_json(common.DATA_STAGING / "meteorisk-points.json")

        # 1. day0 derivato dai DATI (giorno fetch), NON dall'init run
        ok = meta.get("day0") == EXPECT_DAY0
        print("[%s] metadata.day0 == %s (data, non %s da init)" % (
            "PASS" if ok else "FAIL", EXPECT_DAY0, INIT_DAY))
        print("      got metadata.day0=%r  run_init_ts->giorno=%s" % (
            meta.get("day0"), INIT_DAY))
        failures += 0 if ok else 1

        # 2. time_base = primo timestamp orario
        ok = meta.get("time_base") == EXPECT_BASE
        print("[%s] metadata.time_base == %s" % ("PASS" if ok else "FAIL", EXPECT_BASE))
        print("      got time_base=%r" % meta.get("time_base"))
        failures += 0 if ok else 1

        # 3. coerenza metadata <-> points
        ok = points.get("day0") == meta.get("day0")
        print("[%s] points.day0 == metadata.day0 == %s" % ("PASS" if ok else "FAIL", EXPECT_DAY0))
        failures += 0 if ok else 1

        # 4. validate PASS (nuova invariante day0 == time_base[:10])
        rc, out = run(VA, "--staging")
        ok = rc == 0 and "PASS" in out
        print("[%s] validate --staging PASS (day0==time_base[:10])" % ("PASS" if ok else "FAIL"))
        failures += 0 if ok else 1

        # 5. invariant negativa: day0 sfalsato (= vecchia logica run-based) MAI valido
        if common.DATA_STAGING.exists():
            tamper = common.DATA_WORK / "_t_day0_misaligned"
            if tamper.exists():
                shutil.rmtree(tamper)
            shutil.copytree(common.DATA_STAGING, tamper)
            tmeta = read_json(tamper / "metadata.json")
            tmeta["day0"] = INIT_DAY  # il vecchio (sbagliato) day0 da init run
            (tamper / "metadata.json").write_text(json.dumps(tmeta, indent=2), encoding="utf-8")
            rc, out = run(VA, "--dir", str(tamper))
            ok = rc == 1 and "FAIL" in out and "metadata.day0 == time_base[:10]" in out
            print("[%s] validate FAIL su day0 sfalsato %s (dataset <> pubblicabile)" % (
                "PASS" if ok else "FAIL", INIT_DAY))
            failures += 0 if ok else 1
            shutil.rmtree(tamper)

        # 6. invariant negativa: solo points.day0 sfalsato MAI valido
        if common.DATA_STAGING.exists():
            tamper = common.DATA_WORK / "_t_points_day0_misaligned"
            if tamper.exists():
                shutil.rmtree(tamper)
            shutil.copytree(common.DATA_STAGING, tamper)
            tpoints = read_json(tamper / "meteorisk-points.json")
            tpoints["day0"] = INIT_DAY
            (tamper / "meteorisk-points.json").write_text(json.dumps(tpoints, separators=(",", ":")),
                                                          encoding="utf-8")
            rc, out = run(VA, "--dir", str(tamper))
            ok = rc == 1 and "points.day0 == metadata.day0" in out
            print("[%s] validate FAIL su points.day0 sfalsato %s" % ("PASS" if ok else "FAIL", INIT_DAY))
            failures += 0 if ok else 1
            shutil.rmtree(tamper)

        # 7. SEMANTICA: il day0 effettivo deve coincidere col giorno del fetch metadata
        ok = meta.get("run_info", {}).get("fetched_at", "").startswith(EXPECT_DAY0)
        print("[%s] run_info.fetched_at == giorno %s (fonte di verita' per il client)" % (
            "PASS" if ok else "FAIL", EXPECT_DAY0))
        print("      got fetched_at=%r" % meta.get("run_info", {}).get("fetched_at"))
        failures += 0 if ok else 1
    finally:
        # ripristina lo stato salvato (non sporcare gli stati del repo)
        if saved_state is not None:
            STATE_JSON.write_text(json.dumps(saved_state, indent=2, ensure_ascii=False), encoding="utf-8")
        elif STATE_JSON.exists():
            STATE_JSON.unlink()

    print("RESULT: %s (%d errori)" % ("PASS" if failures == 0 else "FAIL", failures))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())