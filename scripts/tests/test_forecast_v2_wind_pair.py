# -*- coding: utf-8 -*-
"""Guardia VENTO (1.2.4.11): velocita' + direzione pubblicate IN COPPIA.

Difetto che questa guardia avrebbe intercettato (presente fino alla 1.2.4.10):
il motore pubblicava ``wind_speed_10m/850hPa/300hPa`` SENZA le direzioni, e le
componenti ``wind_u_component_10m``/``wind_v_component_10m`` al loro posto. La
libreria client ``@openmeteo/weather-map-layer`` (``getSourceVars``) per un nome
che corrisponde a ``_(?:speed|direction)_`` pretende la COPPIA nel ``.om``:

  - ``wind_speed_10m`` -> primaria ``wind_speed_10m``, secondaria
    ``wind_direction_10m``; se la secondaria manca il reader LANCIa
    ``Secondary variable ... not found`` e la mappa va in errore;
  - per un nome ``_[uv]_component`` la libreria legge u e v e mostra da sola
    ``sqrt(u^2+v^2)``.

Fix: si pubblicano velocita' E direzione (coppia), si SMETTE di pubblicare u/v
(diventano INPUT). La derivazione della direzione usa la STESSA convenzione
della libreria: ``(degrees(atan2(u, v)) + 180) % 360`` (direzione
meteorologica, quella DA cui soffia il vento).

Il modulo del motore si carica via ``importlib`` per percorso
(``scripts/forecast_v2`` non e' un package), stesso pattern di
``test_forecast_v2_derived.py``. Se ``numpy``/``omfiles`` mancano -> SKIPPED.
"""
import importlib.util
import os
import re

import pytest

np = pytest.importorskip("numpy")
omfiles = pytest.importorskip("omfiles")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ENGINE_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2",
                           "forecast_v2_engine.py")


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "forecast_v2_engine_windpair_ut", ENGINE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ENGINE = _load_engine()

# Stessa forma della libreria client: un nome "vento-coppia" e' quello che
# contiene `_speed_` oppure `_direction_` (segmento delimitato da underscore).
WIND_PAIR_RE = re.compile(r"_(?:speed|direction)_")

LEVELS = ("10m", "850hPa", "300hPa")


def _complement(name):
    """Complementare speed<->direction di un nome vento-coppia (o None)."""
    if "_speed_" in name:
        return name.replace("_speed_", "_direction_", 1)
    if "_direction_" in name:
        return name.replace("_direction_", "_speed_", 1)
    return None


def wind_pair_offenders(published_names):
    """Nomi che corrispondono a `_(?:speed|direction)_` senza il complementare.

    Ritorna la lista di tuple ``(nome, complementare_atteso)`` mancanti. Lista
    vuota = la coppia e' completa. E' la GUARDIA: se un `wind_speed_*` resta
    senza `wind_direction_*` (o viceversa) qui si produce un offender.
    """
    names = set(published_names)
    offenders = []
    for n in sorted(names):
        if not WIND_PAIR_RE.search(n):
            continue
        comp = _complement(n)
        if comp is not None and comp not in names:
            offenders.append((n, comp))
    return offenders


def assert_wind_pairs_complete(published_names):
    offenders = wind_pair_offenders(published_names)
    assert not offenders, (
        "variabile/i vento pubblicata/e senza il complementare speed<->direction "
        "(il reader del client lancerebbe 'Secondary variable ... not found'): %s"
        % (offenders,))


# ---------------------------------------------------------------------------
# 1) GUARDIA sui nomi PUBBLICATI dal motore: coppia speed<->direction completa
# ---------------------------------------------------------------------------
def test_published_wind_pairs_are_complete():
    published = [rv["name"] for rv in ENGINE.V2_RISK_VARS]
    assert_wind_pairs_complete(published)
    # le 3 velocita' e le 3 direzioni devono esserci davvero (non banalmente
    # vuote: altrimenti la guardia passerebbe senza controllare nulla).
    for level in LEVELS:
        assert "wind_speed_%s" % level in published, level
        assert "wind_direction_%s" % level in published, level


def test_derived_frame_publishes_wind_pairs():
    """La guardia vale anche sull'insieme REALE prodotto da derive_per_frame."""
    arrays = {}
    for level in LEVELS:
        arrays["wind_u_component_%s" % level] = np.array(
            [[3.0, 0.0], [1.0, 5.0]], dtype=np.float32)
        arrays["wind_v_component_%s" % level] = np.array(
            [[4.0, -1.0], [-2.0, 12.0]], dtype=np.float32)
    meta = {}
    ENGINE.derive_per_frame(arrays, meta, "ut")
    published = [rv["name"] for rv in ENGINE.V2_RISK_VARS if rv["name"] in arrays]
    assert_wind_pairs_complete(published)
    for level in LEVELS:
        assert "wind_speed_%s" % level in arrays, level
        assert "wind_direction_%s" % level in arrays, level


