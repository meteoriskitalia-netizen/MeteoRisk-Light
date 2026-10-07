# -*- coding: utf-8 -*-
"""Test A2 - aggregate.py (A1): SSI v2 = combinazione pesata dei layer.

Tutti i valori attesi sono quelli calcolati dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import pytest

import aggregate as agg

# Nota: 'poc' nel primo errore e' solo una chiave sconosciuta a caso
# (coincidente col refuso 'poc' documentato in main_integr.patch.md).
UNKNOWN = dict(agg.DEFAULT_WEIGHTS, poc=0.1)


def test_component_keys_order_and_weights_sum():
    assert agg.COMPONENT_KEYS == ("base", "hook", "structure", "env", "ot",
                                  "lightning")
    assert round(sum(agg.DEFAULT_WEIGHTS.values()), 6) == 1.0


def test_aggregate_all_present():
    out = agg.aggregate_ssi_v2(80, 60, 50, 40, 100, 100)
    assert out["ssi_v2"] == 72
    assert out["weights"] == agg.DEFAULT_WEIGHTS
    assert out["weights_sum"] == 1.0
    assert out["missing"] == []
    assert out["partial"] is False
    assert out["base"] == 80.0
    assert out["hook"] == 60.0
    assert out["structure"] == 50.0
    assert out["env"] == 40.0
    assert out["ot"] == 100.0
    assert out["lightning"] == 100.0


def test_aggregate_base_clamped_high_and_low():
    assert agg.aggregate_ssi_v2(150, 60, 50, 40, 100, 100)["ssi_v2"] == 84
    assert agg.aggregate_ssi_v2(-10, 60, 50, 40, 100, 100)["ssi_v2"] == 24


def test_aggregate_missing_components_are_partial():
    out = agg.aggregate_ssi_v2(80, None, 50, 40, None, 100)
    assert out["ssi_v2"] == 59
    assert out["missing"] == ["hook", "ot"]
    assert out["partial"] is True
    # None conta 0 nello score (clamp documentato), MAI un valore inventato
    assert out["hook"] == 0.0
    assert out["ot"] == 0.0


def test_aggregate_weights_explicit_override_same_result():
    out = agg.aggregate_ssi_v2(80, 60, 50, 40, 100, 100,
                               weights=dict(agg.DEFAULT_WEIGHTS))
    assert out["ssi_v2"] == 72
    assert out["weights_sum"] == 1.0


def test_invalid_weights_raise_no_fabricated_total():
    with pytest.raises(ValueError, match=r"unknown_weight_keys:\['poc'\]"):
        agg.aggregate_ssi_v2(50, 50, 50, 50, 50, 50, weights=UNKNOWN)
    with pytest.raises(ValueError, match="weights_sum_gt_1:1.1"):
        agg.aggregate_ssi_v2(50, 50, 50, 50, 50, 50,
                             weights=dict(agg.DEFAULT_WEIGHTS, base=0.7))
    with pytest.raises(ValueError,
                       match=r"missing_weight_keys:\['env', 'hook', "
                             r"'lightning', 'ot', 'structure'\]"):
        agg.aggregate_ssi_v2(50, 50, 50, 50, 50, 50,
                             weights={"base": 1.0})
