#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Genera le coordinate REALI dei punti di campionamento con il PORT fedele della
logica dell'app (buildProvinceSamplesV1/V2 + tavole orografiche + geometria).
Scrive data/_workdir/real_points.json (mai pubblicato: è un artefatto di lavoro).

Il port è stato bloccato contro il riferimento vero dell'app (v1=265, v2=257,
ordine e coordinate identici — vedi scripts/tests/test_sample_port.py).
"""

import json
import sys

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common


def main():
    points = common.generate_real_points()
    if not points:
        print("[generate_points] Nessun punto generato.")
        return 2
    common.DATA_WORK.mkdir(parents=True, exist_ok=True)
    out = common.DATA_WORK / "real_points.json"
    out.write_text(json.dumps(points, separators=(",", ":")), encoding="utf-8")
    print("[generate_points] %d coordinate reali (V2, tutte le 107 province) → data/_workdir/real_points.json"
          % len(points))
    return 0


if __name__ == "__main__":
    sys.exit(main())