# ---------------------------------------------------------------------------
# 2) PROVA DI SENSIBILITA': togliendo la direzione la guardia FALLISCE
# ---------------------------------------------------------------------------
def test_guard_sensitivity_removing_direction_fails():
    published = [rv["name"] for rv in ENGINE.V2_RISK_VARS]
    broken = [n for n in published if n != "wind_direction_10m"]
    assert wind_pair_offenders(broken) == [("wind_speed_10m", "wind_direction_10m")]
    with pytest.raises(AssertionError) as exc:
        assert_wind_pairs_complete(broken)
    assert "wind_speed_10m" in str(exc.value)
    assert "wind_direction_10m" in str(exc.value)


def test_guard_sensitivity_removing_speed_fails():
    published = [rv["name"] for rv in ENGINE.V2_RISK_VARS]
    broken = [n for n in published if n != "wind_speed_850hPa"]
    with pytest.raises(AssertionError):
        assert_wind_pairs_complete(broken)


# ---------------------------------------------------------------------------
# 3) u/v a 10 m: NON pubblicate, presenti tra gli INPUT
# ---------------------------------------------------------------------------
def test_uv_10m_not_published_but_inputs():
    pub = {rv["name"] for rv in ENGINE.V2_RISK_VARS}
    inp = {v["name"] for v in ENGINE.V2_INPUT_VARS}
    assert "wind_u_component_10m" not in pub
    assert "wind_v_component_10m" not in pub
    assert "wind_u_component_10m" in inp
    assert "wind_v_component_10m" in inp
    # letti dai reader ma non pubblicati
    read = {rv["name"] for rv in ENGINE.V2_READ_VARS}
    assert {"wind_u_component_10m", "wind_v_component_10m"} <= read
    assert ENGINE.V2_INPUT_VAR_NAMES == frozenset(inp)


# ---------------------------------------------------------------------------
# 4) CONVENZIONE della direzione: 4 casi noti, valori ESATTI
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("u,v,expected", [
    (0.0, -1.0, 0.0),     # vento da NORD
    (-1.0, 0.0, 90.0),    # da OVEST
    (0.0, 1.0, 180.0),    # da SUD
    (1.0, 0.0, 270.0),    # da EST
])
def test_wind_direction_from_uv_known_cases(u, v, expected):
    got = float(ENGINE.wind_direction_from_uv(u, v))
    assert got == expected, (u, v, got, expected)


def test_wind_direction_matches_library_formula():
    """Riapplica a mano (degrees(atan2(u,v))+180)%360 e confronta."""
    rng = np.random.default_rng(1234)
    u = rng.normal(0, 5, size=(64,)).astype(np.float32)
    v = rng.normal(0, 5, size=(64,)).astype(np.float32)
    expected = (np.degrees(np.arctan2(u.astype(np.float64),
                                      v.astype(np.float64))) + 180.0) % 360.0
    got = np.asarray(ENGINE.wind_direction_from_uv(u, v), dtype=np.float64)
    assert np.allclose(got, expected, atol=1e-4)


def test_wind_direction_range_and_no_nan():
    u = np.array([0.0, 0.0], dtype=np.float32)
    v = np.array([0.0, 0.0], dtype=np.float32)
    d = np.asarray(ENGINE.wind_direction_from_uv(u, v), dtype=np.float64)
    assert np.all(np.isfinite(d)), "u=v=0 non deve produrre NaN"
    big = np.asarray(ENGINE.wind_direction_from_uv(
        np.array([1e6, -1e6], dtype=np.float32),
        np.array([1e6, 1e6], dtype=np.float32)), dtype=np.float64)
    assert np.all(big >= 0.0) and np.all(big < 360.0)


def test_published_direction_never_360_after_quantization(tmp_path):
    """359.5..360 verrebbe arrotondato a 360 (fuori [0,360)): va mappato a 0.

    Caso reale misurato su dwd_icon_eu: 5 celle su 395569 a 360.0 prima del
    wrap. Qui u=0.00524, v=-1 -> formula ~359.7 gradi.
    """
    u = np.array([[0.00524]], dtype=np.float32)
    v = np.array([[-1.0]], dtype=np.float32)
    raw = float(ENGINE.wind_direction_from_uv(u, v)[0, 0])
    assert 359.0 < raw < 360.0, raw

    arrays = {"wind_u_component_10m": u, "wind_v_component_10m": v}
    ENGINE.derive_per_frame(arrays, {}, "ut")
    assert float(arrays["wind_direction_10m"][0, 0]) == 0.0

    # anche il `.om` scritto/riletto resta in [0,360)
    path = str(tmp_path / "dir_wrap.om")
    ENGINE.write_frame_om(path, ["wind_direction_10m"],
                          {"wind_direction_10m": arrays["wind_direction_10m"]},
                          "GEOGCRS[...]", "lat lon", 0, 0)
    _, out = _read_children(path)
    val = float(out["wind_direction_10m"][0, 0])
    assert val == 0.0, val
    assert 0.0 <= val < 360.0


