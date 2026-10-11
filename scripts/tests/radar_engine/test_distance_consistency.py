#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Consistenza delle due convenzioni DISTANZA del progetto (M3).

- `radar_engine.tracking.haversine_km` -> geodetica (R=6371.0088 km), usata dal
  radar engine (tracking/kinematics/storm_tracking/scoring).
- `common.km_between` -> equirettangolare (111.32 km/grado), usata per
  griglia/sampling (port fedele dell'app).

Le due formule NON sono identiche ma devono concordare entro lo 0.5% relativo
a ~41°N (latitudine Italia) nel range operativo 10-200 km. Questo test fissa il
contratto: nessuna delle due viene modificata nei valori, solo documentata.
"""

import math

from radar_engine.tracking import haversine_km
from common import km_between


_TOL = 0.005  # deviazione relativa massima ammessa (0.5%)


def _rel_diff(kmb, hkm):
    return abs(kmb - hkm) / hkm


def test_north_south_10_to_200km_consistent():
    for lat0 in (38.0, 41.0, 44.0, 46.0):
        for dist in (10.0, 25.0, 50.0, 100.0, 200.0):
            dlat = dist / 111.32
            a = {"lat": lat0, "lon": 12.0}
            b = {"lat": lat0 + dlat, "lon": 12.0}
            kmb = km_between(a, b)
            hkm = haversine_km((12.0, lat0), (12.0, lat0 + dlat))
            assert _rel_diff(kmb, hkm) <= _TOL


def test_east_west_10_to_200km_consistent():
    for lat0 in (38.0, 41.0, 44.0, 46.0):
        for dist in (10.0, 25.0, 50.0, 100.0, 200.0):
            dlon = dist / (111.32 * math.cos(math.radians(lat0)))
            a = {"lat": lat0, "lon": 12.0}
            b = {"lat": lat0, "lon": 12.0 + dlon}
            kmb = km_between(a, b)
            hkm = haversine_km((12.0, lat0), (12.0 + dlon, lat0))
            assert _rel_diff(kmb, hkm) <= _TOL


def test_haversine_symmetric_and_zero():
    assert haversine_km((12.0, 41.0), (12.0, 41.0)) == 0.0
    d1 = haversine_km((12.0, 41.0), (12.1, 41.0))
    d2 = haversine_km((12.1, 41.0), (12.0, 41.0))
    assert d1 == d2
