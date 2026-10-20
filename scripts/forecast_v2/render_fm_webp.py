#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Server-side pre-render of forecast-v2 (.om) frames to WebP.

Reads the engine output layout ``<in>/<domain>/latest.json`` plus the
per-frame ``<VALID>.om`` files, dequantizes each requested variable with the
omfiles reader, remaps rows into Web Mercator vertical spacing, colors every
pixel with the exact client scale (``color_scales.json``, WML v0.2.2) and
writes, for every requested REGION and THEME, ``<out>/<region>/<domain>/<var>/<VALID>.webp``
(the ``dark`` theme nested under ``.../<var>/dark/``) together with a per-variable
``latest.json`` (one per theme) and a per-region ``index.json`` consumable by the
client's WebP path.

Regions
-------
The raster fast-path only ships two overlapping areas (``italia`` and
``europa``), NOT the whole globe. For each (region, domain) pair the rendered
extent is the intersection of the domain coverage ``bbox`` (engine-written,
manifest key ``bbox``) with the region bbox; pairs that do not intersect are
skipped. The raw frame array is cropped to that intersection on the domain grid
and only THEN resampled to Mercator, so each WebP spans exactly its own
``imageOverlay`` bounds.

Design notes (empirically confirmed against the live engine output):
  * ``OmFileReader.read_array`` returns values already in real units
    (precip mm, temperaure degC, cloud %) -- no manual scale/offset.
  * Frame arrays are 2D ``(ny, nx)`` float32 with row 0 = south (ascending
    latitude); scalar children carry ``crs_wkt``/``coordinates``/times.
  * The grid used for reprojection comes from the manifest ``grid`` block
    (already the domain's cropped ``out_grid``), NOT from the source ``crs_wkt``.
  * Color lookup is the DISCRETE "breakpoint" step used by the WML worker:
    ``colors[max(0, last_index_with_breakpoint <= value)]`` with raster alpha
    scaled to 0..255 and NaN forced transparent.

This script is standalone: the RENDER path only needs ``numpy``, ``omfiles`` and
``Pillow``. The heavy deps are captured lazily at import time (the ImportError is
stored, not raised) so the ``--index-only`` mode (pure JSON/filesystem) works even
in a bare Python env; the render path re-FATALS them on first use via
``_require_render_deps()`` with the same messages as before.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import shutil
import sys
import time
import traceback

# Heavy deps (numpy/PIL/omfiles): capture ImportError at import time WITHOUT
# raising, so `--index-only` (no rendering) imports cleanly in a bare env.
# The render path calls _require_render_deps() and gets the original FATAL.
_IMPORT_ERRORS: dict = {}

try:
    import numpy as np
except Exception as exc:  # pragma: no cover
    np = None
    _IMPORT_ERRORS["numpy"] = exc

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover
    Image = None
    _IMPORT_ERRORS["PIL"] = exc

try:
    from omfiles import OmFileReader
except Exception as exc:  # pragma: no cover
    OmFileReader = None
    _IMPORT_ERRORS["omfiles"] = exc


def _require_render_deps() -> None:
    """FATAL (same messages as the old eager import) if a render dep is missing."""
    if not _IMPORT_ERRORS:
        return
    for name, exc in _IMPORT_ERRORS.items():
        if name == "PIL":
            print("FATAL: Pillow (PIL) is required: %s" % exc, file=sys.stderr)
        elif name == "omfiles":
            print("FATAL: omfiles is required: %s" % exc, file=sys.stderr)
        else:
            print("FATAL: %s is required: %s" % (name, exc), file=sys.stderr)
    raise next(iter(_IMPORT_ERRORS.values()))


HERE = os.path.dirname(os.path.abspath(__file__))
COLOR_SCALES_PATH = os.path.join(HERE, "color_scales.json")

WEBP_VERSION = "fm2-webp-1"
MERC_LAT_LIMIT = 85.05112878
UPSAMPLE_TARGET = 512
UPSCALE_MAX_DEFAULT = 8

# Rendered regions, keyed by region id. Tuple is (lat_min, lon_min, lat_max,
# lon_max) -- the SAME ordering convention used by the engine (V2_BBOX /
# DOMAIN_BBOX). ``None`` is the "domain" sentinel: the region spans the whole
# engine manifest bbox (the domain's own coverage), NOT a fixed window.
REGIONS = {
    "italia": (36.5, 6.5, 47.2, 18.6),
    "europa": (34.0, -25.0, 72.0, 45.0),
    "domain": None,
}

# The 'domain' sentinel region is rendered only for NON-global domains: a global
# domain's "whole domain" is the globe, which the client already reaches through
# its other regions and which is far too large to publish as a single WebP.
# "Global" is decided by _is_global_bbox, whose thresholds are ALIGNED to the
# client predicate fmIsGlobalDomain (mri-light-1.2.4.11.html:
# lon_span > 300 || lat_span > 120) -- the same classification on both sides.
DOMAIN_REGION = "domain"
DOMAIN_REGION_SKIP_GLOBAL = True


# --------------------------------------------------------------------------- #
# Color scales
# --------------------------------------------------------------------------- #
def load_color_scales(path: str = COLOR_SCALES_PATH) -> dict:
    """Load the WML v0.2.2 color scales (tolerates a UTF-8 BOM)."""
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    scales = data.get("scales")
    if not isinstance(scales, dict) or not scales:
        raise ValueError("color_scales.json: missing/empty 'scales' map")
    return scales


def _scale_variant(scales: dict, var: str, theme: str):
    """Return the {breakpoints, colors, unit} dict for var/theme, or None.

    Every shipped scale is nested ``{dark:{...}, light:{...}}``; a flat scale
    (no variants) is also tolerated defensively.
    """
    entry = scales.get(var)
    if entry is None:
        return None
    if "breakpoints" in entry and "colors" in entry:
        return entry  # already flat
    variant = entry.get(theme) or entry.get("light") or entry.get("dark")
    if variant is None or "breakpoints" not in variant or "colors" not in variant:
        return None
    return variant


def make_lookup(scales: dict, var: str, theme: str):
    """Build a vectorized RGBA lookup for one variable/theme.

    Returns ``None`` when no scale exists for the variable.
    """
    variant = _scale_variant(scales, var, theme)
    if variant is None:
        return None

    bps = np.asarray(variant["breakpoints"], dtype=np.float64)
    cols = np.asarray(variant["colors"], dtype=np.float64)
    if bps.ndim != 1 or cols.ndim != 2 or cols.shape[1] < 4 or cols.shape[0] != bps.shape[0]:
        raise ValueError("color_scales.json: malformed scale for %r" % var)

    rgb = np.clip(cols[:, 0:3], 0.0, 255.0).astype(np.uint8)
    alpha = np.clip(np.rint(cols[:, 3] * 255.0), 0.0, 255.0).astype(np.uint8)
    n = bps.shape[0]

    def lookup(values: np.ndarray) -> np.ndarray:
        """values: 2D float array -> RGBA uint8 (h, w, 4), NaN transparent."""
        flat = np.asarray(values, dtype=np.float64).reshape(-1)
        idx = np.searchsorted(bps, flat, side="right") - 1
        np.clip(idx, 0, n - 1, out=idx)
        out = np.empty((flat.size, 4), dtype=np.uint8)
        out[:, 0:3] = rgb[idx]
        out[:, 3] = alpha[idx]
        bad = ~np.isfinite(flat)
        if bad.any():
            out[bad, :] = 0
        return out.reshape(values.shape[0], values.shape[1], 4)

    return lookup


# --------------------------------------------------------------------------- #
# Reprojection (latitude -> Web Mercator vertical spacing)
# --------------------------------------------------------------------------- #
def _merc_y(lat_deg: float) -> float:
    lat = max(-MERC_LAT_LIMIT, min(MERC_LAT_LIMIT, float(lat_deg)))
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0))


