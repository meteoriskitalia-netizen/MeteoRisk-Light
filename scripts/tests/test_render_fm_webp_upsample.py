# -*- coding: utf-8 -*-
"""Test dell'UPSAMPLING bilineare dei VALORI in render_fm_webp.py.

Il modulo viene caricato via importlib per percorso (scripts/forecast_v2 non e'
un package), con la stessa convenzione di test_render_fm_webp_index.py. NESSUN
mock del sistema sotto test: build_row_resampler, make_lookup e parse_args sono
quelli reali; l'oracolo storico e' copiato VERBATIM dall'algoritmo pre-modifica
(rows-only Mercator, due tap r0/r1, sostituzioni NaN) cosi' da provare che
scale == 1 resta byte-identico.
"""
import importlib.util
import os

import pytest

np = pytest.importorskip("numpy")

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RENDERER_PATH = os.path.join(REPO_ROOT, "scripts", "forecast_v2", "render_fm_webp.py")


def _load_renderer():
    spec = importlib.util.spec_from_file_location("render_fm_webp_upsample_ut",
                                                  RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mod = _load_renderer()

# Grid sintetico realistico (latitudini 35..42.5): il remap Mercator sposta le
# righe in modo non banale, quindi l'oracolo "scale 1" ha senso.
GRID = {"nx": 8, "ny": 16, "lonMin": 6.5, "latMin": 35.0, "dx": 0.5, "dy": 0.5}


def _sample_frame():
    """Frame float32 (ny, nx) con DUE bande di righe NaN.

    Le bande (righe 4..7 e 12..15) sono scelte per esercitare TUTTI i rami NaN
    dell'algoritmo storico: entrambi-i-tap finiti, top finito/bot NaN, top NaN/
    bot finito e entrambi NaN (riga di output trasparente).
    """
    rng = np.random.default_rng(1234)
    arr = rng.uniform(-30.0, 40.0, size=(GRID["ny"], GRID["nx"])).astype(np.float32)
    arr[4:8, :] = np.nan
    arr[12:16, :] = np.nan
    return arr


def _historical_oracle(grid):
    """Copia VERBATIM dell'algoritmo storico (rows-only Mercator, due tap)."""
    ny = int(grid["ny"])
    dy = float(grid["dy"])
    lat_min = float(grid["latMin"])
    lat_north = lat_min + (ny - 1) * dy

    if ny <= 1:
        return lambda arr: np.asarray(arr, dtype=np.float32).copy()

    my_south = mod._merc_y(lat_min)
    my_north = mod._merc_y(lat_north)
    span = my_north - my_south

    positions = np.empty(ny, dtype=np.float64)
    for r in range(ny):
        t = r / (ny - 1)
        lat = mod._inv_merc_y(my_north - t * span)
        positions[r] = (lat - lat_min) / dy

    r0 = np.clip(np.floor(positions).astype(np.int64), 0, ny - 1)
    r1 = np.clip(r0 + 1, 0, ny - 1)
    w = (positions - r0)[:, None]

    def resample(arr: np.ndarray) -> np.ndarray:
        a = np.asarray(arr, dtype=np.float32)
        if a.shape[0] != ny:
            raise ValueError("row count %d != grid ny %d" % (a.shape[0], ny))
        top = a[r0, :]
        bot = a[r1, :]
        out = top * (1.0 - w) + bot * w
        fa = np.isfinite(top)
        fb = np.isfinite(bot)
        out = np.where(~fa & fb, bot, out)
        out = np.where(fa & ~fb, top, out)
        out = np.where(~fa & ~fb, np.float32(np.nan), out)
        return out.astype(np.float32)

    return resample


def test_scale1_is_identical_to_historical_oracle():
    arr = _sample_frame()
    got = mod.build_row_resampler(GRID, 1)(arr)
    exp = _historical_oracle(GRID)(arr)
    assert got.shape == exp.shape == (GRID["ny"], GRID["nx"])
    assert np.array_equal(got, exp, equal_nan=True)
    # Il frame contiene davvero dei NaN: la comparazione sopra e' significativa.
    assert np.isnan(got).any()
    assert np.array_equal(np.isnan(got), np.isnan(exp))


def test_scale1_default_arg_identical():
    arr = _sample_frame()
    default = mod.build_row_resampler(GRID)(arr)
    explicit = mod.build_row_resampler(GRID, 1)(arr)
    assert np.array_equal(default, explicit, equal_nan=True)


def test_upsample_shape_and_pixels_are_exact_palette_colors():
    nx = GRID["nx"]
    ny = GRID["ny"]
    scale = 3
    arr = _sample_frame()
    out = mod.build_row_resampler(GRID, scale)(arr)
    assert out.shape == ((ny - 1) * scale + 1, (nx - 1) * scale + 1)

    scales = mod.load_color_scales()
    var = "temperature_2m"
    lk = mod.make_lookup(scales, var, "light")
    assert lk is not None
    variant = mod._scale_variant(scales, var, "light")
    bps = np.asarray(variant["breakpoints"], dtype=np.float64)
    cols = np.asarray(variant["colors"], dtype=np.float64)
    rgb = np.clip(cols[:, 0:3], 0.0, 255.0).astype(np.uint8)
    alpha = np.clip(np.rint(cols[:, 3] * 255.0), 0.0, 255.0).astype(np.uint8)

    rgba = lk(out)
    finite = np.isfinite(out)
    flat = out[finite].astype(np.float64)
    idx = np.searchsorted(bps, flat, side="right") - 1
    np.clip(idx, 0, bps.shape[0] - 1, out=idx)
    expected = np.concatenate([rgb[idx], alpha[idx][:, None]], axis=1)
    got = rgba[finite]
    # Ogni pixel FINITO indossa ESATTAMENTE una riga della palette (nessun blend).
    assert np.array_equal(got, expected)
    # I pixel NON finiti sono trasparenti (0,0,0,0).
    assert np.all(rgba[~finite] == 0)


def test_nan_not_propagated():
    nx = GRID["nx"]
    ny = GRID["ny"]
    scale = 3
    arr = _sample_frame()
    # Un solo NaN INTERNO alla cella (i, j).
    i, j = ny // 2, nx // 2
    arr = np.ones((ny, nx), dtype=np.float32)
    arr[i, j] = np.nan
    out = mod.build_row_resampler(GRID, scale)(arr)
    nan = np.isnan(out)
    assert nan.any()  # il NaN sorgente si vede (non viene "spalmato" ovunque)

    # Ricostruzione INDIPENDENTE (formula Mercator standard, la stessa usata per
    # la proiezione) delle righe/colonne sorgente campionate da ogni pixel.
    out_ny, out_nx = out.shape
    dy = float(GRID["dy"])
    lat_min = float(GRID["latMin"])
    my_s = mod._merc_y(lat_min)
    my_n = mod._merc_y(lat_min + (ny - 1) * dy)
    span = my_n - my_s
    row_pos = np.empty(out_ny, dtype=np.float64)
    for r in range(out_ny):
        t = r / (out_ny - 1)
        row_pos[r] = (mod._inv_merc_y(my_n - t * span) - lat_min) / dy
    r0 = np.clip(np.floor(row_pos).astype(np.int64), 0, ny - 1)
    c0 = np.clip((np.arange(out_nx) // scale).astype(np.int64), 0, nx - 1)
    touched_rows = (r0 == i) | (r0 == i - 1)
    touched_cols = (c0 == j) | (c0 == j - 1)
    allowed = touched_rows[:, None] & touched_cols[None, :]
    # Ogni NaN di output sta in un pixel il cui stencil tocca la cella (i, j).
    assert not (nan & ~allowed).any()
    assert (nan & allowed).any()
    # I bordi restano FINITI (il NaN e' interno).
    assert not np.isnan(out[0, :]).any()
    assert not np.isnan(out[-1, :]).any()
    assert not np.isnan(out[:, 0]).any()
    assert not np.isnan(out[:, -1]).any()


def test_auto_policy_and_resolve():
    f = mod._upsample_factor
    assert f(25, 22, 512, 8) == 8
    assert f(49, 43, 512, 8) == 8
    assert f(605, 202, 512, 8) == 3
    assert f(1215, 746, 512, 8) == 1
    assert f(1440, 721, 512, 8) == 1
    assert f(881, 449, 512, 8) == 2

    auto = mod.parse_args(["--upsample", "auto"])
    assert mod.resolve_upsample(auto, 605, 202) == 3
    assert mod.resolve_upsample(auto, 1215, 746) == 1
    assert mod.resolve_upsample(auto, 25, 22) == 8

    one = mod.parse_args(["--upsample", "1"])
    assert mod.resolve_upsample(one, 605, 202) == 1
    three = mod.parse_args(["--upsample", "3"])
    assert mod.resolve_upsample(three, 1215, 746) == 3

    # 'auto' con --upsample-max abbassato limita il fattore.
    capped = mod.parse_args(["--upsample", "auto", "--upsample-max", "4"])
    assert mod.resolve_upsample(capped, 25, 22) == 4


def test_cli_defaults():
    a = mod.parse_args([])
    assert a.upsample == "auto"
    assert a.upsample_max == 8
    assert mod.UPSAMPLE_TARGET == 512
    assert mod.UPSCALE_MAX_DEFAULT == 8