# ---------------------------------------------------------------------------
# 5) ATOMICITA': velocita' e direzione omesse/aggiunte INSIEME
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("level", LEVELS)
def test_speed_and_direction_omitted_together(level):
    # sorgente COMPLETA -> entrambe presenti
    full = {
        "wind_u_component_%s" % level: np.array([[3.0]], dtype=np.float32),
        "wind_v_component_%s" % level: np.array([[4.0]], dtype=np.float32),
    }
    ENGINE.derive_per_frame(full, {}, "ut")
    assert "wind_speed_%s" % level in full
    assert "wind_direction_%s" % level in full

    # manca UNA componente -> ENTRAMBE omesse
    for keep in ("wind_u_component_%s" % level, "wind_v_component_%s" % level):
        partial = {keep: np.array([[3.0]], dtype=np.float32)}
        warns = ENGINE.derive_per_frame(partial, {}, "ut")
        assert "wind_speed_%s" % level not in partial, (level, keep)
        assert "wind_direction_%s" % level not in partial, (level, keep)
        assert any(("wind_speed_%s" % level) in w for w in warns), warns
        assert any(("wind_direction_%s" % level) in w for w in warns), warns


# ---------------------------------------------------------------------------
# 6) End-to-end su un `.om` REALE (writer + reader omfiles reali)
# ---------------------------------------------------------------------------
def _read_children(path):
    reader = omfiles.OmFileReader.from_path(path)
    try:
        # usa la STESSA iterazione del motore (child non-scalar)
        names = ENGINE.array_child_names(reader)
        out = {}
        for n in sorted(names):
            child = reader.get_child_by_name(n)
            ny, nx = int(child.shape[0]), int(child.shape[1])
            out[n] = np.asarray(
                child.read_array((slice(0, ny), slice(0, nx))),
                dtype=np.float64)
    finally:
        reader.close()
    return names, out


def test_e2e_real_om_speed_and_direction_coherent(tmp_path):
    """Frame `.om` SORGENTE (solo u/v) -> derivazione -> frame PUBBLICATO.

    Verifica, sul `.om` scritto e RILETTO da omfiles reali, che:
      - u/v NON sono nel file pubblicato;
      - esistono le COPPIE wind_speed_*/wind_direction_*;
      - speed == hypot(u, v) (delta < 1e-3);
      - la direzione coincide con la formula della libreria;
      - u = v = 0 non produce NaN.
    """
    u = np.array([[3.0, 0.0], [1.0, 0.0]], dtype=np.float32)
    v = np.array([[4.0, -1.0], [0.0, 0.0]], dtype=np.float32)
    src = str(tmp_path / "source.om")
    ENGINE.write_frame_om(src, ["wind_u_component_10m", "wind_v_component_10m"],
                          {"wind_u_component_10m": u,
                           "wind_v_component_10m": v}, "GEOGCRS[...]", "lat lon", 0, 0)
    src_names, src_arr = _read_children(src)
    assert src_names == {"wind_u_component_10m", "wind_v_component_10m"}

    # derivazione sul contenuto REALE letto dal `.om` sorgente
    arrays = {k: v_.astype(np.float32) for k, v_ in src_arr.items()}
    meta = {}
    warns = ENGINE.derive_per_frame(arrays, meta, "dwd_icon_eu")
    # dew_point_2m (mancano T/RH) e i livelli 850/300 hPa (mancano le loro u/v)
    # non sono calcolabili: warning attesi su quelli, NESSUN warning sui 10 m
    # (le sorgenti u/v 10 m ci sono).
    assert not any(("wind_speed_10m" in w) or ("wind_direction_10m" in w)
                   for w in warns), warns

    ordered = [rv["name"] for rv in ENGINE.V2_RISK_VARS]
    pub = str(tmp_path / "published.om")
    ENGINE.write_frame_om(pub, ordered, arrays, "GEOGCRS[...]", "lat lon", 0, 0)
    names, out = _read_children(pub)

    # (a) niente u/v nel pubblicato
    assert "wind_u_component_10m" not in names
    assert "wind_v_component_10m" not in names
    # (b) coppia presente e completa
    assert {"wind_speed_10m", "wind_direction_10m"} <= names
    assert_wind_pairs_complete(names)

    # (c) speed == hypot(u, v)
    speed = out["wind_speed_10m"]
    hypot = np.hypot(u.astype(np.float64), v.astype(np.float64))
    assert np.allclose(speed, hypot, atol=1e-3), (speed, hypot)

    # (d) direction coincide con la formula (tolleranza = mezzo passo di 1 grado
    #     dichiarato in VARIABLE_STEP: il `.om` scrive la direzione intera)
    expected_dir = (np.degrees(np.arctan2(u.astype(np.float64),
                                          v.astype(np.float64))) + 180.0) % 360.0
    assert np.allclose(out["wind_direction_10m"], expected_dir, atol=0.5)

    # (e) cella u=v=0 (in basso a destra: u=0, v=0): niente NaN, speed 0
    assert np.isfinite(out["wind_speed_10m"][1, 1])
    assert out["wind_speed_10m"][1, 1] == pytest.approx(0.0, abs=1e-3)
    assert np.isfinite(out["wind_direction_10m"][1, 1])
