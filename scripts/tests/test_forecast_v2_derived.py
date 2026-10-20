# -*- coding: utf-8 -*-
"""Derivate del motore V2 (1.2.4.11): dew_point_2m, wind_speed_*, precipitation_cum.

Il motore pubblica 6 nuove variabili, 4 delle quali DERIVATE:

  - ``relative_humidity_700hPa``  DIRETTA (esposta dalla sorgente);
  - ``dew_point_2m``              DERIVATA per-frame (Magnus da T2m + RH2m);
  - ``wind_speed_10m``            DERIVATA per-frame (sqrt(u^2+v^2) a 10 m);
  - ``wind_direction_10m``        DERIVATA per-frame (direzione meteo, u/v 10 m);
  - ``wind_speed_850hPa``         DERIVATA per-frame (INPUT u/v 850 hPa);
  - ``wind_direction_850hPa``     DERIVATA per-frame (direzione meteo, u/v 850);
  - ``wind_speed_300hPa``         DERIVATA per-frame (INPUT u/v 300 hPa);
  - ``wind_direction_300hPa``     DERIVATA per-frame (direzione meteo, u/v 300);
  - ``precipitation_cum``         DERIVATA STATEFUL (somma di ``precipitation``
                                  lungo la serie ORDINATA dei valid_times).

Le componenti ``wind_[uv]_component_10m``/``_850hPa``/``_300hPa`` sono INPUT:
lette ma NON pubblicate (``V2_INPUT_VARS``), e entrano nel set letto dai reader
(``V2_READ_VARS`` = pubblicate + input). Dalla 1.2.4.11 le u/v a 10 m non sono
piu' pubblicate: il vento esce come COPPIA velocita'+direzione (vedi
``test_forecast_v2_wind_pair.py``).

Il modulo del motore viene caricato via ``importlib`` per percorso
(``scripts/forecast_v2`` non e' un package), stesso pattern di
``test_forecast_v2_scale_step.py``. Il motore importa ``omfiles`` a top-level:
se ``numpy``/``omfiles`` mancano i test sono SKIPPED.
"""
import importlib.util
import os

import pytest

np = pytest.importorskip("numpy")
omfiles = pytest.importorskip("omfiles")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ENGINE_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2",
                           "forecast_v2_engine.py")


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "forecast_v2_engine_derived_ut", ENGINE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENGINE = _load_engine()

DERIVED_PER_FRAME = ("dew_point_2m",
                     "wind_speed_10m", "wind_direction_10m",
                     "wind_speed_850hPa", "wind_direction_850hPa",
                     "wind_speed_300hPa", "wind_direction_300hPa")


# ---------------------------------------------------------------------------
# (a) Dew point (Magnus) contro valori CALCOLATI A MANO
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("t,rh,expected", [
    (20.0, 50.0, 9.27),    # formula: 9.2553
    (30.0, 80.0, 26.2),    # formula: 26.176
    (0.0, 100.0, 0.0),     # saturazione a 0 degC -> 0.0
])
def test_dew_point_hand_values(t, rh, expected):
    got = float(ENGINE.dew_point_magnus(t, rh))
    assert got == pytest.approx(expected, abs=0.1), (
        "dew point T=%s RH=%s: %s (atteso ~%s)" % (t, rh, got, expected))


def test_dew_point_matches_hand_formula():
    """Verifica indipendente: riapplica la formula a mano e la confronta."""
    a, b = 17.62, 243.12
    for t, rh in [(20.0, 50.0), (30.0, 80.0), (0.0, 100.0), (12.5, 33.0)]:
        alpha = np.log(rh / 100.0) + a * t / (b + t)
        expected = b * alpha / (a - alpha)
        got = float(ENGINE.dew_point_magnus(t, rh))
        assert got == pytest.approx(expected, abs=1e-3), (t, rh)


# ---------------------------------------------------------------------------
# (b) Wind speed = sqrt(u^2 + v^2); il segno NON conta
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("u,v,expected", [
    (3.0, 4.0, 5.0),
    (-3.0, -4.0, 5.0),     # segni invertiti: stessa velocita'
    (0.0, 0.0, 0.0),
    (1.0, 1.0, 2.0 ** 0.5),
])
def test_wind_speed_from_uv(u, v, expected):
    got = float(ENGINE.wind_speed_from_uv(u, v))
    assert got == pytest.approx(expected, abs=1e-4), (u, v)


