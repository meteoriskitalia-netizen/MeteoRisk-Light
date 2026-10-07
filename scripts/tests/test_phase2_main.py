# -*- coding: utf-8 -*-
"""Test B2 (PHASE2_VERSION 0.4.0) - main.py: layer PER CANDIDATO.

Covers gli interventi B2 nel layer Fase 2:
  - _candidate_position / _candidate_window / _crop (coordinate inverse
    lon/lat -> pixel via models.RasterData.lonlat_to_pixel, intersezione
    con la griglia, FAIL SAFE fuori raster);
  - _structure_at_window (crop + unita': ETM metri -> km, POH frazione -> %);
  - _hook_for_candidate (footprint sulle ultime N griglie);
  - _phase2_evaluate su bundle sintetico con fetch/fulmini SOSTITUITI
    (nessuna rete): sub-valori sul singolo candidato, I/O una volta sola,
    status e components_status dai candidati;
  - output._phase2_summary per latest.json.

Le griglie sono sintetiche ma con CRS EPSG:4326 reale (pyproj) e affine
reale (affine), cosi' la catena di coordinate e' quella produttiva.
Nessun valore dedotto a mano: i valori attesi provengono dall'esecuzione
di radar_engine.main reale (scratch_b2_values.py, radarvenv Python 3.13).
"""
import copy
import os
import sys

import numpy as np
import pytest
from affine import Affine
from pyproj import CRS as PjCRS
from pyproj import Transformer

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.dirname(_TESTS_DIR)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import radar_engine.main as eng                   # noqa: E402
import radar_engine.models as models              # noqa: E402
import radar_engine.output as eng_out             # noqa: E402
import radar_engine.phase2.hook as hook_mod       # noqa: E402
import radar_engine.phase2.lightning as ltg_mod   # noqa: E402
import radar_engine.phase2.vertical_structure as vs_mod  # noqa: E402

# Griglia sintetica: lon 8..18, lat 36..46 (100 px da 0.1 gradi).
LON0, LAT0, PX, ROWS, COLS = 8.0, 46.0, 0.1, 100, 100
IN_GRID = (13.0, 41.0)
OUT_GRID = (30.0, 41.0)


def _frame(data=None, value=5.0, lon0=LON0, lat0=LAT0, px=PX):
    """RasterData sintetico (CRS EPSG:4326 reale, affine reale)."""
    if data is None:
        data = np.full((ROWS, COLS), float(value))
    data = np.asarray(data, dtype="float64")
    rows, cols = data.shape
    transform = Affine(px, 0.0, lon0, 0.0, -px, lat0)
    crs = PjCRS.from_epsg(4326)
    valid = np.isfinite(data)
    return models.RasterData(
        data=data, valid_mask=valid, nodata_mask=~valid, rows=rows, cols=cols,
        crs=crs, crs_wkt=crs.to_wkt(), transform=transform,
        pixel_area_km2=(px * 111.0) ** 2, time_ms=0,
        time_iso="2026-10-07T00:00:00Z", source_path="synthetic",
        declared_nodata=None,
        geo_transform=Transformer.from_crs(crs, "EPSG:4326", always_xy=True))


def _bundle():
    bundle = models.EngineBundle("ok", "2026-10-07T00:00:00Z", "test")
    bundle.frames = [_frame(), _frame()]
    bundle.supercells = [
        {"id": 1, "position": [IN_GRID[0], IN_GRID[1]], "ssi": 80.0},
        {"id": 2, "position": [OUT_GRID[0], OUT_GRID[1]], "ssi": 70.0},
        {"id": 3, "ssi": 90.0},                 # nessuna posizione
    ]
    # deepcopy: merge_config e' shallow, mutare la config toccherebbe CONFIG
    cfg = copy.deepcopy(eng.load_config())
    cfg["phase2"]["environment"]["enabled"] = False   # nessuna rete in test
    return bundle, cfg


# ---------------------------------------------------------------------------
# Intervento 2/3: posizione e finestra per-candidato
# ---------------------------------------------------------------------------

