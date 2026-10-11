#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test L3 — PIN delle euristiche di area e scoring provincia in common.py.

Valori pinned: non modificare senza un tuning documentato. Questi numeri sono
ereditati dall'app (port fedele); i test li congelano per rendere OVVIA ogni
variazione numerica. In particolare:
  - _RING_AREA_KM2_PER_DEG2 = 9250.0  (deg^2 -> km^2, NON lat-dipendente)
  - _SCORE_STORM_HOUR_BONUS = 150     (ora con weathercode temporalesco >=95)
  - _SCORE_HAIL_HOUR_BONUS = 300      (ora con grandine 96/99)
Se un tuning futuro cambia uno di questi valori, aggiornare QUI e nel commento
di common.py nella stessa modifica (mai silenziosamente)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import common  # noqa: E402


# --- (a) ring_area: stessa formula (shoelace, deg^2) di prima della refactor ---

def test_ring_area_known_square_deg2():
    # quadrato 2x2 deg: area shoelace = 4.0 deg^2 (formula invariata)
    ring = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
    assert common.ring_area(ring) == 4.0


def test_ring_area_triangle_deg2():
    # triangolo: (0,0),(4,0),(0,3) -> |0+12+0 - 0|/2 = 6.0 deg^2
    ring = [(0.0, 0.0), (4.0, 0.0), (0.0, 3.0), (0.0, 0.0)]
    assert common.ring_area(ring) == 6.0


def test_ring_area_conversion_factor_pinned():
    # il fattore fisso deg^2 -> km^2 applicato dai sample builder
    assert common._RING_AREA_KM2_PER_DEG2 == 9250.0
    # 4 deg^2 * 9250 = 37000 km^2 (valore pinned)
    ring = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (0.0, 0.0)]
    assert common.ring_area(ring) * common._RING_AREA_KM2_PER_DEG2 == 37000.0


# --- (b) score_point_for_province: bonus ora-storm=150, ora-grandine=300 ---

def test_score_bonus_constants_pinned():
    assert common._SCORE_STORM_HOUR_BONUS == 150
    assert common._SCORE_HAIL_HOUR_BONUS == 300


def test_score_point_for_province_known_total():
    # cape max=500; 2 ore temporalesche (wc 95,96); 1 ora grandine (wc 96)
    rec = {"hourly": {"cape": [100, 500, 200], "weathercode": [95, 96, 3]}}
    # 500 + 2*150 + 1*300 = 1100
    assert common.score_point_for_province(rec) == 1100


def test_score_point_for_province_no_bonus_is_max_cape():
    rec = {"hourly": {"cape": [10, 42, 7], "weathercode": [1, 3, 2]}}
    assert common.score_point_for_province(rec) == 42


def test_score_point_for_province_hail_only():
    # wc 99 e' grandine (hail) e anche temporale (>=95): 2*150 + 2*300
    rec = {"hourly": {"cape": [0, 0], "weathercode": [99, 99]}}
    assert common.score_point_for_province(rec) == 0 + 2 * 150 + 2 * 300


if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v"]))
