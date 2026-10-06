#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — fetch.py

Costruzione della finestra temporale multi-frame (target T-20..T0, VMI PT5M)
a partire dai timestamp REALI dei prodotti Radar-DPC.

Regole:
  - il bootstrap usa findLastProductByType (NO WebSocket in Fase 1, GitHub
    Actions = ambiente non persistente);
  - la finestra è costruita SUI timestamp dei prodotti (mai orologio di sistema
    per inferire l'ora radar);
  - candidati arrotondati per difetto al passo del prodotto;
  - frame non disponibili (404) -> buco temporale, NON timestamp inventati;
  - verifica di monotonicità strettamente crescente dei timestamp;
  - ogni download è validato (magic TIFF + apertura raster + CRS);
  - i raw GeoTIFF vivono SOLO in cache_dir (escluso dal repo via .gitignore).
"""

import os
from datetime import datetime, timezone

from . import models
from . import preprocess
from . import source_dpc


def _cache_path(cache_dir, s3_key):
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, os.path.basename(s3_key))


def _save_bytes(path, content):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(content)
    os.replace(tmp, path)
    return path


def fetch_frames(config, product=None, max_frames=None):
    """Recupera i frames VMI recenti e li valida, ordinati dal più vecchio.

    Ritorna (frames, warnings, latest_time_ms):
      frames:  list[RasterData] ordinata crescente (timestamp reali)
      latest_time_ms: timestamp EPOCH ms del frame più recente (None se vuoto)
    Solleva SourceError se l'API non risponde affatto (bootstrap fallito)."""
    src = config["source"]
    product = product or src["product_type"]
    max_frames = int(max_frames or src.get("history_frames", 6))

    latest = source_dpc.get_latest_product(
        product,
        api_base=src.get("api_base"),
        origin=src.get("origin_header"),
        timeout_s=src.get("http_timeout_s", 30),
    )

    step_ms = latest.period_s * 1000
    candidates = []
    seen = set()
    for k in range(max_frames):
        raw_ts = latest.time_ms - k * step_ms
        ts = source_dpc.round_down_to_step(raw_ts, latest.period_s)
        if ts in seen:
            continue
        seen.add(ts)
        candidates.append(ts)
    candidates.reverse()  # dal più vecchio al più recente

    cache_dir = src["cache_dir"]
    keep = bool(src.get("keep_raw_frames", False))
    frames = []
    warnings = []
    for ts in candidates:
        probe = models.ProductInfo(product, ts, latest.period_s)
        try:
            s3_key, content = source_dpc.download_product(
                probe,
                api_base=src.get("api_base"),
                origin=src.get("origin_header"),
                timeout_s=src.get("http_timeout_s", 30),
                download_timeout_s=src.get("download_timeout_s", 60),
                retries=src.get("max_download_retries", 2),
            )
        except models.SourceError as exc:
            warnings.append(f"frame@{ts} skipped: {exc}")
            continue

        try:
            real_ts = source_dpc.parse_key_timestamp(s3_key)
        except ValueError as exc:
            real_ts = ts
            warnings.append(f"frame@{ts}: {exc} (fallback al candidato)")
        path = _save_bytes(_cache_path(cache_dir, s3_key), content)
        try:
            source_dpc.validate_download(path)
        except models.ValidationError as exc:
            warnings.append(f"frame@{real_ts} invalid: {exc}")
            if not keep:
                _cleanup(path)
            continue
        raster = preprocess.read_raster(
            path,
            nodata_values=src_preprocess_nodata(config),
            declared_nodata_from_tiff=True,
            geo_plausible_bbox=config["preprocess"].get("geo_plausible_bbox"),
        )
        raster.time_ms = real_ts
        raster.time_iso = _iso(real_ts)
        raster.source_path = path
        frames.append(raster)
        if not keep:
            _cleanup(path)

    frames.sort(key=lambda r: r.time_ms)
    # monotonicità STRETTA: drop di eventuali duplicati/correzioni non ordinabili
    deduped = []
    for r in frames:
        if deduped and r.time_ms <= deduped[-1].time_ms:
            warnings.append(f"frame@{r.time_ms} dropped (monotonicity)")
            continue
        deduped.append(r)
    frames = deduped

    latest_ts = frames[-1].time_ms if frames else None
    return frames, warnings, latest_ts


def src_preprocess_nodata(config):
    return config["preprocess"].get("nodata_values", [])


def _cleanup(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _iso(ms):
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")