def _inv_merc_y(y: float) -> float:
    return math.degrees(2.0 * math.atan(math.exp(y)) - math.pi / 2.0)


def _upsample_factor(nx: int, ny: int, target: int, max_scale: int) -> int:
    """Integer upsampling factor for a grid, clamped to [1, max_scale]."""
    shorter = min(int(nx), int(ny))
    if shorter < 1 or target <= 0:
        return 1
    return max(1, min(int(max_scale), int(math.ceil(target / shorter))))


def resolve_upsample(args, nx: int, ny: int) -> int:
    """Resolve the per-crop upsampling factor from the parsed CLI arguments."""
    up = getattr(args, "upsample", "auto")
    if isinstance(up, str) and up.strip().lower() == "auto":
        return _upsample_factor(nx, ny, UPSAMPLE_TARGET,
                                int(getattr(args, "upsample_max", UPSCALE_MAX_DEFAULT)))
    try:
        return max(1, int(up))
    except (TypeError, ValueError):
        return 1


def build_row_resampler(grid: dict, scale: int = 1):
    """Return f(arr)->arr remapping rows to uniform Mercator spacing (row0=north).

    ``scale == 1`` is the historical row-only resampler: width stays ``nx``,
    height stays ``ny`` and the result is byte-identical. ``scale > 1``
    BILINEARLY interpolates the DATA VALUES onto an
    ``((nx-1)*scale+1, (ny-1)*scale+1)`` grid (Mercator rows, linear-lon
    columns) BEFORE the discrete breakpoint lookup, so band edges become smooth
    while every pixel still wears an EXACT palette color (never a blend). Fully
    vectorized with numpy.

    NaN policy: a non-finite sample never gains a color. At ``scale > 1`` a
    pixel is emitted only when ALL of its bilinear source corners are finite;
    otherwise it is NaN (transparent). At ``scale == 1`` the historical two-tap
    rule is kept verbatim.
    """
    ny = int(grid["ny"])
    nx = int(grid["nx"])
    dy = float(grid["dy"])
    lat_min = float(grid["latMin"])
    lat_north = lat_min + (ny - 1) * dy
    scale = max(1, int(scale))

    if scale <= 1:
        if ny <= 1:
            return lambda arr: np.asarray(arr, dtype=np.float32).copy()
        my_south = _merc_y(lat_min)
        my_north = _merc_y(lat_north)
        span = my_north - my_south
        positions = np.empty(ny, dtype=np.float64)
        for r in range(ny):
            t = r / (ny - 1)
            lat = _inv_merc_y(my_north - t * span)
            positions[r] = (lat - lat_min) / dy
        r0 = np.clip(np.floor(positions).astype(np.int64), 0, ny - 1)
        r1 = np.clip(r0 + 1, 0, ny - 1)
        w = (positions - r0)[:, None]

        def resample_1(arr: np.ndarray) -> np.ndarray:
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

        return resample_1

    out_nx = (nx - 1) * scale + 1
    out_ny = (ny - 1) * scale + 1
    col_pos = np.arange(out_nx, dtype=np.float64) / scale
    c0 = np.clip(col_pos.astype(np.int64), 0, nx - 1)
    c1 = np.clip(c0 + 1, 0, nx - 1)
    wc = (col_pos - c0).astype(np.float32)
    my_south = _merc_y(lat_min)
    my_north = _merc_y(lat_north)
    span = my_north - my_south
    row_pos = np.empty(out_ny, dtype=np.float64)
    for r in range(out_ny):
        t = r / (out_ny - 1)
        lat = _inv_merc_y(my_north - t * span)
        row_pos[r] = (lat - lat_min) / dy
    r0 = np.clip(np.floor(row_pos).astype(np.int64), 0, ny - 1)
    r1 = np.clip(r0 + 1, 0, ny - 1)
    wr = (row_pos - r0).astype(np.float32)
    wR0 = (1.0 - wr)[:, None]
    wR1 = wr[:, None]
    wC0 = (1.0 - wc)[None, :]
    wC1 = wc[None, :]
    one = np.float32(1.0)
    cover_eps = np.float32(1e-6)

    def resample_up(arr: np.ndarray) -> np.ndarray:
        a = np.asarray(arr, dtype=np.float32)
        if a.shape[0] != ny or a.shape[1] != nx:
            raise ValueError("shape %r != grid (ny=%d, nx=%d)"
                             % (tuple(a.shape), ny, nx))
        finite = np.isfinite(a)
        af = np.where(finite, a, np.float32(0.0))
        v00 = af[r0][:, c0]; v01 = af[r0][:, c1]
        v10 = af[r1][:, c0]; v11 = af[r1][:, c1]
        m00 = finite[r0][:, c0]; m01 = finite[r0][:, c1]
        m10 = finite[r1][:, c0]; m11 = finite[r1][:, c1]
        num = (v00 * wC0 + v01 * wC1) * wR0 + (v10 * wC0 + v11 * wC1) * wR1
        den = (m00 * wC0 + m01 * wC1) * wR0 + (m10 * wC0 + m11 * wC1) * wR1
        ok = den >= (one - cover_eps)
        safe = np.where(den > 0, den, one)
        out = np.where(ok, num / safe, np.float32(np.nan))
        return out.astype(np.float32)

    return resample_up


