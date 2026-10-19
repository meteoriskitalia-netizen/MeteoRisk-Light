# -*- coding: utf-8 -*-
"""Regressione: passo di quantizzazione delle variabili .om pubblicate (1.2.4.10).

Bug (diagnosticato e riprodotto): il motore passava a ``omfiles.write_array`` il
PASSO della variabile (la vecchia tabella ``VARIABLE_SCALE``) al posto del
FATTORE DI SCALA. ``omfiles`` memorizza ``int = round(valore * scale_factor)`` e
rilegge ``valore = int / scale_factor``, quindi il passo effettivo e'
``1/scale_factor``. Con ``scale_factor=0.1`` (temperatura, pressione, vento,
pioggia, neve) il passo in produzione diventava 10 (10 degC, 10 hPa, 10 m/s,
10 mm/h: mappe collassate) e la pioggia spariva (4.0 mm/h -> 0.0).

Fix: la tabella e' rinominata ``VARIABLE_STEP`` (contiene PASSI) e il writer
riceve ``scale_factor = 1.0 / passo`` (``write_frame_om``).

Il modulo viene caricato via ``importlib`` per percorso (``scripts/forecast_v2``
non e' un package), stesso pattern di ``test_render_fm_webp_index.py``. Se il
modulo non e' importabile si ricade su un'estrazione ``ast`` del dizionario.
Il round-trip usa ``omfiles`` + ``numpy`` REALI: se mancano i test sono SKIPPED.
"""
import ast
import importlib.util
import os

import pytest

np = pytest.importorskip("numpy")
omfiles = pytest.importorskip("omfiles")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ENGINE_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2",
                           "forecast_v2_engine.py")

# Passi attesi per le variabili chiave (unita' della variabile).
EXPECTED_STEPS = {
    "temperature_2m": 0.1,
    "pressure_msl": 0.1,
    "wind_gusts_10m": 0.1,
    "rain": 0.1,
    "precipitation": 0.1,
    "cloud_cover": 1.0,
    "relative_humidity_2m": 1.0,
    "cape": 10.0,
    "freezing_level_height": 10.0,
}

_COMPRESSION = "pfor_delta_2d_int16"   # stessa compressione usata dal motore


def _load_engine():
    """Carica il motore per percorso; ritorna (module, errore)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "forecast_v2_engine_scale_ut", ENGINE_PATH)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, None
    except Exception as exc:  # pragma: no cover - dipende dall'ambiente
        return None, exc


ENGINE, ENGINE_ERR = _load_engine()


def _steps_from_source():
    """Fallback: estrae VARIABLE_STEP/DEFAULT_STEP dal sorgente via ast."""
    with open(ENGINE_PATH, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=ENGINE_PATH)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id in (
                        "VARIABLE_STEP", "DEFAULT_STEP"):
                    found[tgt.id] = ast.literal_eval(node.value)
    return found


def _load_steps():
    """Ritorna (steps: dict, default_step: float, source: str)."""
    if ENGINE is not None:
        return dict(ENGINE.VARIABLE_STEP), float(ENGINE.DEFAULT_STEP), "import"
    found = _steps_from_source()
    return dict(found["VARIABLE_STEP"]), float(found["DEFAULT_STEP"]), "ast"


def _roundtrip_om(path, name, value, scale_factor):
    """Scrive un valore float32 con l'API reale del motore e lo rilegge.

    Ritorna (valore_ricostruito, scale_factor_letto_dal_reader).
    """
    arr = np.array([[value]], dtype=np.float32)
    w = omfiles.OmFileWriter.at_path(path)
    c = w.write_array(arr, chunks=[1, 1], scale_factor=float(scale_factor),
                      add_offset=0.0, compression=_COMPRESSION, name=name)
    root = w.write_group(name="", children=[c])
    w.close(root)

    reader = omfiles.OmFileReader.from_path(path)
    try:
        child = reader.get_child_by_name(name)
        out = np.asarray(child.read_array((slice(0, 1), slice(0, 1))),
                         dtype=np.float32)[0, 0]
        sf = float(child.scale_factor)
    finally:
        reader.close()
    return float(out), sf


# ---------------------------------------------------------------------------
# 1) Il dizionario dichiara PASSI (non fattori di scala)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("var,step", sorted(EXPECTED_STEPS.items()))
def test_variable_step_declared_per_variable(var, step):
    steps, _default, _src = _load_steps()
    assert var in steps, "variabile '%s' assente dalla tabella" % var
    assert steps[var] == pytest.approx(step), (
        "passo errato per '%s': atteso %s, trovato %s" % (var, step, steps[var]))


def test_default_step_is_one():
    _steps, default, _src = _load_steps()
    assert default == pytest.approx(1.0)


def test_variable_scale_name_is_gone():
    """La vecchia tabella/attributo (fattore di scala) non deve piu' esistere."""
    with open(ENGINE_PATH, "r", encoding="utf-8") as fh:
        source = fh.read()
    assert "VARIABLE_SCALE" not in source
    assert "DEFAULT_SCALE" not in source


