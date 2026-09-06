#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit test 1.0.0.5/1.0.0.6 + 1.0.0.8 hardening: request planner, dedup,
pre-flight guardrails, usage state (osservabilita' separata dal blocco).
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
        flat = {tuple(c[:2]) for c in coords}
        self.assertIn((40.0, 8.0), flat)
        self.assertIn((40.0002, 8.0002), flat)
        idxs0 = next(c[2] for c in coords if c[0] == 40.0)
        self.assertIn(0, idxs0)

    def test_plan_counts(self):
        pts = make_points()
        sys.path.insert(0, str(ROOT))
        import request_planner  # noqa: E402
        plan = request_planner.build_plan(pts, batch_size=4)
        self.assertEqual(plan["points"], 12)
        self.assertEqual(plan["unique_coordinates"], 10)
        self.assertEqual(plan["duplicate_observations"], 2)
        self.assertGreaterEqual(plan["naive_requests"], plan["optimized_requests"])
        self.assertEqual(len(plan["batches"]), 3)
        self.assertEqual(plan["optimized_requests"], 6)
        self.assertEqual(plan["requests_saved"], 4)
        self.assertAlmostEqual(plan["efficiency_gain_pct"], 40.0, places=2)
        self.assertEqual(plan["estimated_locations_total"], 10)


class GuardrailsTest(unittest.TestCase):
    def setUp(self):
        self._dl = getattr(common, "API_DAILY_LIMIT", 10000)
        self._tmp = tempfile.TemporaryDirectory()
        common.API_USAGE_JSON = Path(self._tmp.name) / "api_usage.json"
        common.API_USAGE_JSON.write_text(json.dumps({
            "api_usage_guardrails": {
                "enabled": True,
                "daily_safety_ceiling": 100,
                "warn_threshold_fraction": 0.8,
                "hard_stop_enabled": True,
            },
            "days": {},
            "last_update": None,
        }, indent=2), encoding="utf-8")

    def tearDown(self):
        common.API_DAILY_LIMIT = self._dl
        common.API_USAGE_JSON = common.DATA_STATE / "api_usage.json"
        self._tmp.cleanup()

    def test_defaults_fill_missing_keys(self):
        common.API_USAGE_JSON.write_text(json.dumps({"days": {}}), encoding="utf-8")
        g = common.guardrails()
        self.assertIn("daily_safety_ceiling", g)
        self.assertIn("warn_threshold_fraction", g)
        self.assertIn("hard_stop_enabled", g)
        self.assertTrue(g["enabled"])

    def test_migration_old_schema(self):
        # stato 1.0.0.7: daily_limit -> migrato a api_usage_guardrails.daily_safety_ceiling
        common.API_USAGE_JSON.write_text(json.dumps({"daily_limit": 77, "days": {}}), encoding="utf-8")
        self.assertEqual(common.effective_budget(), 77)
        st = json.loads(common.API_USAGE_JSON.read_text(encoding="utf-8"))
        self.assertNotIn("daily_limit", st)
        self.assertEqual(st["api_usage_guardrails"]["daily_safety_ceiling"], 77)

    def test_effective_budget(self):
        self.assertEqual(common.effective_budget(), 100)

    def test_usage_roundtrip_with_telemetry(self):
        common.record_api_usage(requests=4, failed=1, successful=3,
                                batches=4, locations=120, bytes_=2048,
                                checks=2, canary=1, forecast=3, retries=1)
        u = common.usage_today()
        self.assertEqual(u["requests"], 4)
        self.assertEqual(u["successful"], 3)
        self.assertEqual(u["failed"], 1)
        self.assertEqual(u["checks"], 2)
        self.assertEqual(u["canary_requests"], 1)
        self.assertEqual(u["forecast_requests"], 3)
        self.assertEqual(u["retries"], 1)
        self.assertEqual(u["batches"], 4)
        self.assertEqual(u["locations"], 120)
        self.assertEqual(u["bytes"], 2048)
        self.assertEqual(common.available_today(), 96)
        st = json.loads(common.API_USAGE_JSON.read_text(encoding="utf-8"))
        self.assertIn(common.usage_day_key(), st["days"])

    def test_usage_daily_roll(self):
        day = "2000-01-01"
        common.record_api_usage(requests=3, day=day)
        self.assertEqual(common.usage_today(day)["requests"], 3)
        self.assertEqual(common.usage_today()["requests"], 0)

    def test_preflight_ok_under_ceiling(self):
        r = common.guard_planned_requests(10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["planned"], 10)
        self.assertEqual(r["available"], 100)
        self.assertEqual(r["worst"], 30)
        self.assertFalse(r["warned"])

    def test_preflight_blocked_only_above_ceiling(self):
        r = common.guard_planned_requests(101)
        self.assertFalse(r["ok"])
        self.assertIsNotNone(r["reason"])
        self.assertIn("HARD SAFETY CEILING", r["reason"])

    def test_preflight_boundary(self):
        self.assertTrue(common.guard_planned_requests(100)["ok"])
        self.assertFalse(common.guard_planned_requests(101)["ok"])

    def test_no_preventive_rationing(self):
        # consumi sotto il tetto MAI bloccati, anche oltre soglia warn (osservabilita' distinta)
        common.record_api_usage(requests=98)
        r = common.guard_planned_requests(1)
        self.assertTrue(r["ok"])
        self.assertTrue(r["warned"])
        # sotto soglia warn: nessun warning
        common.API_USAGE_JSON.write_text(json.dumps({
            "api_usage_guardrails": {
                "enabled": True, "daily_safety_ceiling": 100,
                "warn_threshold_fraction": 0.8, "hard_stop_enabled": True,
            }, "days": {}, "last_update": None}), encoding="utf-8")
        common.record_api_usage(requests=2)
        r = common.guard_planned_requests(1)
        self.assertTrue(r["ok"])
        self.assertFalse(r["warned"])

    def test_hard_stop_disabled_only_observes(self):
        common.API_USAGE_JSON.write_text(json.dumps({
            "api_usage_guardrails": {
                "enabled": True, "daily_safety_ceiling": 100,
                "warn_threshold_fraction": 0.8, "hard_stop_enabled": False,
            }, "days": {}, "last_update": None}), encoding="utf-8")
        r = common.guard_planned_requests(150)
        self.assertTrue(r["ok"])

    def test_ensure_api_budget_alias(self):
        r = common.ensure_api_budget(10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["planned"], 10)

    def test_retry_constants(self):
        self.assertGreaterEqual(common.RETRY_LIMIT, 1)
        self.assertGreater(common.RETRY_BACKOFF_BASE_S, 0)
        self.assertGreaterEqual(common.BATCH_MAX_LOCATIONS, 1)
        self.assertGreater(common.API_MIN_REQUEST_INTERVAL_S, 0)
        pts = json.loads((common.DATA_WORK / "real_points.json").read_text(encoding="utf-8")) \
            if (common.DATA_WORK / "real_points.json").exists() else make_points()
        coords = common.unique_coordinates(pts)
        import math
        batches = -(-len(coords) // common.BATCH_MAX_LOCATIONS)  # ceil
        self.assertTrue(batches >= math.ceil(len(coords) / common.BATCH_MAX_LOCATIONS))


if __name__ == "__main__":
    unittest.main(verbosity=2)