def test_candidate_position_valid_and_invalid():
    assert eng._candidate_position({"position": [13.0, 41.0]}) == (13.0, 41.0)
    assert eng._candidate_position({"position": ["13.0", "41.0"]}) == (13.0, 41.0)
    assert eng._candidate_position({"position": [13.0]}) is None
    assert eng._candidate_position({}) is None
    assert eng._candidate_position({"position": [float("nan"), 41.0]}) is None
    assert eng._candidate_position({"position": ["x", 41.0]}) is None
    assert eng._candidate_position({"position": []}) is None


def test_candidate_window_contains_center_and_fits_radius():
    frame = _frame()
    lon, lat = IN_GRID
    win = eng._candidate_window(frame, lon, lat, 45.0)
    assert win is not None
    r0, r1, c0, c1 = win
    assert 0 <= r0 <= r1 < ROWS
    assert 0 <= c0 <= c1 < COLS
    assert r1 - r0 >= 1 and c1 - c0 >= 1        # finestra, non un punto
    cr, cc = frame.lonlat_to_pixel(lon, lat)
    assert r0 <= cr <= r1 and c0 <= cc <= c1
    # ogni angolo della finestra resta entro radius + 1 px (margine ~15 km)
    for rr in (r0, r1):
        for cidx in (c0, c1):
            plon, plat = frame.pixel_to_lonlat(rr, cidx)
            assert ltg_mod.haversine_km(plon, plat, lon, lat) <= 45.0 + 25.0


def test_candidate_window_clamped_on_grid_edge():
    frame = _frame()
    win = eng._candidate_window(frame, 8.05, 45.95, 45.0)
    assert win is not None
    r0, r1, c0, c1 = win
    assert r0 == 0 and c0 == 0                  # clamp sul bordo griglia
    assert r1 < ROWS and c1 < COLS


def test_candidate_window_out_of_grid_returns_none():
    frame = _frame()
    assert eng._candidate_window(frame, 30.0, 41.0, 45.0) is None   # lon fuori
    assert eng._candidate_window(frame, 13.0, 60.0, 45.0) is None   # lat fuori
    assert eng._candidate_window(frame, 13.0, 41.0, 0.0) is None    # raggio 0
    assert eng._candidate_window(frame, 13.0, 41.0, -5.0) is None   # negativo
    assert eng._crop((2, 3, 4, 5), np.arange(100).reshape(10, 10)).shape == (2, 2)


# ---------------------------------------------------------------------------
# Intervento 3/4: struttura sul crop (unita') e hook sul footprint
# ---------------------------------------------------------------------------

def test_structure_at_window_absent_products_returns_none():
    empty = dict(vil=None, etm=None, poh=None, low=None, high=None)
    assert eng._structure_at_window(empty, (0, 10, 0, 10), vs_mod) == (None, None)
    assert eng._structure_at_window(empty, None, vs_mod) == (None, None)


def test_structure_at_window_all_nan_returns_none():
    rd = _frame(np.full((20, 20), np.nan))
    products = dict(vil=rd, etm=None, poh=None, low=None, high=None)
    assert eng._structure_at_window(products, (0, 10, 0, 10), vs_mod) == (None, None)


def test_structure_at_window_crops_and_converts_units():
    products = dict(vil=_frame(np.full((20, 20), 20.0)),
                    etm=_frame(np.full((20, 20), 9000.0)),    # 9000 m
                    poh=_frame(np.full((20, 20), 0.35)),      # frazione 0.35
                    low=None, high=None)
    score, feats = eng._structure_at_window(products, (0, 19, 0, 19), vs_mod)
    assert score is not None and 0.0 <= score <= 100.0
    assert feats["etm_max"] == 9.0                # metri -> km
    assert feats["poh_max"] == 35.0               # frazione -> %
    assert feats["vil_max"] == 20.0
    # crop piu' piccolo: stesse unita', sottoinsieme della griglia
    score2, feats2 = eng._structure_at_window(products, (5, 9, 5, 9), vs_mod)
    assert feats2["etm_max"] == 9.0
    assert feats2["vil_max"] == 20.0
    assert 0.0 <= score2 <= 100.0
    # nessuna conversione applicherebbe le soglie a valori 1000x (25.0 vs 50.0
    # sul caso scratch): il crop convertito NON deve coincidere con l'ingrosso
    nan = np.full((1, 3), np.nan)
    assert vs_mod.structure_score(np.array([[20.0, 20.0, 20.0]]),
                                  np.array([[2000.0, 9000.0, 12500.0]]),
                                  np.array([[0.0, 0.35, 0.72]]),
                                  nan, nan) == 25.0
    assert vs_mod.structure_score(np.array([[20.0, 20.0, 20.0]]),
                                  np.array([[2.0, 9.0, 12.5]]),
                                  np.array([[0.0, 35.0, 72.0]]),
                                  nan, nan) == 50.0