# --------------------------------------------------------------------------- #
# Region cropping
# --------------------------------------------------------------------------- #
def region_crop(bbox: dict, grid: dict, region):
    """Intersect a domain coverage bbox with a region bbox on the domain grid.

    ``bbox`` is the engine manifest object ``{lat_min,lon_min,lat_max,lon_max}``;
    ``grid`` is the engine ``out_grid`` ``{nx,ny,lonMin,latMin,dx,dy}`` (already
    the domain's crop, matching the frame array shape). ``region`` is a
    ``(lat_min, lon_min, lat_max, lon_max)`` tuple.

    Returns ``(row0, row1, col0, col1, cropped_grid)`` (half-open slices on the
    raw ``(ny, nx)`` array, row 0 = south) or ``None`` when the two boxes do not
    intersect. The cropped grid keeps the SAME ``dx``/``dy`` and snaps to grid
    columns/rows so the result is an exact sub-grid of the domain frame.
    """
    lat_min = max(float(bbox["lat_min"]), float(region[0]))
    lon_min = max(float(bbox["lon_min"]), float(region[1]))
    lat_max = min(float(bbox["lat_max"]), float(region[2]))
    lon_max = min(float(bbox["lon_max"]), float(region[3]))
    if lat_max <= lat_min or lon_max <= lon_min:
        return None

    ny = int(grid["ny"])
    nx = int(grid["nx"])
    dy = float(grid["dy"])
    dx = float(grid["dx"])
    g_lat0 = float(grid["latMin"])
    g_lon0 = float(grid["lonMin"])
    if dx <= 0 or dy <= 0 or nx < 1 or ny < 1:
        return None

    eps = 1e-9
    row0 = int(math.ceil((lat_min - g_lat0) / dy - eps))
    row1 = int(math.floor((lat_max - g_lat0) / dy + eps)) + 1
    col0 = int(math.ceil((lon_min - g_lon0) / dx - eps))
    col1 = int(math.floor((lon_max - g_lon0) / dx + eps)) + 1

    row0 = max(0, min(row0, ny))
    row1 = max(0, min(row1, ny))
    col0 = max(0, min(col0, nx))
    col1 = max(0, min(col1, nx))
    if row1 <= row0 or col1 <= col0:
        return None

    cropped = {
        "nx": col1 - col0,
        "ny": row1 - row0,
        "latMin": round(g_lat0 + row0 * dy, 10),
        "lonMin": round(g_lon0 + col0 * dx, 10),
        "dx": dx,
        "dy": dy,
    }
    return row0, row1, col0, col1, cropped


