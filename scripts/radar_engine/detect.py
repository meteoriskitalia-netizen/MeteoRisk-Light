#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — detect.py

Convective Cell Detection (Fase 1):

  VMI dBZ
   |-> valid data mask (preprocess: only DATA VALIDITY)
   |-> meteorological threshold (dbz_threshold, configurabile)
   |-> morphological opening/closing (anti-rumore / anti-frammentazione)
   |-> connected components (8-connessi)
   |-> remove small components (min_area_km2, EXPERIMENTAL)
   |-> extract cell descriptors

Le metriche per cella sono DESCRITTORI GEOMETRICI (area, centroide, bbox, max/
mean/p90 dBZ, eccentricità, solidity, compactness): NON vengono interpretate
come firma supercellulare in Fase 1 (niente hook-echo/mesocyclone).
"""

import numpy as np
import scipy.ndimage as ndi
from scipy.spatial import ConvexHull, QhullError

from . import models

_EPS = 1e-12


def _struct(radius, connectivity_8=True):
    """Structuring element per morfologia/labelling: quadrato (2r+1)^2."""
    if radius <= 0:
        # Base di connettività: 8 (full 3x3) o 4 (plus).
        return np.ones((3, 3), dtype=bool) \
            if connectivity_8 else ndi.generate_binary_structure(2, 2)
    return np.ones((2 * radius + 1, 2 * radius + 1), dtype=bool)


def _eccentricity(mask):
    """Eccentricità del componente (0 cerchio -> 1 segmento), da momenti 2nd ord."""
    rows, cols = np.nonzero(mask)
    if rows.size < 3:
        return 0.0
    mr, mc = rows.mean(), cols.mean()
    m20 = ((rows - mr) ** 2).mean()
    m02 = ((cols - mc) ** 2).mean()
    m11 = ((rows - mr) * (cols - mc)).mean()
    cov = np.array([[m20, m11], [m11, m02]])
    eig = np.linalg.eigvalsh(cov)
    lam1, lam2 = max(eig[1], _EPS), max(eig[0], _EPS)
    if lam1 <= _EPS:
        return 0.0
    return float(np.sqrt(max(0.0, 1.0 - lam2 / lam1)))


def _solidity(mask):
    """solidity = area / area hull convessa del componente."""
    rows, cols = np.nonzero(mask)
    n = rows.size
    if n < 3:
        return 1.0
    pts = np.column_stack((cols, rows))
    try:
        hull = ConvexHull(pts)
        hull_area = hull.volume  # 2D simplex area
    except QhullError:
        return 1.0
    if hull_area <= 0:
        return 1.0
    return float(min(1.0, n / hull_area))


def _perimeter_px(mask):
    """Prossimità di perimetro: pixel del componente con almeno un vicino fuori."""
    se = np.ones((3, 3), dtype=bool)
    eroded = ndi.binary_erosion(mask, structure=se)
    return int((mask & ~eroded).sum())


def _compactness(mask):
    """compactness = P^2 / (4*pi*A), P prox. del perimetro (0..1 -> ~cerchio)."""
    area = int(mask.sum())
    if area <= 0:
        return 999.0
    perim = _perimeter_px(mask)
    return float(perim * perim / (4.0 * np.pi * area))


def detect_cells(raster, cfg_detect):
    """Rileva le celle convettive su un RasterData valido.

    cfg_detect: blocco CONFIG['detect']. Ritorna lista [DetectedCell] ordinata
    per area decrescente. Nessun riferimento a coordinate geografiche dubbie:
    le conversioni usano la geo_transform del raster (pyproj)."""
    if not np.any(raster.valid_mask):
        return []

    threshold = float(cfg_detect["dbz_threshold"])
    binary = raster.valid_mask & (raster.data >= threshold)

    radius = int(cfg_detect.get("morph_radius_px", 0))
    if radius > 0:
        se = _struct(radius, cfg_detect.get("connectivity_8", True))
        binary = ndi.binary_opening(binary, structure=se)
        binary = ndi.binary_closing(binary, structure=se)

    lab, nlabels = ndi.label(binary, structure=_struct(0, cfg_detect.get("connectivity_8", True)))
    if nlabels == 0:
        return []

    min_area = float(cfg_detect["min_area_km2"])
    max_area = float(cfg_detect["max_area_km2"])
    slices = ndi.find_objects(lab)
    pixels_per_km2 = 1.0 / raster.pixel_area_km2
    cells = []

    for idx in range(1, nlabels + 1):
        slc = slices[idx - 1]
        mask = lab[slc] == idx
        pixel_area = float(mask.sum())
        area_km2 = pixel_area * raster.pixel_area_km2
        if area_km2 < min_area or area_km2 > max_area:
            continue

        values = raster.data[slc][mask]
        row_slc, col_slc = slc
        rows, cols = np.nonzero(mask)
        raster_rows = rows + row_slc.start
        raster_cols = cols + col_slc.start

        cr_row = float(raster_rows.mean())
        cr_col = float(raster_cols.mean())
        lon, lat = raster.pixel_to_lonlat(cr_row, cr_col)
        lon, lat = float(lon), float(lat)

        bbox = (
            int(raster_rows.min()), int(raster_cols.min()),
            int(raster_rows.max()), int(raster_cols.max()),
        )

        cell = models.DetectedCell(
            cell_id=f"{raster.time_iso}-{idx:03d}" if raster.time_iso else f"cell-{idx:03d}",
            timestamp_ms=raster.time_ms if raster.time_ms else 0,
            timestamp_iso=raster.time_iso or "",
            area_km2=area_km2,
            centroid_raster=(cr_row, cr_col),
            centroid_lonlat=(lon, lat),
            bbox_raster=bbox,
            max_dbz=float(values.max()),
            mean_dbz=float(values.mean()),
            p90_dbz=float(np.percentile(values, 90)),
            eccentricity=_eccentricity(mask),
            solidity=_solidity(mask),
            compactness=_compactness(mask),
            pixel_count=int(pixel_area),
            frame_index=None,
        )
        cells.append(cell)

    cells.sort(key=lambda c: c.area_km2, reverse=True)
    for i, c in enumerate(cells):
        c.frame_index = i
    return cells