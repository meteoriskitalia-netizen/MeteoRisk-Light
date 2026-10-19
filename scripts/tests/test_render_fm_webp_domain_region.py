# -*- coding: utf-8 -*-
"""Test della regione 'domain' (sentinella None) in render_fm_webp.py.

Il modulo viene caricato via importlib per percorso (scripts/forecast_v2 non e'
un package), con la stessa convenzione di test_render_fm_webp_index.py.

bbox/grid costanti: presi dai MANIFEST REALI pubblicati sul remoto
(``https://raw.githubusercontent.com/meteoriskitalia-netizen/MeteoRisk-Light/
main/data/fm2/<dom>/latest.json``) per TUTTI i domini (globali e non), letti il
2026-10-17T23:32Z (UTC). Nessun valore e' inferito dal motore: i numeri qui sotto
sono quelli effettivamente pubblicati nel campo ``bbox`` del manifest.
"""
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RENDERER_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2", "render_fm_webp.py")


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_fm_webp_domain_ut",
                                                  RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_renderer()

# (bbox, grid) dai manifest reali (data/fm2/<dom>/latest.json).
CASES = {
    "dwd_icon_eu": (
        {"lat_min": 34.0, "lon_min": -15.0, "lat_max": 62.0, "lon_max": 40.0},
        {"nx": 881, "ny": 449, "lonMin": -15.0, "latMin": 34.0,
         "dx": 0.0625, "dy": 0.0625},
    ),
    "dwd_icon_d2": (
        {"lat_min": 43.18, "lon_min": -3.94, "lat_max": 58.08, "lon_max": 20.34},
        {"nx": 1215, "ny": 746, "lonMin": -3.94, "latMin": 43.18,
         "dx": 0.019999998352553543, "dy": 0.019999999999999997},
    ),
    "italia_meteo_arpae_icon_2i": (
        {"lat_min": 36.5, "lon_min": 6.5, "lat_max": 47.2, "lon_max": 18.6},
        {"nx": 485, "ny": 536, "lonMin": 6.5, "latMin": 36.5,
         "dx": 0.025, "dy": 0.019999999999999993},
    ),
}

# Domini GLOBALI: bbox REALI letti da data/fm2/<dom>/latest.json (2026-10-17T23:32Z).
GLOBAL_BBOX = {
    "dwd_icon": (-90.0, -180.0, 90.0, 179.75),
    "ukmo_global_deterministic_10km": (-90.0, -180.0, 89.90625, 179.85938),
    "cma_grapes_global": (-89.9375, -180.0, 89.9375, 179.875),
    "ecmwf_ifs025": (-90.0, -180.0, 90.0, 179.75),
    "ncep_gfs025": (-90.0, -180.0, 90.0, 179.75),
    "meteofrance_arpege_world025": (-90.0, -180.0, 90.0, 179.75),
    "jma_gsm": (-90.0, -180.0, 90.0, 179.5),
}

# Domini NON globali: bbox reali.
NON_GLOBAL_BBOX = {
    "dwd_icon_eu": (34.0, -15.0, 62.0, 40.0),
    "dwd_icon_d2": (43.18, -3.94, 58.08, 20.34),
    "italia_meteo_arpae_icon_2i": (36.5, 6.5, 47.2, 18.6),
}


def _bbox_dict(t):
    return {"lat_min": t[0], "lon_min": t[1], "lat_max": t[2], "lon_max": t[3]}


def _write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def test_domain_sentinel_is_none_and_constants():
    assert mod.REGIONS["domain"] is None
    assert mod.DOMAIN_REGION == "domain"
    assert mod.DOMAIN_REGION_SKIP_GLOBAL is True
    # Default della CLI: la nuova regione e' inclusa.
    assert mod.parse_args([]).regions is None
    assert "domain" in mod.REGIONS


def test_resolve_region_bbox_domain_maps_to_manifest_bbox():
    for name, (bbox, _grid) in CASES.items():
        got = mod.resolve_region_bbox(mod.REGIONS["domain"], bbox)
        assert got == (bbox["lat_min"], bbox["lon_min"],
                       bbox["lat_max"], bbox["lon_max"]), name
    # Una regione normale (tupla) e' restituita invariata.
    assert mod.resolve_region_bbox((1.0, 2.0, 3.0, 4.0), {"lat_min": 0}) == \
        (1.0, 2.0, 3.0, 4.0)