def resolve_region_bbox(region_value, domain_bbox):
    """Resolve a REGIONS entry to a bbox tuple.

    ``None`` is the 'domain' sentinel: it maps to the engine manifest bbox (the
    whole domain grid). Any other value is already a ``(lat_min, lon_min,
    lat_max, lon_max)`` tuple and is returned unchanged.
    """
    if region_value is None:
        return (float(domain_bbox["lat_min"]), float(domain_bbox["lon_min"]),
                float(domain_bbox["lat_max"]), float(domain_bbox["lon_max"]))
    return region_value


def _is_global_bbox(bbox: dict) -> bool:
    """True when the domain spans (near) the whole globe.

    Thresholds ALIGNED to the client predicate ``fmIsGlobalDomain``
    (``mri-light-1.2.4.11.html``): ``lon_span > 300 || lat_span > 120``.
    """
    return ((float(bbox["lon_max"]) - float(bbox["lon_min"])) > 300.0
            or (float(bbox["lat_max"]) - float(bbox["lat_min"])) > 120.0)


def cropped_bbox(cropped_grid: dict) -> dict:
    """Grid-aligned bbox object for a cropped grid (pixel-centre extremes)."""
    ny = int(cropped_grid["ny"])
    nx = int(cropped_grid["nx"])
    dy = float(cropped_grid["dy"])
    dx = float(cropped_grid["dx"])
    lat_min = float(cropped_grid["latMin"])
    lon_min = float(cropped_grid["lonMin"])
    return {
        "lat_min": round(lat_min, 10),
        "lon_min": round(lon_min, 10),
        "lat_max": round(lat_min + (ny - 1) * dy, 10),
        "lon_max": round(lon_min + (nx - 1) * dx, 10),
    }


def image_bounds(bbox: dict) -> list:
    """Leaflet ``imageOverlay`` bounds ``[[south, west], [north, east]]``."""
    return [
        [float(bbox["lat_min"]), float(bbox["lon_min"])],
        [float(bbox["lat_max"]), float(bbox["lon_max"])],
    ]


# --------------------------------------------------------------------------- #
# Manifest / frame helpers
# --------------------------------------------------------------------------- #
def valid_time_to_filename(valid_time: str) -> str:
    """``2026-10-14T16:00Z`` -> ``2026-10-14T1600`` (matche engine naming)."""
    return str(valid_time).replace(":", "").replace("Z", "")


