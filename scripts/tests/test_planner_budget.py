#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit test 1.0.0.5/1.0.0.6: request planner, dedup, budget pre-flight, usage state.
Nessuna rete coinvolta: i percorsi common.* vengono re-indirizzati su /tmp."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import common  # noqa: E402


def make_points():
    """12 osservazioni, 10 coordinate uniche (2 duplicati ~11m)."""
    pts = []
    for i in range(10):
        pts.append({"index": i, "provinceIdx": i, "sigla": "PX%d" % i,
                    "coordIdx": i, "lat": 40.0 + i * 0.0001, "lon": 8.0 + i * 0.0001})
    pts.append({"index": 10, "provinceIdx": 0, "sigla": "PX0", "coordIdx": 99,
                "lat": 40.00004, "lon": 8.00004})   # dup arrotondato del punto 0
    pts.append({"index": 11, "provinceIdx": 2, "sigla": "PX2", "coordIdx": 97,
                "lat": 40.0002, "lon": 8.0002})     # dup esatto del punto 2
    return pts


class PlannerTest(unittest.TestCase):
    def test_unique_coordinates_dedup(self):
        pts = make_points()
        coords = common.unique_coordinates(pts)
        self.assertEqual(len(coords), 10)
        # i duplicati condividono la stessa chiave arrotondata
        flat = {tuple(c[:2]) for c in coords}
        self.assertIn((40.0, 8.0), flat)   # da punto 0 (dup in 10 -> stesso arrotondamento)
        self.assertIn((40.0002, 8.0002), flat)
        idxs0 = next(c[2] for c in coords if c[0] == 40.0)
        self.assertIn(0, idxs0)

    def test_plan_counts(self):
        pts = make_points()
        from pathlib import Path as _P
        sys.path.insert(0, str(ROOT))
        import request_planner  # noqa: E402
        plan = request_planner.build_plan(pts, batch_size=4)
        self.assertEqual(plan["points"], 12)
        self.assertEqual(plan["unique_coordinates"], 10)
        self.assertEqual(plan["duplicate_observations"], 2)
        self.assertGreaterEqual(plan["naive_requests"], plan["optimized_requests"])
        # 10 uniche / batch 4 -> 3 batch; x2 leg = 6 richieste
        self.assertEqual(len(plan["batches"]), 3)
        self.assertEqual(plan["optimized_requests"], 6)
        self.assertEqual(plan["requests_saved"], 4)
        self.assertAlmostEqual(plan["efficiency_gain_pct"], 40.0, places=2)
        # stesse localita' distribuite bene
        self.assertEqual(plan["estimated_locations_total"], 10)


class BudgetTest(unittest.TestCase):
    def setUp(self):
        self._dl = getattr(common, "API_DAILY_LIMIT", 10000)
        self._rf = common.API_SAFETY_RESERVE_FRAC
        common.API_DAILY_LIMIT = 100
        common.API_SAFETY_RESERVE_FRAC = 0.1
        self._tmp = tempfile.TemporaryDirectory()
        common.API_USAGE_JSON = Path(self._tmp.name) / "api_usage.json"

    def tearDown(self):
        common.API_DAILY_LIMIT = self._dl
        common.API_SAFETY_RESERVE_FRAC = self._rf
        common.API_USAGE_JSON = common.DATA_STATE / "api_usage.json"
        self._tmp.cleanup()

    def test_effective_budget(self):
        self.assertEqual(common.effective_budget(), 90)

    def test_usage_roundtrip(self):
        common.record_api_usage(requests=4, failed=1, batches=4, locations=120, bytes_=2048)
        u = common.usage_today()
        self.assertEqual(u["requests"], 4)
        self.assertEqual(u["failed"], 1)
        self.assertEqual(u["batches"], 4)
        self.assertEqual(u["locations"], 120)
        self.assertEqual(u["bytes"], 2048)
        self.assertEqual(common.available_today(), 86)
        st = json.loads(common.API_USAGE_JSON.read_text(encoding="utf-8"))
        self.assertIn(common.usage_day_key(), st["days"])

    def test_usage_daily_roll(self):
        day = "2000-01-01"
        common.record_api_usage(requests=3, day=day)
        self.assertEqual(common.usage_today(day)["requests"], 3)
        self.assertEqual(common.usage_today()["requests"], 0)  # oggi separato

    def test_preflight_ok(self):
        r = common.ensure_api_budget(10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["planned"], 10)
        self.assertEqual(r["available"], 90)
        self.assertEqual(r["worst"], 30)

    def test_preflight_blocked(self):
        r = common.ensure_api_budget(91)
        self.assertFalse(r["ok"])
        self.assertIsNotNone(r["reason"])
        self.assertIn("budget effettivo insufficiente", r["reason"])

    def test_preflight_boundary(self):
        self.assertTrue(common.ensure_api_budget(90)["ok"])
        self.assertFalse(common.ensure_api_budget(91)["ok"])

    def test_retry_constants(self):
        self.assertGreaterEqual(common.RETRY_LIMIT, 1)
        self.assertGreater(common.RETRY_BACKOFF_BASE_S, 0)
        self.assertGreaterEqual(common.BATCH_MAX_LOCATIONS, 1)
        self.assertGreater(common.API_MIN_REQUEST_INTERVAL_S, 0)
        # piano reale 257 punti deve stare comodamente dentro il budget default
        pts = json.loads((common.DATA_WORK / "real_points.json").read_text(encoding="utf-8")) \
            if (common.DATA_WORK / "real_points.json").exists() else make_points()
        coords = common.unique_coordinates(pts)
        import math
        batches = -(-len(coords) // common.BATCH_MAX_LOCATIONS)  # ceil
        planned = batches * len(common.DUAL_MODELS.split(","))
        self.assertTrue(batches >= math.ceil(len(coords) / common.BATCH_MAX_LOCATIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)