def test_region_crop_whole_domain_does_not_crop():
    for name, (bbox, grid) in CASES.items():
        rb = mod.resolve_region_bbox(mod.REGIONS["domain"], bbox)
        cr = mod.region_crop(bbox, grid, rb)
        assert cr is not None, name
        # region_crop ritorna (row0, row1, col0, col1, cropped_grid); la regione
        # 'domain' NON ritaglia nulla -> le prime 4 voci sono (0, ny, 0, nx).
        assert cr[:4] == (0, grid["ny"], 0, grid["nx"]), name
        assert cr[4]["nx"] == grid["nx"] and cr[4]["ny"] == grid["ny"], name


def test_is_global_bbox_flag():
    for name, t in GLOBAL_BBOX.items():
        assert mod._is_global_bbox(_bbox_dict(t)) is True, name
    for name, t in NON_GLOBAL_BBOX.items():
        assert mod._is_global_bbox(_bbox_dict(t)) is False, name


def test_index_only_domain_region_end_to_end(tmp_path):
    in_dir = str(tmp_path / "in")
    out_dir = str(tmp_path / "out")
    dom = "dwd_icon_eu"
    var = "temperature_2m"
    bbox = CASES[dom][0]
    valid_times = ["2026-10-14T16:00Z", "2026-10-14T17:00Z"]

    # Gate --in: manifest engine del dominio (per --index-only conta solo la sua
    # presenza + il bbox che finisce nel per-var manifest).
    _write_json(os.path.join(in_dir, dom, "latest.json"),
                {"domain": dom, "bbox": bbox, "grid": CASES[dom][1],
                 "frames": [], "valid_times": []})

    # Output renderizzato: <out>/domain/<dom>/<var>/ con 2 .webp + latest.json.
    vdir = os.path.join(out_dir, "domain", dom, var)
    os.makedirs(vdir, exist_ok=True)
    frames = []
    for vt in valid_times:
        fname = vt.replace(":", "").replace("Z", "") + ".webp"
        with open(os.path.join(vdir, fname), "wb") as fh:
            fh.write(b"RIFF\x00\x00\x00\x00WEBPdummy")
        frames.append({"file": fname, "valid_time": vt, "bytes": 5})
    _write_json(os.path.join(vdir, "latest.json"), {
        "version": mod.WEBP_VERSION,
        "generated_at": "2026-10-14T20:00:00Z",
        "region": "domain",
        "domain": dom,
        "variable": var,
        "theme": "light",
        "unit": "degC",
        "bbox": bbox,
        "bounds": [[bbox["lat_min"], bbox["lon_min"]],
                   [bbox["lat_max"], bbox["lon_max"]]],
        "grid": {"nx": CASES[dom][1]["nx"], "ny": CASES[dom][1]["ny"]},
        "frames": frames,
        "valid_times": list(valid_times),
    })

    rc = mod.main(["--in", in_dir, "--out", out_dir, "--index-only",
                   "--regions", "domain", "--domains", dom])
    assert rc == 0

    idx_path = os.path.join(out_dir, "domain", "index.json")
    assert os.path.isfile(idx_path)
    with open(idx_path, encoding="utf-8") as fh:
        idx = json.load(fh)
    assert idx["region"] == "domain"
    assert set(idx) == {"version", "generated_at", "region", "theme",
                        "themes", "bbox", "domains"}
    assert dom in idx["domains"]
    assert idx["domains"][dom]["variables"][var]["dir"] == "%s/%s" % (dom, var)
    # bbox della regione 'domain' = unione dei bbox dei domini indicizzati
    # (qui un solo dominio) -> coincide col bbox del dominio.
    assert idx["bbox"] == bbox