def read_manifest(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def frame_array_names(reader) -> list:
    names = []
    for i in range(int(reader.num_children)):
        child = reader.get_child_by_index(i)
        if not child.is_scalar:
            names.append(child.name)
    return names


def read_frame_array(reader, name: str) -> np.ndarray:
    child = reader.get_child_by_name(name)
    if child is None:
        raise KeyError(name)
    arr = child.read_array((slice(None), slice(None)))
    return np.ascontiguousarray(np.asarray(arr, dtype=np.float32))


def write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def save_webp(rgba: np.ndarray, path: str, quality: int, lossless: bool,
              method: int = 6) -> int:
    img = Image.fromarray(rgba, mode="RGBA")
    # method=6 (default) keeps the historical output byte-compatible; CI passes 4.
    kwargs = {"format": "WEBP", "method": int(method)}
    if lossless:
        kwargs["lossless"] = True
    else:
        kwargs["quality"] = int(quality)
    img.save(path, **kwargs)
    return os.path.getsize(path)


def prune_dir(dir_path: str, keep_files: set) -> int:
    """Delete stale ``*.webp`` in ``dir_path`` that are not in ``keep_files``.

    Returns the number of removed files. Mirrors the engine prune concept so the
    published tree does not grow without bound.
    """
    removed = 0
    try:
        entries = os.listdir(dir_path)
    except OSError:
        return 0
    for name in entries:
        if not name.lower().endswith(".webp"):
            continue
        if name in keep_files:
            continue
        try:
            os.remove(os.path.join(dir_path, name))
            removed += 1
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------- #
# Core render loop
# --------------------------------------------------------------------------- #
def _utcnow_iso() -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _theme_var_dir(out_root: str, region: str, domain: str, var: str, theme: str) -> str:
    """Output directory for one region/domain/var/theme.

    ``light`` (the client default) keeps the plain path; ``dark`` is nested in a
    ``dark/`` subdir so both themes can coexist in a single published root.
    """
    base = os.path.join(out_root, region, domain, var)
    return os.path.join(base, "dark") if theme == "dark" else base


def render_domain(domain_dir: str, out_root: str, scales: dict, args, regions: dict) -> tuple:
    """Render one domain into every intersecting region, for every requested theme.

    Returns ``(ok_frames, skipped, err, contrib, frames_done, bytes_written)``
    where ``contrib`` is a dict
    ``{region: {"bbox":..., "grid":..., "bounds":..., "variables": {...}}}``
    used by the caller to assemble each region ``index.json``, ``frames_done``
    is the number of ``.om`` frames actually opened/processed and
    ``bytes_written`` the total size of the WebP files produced (observability).
    """
    manifest_path = os.path.join(domain_dir, "latest.json")
    if not os.path.isfile(manifest_path):
        return 0, 0, "no latest.json", {}, 0, 0

    manifest = read_manifest(manifest_path)
    grid = manifest.get("grid")
    bbox = manifest.get("bbox")
    frames = manifest.get("frames") or []
    if not isinstance(grid, dict) or not isinstance(bbox, dict) or not frames:
        return 0, 0, "manifest missing grid/bbox/frames", {}, 0, 0

    if args.limit_frames and args.limit_frames > 0:
        frames = frames[:args.limit_frames]

    domain = os.path.basename(os.path.normpath(domain_dir))
    if args.domains and domain not in args.domains:
        return 0, 0, None, {}, 0, 0

    # Pre-compute the per-region crop + Mercator resampler for this domain.
    crops = {}
    for rname in regions:
        if (rname == DOMAIN_REGION and DOMAIN_REGION_SKIP_GLOBAL
                and _is_global_bbox(bbox)):
            continue
        rbbox = resolve_region_bbox(regions[rname], bbox)
        cr = region_crop(bbox, grid, rbbox)
        if cr is None:
            continue
        row0, row1, col0, col1, cgrid = cr
        scale = resolve_upsample(args, cgrid["nx"], cgrid["ny"])
        crops[rname] = {
            "row0": row0, "row1": row1, "col0": col0, "col1": col1,
            "grid": cgrid,
            "resample": build_row_resampler(cgrid, scale),
            "scale": scale,
        }
    if not crops:
        return 0, 0, "no region intersection", {}, 0, 0
    print("  [upsample] %s"
          % ", ".join("%s x%d" % (r, crops[r]["scale"]) for r in crops))

    # Decide variable set: CLI filter, else manifest var_order print-safe.
    var_order = [v for v in (manifest.get("var_order") or []) if isinstance(v, str)]
    requested = args.vars if args.vars else var_order

    rendered = 0
    skipped = 0
    frames_done = 0
    bytes_written = 0
    themes = args.themes
    # rname -> var -> theme -> [frame records]
    var_frames = {rname: {} for rname in crops}
    var_meta = {}                                # var -> unit

    for frame in frames:
        fname = frame.get("file")
        valid_time = frame.get("valid_time")
        if not fname or not valid_time:
            skipped += 1
            continue
        frame_path = os.path.join(domain_dir, fname)
        if not os.path.isfile(frame_path):
            print("  ! missing frame %s" % frame_path, file=sys.stderr)
            skipped += 1
            continue

        try:
            reader = OmFileReader(frame_path)
        except Exception as exc:
            print("  ! cannot open %s: %s" % (frame_path, exc), file=sys.stderr)
            skipped += 1
            continue

        try:
            present = frame_array_names(reader)
            names = [v for v in requested if v in present] if requested else present
            if args.vars:
                missing = [v for v in requested if v not in present]
                if missing:
                    print("  ~ %s: vars absent in frame: %s"
                          % (valid_time, ",".join(missing)), file=sys.stderr)

            base_name = valid_time_to_filename(valid_time)
            for var in names:
                theme_lookups = {}
                for t in themes:
                    lk = make_lookup(scales, var, t)
                    if lk is not None:
                        theme_lookups[t] = lk
                if not theme_lookups:
                    print("  ~ no color scale for %r (skip)" % var, file=sys.stderr)
                    continue
                try:
                    arr = read_frame_array(reader, var)
                except Exception as exc:
                    print("  ! %s/%s read failed: %s" % (valid_time, var, exc),
                          file=sys.stderr)
                    continue

                if var not in var_meta:
                    variant = _scale_variant(scales, var, themes[0])
                    var_meta[var] = variant.get("unit") if variant else None

                for rname, crop in crops.items():
                    sub = arr[crop["row0"]:crop["row1"], crop["col0"]:crop["col1"]]
                    arr_out = crop["resample"](sub)
                    for t, lk in theme_lookups.items():
                        rgba = lk(arr_out)

                        vdir = _theme_var_dir(out_root, rname, domain, var, t)
                        os.makedirs(vdir, exist_ok=True)
                        wpath = os.path.join(vdir, base_name + ".webp")
                        size = save_webp(rgba, wpath, args.quality, args.lossless,
                                         args.method)
                        rendered += 1
                        bytes_written += size

                        var_frames[rname].setdefault(var, {}).setdefault(t, []).append({
                            "file": base_name + ".webp",
                            "valid_time": valid_time,
                            "bytes": size,
                        })
        finally:
            try:
                reader.close()
            except Exception:
                pass
        frames_done += 1

    if not any(var_frames[r] for r in var_frames):
        return rendered, skipped, "nothing rendered", {}, frames_done, bytes_written

    generated_at = _utcnow_iso()

    contrib = {}
    for rname, frames_by_var in var_frames.items():
        if not frames_by_var:
            continue
        cgrid = crops[rname]["grid"]
        cbbox = cropped_bbox(cgrid)
        grid_summary = {"nx": int(cgrid["nx"]), "ny": int(cgrid["ny"])}
        bounds = image_bounds(cbbox)
        variables = {}
        for var, recs_by_theme in frames_by_var.items():
            unit = var_meta.get(var)
            entry = {
                "unit": unit,
                "dir": "%s/%s" % (domain, var),
                "themes": {},
            }
            for theme, recs in recs_by_theme.items():
                recs.sort(key=lambda r: r["valid_time"])
                keep = {r["file"] for r in recs}
                vdir = _theme_var_dir(out_root, rname, domain, var, theme)
                prune_dir(vdir, keep)
                vmanifest = {
                    "version": WEBP_VERSION,
                    "generated_at": generated_at,
                    "region": rname,
                    "domain": domain,
                    "variable": var,
                    "theme": theme,
                    "unit": unit,
                    "bbox": cbbox,
                    "bounds": bounds,
                    "grid": grid_summary,
                    "frames": recs,
                    "valid_times": [r["valid_time"] for r in recs],
                }
                write_json(os.path.join(vdir, "latest.json"), vmanifest)
                sub = "%s/%s" % (domain, var)
                if theme == "dark":
                    sub = "%s/dark" % sub
                entry["themes"][theme] = {
                    "count": len(recs),
                    "valid_times": [r["valid_time"] for r in recs],
                    "dir": sub,
                    "manifest": "%s/latest.json" % sub,
                }
            variables[var] = entry
        contrib[rname] = {
            "bbox": cbbox,
            "bounds": bounds,
            "grid": grid_summary,
            "variables": variables,
        }

    return rendered, skipped, None, contrib, frames_done, bytes_written


def write_region_index(out_root: str, region_name: str, region_bbox, domains: dict,
                       themes) -> None:
    """Write ``<out>/<region>/index.json`` summarising the published domains."""
    if region_bbox is None:
        # 'domain' sentinel: there is no single fixed region bbox -> use the
        # union of the indexed domains' own bboxes (fallback: the whole globe).
        lat_min = lon_min = lat_max = lon_max = None
        for d in domains.values():
            bb = (d or {}).get("bbox") or {}
            if bb.get("lat_min") is None:
                continue
            lat_min = bb["lat_min"] if lat_min is None else min(lat_min, bb["lat_min"])
            lon_min = bb["lon_min"] if lon_min is None else min(lon_min, bb["lon_min"])
            lat_max = bb["lat_max"] if lat_max is None else max(lat_max, bb["lat_max"])
            lon_max = bb["lon_max"] if lon_max is None else max(lon_max, bb["lon_max"])
        if lat_min is None:
            lat_min, lon_min, lat_max, lon_max = -90.0, -180.0, 90.0, 180.0
        region_bbox = (lat_min, lon_min, lat_max, lon_max)
    theme_list = list(themes)
    index = {
        "version": WEBP_VERSION,
        "generated_at": _utcnow_iso(),
        "region": region_name,
        "theme": theme_list[0] if theme_list else "light",
        "themes": theme_list,
        "bbox": {
            "lat_min": float(region_bbox[0]),
            "lon_min": float(region_bbox[1]),
            "lat_max": float(region_bbox[2]),
            "lon_max": float(region_bbox[3]),
        },
        "domains": domains,
    }
    rdir = os.path.join(out_root, region_name)
    os.makedirs(rdir, exist_ok=True)
    write_json(os.path.join(rdir, "index.json"), index)


def copy_color_scales(out_dir: str) -> None:
    """Publish the exact color scales the renderer used (single source of truth).

    Shared by the render path and the ``--index-only`` path so both publish the
    identical file with the identical code.
    """
    if os.path.isfile(COLOR_SCALES_PATH):
        try:
            shutil.copyfile(COLOR_SCALES_PATH, os.path.join(out_dir, "color_scales.json"))
        except OSError as exc:
            print("  ! could not copy color_scales.json: %s" % exc, file=sys.stderr)


# --------------------------------------------------------------------------- #
# --index-only: rebuild region indexes from the existing per-var manifests
# --------------------------------------------------------------------------- #
def _valid_var_manifest(obj) -> bool:
    """Structural validation of a per-var WebP ``latest.json`` (index-only)."""
    if not isinstance(obj, dict):
        return False
    valid_times = obj.get("valid_times")
    frames = obj.get("frames")
    if not isinstance(valid_times, list) or not valid_times:
        return False
    if not isinstance(frames, list) or not frames:
        return False
    if not all(isinstance(f, dict) and f.get("file") for f in frames):
        return False
    if obj.get("bounds") is None or obj.get("grid") is None:
        return False
    return True


def rebuild_index(in_dir: str, out_dir: str, domain_list, region_names, themes) -> int:
    """Rebuild ``<out>/<region>/index.json`` from the existing per-var manifests.

    ``--index-only`` mode (single-writer merge job): NO rendering, pure
    JSON/filesystem (works without numpy/Pillow/omfiles). ``domain_list`` is the
    EXPLICIT list of successfully rendered shard domains. For every region only
    the domains that (a) are present under ``in_dir`` (engine tree:
    ``<in>/<domain>/latest.json``) and (b) have at least ONE structurally valid
    var manifest are indexed; every valid theme dir is PRUNED against the
    keep-set of its OWN manifest (only ``*.webp``, by file name). Invalid var
    manifests are skipped ENTIRELY (NO prune, NO index entry). The index schema
    and writer are the render path's ones (``write_region_index``) to avoid any
    schema drift. Returns the number of region indexes written (0 = publish
    nothing).
    """
    written = 0
    for rname in region_names:
        region_bbox = REGIONS[rname]
        index_domains = {}
        region_themes = set()
        for domain in domain_list:
            if not os.path.isfile(os.path.join(in_dir, domain, "latest.json")):
                print("[index] %s/%s: escluso: non presente in --in" % (rname, domain))
                continue
            region_var_root = os.path.join(out_dir, rname, domain)
            if not os.path.isdir(region_var_root):
                print("[index] %s/%s: escluso: nessun output renderizzato"
                      % (rname, domain))
                continue

            variables = {}
            dom_bbox = dom_bounds = dom_grid = None
            for var in sorted(os.listdir(region_var_root)):
                var_dir = os.path.join(region_var_root, var)
                if not os.path.isdir(var_dir):
                    continue
                theme_manifests = {}          # theme -> (theme_dir, manifest)
                invalid = False
                for theme in themes:
                    tdir = _theme_var_dir(out_dir, rname, domain, var, theme)
                    mpath = os.path.join(tdir, "latest.json")
                    if not os.path.isfile(mpath):
                        continue              # theme not rendered for this var
                    try:
                        vman = read_manifest(mpath)
                    except Exception as exc:
                        print("  ! %s/%s/%s/%s: manifest illeggibile (%s): "
                              "variabile saltata (nessun prune)"
                              % (rname, domain, var, theme, exc), file=sys.stderr)
                        invalid = True
                        break
                    if not _valid_var_manifest(vman):
                        print("  ! %s/%s/%s/%s: manifest non valido: variabile "
                              "saltata (nessun prune)"
                              % (rname, domain, var, theme), file=sys.stderr)
                        invalid = True
                        break
                    theme_manifests[theme] = (tdir, vman)
                if invalid:
                    continue                  # NO prune, NO index entry
                if not theme_manifests:
                    print("  ~ %s/%s/%s: nessun latest.json per alcun tema (skip)"
                          % (rname, domain, var), file=sys.stderr)
                    continue

                entry = {"unit": None, "dir": "%s/%s" % (domain, var), "themes": {}}
                for theme, (tdir, vman) in sorted(theme_manifests.items()):
                    keep = {f["file"] for f in vman["frames"]}
                    prune_dir(tdir, keep)
                    sub = "%s/%s" % (domain, var)
                    if theme == "dark":
                        sub = "%s/dark" % sub
                    # Same theme-entry shape as the render path's writer.
                    entry["themes"][theme] = {
                        "count": len(vman["valid_times"]),
                        "valid_times": list(vman["valid_times"]),
                        "dir": sub,
                        "manifest": "%s/latest.json" % sub,
                    }
                    region_themes.add(theme)
                    if entry["unit"] is None:
                        entry["unit"] = vman.get("unit")
                    # bbox/bounds/grid: first valid var manifest of this domain in
                    # this region (identical across vars by construction).
                    if dom_bbox is None:
                        dom_bbox = vman.get("bbox")
                        dom_bounds = vman.get("bounds")
                        dom_grid = vman.get("grid")
                variables[var] = entry

            if not variables:
                print("[index] %s/%s: escluso: nessun manifest variabile valido"
                      % (rname, domain), file=sys.stderr)
                continue
            index_domains[domain] = {
                "bbox": dom_bbox,
                "bounds": dom_bounds,
                "grid": dom_grid,
                "variables": variables,
            }

        if not index_domains:
            print("[index] %s: NESSUN dominio valido -> index.json NON scritto"
                  % rname)
            continue
        theme_list = [t for t in themes if t in region_themes]
        write_region_index(out_dir, rname, region_bbox, index_domains, theme_list)
        print("[index] %s: index.json scritto (%d domini: %s)"
              % (rname, len(index_domains), ", ".join(sorted(index_domains))))
        written += 1
    return written


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _split_csv(value):
    if not value:
        return None
    items = [p.strip() for p in str(value).split(",") if p.strip()]
    return items or None


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Pre-render forecast-v2 .om frames to colored WebP per region."
    )
    ap.add_argument("--in", dest="in_dir", default="data/fm2",
                    help="engine output root containing <domain>/latest.json")
    ap.add_argument("--out", dest="out_dir", default="data/fm2-rendered",
                    help="output root (client default base is fm2-rendered/)")
    ap.add_argument("--regions", default=None,
                    help="comma-separated region allow-list (default: "
                         "italia,europa,domain)")
    ap.add_argument("--domains", default=None,
                    help="comma-separated domain allow-list (default: all found)")
    ap.add_argument("--vars", default=None,
                    help="comma-separated variable allow-list (default: manifest order)")
    ap.add_argument("--themes", default="light,dark",
                    help="comma-separated color-scale themes (default: light,dark; "
                         "dark is written under <var>/dark/)")
    ap.add_argument("--quality", type=int, default=90, help="WebP quality (lossy)")
    ap.add_argument("--lossless", action="store_true", help="write lossless WebP")
    ap.add_argument("--limit-frames", type=int, default=0,
                    help="render at most N frames per domain (debug)")
    ap.add_argument("--method", type=int, default=6,
                    help="Pillow WebP encode method (0-6; 6=max compression/slowest)")
    ap.add_argument("--upsample", default="auto",
                    help="image upsampling: 'auto' targets ~%d px on the shorter axis "
                         "(capped by --upsample-max) or an integer factor >=1 "
                         "(1 = no upsampling, historical output)" % UPSAMPLE_TARGET)
    ap.add_argument("--upsample-max", type=int, default=UPSCALE_MAX_DEFAULT,
                    help="upper bound for the 'auto' upsampling factor "
                         "(default: %d)" % UPSCALE_MAX_DEFAULT)
    ap.add_argument("--index-only", action="store_true",
                    help="rebuild region index.json from existing per-var latest.json "
                         "manifests; no rendering (works without numpy/Pillow/omfiles)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    args.domains = _split_csv(args.domains)
    args.vars = _split_csv(args.vars)

    themes = _split_csv(args.themes) or ["light"]
    bad = [t for t in themes if t not in ("light", "dark")]
    if bad:
        print("FATAL: unknown theme(s) %s (known: light,dark)" % ",".join(bad),
              file=sys.stderr)
        return 2
    args.themes = themes

    region_names = _split_csv(args.regions) or list(REGIONS.keys())
    regions = {}
    for name in region_names:
        if name not in REGIONS:
            print("FATAL: unknown region %r (known: %s)"
                  % (name, ",".join(REGIONS)), file=sys.stderr)
            return 2
        regions[name] = REGIONS[name]

    in_dir = os.path.abspath(args.in_dir)
    out_dir = os.path.abspath(args.out_dir)

    if not os.path.isdir(in_dir):
        print("FATAL: --in not a directory: %s" % in_dir, file=sys.stderr)
        return 2

    # Domain dirs carrying an engine manifest (the --in gate, both modes).
    in_domains = []
    for name in sorted(os.listdir(in_dir)):
        ddir = os.path.join(in_dir, name)
        if os.path.isdir(ddir) and os.path.isfile(os.path.join(ddir, "latest.json")):
            in_domains.append(name)

    if args.index_only:
        # --index-only: rebuild the region indexes ONLY (no rendering, no heavy
        # deps). --domains is the EXPLICIT list of successfully rendered shard
        # domains; an empty list publishes nothing (exit 1).
        if not in_domains:
            print("FATAL: no domain dirs with latest.json under %s" % in_dir,
                  file=sys.stderr)
            return 2
        if not args.domains:
            print("FATAL: --index-only requires a non-empty --domains "
                  "(comma-separated rendered shard domains)", file=sys.stderr)
            return 2
        os.makedirs(out_dir, exist_ok=True)
        written = rebuild_index(in_dir, out_dir, args.domains, region_names, themes)
        if not written:
            print("[index] nessuna regione ha un dominio valido: nulla pubblicato.",
                  file=sys.stderr)
            return 1
        copy_color_scales(out_dir)
        print("[done] region index written: %d -> %s" % (written, out_dir))
        return 0

    _require_render_deps()

    try:
        scales = load_color_scales()
    except Exception as exc:
        print("FATAL: cannot load color scales: %s" % exc, file=sys.stderr)
        return 2

    domains = [os.path.join(in_dir, name) for name in in_domains]
    if args.domains:
        domains = [d for d in domains if os.path.basename(os.path.normpath(d)) in args.domains]
    if not domains:
        print("FATAL: no domain dirs with latest.json under %s" % in_dir, file=sys.stderr)
        return 2

    os.makedirs(out_dir, exist_ok=True)

    total_ok = 0
    any_domain_ok = False
    index_domains = {rname: {} for rname in regions}

    for ddir in domains:
        domain = os.path.basename(os.path.normpath(ddir))
        print("[domain] %s" % domain)
        t0 = time.perf_counter()
        try:
            ok, skipped, err, contrib, nframes, nbytes = render_domain(
                ddir, out_dir, scales, args, regions)
        except Exception:
            print("  ! domain %s crashed:" % domain, file=sys.stderr)
            traceback.print_exc()
            continue
        # Observability: per-domain timing/size (no frame caps anywhere).
        print("[render] %s: %d frame, %d webp, %d bytes, %.1fs"
              % (domain, nframes, ok, nbytes, time.perf_counter() - t0))
        if ok:
            any_domain_ok = True
            total_ok += ok
            print("  ok=%d skipped=%d%s"
                  % (ok, skipped, (" (%s)" % err) if err else ""))
        else:
            print("  ok=0 skipped=%d (%s)" % (skipped, err or "empty"))
        for rname, entry in contrib.items():
            index_domains[rname][domain] = entry

    for rname in regions:
        write_region_index(out_dir, rname, regions[rname], index_domains[rname], args.themes)

    # Publish the exact color scales the renderer used so the client legend and
    # the raster agree (single source of truth in the cache root).
    copy_color_scales(out_dir)

    print("[done] frames written: %d -> %s" % (total_ok, out_dir))
    return 0 if any_domain_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
