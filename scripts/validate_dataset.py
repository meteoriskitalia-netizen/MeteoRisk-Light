#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 8 (validate_dataset.py): VALIDAZIONE del dataset derivato PRIMA della
pubblicazione. In caso di esito negativo NON viene toccato data/latest
(last known good preservato). Vengono controllate proprietà di forma,
contenuto, integrità territoriale, consistenza col port di coordinate e
coerenza della selezione worst-point ricalcolata.

Output: data/_staging/validation.json (copia anche in data/_workdir per l'Action).
Exit: 0 = valido, 1 = invalido.
"""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main():
    parser = argparse.ArgumentParser(description="Validate derived dataset (staging).")
    parser.add_argument("--staging", action="store_true",
                        help="Valida il contenuto di data/_staging (default: data/latest).")
    parser.add_argument("--dir", default=None,
                        help="Valida una directory arbitraria (per test negativi).")
    args = parser.parse_args()
    if args.dir:
        base = Path(args.dir)
    else:
        base = common.DATA_STAGING if args.staging else common.DATA_LATEST

    checks = []
    errors = []
    infos = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        checks.append({"check": name, "status": status, "detail": detail or ""})
        print("  [%s] %s%s" % (status, name, (" · " + detail) if detail else ""))
        if not cond:
            errors.append(name)

    def info(msg):
        infos.append(msg)
        print("    ~ %s" % msg)

    metadata_path = base / "metadata.json"
    points_path = base / "meteorisk-points.json"
    provinces_path = base / "meteorisk-provinces.json"

    # A. Presenza / tipi
    check("metadata.json existe", metadata_path.exists())
    check("meteorisk-points.json existe", points_path.exists())
    check("meteorisk-provinces.json existe", provinces_path.exists())
    if errors:
        return finish(checks, infos, errors, "FILES")

    meta = read_json(metadata_path)
    points = read_json(points_path)
    provs = read_json(provinces_path)

    check("dataset_type derivato", meta.get("dataset_type") == common.APP_DATA_TYPE,
          "got %s" % meta.get("dataset_type"))
    check("application", meta.get("application") == common.APP_NAME)
    check("source_data dichiara Open-Meteo come input",
          any((s.get("name") == "Open-Meteo" for s in (meta.get("source_data") or []))))
    check("attribution presente e senza endorsement", bool(meta.get("attribution")))
    att = meta.get("attribution") or ""
    check("nessun endorsement/affiliazione",
          not any(w in att.lower() for w in ("affiliated", "endorsed", "by open-meteo", "supported by")))
    check("forecast_days=3", meta.get("forecast_days") == common.FORECAST_DAYS)
    check("timezone Europe/Rome", meta.get("timezone") == common.TIMEZONE)
    check("models_covered = best_match,ecmwf_ifs",
          sorted(meta.get("models_covered") or []) == ["best_match", "ecmwf_ifs"])

    live = meta.get("status") == "live"
    if not live:
        check("dataset vuoto -> struct valida (placeholder)", points.get("status") == "empty"
              and provs.get("status") == "empty")
        check("zero punti/zero province placeholder", points.get("point_count", len(points.get("points", []))) == 0)
        finish(checks, infos, errors, "EMPTY_OK")
        return 0

    # B. Punti: forma e integrità
    plist = points.get("points", [])
    check("point_count == metadati", meta.get("point_count") == len(plist),
          "meta=%s len=%s" % (meta.get("point_count"), len(plist)))
    ids = [p.get("id") for p in plist]
    check("ids univoci e contigui 0..N-1", sorted(ids) == list(range(len(plist))))
    s_prov = set()
    for e in plist:
        check("field lat/lon numerici", isinstance(e.get("lat"), (int, float)) and isinstance(e.get("lon"), (int, float)))
        if not isinstance(e.get("lat"), (int, float)) or not isinstance(e.get("lon"), (int, float)):
            continue
        check("lat in [30,70] lon in [-40,40]", 30 <= e["lat"] <= 70 and -40 <= e["lon"] <= 40,
              "(%s,%s)" % (e["lat"], e["lon"]))
        if e.get("coordIdx") != 0:
            pass  # integrità territoriale verificata in aggregato sotto
        s_prov.add(e.get("provinceIdx"))
        for model_key in ("best_match", "ecmwf_ifs"):
            mod = (e.get("models") or {}).get(model_key)
            check("modello %s presente" % model_key, bool(mod and mod.get("hourly") and mod.get("daily")))
            if not mod:
                continue
            hv = set(mod["hourly"].keys())
            dv = set(mod["daily"].keys())
            check("variabili hourly == param set", hv == set(common.HOURLY_FIELDS),
                  "mancanti=%s" % sorted(set(common.HOURLY_FIELDS) - hv)[:5])
            check("variabili daily == param set", dv == set(common.DAILY_FIELDS))
            lens = {k: len(v) for k, v in mod["hourly"].items()}
            check("hourly array == 72 (3 giorni)", setId(lens.values()) == {72})
            dlens = {k: len(v) for k, v in mod["daily"].items()}
            check("daily array == 3", setId(dlens.values()) == {3})
            check("hourly numerici", all(isinstance(x, (int, float)) for k, v in mod["hourly"].items()
                                          for x in (v or []) if x is not None))
    check("tutte le 107 province coperte", s_prov == set(range(107)),
          "mancanti=%s" % sorted(set(range(107)) - s_prov)[:10])
    check("ogni provincia con >=1 punto", len(s_prov) == 107)

    # integrità territoriale (non-capoluoghi dentro il poligono della propria provincia)
    geojson = json.load(open(common.GEOJSON_PATH, encoding="utf-8"))
    poly_by_sigla = {}
    for f in geojson["features"]:
        sigla = f["properties"].get("SIGLA")
        rings = common.rings_of(f)
        poly_by_sigla[sigla] = [r for r in rings if len(r) >= 4]
    outside = 0
    for e in plist:
        if e.get("coordIdx") == 0:
            continue
        rings = poly_by_sigla.get(e.get("sigla")) or []
        if not any(common.point_in_ring(e, r) for r in rings):
            outside += 1
    check("punti non-capoluogo dentro il poligono (0 tollerato)", outside == 0, "fuori=%d" % outside)

    # C. Province: forma, nomi, selezione worst-point ricalcolata
    prov_list = provs.get("provinces", [])
    check("province_count == 107", provs.get("province_count") == len(prov_list) == 107)
    check("indici 0..106", [p["idx"] for p in prov_list] == list(range(107)))
    regions = common._load_regions()
    mism = 0
    for p in prov_list:
        r = regions[p["idx"]]
        if p.get("sigla") != r["sigla"] or p.get("prov") != r["prov"]:
            mism += 1
    check("sigla/prov coerenti con regions_data", mism == 0, "mismatch=%d" % mism)
    # ricalcolo worst-point da best_match (stesso tie-break: primo max per id crescente)
    by_prov = {}
    for e in plist:
        by_prov.setdefault(e["provinceIdx"], []).append(e)
    sel_mism = 0
    prov_missing = 0
    for p in prov_list:
        sel = p.get("selected_point")
        if not sel:
            prov_missing += 1
            continue
        cands = by_prov.get(p["idx"], [])
        best = None
        bs = -1.0
        for e in cands:
            rec = {"hourly": e["models"]["best_match"]["hourly"], "daily": e["models"]["best_match"]["daily"]}
            sc = common.score_point_for_province(rec)
            if sc > bs:
                bs = sc
                best = e
        if best is None or best["id"] != sel.get("id"):
            sel_mism += 1
    check("worst-point presente per ogni provincia", prov_missing == 0, "mancanti=%d" % prov_missing)
    check("selezione worst-point ricalcolata == dataset (0 mismatch)", sel_mism == 0, "mismatch=%d" % sel_mism)

    # D. Hash e dimensioni
    for rel in ("meteorisk-points.json", "meteorisk-provinces.json"):
        p = base / rel
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        info("sha256 %s = %s (%d byte)" % (rel, sha, p.stat().st_size))

    return finish(checks, infos, errors, "OK" if not errors else "FAIL")


def setId(vals):
    s = {int(v) for v in vals if v is not None}
    return s if s else {72}


def finish(checks, infos, errors, outcome):
    report = {
        "valid": not errors,
        "outcome": outcome,
        "checked_at": common.now_iso(),
        "dataset": {"status": "live"},
        "checks": checks,
        "infos": infos,
        "errors": errors,
    }
    common.DATA_STAGING.mkdir(parents=True, exist_ok=True)
    common.DATA_WORK.mkdir(parents=True, exist_ok=True)
    for p in (common.DATA_STAGING / "validation.json", common.DATA_WORK / "validation.json"):
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False)
    print("RESULT: %s (%d checks, %d errori)" % ("PASS" if not errors else "FAIL", len(checks), len(errors)))
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())