# --------------------------------------------------------------------------- #
# FIX 1.2.4.9 - soglie "global" allineate al client (assi NON trasposti).
#
# Predicato server PRIMA del fix (bug, assi scambiati):
#     lat_span > 300 OR lon_span > 120
# Predicato DOPO il fix == client fmIsGlobalDomain
# (mri-light-1.2.4.10.html): lon_span > 300 OR lat_span > 120.
#
# Per i 10 domini reali i due predicati coincidono (7 globali / 3 non-globali,
# nessun cambio di classificazione: golden sotto), ma si DISTINGUONO sui due
# bbox "discriminanti" qui sotto: con la vecchia implementazione questi test
# FALLISCONO (lon_span=200 veniva saltato a torto, lat_span=150 veniva
# renderizzato a torto). Nessuno dei due e' un caso di bordo: le soglie sono
# 300/120, gli span testati sono 200/40 e 50/150.
# --------------------------------------------------------------------------- #
DISCRIMINATING = {
    # lon_span=200, lat_span=40: VECCHIO -> globale (la regione 'domain' veniva
    # SALTATA a torto); NUOVO -> non globale -> DEVE essere renderizzata.
    "wide_lon": {"lat_min": 10.0, "lon_min": -100.0,
                 "lat_max": 50.0, "lon_max": 100.0},
    # lon_span=50, lat_span=150: VECCHIO -> non globale (renderizzata a torto);
    # NUOVO -> globale -> la regione 'domain' DEVE essere saltata.
    "tall_lat": {"lat_min": -75.0, "lon_min": -25.0,
                 "lat_max": 75.0, "lon_max": 25.0},
}


def _old_transposed_predicate(bbox):
    """Copia VERBATIM della riga server PRE-fix (bug): assi scambiati.

    Solo per provare che i casi sotto DISTINGUONO le due implementazioni.
    """
    return ((float(bbox["lat_max"]) - float(bbox["lat_min"])) > 300.0
            or (float(bbox["lon_max"]) - float(bbox["lon_min"])) > 120.0)


def test_is_global_bbox_discriminates_the_two_predicates():
    wide = DISCRIMINATING["wide_lon"]
    tall = DISCRIMINATING["tall_lat"]
    # Span esatti verificati (i casi non sono banali/ne di bordo).
    assert (wide["lon_max"] - wide["lon_min"]) == 200.0
    assert (wide["lat_max"] - wide["lat_min"]) == 40.0
    assert (tall["lon_max"] - tall["lon_min"]) == 50.0
    assert (tall["lat_max"] - tall["lat_min"]) == 150.0
    # (a) lon_span=200 / lat_span=40 -> NON globale; il vecchio server diceva
    # GLOBALE a torto: senza il fix la regione 'domain' non veniva renderizzata.
    assert _old_transposed_predicate(wide) is True   # il bug
    assert mod._is_global_bbox(wide) is False        # il fix
    # (b) lon_span=50 / lat_span=150 -> GLOBALE; il vecchio server diceva NON
    # globale a torto: senza il fix la regione 'domain' veniva renderizzata.
    assert _old_transposed_predicate(tall) is False  # il bug
    assert mod._is_global_bbox(tall) is True         # il fix


def test_global_classification_golden_10_real_domains():
    """Golden: i 10 domini reali -> 7 globali + 3 non-globali (PRIMA e DOPO)."""
    golden = dict((d, True) for d in GLOBAL_BBOX)
    golden.update((d, False) for d in NON_GLOBAL_BBOX)
    assert len(golden) == 10
    assert sum(1 for v in golden.values() if v) == 7
    assert sum(1 for v in golden.values() if not v) == 3
    for dom, expected in sorted(golden.items()):
        t = GLOBAL_BBOX[dom] if dom in GLOBAL_BBOX else NON_GLOBAL_BBOX[dom]
        assert mod._is_global_bbox(_bbox_dict(t)) is expected, dom
        # Il vecchio predicato trasposto classificava ugualmente questi 10
        # (nessun cambio): il bug si vedeva SOLO sui casi discriminanti sopra.
        assert _old_transposed_predicate(_bbox_dict(t)) is expected, dom


def _grid_for_bbox(bbox, nx, ny):
    """Manifest grid coerente col bbox (stessa convenzione del motore)."""
    return {"nx": nx, "ny": ny,
            "lonMin": bbox["lon_min"], "latMin": bbox["lat_min"],
            "dx": (bbox["lon_max"] - bbox["lon_min"]) / (nx - 1),
            "dy": (bbox["lat_max"] - bbox["lat_min"]) / (ny - 1)}