# ---------------------------------------------------------------------------
# 2) PROVA CHIAVE: round-trip reale con il path di produzione write_frame_om
# ---------------------------------------------------------------------------
def test_write_frame_om_roundtrip_uses_step(tmp_path):
    """Il frame scritto dal motore deve avere il passo voluto, non 10x."""
    if ENGINE is None:  # pragma: no cover
        pytest.skip("motore non importabile: %r" % (ENGINE_ERR,))
    path = str(tmp_path / "frame.om")
    arrays = {
        "temperature_2m": np.array([[20.75, -3.44]], dtype=np.float32),
        "rain": np.array([[4.0, 0.2]], dtype=np.float32),
    }
    nbytes, nvars = ENGINE.write_frame_om(
        path, ["temperature_2m", "rain"], arrays, "GEOGCRS[...]", "{}", 0, 0)
    assert nbytes > 0 and nvars == 2

    reader = omfiles.OmFileReader.from_path(path)
    try:
        temp = np.asarray(
            reader.get_child_by_name("temperature_2m").read_array(
                (slice(0, 1), slice(0, 2))), dtype=np.float32)
        rain = np.asarray(
            reader.get_child_by_name("rain").read_array(
                (slice(0, 1), slice(0, 2))), dtype=np.float32)
    finally:
        reader.close()

    # temperatura: 20.75 con passo 0.1 -> ~20.8 (NON 20.0)
    assert temp[0, 0] == pytest.approx(20.8, abs=1e-3)
    assert temp[0, 1] == pytest.approx(-3.4, abs=1e-3)
    # pioggia: 4.0 mm/h SOPRAVVIVE (NON diventa 0.0)
    assert rain[0, 0] == pytest.approx(4.0, abs=1e-3)
    assert rain[0, 1] == pytest.approx(0.2, abs=1e-3)


def test_write_frame_om_reconstructed_step_is_declared_step(tmp_path):
    """Il passo ricostruito (1/scale_factor) e' quello dichiarato."""
    if ENGINE is None:  # pragma: no cover
        pytest.skip("motore non importabile: %r" % (ENGINE_ERR,))
    path = str(tmp_path / "frame_step.om")
    arrays = {"temperature_2m": np.array([[10.0]], dtype=np.float32)}
    ENGINE.write_frame_om(path, ["temperature_2m"], arrays, "wkt", "{}", 0, 0)
    reader = omfiles.OmFileReader.from_path(path)
    try:
        sf = float(reader.get_child_by_name("temperature_2m").scale_factor)
    finally:
        reader.close()
    assert 1.0 / sf == pytest.approx(0.1, abs=1e-6)


# ---------------------------------------------------------------------------
# 3) Round-trip diretto (API omfiles) per temperatura e pioggia
# ---------------------------------------------------------------------------
def test_direct_roundtrip_temperature_and_rain(tmp_path):
    steps, _default, _src = _load_steps()
    for name, value, expected in (("temperature_2m", 20.75, 20.8),
                                  ("rain", 4.0, 4.0)):
        path = str(tmp_path / ("%s.om" % name))
        got, sf = _roundtrip_om(path, name, value, 1.0 / steps[name])
        assert 1.0 / sf == pytest.approx(steps[name], abs=1e-6)
        assert got == pytest.approx(expected, abs=1e-3), (
            "%s: %s -> %s (atteso ~%s)" % (name, value, got, expected))


# ---------------------------------------------------------------------------
# 4) Il caso SBAGLIATO (scale_factor = passo) fallirebbe: e' la regressione
# ---------------------------------------------------------------------------
def test_wrong_scale_factor_would_lose_values(tmp_path):
    """Dimostra il bug: con scale_factor=0.1 il passo diventa 10.

    Se il motore tornasse a passare il passo come scale_factor, questi valori
    (20.75 -> 20.0, 4.0 -> 0.0) sono esattamente cio' che si vedrebbe.
    """
    steps, _default, _src = _load_steps()
    # ipotesi SBAGLIATA: scale_factor = passo (bug originale)
    temp_wrong, _ = _roundtrip_om(str(tmp_path / "t_bad.om"),
                                  "temperature_2m", 20.75, steps["temperature_2m"])
    rain_wrong, _ = _roundtrip_om(str(tmp_path / "r_bad.om"),
                                  "rain", 4.0, steps["rain"])
    assert temp_wrong == pytest.approx(20.0, abs=1e-3)
    assert rain_wrong == pytest.approx(0.0, abs=1e-3)

    # ipotesi CORRETTA: scale_factor = 1/passo
    temp_ok, _ = _roundtrip_om(str(tmp_path / "t_ok.om"),
                               "temperature_2m", 20.75, 1.0 / steps["temperature_2m"])
    rain_ok, _ = _roundtrip_om(str(tmp_path / "r_ok.om"),
                               "rain", 4.0, 1.0 / steps["rain"])
    assert temp_ok == pytest.approx(20.8, abs=1e-3)
    assert rain_ok == pytest.approx(4.0, abs=1e-3)
    assert temp_ok != pytest.approx(temp_wrong, abs=1e-3)
    assert rain_ok != pytest.approx(rain_wrong, abs=1e-3)
