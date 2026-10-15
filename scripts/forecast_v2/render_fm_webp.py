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

This script is standalone: it only needs ``numpy``, ``omfiles`` and ``Pillow``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import math
import os
import shutil
import sys
import traceback

import numpy as np

try:
    from PIL import Image
except Exception as exc:  # pragma: no cover
    print("FATAL: Pillow (PIL) is required: %s" % exc, file=sys.stderr)
    raise

try:
    from omfiles import OmFileReader
except Exception as exc:  # pragma: no cover
    print("FATAL: omfiles is required: %s" % exc, file=sys.stderr)
    raise


HERE = os.path.dirname(os.path.abspath(__file__))
COLOR_SCALES_PATH = os.path.join(HERE, "color_scales.json")

WEBP_VERSION = "fm2-webp-1"
MERC_LAT_LIMIT = 85.05112878

# Rendered regions, keyed by region id. Tuple is (lat_min, lon_min, lat_max,
# lon_max) -- the SAME ordering convention used by the engine (V2_BBOX /
# DOMAIN_BBOX). These are the ONLY areas the raster fast-path ships.
REGIONS = {
    "italia": (36.5, 6.5, 47.2, 18.6),
    "europa": (34.0, -25.0, 72.0, 45.0),
}


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


def build_row_resampler(grid: dict):
    """Return f(arr)->arr remapping rows to uniform Mercator spacing (row0=north).

    Horizontal axis is left untouched (lon is linear in Mercator), so the
    output width stays ``nx`` and height stays ``ny``.
    """
    ny = int(grid["ny"])
    dy = float(grid["dy"])
    lat_min = float(grid["latMin"])
    lat_north = lat_min + (ny - 1) * dy

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


def save_webp(rgba: np.ndarray, path: str, quality: int, lossless: bool) -> int:
    img = Image.fromarray(rgba, mode="RGBA")
    kwargs = {"format": "WEBP", "method": 6}
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

    Returns ``(ok_frames, skipped, err, contrib)`` where ``contrib`` is a dict
    ``{region: {"bbox":..., "grid":..., "bounds":..., "variables": {...}}}``
    used by the caller to assemble each region ``index.json``.
    """
    manifest_path = os.path.join(domain_dir, "latest.json")
    if not os.path.isfile(manifest_path):
        return 0, 0, "no latest.json", {}

    manifest = read_manifest(manifest_path)
    grid = manifest.get("grid")
    bbox = manifest.get("bbox")
    frames = manifest.get("frames") or []
    if not isinstance(grid, dict) or not isinstance(bbox, dict) or not frames:
        return 0, 0, "manifest missing grid/bbox/frames", {}

    if args.limit_frames and args.limit_frames > 0:
        frames = frames[:args.limit_frames]

    domain = os.path.basename(os.path.normpath(domain_dir))
    if args.domains and domain not in args.domains:
        return 0, 0, None, {}

    # Pre-compute the per-region crop + Mercator resampler for this domain.
    crops = {}
    for rname in regions:
        cr = region_crop(bbox, grid, regions[rname])
        if cr is None:
            continue
        row0, row1, col0, col1, cgrid = cr
        crops[rname] = {
            "row0": row0, "row1": row1, "col0": col0, "col1": col1,
            "grid": cgrid,
            "resample": build_row_resampler(cgrid),
        }
    if not crops:
        return 0, 0, "no region intersection", {}

    # Decide variable set: CLI filter, else manifest var_order print-safe.
    var_order = [v for v in (manifest.get("var_order") or []) if isinstance(v, str)]
    requested = args.vars if args.vars else var_order

    rendered = 0
    skipped = 0
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
                        size = save_webp(rgba, wpath, args.quality, args.lossless)
                        rendered += 1

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

    if not any(var_frames[r] for r in var_frames):
        return rendered, skipped, "nothing rendered", {}

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

    return rendered, skipped, None, contrib


def write_region_index(out_root: str, region_name: str, region_bbox, domains: dict,
                       themes) -> None:
    """Write ``<out>/<region>/index.json`` summarising the published domains."""
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
                    help="comma-separated region allow-list (default: italia,europa)")
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

    try:
        scales = load_color_scales()
    except Exception as exc:
        print("FATAL: cannot load color scales: %s" % exc, file=sys.stderr)
        return 2

    domains = []
    for name in sorted(os.listdir(in_dir)):
        ddir = os.path.join(in_dir, name)
        if os.path.isdir(ddir) and os.path.isfile(os.path.join(ddir, "latest.json")):
            domains.append(ddir)
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
        try:
            ok, skipped, err, contrib = render_domain(ddir, out_dir, scales, args, regions)
        except Exception:
            print("  ! domain %s crashed:" % domain, file=sys.stderr)
            traceback.print_exc()
            continue
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
    if os.path.isfile(COLOR_SCALES_PATH):
        try:
            shutil.copyfile(COLOR_SCALES_PATH, os.path.join(out_dir, "color_scales.json"))
        except OSError as exc:
            print("  ! could not copy color_scales.json: %s" % exc, file=sys.stderr)

    print("[done] frames written: %d -> %s" % (total_ok, out_dir))
    return 0 if any_domain_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
