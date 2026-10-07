# -*- coding: utf-8 -*-
"""Test A2 - aggregate.py (A1): SSI v2 = combinazione pesata dei layer.

I valori dei test A2 provenienti dallo scratch A2 eseguito (_scratch_a2.py,
radarvenv Python 3.13) restano invariati. I test B2 (rinormalizzazione pesi,
componenti None) hanno i valori verificati eseguendo aggregate.py reale con
scratch_b2_values.py (radarvenv Python 3.13): nessun valore dedotto a mano.
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
    # B2: pesi RINORMALIZZATI sui presenti (0.60+0.10+0.08+0.03 = 0.81):
    # (59.259 + 6.173 + 3.951 + 3.704) / 0.81 = 73.086 -> 73
    out = agg.aggregate_ssi_v2(80, None, 50, 40, None, 100)
    assert out["ssi_v2"] == 73
    assert out["missing"] == ["hook", "ot"]
    assert out["partial"] is True
    # None resta None in output: MAI un valore inventato 0.0
    assert out["hook"] is None
    assert out["ot"] is None
    # i pesi usati coprono esattamente i componenti presenti e sommano 1.00
    assert set(out["weights_effective"]) == set(out["present"])
    assert round(out["weights_effective_sum"], 6) == 1.0
    assert round(out["weights_effective"]["base"], 6) == 0.740741


def test_effective_weights_renormalization():
    eff = agg.effective_weights(agg.DEFAULT_WEIGHTS, ["base", "hook"])
    assert set(eff) == {"base", "hook"}
    assert round(eff["base"], 6) == 0.8
    assert round(eff["hook"], 6) == 0.2
    assert round(sum(eff.values()), 6) == 1.0
    eff_all = agg.effective_weights(agg.DEFAULT_WEIGHTS, agg.COMPONENT_KEYS)
    assert {k: round(v, 6) for k, v in eff_all.items()} == agg.DEFAULT_WEIGHTS
    with pytest.raises(ValueError, match="unknown_weight_keys"):
        agg.effective_weights(agg.DEFAULT_WEIGHTS, ["base", "poc"])


def test_aggregate_base_missing_redistributes_heavy_weight():
    # senza base (peso 0.60) il resto si ridistribuisce su 0.40:
    # 0.375*60 + 0.25*50 + 0.2*40 + 0.1*100 + 0.075*90 = 59.75 -> 60
    out = agg.aggregate_ssi_v2(None, 60, 50, 40, 100, 90)
    assert out["missing"] == ["base"]
    assert out["base"] is None
    assert out["ssi_v2"] == 60
    assert round(out["weights_effective_sum"], 6) == 1.0
    assert round(out["weights_effective"]["hook"], 6) == 0.375


def test_aggregate_only_base_present_uses_full_weight():
    out = agg.aggregate_ssi_v2(80, None, None, None, None, None)
    assert out["ssi_v2"] == 80
    assert out["missing"] == ["hook", "structure", "env", "ot", "lightning"]
    assert out["weights_effective"] == {"base": 1.0}


def test_aggregate_all_components_none_gives_no_score():
    out = agg.aggregate_ssi_v2(None, None, None, None, None, None)
    assert out["ssi_v2"] is None
    assert out["present"] == []
    assert out["missing"] == list(agg.COMPONENT_KEYS)
    assert out["partial"] is True
    assert out["weights_effective"] == {}
    assert out["weights_effective_sum"] == 0


def test_aggregate_all_weights_zero_gives_no_score():
    # presenti ma tutti a peso 0 -> nessuna media pesata possibile
    zero = {k: 0.0 for k in agg.COMPONENT_KEYS}
    out = agg.aggregate_ssi_v2(80, 60, 50, 40, 100, 100, weights=zero)
    assert out["weights_sum"] == 0.0
    assert out["ssi_v2"] is None
    assert out["weights_effective"] == {}


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
