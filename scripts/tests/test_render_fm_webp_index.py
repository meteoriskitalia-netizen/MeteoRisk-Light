# -*- coding: utf-8 -*-
"""Test della modalita' --index-only di render_fm_webp.py (hotfix CI 1.2.4.8.1).

Alberi di test sintetici: NESSUNA dipendenza di rete ne' dati .om reali, NESSUN
mock del sistema sotto test (i manifest/latest.json/.webp sono file reali su un
tmp_path). Il modulo viene caricato via importlib per percorso perche'
scripts/forecast_v2 non e' un package.
"""
import importlib.util
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RENDERER_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2", "render_fm_webp.py")


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_fm_webp_under_test",
                                                  RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_renderer()

VALID_TIMES = ["2026-10-14T16:00Z", "2026-10-14T17:00Z", "2026-10-14T18:00Z"]
FRAME_FILES = ["2026-10-14T1600.webp", "2026-10-14T1700.webp", "2026-10-14T1800.webp"]
STALE_FILE = "2020-01-01T0000.webp"


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def _var_manifest(region, domain, var, theme):
    """Per-var WebP latest.json realistico (stessa forma del render path)."""
    bbox = {"lat_min": 36.5, "lon_min": 6.5, "lat_max": 47.2, "lon_max": 18.6}
    return {
        "version": mod.WEBP_VERSION,
        "generated_at": "2026-10-14T20:00:00Z",
        "region": region,
        "domain": domain,
        "variable": var,
        "theme": theme,
        "unit": "degC",
        "bbox": bbox,
        "bounds": [[36.5, 6.5], [47.2, 18.6]],
        "grid": {"nx": 10, "ny": 12},
        "frames": [{"file": f, "valid_time": vt, "bytes": 5}
                   for f, vt in zip(FRAME_FILES, VALID_TIMES)],
        "valid_times": list(VALID_TIMES),
    }


def _make_theme_dir(out, region, domain, var, theme, malformed=False):
    """Crea <out>/<region>/<domain>/<var>[/dark] con 3 .webp + 1 stale + latest."""
    tdir = os.path.join(out, region, domain, var)
    if theme == "dark":
        tdir = os.path.join(tdir, "dark")
    os.makedirs(tdir, exist_ok=True)
    for f in FRAME_FILES:
        with open(os.path.join(tdir, f), "wb") as fh:
            fh.write(b"RIFF\x00\x00\x00\x00WEBPdummy")
    with open(os.path.join(tdir, STALE_FILE), "wb") as fh:
        fh.write(b"STALE-not-in-manifest")
    if malformed:
        with open(os.path.join(tdir, "latest.json"), "w", encoding="utf-8") as fh:
            fh.write("{ questo non e' JSON valido")
    else:
        _write_json(os.path.join(tdir, "latest.json"),
                    _var_manifest(region, domain, var, theme))


@pytest.fixture
def tree(tmp_path):
    """Albero sintetico: in/domA (engine), out con domA (valido) e domB (orfano)."""
    in_dir = str(tmp_path / "in")
    out_dir = str(tmp_path / "out")
    # Gate --in: solo domA ha <domain>/latest.json engine.
    os.makedirs(os.path.join(in_dir, "domA"))
    _write_json(os.path.join(in_dir, "domA", "latest.json"),
                {"domain": "domA", "reference_time": "2026-10-14T12:00Z",
                 "frames": [], "valid_times": []})
    # italia/domA/varX: light + dark (3 frame listati + 1 stale ciascuna).
    _make_theme_dir(out_dir, "italia", "domA", "varX", "light")
    _make_theme_dir(out_dir, "italia", "domA", "varX", "dark")
    # italia/domA/varMalformed: latest.json rotto -> saltata, file intatti.
    _make_theme_dir(out_dir, "italia", "domA", "varMalformed", "light",
                    malformed=True)
    # europa/domA/varY: SOLO light -> themes deve essere ["light"].
    _make_theme_dir(out_dir, "europa", "domA", "varY", "light")
    # domB: presente SOLO sotto out (non sotto in/) -> escluso dalla gate.
    _make_theme_dir(out_dir, "italia", "domB", "varZ", "light")
    return {"in": in_dir, "out": out_dir}


def _webp_names(dir_path):
    return sorted(f for f in os.listdir(dir_path) if f.lower().endswith(".webp"))