def test_hook_for_candidate_footprint_window():
    frames = [_frame(), _frame(), _frame()]
    hcfg = {"history_frames": 3, "dbz_threshold": 45.0}
    # griglia uniforme 5 dBZ -> nessun uncino -> score reale 0.0 (non None)
    assert eng._hook_for_candidate(frames, IN_GRID[0], IN_GRID[1], 45.0,
                                   hcfg, hook_mod) == 0.0
    # candidato fuori griglia -> nessuna finestra -> None
    assert eng._hook_for_candidate(frames, OUT_GRID[0], OUT_GRID[1], 45.0,
                                   hcfg, hook_mod) is None
    # nessun frame -> None
    assert eng._hook_for_candidate([], IN_GRID[0], IN_GRID[1], 45.0,
                                   hcfg, hook_mod) is None
    # disco interamente fuori griglia (lon 19 - 0.54 gradi > 18) -> None
    assert eng._hook_for_candidate(frames, 19.0, 41.0, 45.0,
                                   hcfg, hook_mod) is None
    # disco PARZIALMENTE dentro il bordo (lon 18.4) -> finestra clampata,
    # componente calcolata sulla parte dentro la griglia (0.0, non None)
    assert eng._hook_for_candidate(frames, 18.4, 41.0, 45.0,
                                   hcfg, hook_mod) == 0.0


# ---------------------------------------------------------------------------
# Intervento 6: _phase2_evaluate su bundle (I/O sostituiti)
# ---------------------------------------------------------------------------

def test_phase2_evaluate_per_candidate_no_network(monkeypatch):
    fetch_calls = []
    ltg_calls = []

    def fake_fetch_frames(config, product=None, max_frames=None):
        fetch_calls.append(product)
        return [], [], None                     # nessun prodotto DPC

    def fake_fetch_ltg(epoch_ms=None, **kw):
        ltg_calls.append(epoch_ms)
        return {"strikes": [(13.0, 41.0)] * 100, "count": 100}

    monkeypatch.setattr("radar_engine.fetch.fetch_frames", fake_fetch_frames)
    monkeypatch.setattr("radar_engine.phase2.lightning.fetch_ltg", fake_fetch_ltg)

    bundle, cfg = _bundle()
    eng._phase2_evaluate(bundle, cfg)
    result = bundle.phase2

    # I/O una volta per prodotto / per slot, MAI per candidato
    assert fetch_calls == ["VIL", "ETM", "POH"]
    assert len(ltg_calls) == 4

    c1, c2, c3 = bundle.supercells
    # candidato 1 in griglia: hook + fulmini presenti, struttura/env/OT no
    assert c1["hook"] == 0.0
    assert c1["lightning"] == 70.0
    assert c1["structure"] is None
    assert c1["structure_features"] is None
    assert c1["env"] is None
    assert c1["env_scp"] is None
    assert c1["ot"] is None
    assert c1["ssi_v2"] == 64
    # candidato 2 fuori griglia: nessun layer per-candidato, base NON penalizzata
    assert c2["hook"] is None
    assert c2["lightning"] is None
    assert c2["structure"] is None
    assert c2["ssi_v2"] == 70
    # candidato 3 senza posizione: tutte le chiavi presenti, tutti None
    for key in ("hook", "structure", "structure_features", "env", "env_scp",
                "ot", "lightning", "ssi_v2"):
        assert key in c3
    assert c3["hook"] is None and c3["lightning"] is None
    assert c3["ssi_v2"] == 90

    # status dai componenti presenti su almeno un candidato
    assert result["version"] == "0.4.0"
    assert result["components_status"] == {"hook": 1, "structure": 0,
                                           "env": 0, "ot": 0, "lightning": 1}
    assert result["status"] == "partial"
    assert "phase2 structure: no products available" in result["warnings"]
    assert "ot_unavailable:dn_to_kelvin_non_configurato" in result["warnings"]
    assert "candidate without valid position" in " ".join(result["warnings"])


