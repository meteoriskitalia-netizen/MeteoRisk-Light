#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — preprocess.py

Caricamento e validazione dei GeoTIFF VMI Float32: separa esplicitamente
  - DATA VALIDITY  (validità numerica: NaN/inf, nodata dichiarato, valori -9999/-9998)
  - METEOROLOGICAL THRESHOLD (soglia dBZ, v. detect.py)

CRS/GEOREFERENZIAZIONE (FAIL SAFE): il CRS è LETTO dal file (mai assunto). Se il
CRS non è interpretabile -> CrsError e nessuna coordinata pubblicata. La
conversione pixel->EPSG:4326 passa per pyproj sulla CRS del file. Un controllo
di plausibilità geografica (bbox Italia) blocca coordinate assurde.
"""

import numpy as np

from . import models


def read_raster(path, nodata_values, declared_nodata_from_tiff=True,
                geo_plausible_bbox=None):
    """Legge e valida un GeoTIFF VMI su griglia custom TM (Italia).

    Ritorna RasterData. Solleva ValidationError (file corrotto/formato non
    atteso) o CrsError (georeferenziazione non interpretabile -> FAIL SAFE)."""
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover
        raise models.ValidationError("rasterio_missing") from exc

    try:
        with rasterio.open(path) as ds:
            arr = ds.read(1).astype("float64")
            rows, cols = arr.shape
            crs_obj = ds.crs
            transform = ds.transform
            declared = ds.nodata
    except models.ValidationError:
        raise
    except Exception as exc:
        raise models.ValidationError(f"raster_corrupted:{exc}") from exc

    # --- CRS: letto dal file, MAI assunto. Fail-safe se non interpretabile. ---
    if crs_obj is None or not (crs_obj.to_wkt() or "").strip():
        raise models.CrsError("crs_missing_in_tiff")
    crs_wkt = crs_obj.to_wkt()
    try:
        from pyproj import CRS as PjCRS, Transformer
        crs = PjCRS.from_wkt(crs_wkt)
        geo_transform = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
    except Exception as exc:
        raise models.CrsError(f"crs_uninterpretable:{exc}") from exc

    # --- nodata set: dichiarati + lista config + NaN/inf ---
    nodata_set = set(float(v) for v in (nodata_values or []))
    if declared_nodata_from_tiff and declared is not None:
        nodata_set.add(float(declared))

    finite = np.isfinite(arr)
    if nodata_set:
        is_nodata = np.zeros(arr.shape, dtype=bool)
        for v in nodata_set:
            is_nodata |= np.abs(arr - v) <= 1e-6
    else:
        is_nodata = np.zeros(arr.shape, dtype=bool)

    valid_mask = finite & (~is_nodata)
    nodata_mask = ~valid_mask

    pixel_area_km2 = (abs(transform.a) * abs(transform.e)) / 1e6
    if not (pixel_area_km2 > 0):
        raise models.ValidationError("degenerate_transform")

    # Plausibilità geografica: il centroide del raster deve cadere in bbox Italia.
    if geo_plausible_bbox is not None:
        center_lon, center_lat = pixel_to_lonlat(
            crs, transform, rows / 2.0, cols / 2.0, geo_transform
        )
        lon_r = geo_plausible_bbox["lon"]
        lat_r = geo_plausible_bbox["lat"]
        if not (lon_r[0] <= center_lon <= lon_r[1]
                and lat_r[0] <= center_lat <= lat_r[1]):
            raise models.CrsError(
                f"geographic_plausibility_failed:center={center_lon:.3f},{center_lat:.3f}"
            )

    return models.RasterData(
        data=arr, valid_mask=valid_mask, nodata_mask=nodata_mask,
        rows=rows, cols=cols, crs=crs, crs_wkt=crs_wkt,
        transform=transform, pixel_area_km2=pixel_area_km2,
        time_ms=None, time_iso=None, source_path=str(path),
        declared_nodata=(float(declared) if declared is not None else None),
        geo_transform=geo_transform,
    )


def build_pixel_to_lonlat(crs, transform):
    """Costruttore alternativo di Transformer CRS->EPSG:4326 (riuso nei test)."""
    from pyproj import Transformer
    return Transformer.from_crs(crs, "EPSG:4326", always_xy=True)


def pixel_to_lonlat(crs, transform, row, col, geo_transform=None):
    """Converte (row, col) -> (lon, lat). centro del pixel."""
    if geo_transform is None:
        geo_transform = build_pixel_to_lonlat(crs, transform)
    x, y = transform @ (float(col) + 0.5, float(row) + 0.5)
    lon, lat = geo_transform.transform(x, y)
    return float(lon), float(lat)


def strip_nodata_decimals(values, nodata_values=None):
    """Rimuove i valori di nodata residui da un array (uso nei test/metriche)."""
    out = values[np.isfinite(values)]
    for v in (nodata_values or []):
        out = out[np.abs(out - float(v)) > 1e-6]
    return out