def test_index_only_writes_schema_prunes_and_copies_scales(tree):
    rc = mod.main(["--in", tree["in"], "--out", tree["out"],
                   "--index-only", "--domains", "domA,domB"])
    assert rc == 0

    # (1) indice italia: schema esatto, domA presente, domB assente.
    idx_path = os.path.join(tree["out"], "italia", "index.json")
    assert os.path.isfile(idx_path)
    with open(idx_path, encoding="utf-8") as fh:
        idx = json.load(fh)
    assert set(idx) == {"version", "generated_at", "region", "theme",
                        "themes", "bbox", "domains"}
    assert idx["version"] == mod.WEBP_VERSION
    assert idx["region"] == "italia"
    assert idx["theme"] == "light"
    assert idx["themes"] == ["light", "dark"]
    assert idx["bbox"] == {"lat_min": 36.5, "lon_min": 6.5,
                           "lat_max": 47.2, "lon_max": 18.6}
    assert "domA" in idx["domains"]
    assert "domB" not in idx["domains"]

    dom = idx["domains"]["domA"]
    assert set(dom) == {"bbox", "bounds", "grid", "variables"}
    assert dom["bounds"] == [[36.5, 6.5], [47.2, 18.6]]
    assert dom["grid"] == {"nx": 10, "ny": 12}

    varx = dom["variables"]["varX"]
    assert varx["dir"] == "domA/varX"
    assert varx["unit"] == "degC"
    assert set(varx["themes"]) == {"light", "dark"}
    light = varx["themes"]["light"]
    assert light == {"count": 3, "valid_times": VALID_TIMES,
                     "dir": "domA/varX", "manifest": "domA/varX/latest.json"}
    dark = varx["themes"]["dark"]
    assert dark == {"count": 3, "valid_times": VALID_TIMES,
                    "dir": "domA/varX/dark",
                    "manifest": "domA/varX/dark/latest.json"}
    # (3) varMalformed: saltata interamente, ABSENTE dall'indice.
    assert "varMalformed" not in dom["variables"]

    # (2) prune: stale rimosso, i 3 listati restano (light E dark, keep-set propri).
    for sub in ("varX", os.path.join("varX", "dark")):
        tdir = os.path.join(tree["out"], "italia", "domA", sub)
        assert _webp_names(tdir) == sorted(FRAME_FILES)
        assert STALE_FILE not in os.listdir(tdir)
    # (3) file della var malformed NON potati.
    mdir = os.path.join(tree["out"], "italia", "domA", "varMalformed")
    assert os.path.isfile(os.path.join(mdir, STALE_FILE))
    assert os.path.isfile(os.path.join(mdir, FRAME_FILES[0]))

    # (4) europa scritto; solo light -> themes ["light"].
    with open(os.path.join(tree["out"], "europa", "index.json"),
              encoding="utf-8") as fh:
        eidx = json.load(fh)
    assert eidx["themes"] == ["light"]
    assert eidx["theme"] == "light"
    assert "domA" in eidx["domains"]
    assert "domB" not in eidx["domains"]
    assert set(eidx["domains"]["domA"]["variables"]) == {"varY"}

    # (5) color_scales.json copiato alla radice di out.
    assert os.path.isfile(os.path.join(tree["out"], "color_scales.json"))
    # (6) exit code 0 gia' assertato in test.


def test_index_only_empty_domain_list_exits_2_nothing_written(tree):
    # --domains mancante/vuoto: errore CLI (exit 2), nessuna scrittura.
    rc = mod.main(["--in", tree["in"], "--out", tree["out"], "--index-only"])
    assert rc == 2
    assert not os.path.exists(os.path.join(tree["out"], "italia", "index.json"))
    assert not os.path.exists(os.path.join(tree["out"], "europa", "index.json"))
    assert not os.path.isfile(os.path.join(tree["out"], "color_scales.json"))


def test_index_only_no_valid_domain_exits_1_nothing_written(tree):
    # Lista non vuota ma nessun dominio valido (domB non esiste in --in):
    # exit 1, nessun indice, nessuna pota, nessun color_scales.
    rc = mod.main(["--in", tree["in"], "--out", tree["out"],
                   "--index-only", "--domains", "domB"])
    assert rc == 1
    assert not os.path.exists(os.path.join(tree["out"], "italia", "index.json"))
    assert not os.path.isfile(os.path.join(tree["out"], "color_scales.json"))
    zdir = os.path.join(tree["out"], "italia", "domB", "varZ")
    assert os.path.isfile(os.path.join(zdir, STALE_FILE))  # non potato


def test_index_only_requires_in_dir_with_manifest(tmp_path):
    # --in senza alcun <dominio>/latest.json: gate exit 2 (stesso stile del render).
    os.makedirs(str(tmp_path / "in"))
    rc = mod.main(["--in", str(tmp_path / "in"), "--out", str(tmp_path / "out"),
                   "--index-only", "--domains", "domA"])
    assert rc == 2


def test_module_imports_with_lazy_heavy_deps():
    # Il modulo va importato anche senza numpy/PIL/omfiles: il meccanismo lazy e'
    # il dict _IMPORT_ERRORS (catturato, non alzato) + _require_render_deps().
    assert isinstance(mod._IMPORT_ERRORS, dict)
    assert callable(mod._require_render_deps)
    # Con deps presenti l'import non ha FATALato (altrimenti mod non esisterebbe).


def test_require_render_deps_fatals_when_missing(monkeypatch, capsys):
    # Simula l'assenza di numpy SENZA disinstallare: FATAL su stderr + re-raise.
    err = ImportError("numpy assente (simulato)")
    monkeypatch.setattr(mod, "_IMPORT_ERRORS", {"numpy": err})
    with pytest.raises(ImportError):
        mod._require_render_deps()
    assert "FATAL" in capsys.readouterr().err
    # Deps effettivamente presenti in questo ambiente: nessun FATAL.
    monkeypatch.setattr(mod, "_IMPORT_ERRORS", {})
    mod._require_render_deps()  # non deve alzare


def test_parse_args_method_default_and_override():
    assert mod.parse_args([]).method == 6
    assert mod.parse_args(["--method", "4"]).method == 4
    assert mod.parse_args([]).index_only is False
    assert mod.parse_args(["--index-only"]).index_only is True


def test_save_webp_method_parameter():
    # Se Pillow/numpy non sono installati questo e' l'UNICO test saltato.
    try:
        import numpy as np
        from PIL import Image  # noqa: F401
    except ImportError:
        pytest.skip("Pillow/numpy non disponibili: test save_webp saltato")
    import tempfile
    arr = np.zeros((4, 4, 4), dtype=np.uint8)
    arr[..., 0] = 200
    arr[..., 3] = 255
    with tempfile.TemporaryDirectory() as tmp:
        for method in (0, 6):
            path = os.path.join(tmp, "m%d.webp" % method)
            size = mod.save_webp(arr, path, quality=90, lossless=False,
                                 method=method)
            assert size > 0
            with open(path, "rb") as fh:
                head = fh.read(12)
            assert head[:4] == b"RIFF" and head[8:12] == b"WEBP"