def _write_frame_om(path, arrays, valid_unix, ref_unix):
    """Scrive un .om multi-variabile REALE (stessa API del motore engine)."""
    import numpy as _np
    from omfiles import OmFileWriter
    w = OmFileWriter.at_path(path)
    children = []
    for name, a in arrays.items():
        arr = _np.ascontiguousarray(_np.asarray(a, dtype=_np.float32))
        chunks = [min(32, int(arr.shape[0])), min(32, int(arr.shape[1]))]
        children.append(w.write_array(arr, chunks=chunks, scale_factor=1.0,
                                      add_offset=0.0,
                                      compression="pfor_delta_2d_int16",
                                      name=name))
    children.append(w.write_scalar("EPSG:4326", name="crs_wkt"))
    children.append(w.write_scalar("lon,lat", name="coordinates"))
    children.append(w.write_scalar(int(valid_unix), name="valid_time"))
    children.append(w.write_scalar(int(ref_unix), name="forecast_reference_time"))
    root = w.write_group(name="", children=children)
    w.close(root)


def test_domain_region_render_gate_follows_aligned_predicate(tmp_path):
    """Gate end-to-end: 'domain' renderizzata se NON globale, saltata se globale.

    Usa frame .om REALI (OmFileWriter) + il renderer REALE render_domain:
    nessun mock del sistema sotto test.
    """
    np = pytest.importorskip("numpy")
    pytest.importorskip("PIL")
    pytest.importorskip("omfiles")
    mod._require_render_deps()
    var = "temperature_2m"
    vt = "2026-10-14T16:00Z"
    fname = mod.valid_time_to_filename(vt) + ".om"
    scales = mod.load_color_scales()
    # Normalizzazione CLI esattamente come main() fa prima di render_domain
    # (main riga 951-960: _split_csv su domains/vars/themes).
    args = mod.parse_args([])
    args.domains = mod._split_csv(args.domains)
    args.vars = mod._split_csv(args.vars)
    args.themes = mod._split_csv(args.themes) or ["light"]
    assert args.themes == ["light", "dark"]
    regions = {"domain": None}          # solo la regione sotto test

    nx, ny = 20, 10

    def _setup(name, bbox):
        d = str(tmp_path / "in" / name)
        os.makedirs(d, exist_ok=True)
        _write_frame_om(os.path.join(d, fname),
                        {var: np.full((ny, nx), 15.5, dtype=np.float32)},
                        1792012800, 1792009200)
        _write_json(os.path.join(d, "latest.json"),
                    {"domain": name, "bbox": bbox,
                     "grid": _grid_for_bbox(bbox, nx, ny),
                     "frames": [{"file": fname, "valid_time": vt}],
                     "var_order": [var], "valid_times": [vt]})
        return d

    # (a) NON globale (lon_span=200) -> la regione 'domain' DEVE essere
    # renderizzata: col vecchio predicato trasposto qui veniva SALTATA
    # (render_domain avrebbe tornato "no region intersection").
    a_bbox = DISCRIMINATING["wide_lon"]
    out_a = str(tmp_path / "out_a")
    got = mod.render_domain(_setup("wide_lon", a_bbox), out_a, scales, args, regions)
    rendered, skipped, err, contrib, frames_done, bytes_written = got
    assert err is None, err                 # NON saltata
    assert rendered > 0, got
    assert frames_done == 1 and bytes_written > 0, got
    assert "domain" in contrib and contrib["domain"]["variables"][var]
    vdir_a = os.path.join(out_a, "domain", "wide_lon", var)
    wpath_a = os.path.join(vdir_a, mod.valid_time_to_filename(vt) + ".webp")
    assert os.path.isfile(wpath_a) and os.path.getsize(wpath_a) > 0
    assert os.path.isfile(os.path.join(vdir_a, "latest.json"))
    # Anche il theme dark e' stato scritto (default CLI light,dark).
    assert os.path.isfile(os.path.join(vdir_a, "dark",
                                       mod.valid_time_to_filename(vt) + ".webp"))

    # (b) GLOBALE (lat_span=150) -> la regione 'domain' DEVE essere SALTATA:
    # col vecchio predicato trasposto qui veniva renderizzata a torto.
    b_bbox = DISCRIMINATING["tall_lat"]
    out_b = str(tmp_path / "out_b")
    got = mod.render_domain(_setup("tall_lat", b_bbox), out_b, scales, args, regions)
    rendered, skipped, err, contrib, frames_done, bytes_written = got
    assert err == "no region intersection", got
    assert rendered == 0 and frames_done == 0 and bytes_written == 0, got
    assert contrib == {}
    assert not os.path.isdir(os.path.join(out_b, "domain"))
