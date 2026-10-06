#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — source_dpc.py

UNICO modulo che conosce l'infrastruttura Radar-DPC (endpoint REST, formati di
richiesta/risposta, pre-signed URL, codice prodotto). Il resto del motore è
indipendente dalla sorgente e lavora su oggetti astratti (ProductInfo, RasterData).

Riferimenti (documentazione ufficiale DPC, v. docs/RADAR_SOURCE_VERIFICATION.md):
  GET  /findLastProductByType?type=<PROD>   -> {lastProducts:[{productType,time,period}]}
  POST /downloadProduct {productType, productDate} -> {bucket,key,url,expiresSeconds}
  Parametro `origin` richiesto (sicurezza); CORS abilitato.
Solo VMI (dBZ Float32) è usato in Fase 1. WebSocket NON implementato (by design,
Fase 1 = polling findLastProductByType).
"""

import datetime as _dt
import json
import os
import re
import struct
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import models

KEY_FILENAME_RE = re.compile(
    r"(?P<day>\d{2})-(?P<month>\d{2})-(?P<year>\d{4})-(?P<hh>\d{2})-(?P<mm>\d{2})\.tif$"
)

_CT_JSON = {"Content-Type": "application/json"}


def _http_json(url, timeout_s, headers=None, method="GET", body=None):
    """GET/POST JSON con urllib (stdlib). Solleva SourceError su errori HTTP/rete."""
    req = urllib.request.Request(url, method=method, headers=headers or {})
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
        req.data = payload
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise models.SourceError("not_found") from exc
        raise models.SourceError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise models.SourceError(f"network:{exc.__class__.__name__}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise models.SourceError("invalid_json") from exc


def parse_iso_period(period):
    """ISO-8601 duration 'PT5M'/'PT1H' -> secondi interi (solo minuti/ore)."""
    if not period:
        raise models.SourceError("missing_period")
    m = re.fullmatch(r"PT(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?", period)
    if not m:
        raise models.SourceError(f"unsupported_period:{period}")
    h = int(m.group("h") or 0)
    mins = int(m.group("m") or 0)
    if h and mins:
        raise models.SourceError(f"unsupported_period:{period}")
    return (h * 60 + mins) * 60


def round_down_to_step(ts_ms, step_s):
    return (ts_ms // (step_s * 1000)) * (step_s * 1000)


def get_latest_product(product_type="VMI", api_base=None, origin=None,
                       timeout_s=30):
    """findLastProductByType -> ProductInfo (tempo EPOCH ms + periodo ISO-8601).

    Solleva SourceError se il prodotto non esiste / API non risponde. Il tempo
    restituito è quello REALE del prodotto radar (mai orologio di sistema)."""
    base = api_base or "https://radar-api.protezionecivile.it"
    url = f"{base}/findLastProductByType?type={urllib.parse.quote(product_type)}"
    headers = {"Accept": "application/json"}
    if origin:
        headers["Origin"] = origin
    data = _http_json(url, timeout_s, headers, method="GET")
    items = (data or {}).get("lastProducts") or []
    if not items:
        raise models.SourceError("no_product")
    first = items[0]
    period_s = parse_iso_period(first.get("period"))
    return models.ProductInfo(
        product_type=first.get("productType") or product_type,
        time_ms=int(first.get("time")),
        period_s=period_s,
    )


def download_product(product_info, api_base=None, origin=None, timeout_s=30,
                     download_timeout_s=60, retries=2):
    """POST /downloadProduct -> (s3_key, bytes). Orientato ai prodotti VMI-like.

    Scarica subito la pre-signed URL (validità osservata ~300 s). Ritenta il solo
    download in caso di errore transiente. Solleva SourceError."""
    base = api_base or "https://radar-api.protezionecivile.it"
    url = f"{base}/downloadProduct"
    headers = {"Accept": "*/*", "Origin": origin} if origin else {"Accept": "*/*"}
    body = {
        "productType": product_info.product_type,
        "productDate": int(product_info.time_ms),
    }
    resp = _http_json(url, timeout_s, headers, method="POST", body=body)
    s3_url = resp.get("url")
    s3_key = resp.get("key")
    if not s3_url or not s3_key:
        raise models.SourceError("missing_presigned_url")
    last_err = None
    for attempt in range(1 + retries):
        try:
            req = urllib.request.Request(s3_url, method="GET")
            with urllib.request.urlopen(req, timeout=download_timeout_s) as rr:
                content = rr.read()
            return s3_key, content
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_err = exc
    raise models.SourceError(f"download_failed:{last_err.__class__.__name__}")


def parse_key_timestamp(s3_key):
    """Estrae il timestamp REALE del prodotto dal nome file (DD-MM-YYYY-HH-MM.tif).

    ValueError se il nome non è del formato atteso (mai inventare time)."""
    name = os.path.basename(s3_key)
    m = KEY_FILENAME_RE.search(name)
    if not m:
        raise ValueError(f"unparseable_product_key:{name}")
    dt = _dt.datetime(int(m.group("year")), int(m.group("month")),
                      int(m.group("day")), int(m.group("hh")), int(m.group("mm")),
                      tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def validate_download(path):
    """Valida il file scaricato (magic TIFF + apertura raster) PRIMA del uso.

    Ritorna (rows, cols, dtype, bands). Solleva ValidationError."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(12)
    except OSError as exc:
        raise models.ValidationError(f"unreadable:{exc}") from exc
    if len(head) < 8 or head[:4] not in (b"II*\x00", b"MM\x00*"):
        raise models.ValidationError("not_a_tiff")
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover
        raise models.ValidationError("rasterio_missing") from exc
    try:
        with rasterio.open(path) as ds:
            rows, cols = ds.height, ds.width
            dtype = ds.dtypes[0] if ds.count else "?"
            bands = ds.count
    except Exception as exc:
        raise models.ValidationError(f"raster_corrupted:{exc}") from exc
    if bands != 1 or dtype != "float32":
        raise models.ValidationError(f"unsupported_raster:bands={bands},dtype={dtype}")
    return rows, cols, dtype, bands