# ---------------------------------------------------------------------------
# (c) Cumulata stateful: somma per cella in ORDINE di valid_time
# ---------------------------------------------------------------------------
def test_accumulate_precip_cum_series():
    """Serie [1,2,3] -> [1,3,6]."""
    cum = None
    out = []
    for p in (1.0, 2.0, 3.0):
        cum = ENGINE.accumulate_precip_cum(
            cum, np.array([[p]], dtype=np.float32), (1, 1))
        out.append(float(cum[0, 0]))
    assert out == pytest.approx([1.0, 3.0, 6.0]), out


def test_accumulate_precip_cum_frame0_without_precipitation():
    """Il frame al reference_time (precip None) contribuisce 0 e parte da 0."""
    cum = ENGINE.accumulate_precip_cum(None, None, (1, 2))
    assert cum.shape == (1, 2)
    assert np.all(cum == 0.0)
    cum = ENGINE.accumulate_precip_cum(
        cum, np.array([[2.0, 3.0]], dtype=np.float32), (1, 2))
    assert np.allclose(cum, [[2.0, 3.0]])
    cum = ENGINE.accumulate_precip_cum(
        cum, np.array([[1.0, 1.0]], dtype=np.float32), (1, 2))
    assert np.allclose(cum, [[3.0, 4.0]])


def test_accumulate_precip_cum_is_per_cell():
    cum = None
    for frame in ([[1, 0], [0, 2]], [[2, 2], [0, 1]]):
        cum = ENGINE.accumulate_precip_cum(
            cum, np.array(frame, dtype=np.float32), (2, 2))
    assert np.allclose(cum, [[3, 2], [0, 3]]), cum


# ---------------------------------------------------------------------------
# (d) Casi limite: RH=0, RH>100, sorgente assente
# ---------------------------------------------------------------------------
def test_dew_point_rh_zero_clipped_finite():
    got = float(ENGINE.dew_point_magnus(20.0, 0.0))
    assert np.isfinite(got), "RH=0 deve essere protetto (clip a 0.1%), non -inf"
    assert got < -30.0, got


def test_dew_point_rh_above_100_clipped_to_saturation():
    sat = float(ENGINE.dew_point_magnus(20.0, 100.0))
    over = float(ENGINE.dew_point_magnus(20.0, 120.0))
    assert sat == pytest.approx(20.0, abs=1e-3)
    assert over == pytest.approx(sat, abs=1e-4), "RH>100 va clippata a 100"


def test_derive_per_frame_missing_source_warns_and_skips():
    """Sorgente assente: la derivata NON viene aggiunta, con warning."""
    arrays = {"temperature_2m": np.array([[20.0]], dtype=np.float32)}  # RH assente
    meta = {}
    warns = ENGINE.derive_per_frame(arrays, meta, "test_domain")
    assert "dew_point_2m" not in arrays
    assert "dew_point_2m" not in meta
    assert any("dew_point_2m" in w for w in warns), warns


def test_derive_per_frame_adds_derived_with_meta():
    arrays = {
        "temperature_2m": np.array([[20.0]], dtype=np.float32),
        "relative_humidity_2m": np.array([[50.0]], dtype=np.float32),
        "wind_u_component_10m": np.array([[3.0]], dtype=np.float32),
        "wind_v_component_10m": np.array([[4.0]], dtype=np.float32),
    }
    meta = {}
    warns = ENGINE.derive_per_frame(arrays, meta, "d")
    assert float(arrays["wind_speed_10m"][0, 0]) == pytest.approx(5.0, abs=1e-4)
    assert float(arrays["dew_point_2m"][0, 0]) == pytest.approx(9.27, abs=0.1)
    m = meta["wind_speed_10m"]
    assert m["derived"] is True
    assert m["derived_from"] == ["wind_u_component_10m", "wind_v_component_10m"]
    assert meta["dew_point_2m"]["derived"] is True
    # niente warning per cio' che e' calcolabile
    assert not any(("dew_point_2m" in w) or ("wind_speed_10m" in w) for w in warns), warns


