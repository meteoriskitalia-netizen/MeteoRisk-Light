#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Golden regression: il PORT Python della generazione coordinate (V1/V2) deve
ricavare ESATTAMENTE gli stessi punti che l'app MeteoRisk genererebbe a runtime
(verificato e bloccato contro il riferimento dell'app 1.0.0.2/1.0.0.3).

Valori golden: otteeduti eseguendo le funzioni reali dell'app
(buildProvinceSamplesV1/V2) e congelati qui per coprire regressioni future.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import common


def main():
    regions = common._load_regions()
    geojson = common._load_geojson()
    sigla_map = {r["sigla"]: r["idx"] for r in regions}
    v1 = common.build_province_samples_v1(geojson, regions, sigla_map)
    v2 = common.build_province_samples_v2(geojson, regions, sigla_map, len(v1)) if v1 else []
    fail = []

    def check(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        print("  [%s] %s%s" % (status, name, (" " + detail) if detail else ""))
        if not cond:
            fail.append(name)

    print("GOLDEN SAMPLE PORT")
    check("v1 length == 265", len(v1) == 265, "got %d" % len(v1))
    check("v2 length == 257", len(v2) == 257, "got %d" % len(v2))
    check("v2 covers all 107 provinces", len({p["provinceIdx"] for p in v2}) == 107)
    check("v2 coordIdx==0 (capoluoghi) == 107", sum(1 for p in v2 if p["coordIdx"] == 0) == 107)
    # campione congelati (primo punto V2, un punto H, l'ultimo)
    samples = {
        "v2[0]": (v2[0]["provinceIdx"], v2[0]["sigla"], round(v2[0]["lat"], 6), round(v2[0]["lon"], 6)),
        "v2 last": (v2[-1]["provinceIdx"], v2[-1]["sigla"], round(v2[-1]["lat"], 6), round(v2[-1]["lon"], 6)),
    }
    golden = json.load(
        open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "golden_samples.json"), encoding="utf-8")
    )
    for k, v in golden.items():
        check(k, samples.get(k) == tuple(v), "got %s expected %s" % (samples.get(k), v))
    # integrità: ogni punto dentro la propria provincia (ray casting) oppure capoluogo
    not_inside = 0
    for p in v2:
        if p["coordIdx"] == 0:
            continue
        feat = None
        for f in geojson["features"]:
            if f["properties"].get("SIGLA") == p["sigla"]:
                feat = f
                break
        inside = any(common.point_in_ring(p, r) for r in common.rings_of(feat) if len(r) >= 4)
        if not inside:
            not_inside += 1
    check("non-capoluoghi dentro poligono provincia (allowed=0)", not_inside == 0, "failures=%d" % not_inside)

    if fail:
        print("RESULT: FAIL (%d)" % len(fail))
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())