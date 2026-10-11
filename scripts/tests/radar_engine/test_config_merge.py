#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test merge configurazione RICORSIVO (R3) — config.merge_config.

La precedente versione 1-livello perdeva i sotto-blocchi non nominati durante
l'override. Il merge ricorsivo fonde i dict a ogni profondità e sostituisce
scalari/liste, senza mutare CONFIG (fonte immergenza di default immutabile).
"""

from radar_engine.config import CONFIG, merge_config


def test_deep_override_preserves_sibling_blocks():
    ov = {"storm": {"tracking": {"max_coast_frames": 5}}}
    m = merge_config(CONFIG, ov)
    assert m["storm"]["tracking"]["max_coast_frames"] == 5
    assert m["storm"]["aggregation"]["method"] == CONFIG["storm"]["aggregation"]["method"]
    assert m["storm"]["ambiguity"] == CONFIG["storm"]["ambiguity"]


def test_scalars_and_lists_are_replaced_not_merged():
    m = merge_config(CONFIG, {
        "detect": {"dbz_threshold": 25.0},
        "scoring": {"intensity_ref_dbz": [18.0, 58.0]},
    })
    assert m["detect"]["dbz_threshold"] == 25.0
    assert m["detect"]["min_area_km2"] == CONFIG["detect"]["min_area_km2"]
    assert m["scoring"]["intensity_ref_dbz"] == [18.0, 58.0]


def test_nested_phase2_merge_keeps_defaults():
    ov = {"phase2": {"hook": {"footprint_radius_km": 30.0}}}
    m = merge_config(CONFIG, ov)
    assert m["phase2"]["hook"]["footprint_radius_km"] == 30.0
    assert m["phase2"]["hook"]["dbz_threshold"] == CONFIG["phase2"]["hook"]["dbz_threshold"]


def test_merge_does_not_mutate_base_config():
    before = CONFIG["storm"]["tracking"]["max_coast_frames"]
    merge_config(CONFIG, {"storm": {"tracking": {"max_coast_frames": 9}}})
    assert CONFIG["storm"]["tracking"]["max_coast_frames"] == before


def test_empty_or_none_overrides_are_noop():
    assert merge_config(CONFIG, None)["detect"] == CONFIG["detect"]
    assert merge_config(CONFIG, {})["tracking"] == CONFIG["tracking"]