# ---------------------------------------------------------------------------
# Registro / wiring delle variabili
# ---------------------------------------------------------------------------
def test_registry_declares_7_stateless_and_1_stateful():
    assert set(ENGINE.DERIVED_VARS) == set(DERIVED_PER_FRAME) | {"precipitation_cum"}
    for n in DERIVED_PER_FRAME:
        spec = ENGINE.DERIVED_VARS[n]
        assert callable(spec["func"]), n
        assert not spec.get("stateful"), n
    pc = ENGINE.DERIVED_VARS["precipitation_cum"]
    assert pc.get("stateful") is True
    assert pc["sources"] == ["precipitation"]
    assert pc["func"] is None
    # ATOMICITA': speed e direction dello stesso livello hanno le STESSE sorgenti
    # (quindi `derive_per_frame` le omette/aggiunge insieme).
    for level in ("10m", "850hPa", "300hPa"):
        sp = ENGINE.DERIVED_VARS["wind_speed_%s" % level]
        dr = ENGINE.DERIVED_VARS["wind_direction_%s" % level]
        assert dr["sources"] == sp["sources"], level


def test_input_vars_read_but_not_published():
    pub = {rv["name"] for rv in ENGINE.V2_RISK_VARS}
    inp = {v["name"] for v in ENGINE.V2_INPUT_VARS}
    assert inp == {"wind_u_component_10m", "wind_v_component_10m",
                   "wind_u_component_850hPa", "wind_v_component_850hPa",
                   "wind_u_component_300hPa", "wind_v_component_300hPa"}
    assert pub.isdisjoint(inp), "un INPUT non deve essere anche pubblicato"
    assert ENGINE.V2_INPUT_VAR_NAMES == frozenset(inp)
    read = {rv["name"] for rv in ENGINE.V2_READ_VARS}
    assert read == pub | inp, "V2_READ_VARS deve essere pubblicate + input"


def test_derived_names_published_without_direct_source():
    by_name = {rv["name"]: rv for rv in ENGINE.V2_RISK_VARS}
    for n in ENGINE.DERIVED_VARS:
        assert n in by_name, "derivata '%s' non in V2_RISK_VARS" % n
        assert by_name[n]["sources"] == [], "derivata '%s' non deve avere sources" % n


def test_relative_humidity_700hPa_is_direct():
    by_name = {rv["name"]: rv for rv in ENGINE.V2_RISK_VARS}
    assert by_name["relative_humidity_700hPa"]["sources"] == [
        "relative_humidity_700hPa"]
    assert "relative_humidity_700hPa" not in ENGINE.DERIVED_VARS


def test_variable_steps_for_new_variables():
    steps = ENGINE.VARIABLE_STEP
    assert steps["dew_point_2m"] == pytest.approx(0.1)
    assert steps["relative_humidity_700hPa"] == pytest.approx(1.0)
    assert steps["wind_speed_10m"] == pytest.approx(0.1)
    assert steps["wind_speed_850hPa"] == pytest.approx(0.1)
    assert steps["wind_speed_300hPa"] == pytest.approx(0.1)
    assert steps["wind_direction_10m"] == pytest.approx(1.0)
    assert steps["wind_direction_850hPa"] == pytest.approx(1.0)
    assert steps["wind_direction_300hPa"] == pytest.approx(1.0)
    assert steps["precipitation_cum"] == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Round-trip reale (omfiles): la cumulata sopravvive al passo 0.1
# ---------------------------------------------------------------------------
def test_precipitation_cum_roundtrip_real_writer(tmp_path):
    """Il writer reale scrive precipitation_cum col passo giusto, senza saturare.

    1128.7 mm e' il massimo osservato su dati reali (ciclone tropicale, dominio
    globale ECMWF): con passo 0.1 -> int 11287 < 32767 (limite int16).
    """
    path = str(tmp_path / "cum.om")
    val = 1128.7
    arrays = {"precipitation_cum": np.array([[val, 0.1]], dtype=np.float32)}
    nbytes, nvars = ENGINE.write_frame_om(
        path, ["precipitation_cum"], arrays, "GEOGCRS[...]", "{}", 0, 0)
    assert nbytes > 0 and nvars == 1
    reader = omfiles.OmFileReader.from_path(path)
    try:
        child = reader.get_child_by_name("precipitation_cum")
        out = np.asarray(child.read_array((slice(0, 1), slice(0, 2))),
                         dtype=np.float32)
        sf = float(child.scale_factor)
    finally:
        reader.close()
    assert 1.0 / sf == pytest.approx(0.1, abs=1e-6)
    assert np.isfinite(out[0, 0]), "1128.7 mm non deve saturare a NaN"
    assert float(out[0, 0]) == pytest.approx(val, abs=0.05)
    assert float(out[0, 1]) == pytest.approx(0.1, abs=1e-3)
