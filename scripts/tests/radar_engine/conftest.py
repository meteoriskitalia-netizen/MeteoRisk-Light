#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pytest fixtures condivise per i test del radar engine (Fase 1).

Si creano SOLO raster sintetici (nessun dato radar DPC reale nel repository).
La CRS è la Transverse Mercator custom documentata dalla piattaforma Radar-DPC
(metadata pubblico, non dati radar): lat0=42, lon0=12.5, pixel 1 km, origine
(-600000, 650000) — stessa griglia dei GeoTIFF VMI reali.
"""

import os
import sys

import numpy as np
import pytest
from affine import Affine
import rasterio

# Impone che il package radar_engine sia importabile da scripts/.
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from radar_engine import models

# WKT della Transverse Mercator custom Radar-DPC (params documentati in
# docs/RADAR_SOURCE_VERIFICATION.md / docs ufficiali DPC).
DPC_TM_WKT = (
    'PROJCS["unnamed",GEOGCS["WGS 84",DATUM["WGS_1984",'
    'SPHEROID["WGS 84",6378137,298.257223563,AUTHORITY["EPSG","7030"]],'
    'AUTHORITY["EPSG","6326"]],PRIMEM["Greenwich",0],'
    'UNIT["degree",0.0174532925199433,AUTHORITY["EPSG","9122"]],'
    'AUTHORITY["EPSG","4326"]],PROJECTION["Transverse_Mercator"],'
    'PARAMETER["latitude_of_origin",42],PARAMETER["central_meridian",12.5],'
    'PARAMETER["scale_factor",1],PARAMETER["false_easting",0],'
    'PARAMETER["false_northing",0],UNIT["metre",1,AUTHORITY["EPSG","9001"]],'
    'AXIS["Easting",EAST],AXIS["Northing",NORTH]]'
)

DEFAULT_TRANSFORM = Affine(1000.0, 0.0, -600000.0, 0.0, -1000.0, 650000.0)


def write_synthetic_tif(path, arr, transform=DEFAULT_TRANSFORM, crs=DPC_TM_WKT,
                        nodata=None, height=None, width=None):
    """Scrive un GeoTIFF float32 1-banda sintetico (griglia stile VMI)."""
    arr = np.asarray(arr, dtype="float32")
    h, w = arr.shape
    kwargs = dict(
        driver="GTiff", height=h, width=w, count=1, dtype="float32",
        transform=transform if transform is not None else Affine(1000, 0, 0, 0, -1000, 0),
    )
    if crs is not None:
        kwargs["crs"] = crs
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(path, "w", **kwargs) as dst:
        dst.write(arr, 1)
    return path


def blank_grid(rows=1400, cols=1200, fill=-9999.0):
    return np.full((rows, cols), fill, dtype="float32")


def add_blob(arr, row, col, radius, value=40.0):
    """Aggiunge un disco di valore (dBZ) sul raster sintetico."""
    rows, cols = arr.shape
    rr, cc = np.ogrid[:rows, :cols]
    mask = (rr - row) ** 2 + (cc - col) ** 2 <= radius ** 2
    arr[mask] = value
    return arr


@pytest.fixture
def vmi_tif(tmp_path):
    """GeoTIFF VMI sintetico: due blob (40 e 35 dBZ) + nodata -9999/-9998 + NaN."""
    arr = blank_grid()
    arr[700, 300] = -9998.0
    arr[100, 900] = float("nan")
    add_blob(arr, 700, 600, 25, 40.0)
    add_blob(arr, 400, 250, 20, 35.0)
    arr[0, 0] = 10.0  # valore valido 10 dBZ (NON è nodata)
    path = tmp_path / "synthetic_vmi.tif"
    write_synthetic_tif(str(path), arr)
    return path


# ---------------------------------------------------------------------------
# Cellule/track sintetiche per test di tracking e scoring
# ---------------------------------------------------------------------------
_T = 0  # contatore timestamp (msec) per unit test deterministici


def make_cell(lon, lat, ts_ms, area_km2=120.0, max_dbz=42.0, mean_dbz=38.0,
              p90_dbz=40.0, ecc=0.4, solidity=0.9, compactness=3.0,
              frame_index=0, cell_id=None):
    cid = cell_id or "cell-{}-{}-{}-{}".format(lon, lat, ts_ms, np.random.randint(0, 1e9))
    return models.DetectedCell(
        cell_id=cid, timestamp_ms=ts_ms,
        timestamp_iso=_iso(ts_ms),
        area_km2=float(area_km2) if area_km2 is not None else None,
        centroid_raster=(0.0, 0.0), centroid_lonlat=(float(lon), float(lat)),
        bbox_raster=(0, 0, 1, 1), max_dbz=float(max_dbz), mean_dbz=float(mean_dbz),
        p90_dbz=float(p90_dbz), eccentricity=float(ecc), solidity=float(solidity),
        compactness=float(compactness), pixel_count=int(abs(float(area_km2))),
        frame_index=int(frame_index),
    )


def make_track(cells, track_id=1):
    """Costruisce una Track da una lista di celle (punti con 5 min di passo)."""
    track = models.Track(track_id, cells[0].timestamp_ms)
    for i, cell in enumerate(cells):
        track.points.append(models.TrackPoint(track_id, i, cell))
    track.events.append(models.EVENT_BIRTH)
    return track


@pytest.fixture
def cell_factory():
    return make_cell


@pytest.fixture
def track_factory():
    return make_track


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")