def test_phase2_evaluate_all_layers_absent_keeps_candidates(monkeypatch):
    monkeypatch.setattr("radar_engine.fetch.fetch_frames",
                        lambda config, product=None, max_frames=None: ([], [], None))
    monkeypatch.setattr("radar_engine.phase2.lightning.fetch_ltg",
                        lambda epoch_ms=None, **kw: {"strikes": [], "count": 0})
    bundle, cfg = _bundle()
    bundle.frames = []                          # nessuna griglia -> hook None
    eng._phase2_evaluate(bundle, cfg)
    result = bundle.phase2
    # nessun componente oltre la base -> unavailable, ma i candidati restano
    assert result["status"] == "unavailable"
    assert result["components_status"] == {"hook": 0, "structure": 0,
                                           "env": 0, "ot": 0, "lightning": 0}
    assert [c["ssi_v2"] for c in bundle.supercells] == [80, 70, 90]


# ---------------------------------------------------------------------------
# Intervento 7: riepilogo per latest.json
# ---------------------------------------------------------------------------

class _Bundle(object):
    pass


def test_phase2_summary_best_candidate_and_status():
    b = _Bundle()
    b.phase2 = {"status": "partial",
                "components_status": {"hook": 1, "structure": 0, "env": 0,
                                      "ot": 0, "lightning": 2}}
    b.supercells = [
        {"ssi_v2": 70, "hook": 0.0, "structure": None, "env": None,
         "ot": None, "lightning": 55.0},
        {"ssi_v2": 85, "hook": 12.0, "structure": None, "env": None,
         "ot": None, "lightning": 70.0},
        {"ssi_v2": None},
    ]
    out = eng_out._phase2_summary(b)
    assert out["phase2_status"] == "partial"
    assert out["phase2_ssi_v2_max"] == 85
    assert out["phase2_components_status"] == b.phase2["components_status"]
    best = out["phase2_best_candidate"]
    assert best["ssi_v2"] == 85
    assert best["hook"] == 12.0
    assert best["structure"] is None
    assert best["lightning"] == 70.0


def test_phase2_summary_fallback_and_unavailable():
    b = _Bundle()
    b.phase2 = {"status": "unavailable"}          # senza components_status
    b.supercells = [{"ssi_v2": None, "hook": None, "structure": None,
                     "env": None, "ot": None, "lightning": None}]
    out = eng_out._phase2_summary(b)
    assert out["phase2_status"] == "unavailable"
    assert out["phase2_ssi_v2_max"] is None
    assert out["phase2_components_status"] == {"hook": 0, "structure": 0,
                                               "env": 0, "ot": 0,
                                               "lightning": 0}
    assert out["phase2_best_candidate"]["ssi_v2"] is None
    # bundle senza phase2 -> solo lo status, nessuna eccezione
    assert eng_out._phase2_summary(_Bundle()) == {"phase2_status": "unavailable"}


def test_phase2_summary_is_importable_from_output_helpers():
    # il riepilogo entra in latest.json senza dati raw (celle/lat/lon)
    b = _Bundle()
    b.phase2 = {"status": "ok", "components_status": {"hook": 1, "structure": 1,
                                                      "env": 1, "ot": 0,
                                                      "lightning": 1}}
    b.supercells = [{"ssi_v2": 77, "hook": 10.0, "structure": 20.0,
                     "env": 30.0, "ot": None, "lightning": 40.0}]
    out = eng_out._phase2_summary(b)
    assert set(out) == {"phase2_status", "phase2_ssi_v2_max",
                        "phase2_components_status", "phase2_best_candidate"}
    assert "position" not in out["phase2_best_candidate"]
