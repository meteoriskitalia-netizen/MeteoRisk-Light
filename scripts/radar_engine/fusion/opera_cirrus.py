#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — fusion/opera_cirrus.py  (PROTOTIPO ISOLATO, 1.2.3.0)

Adapter multi-sorgente per il composito OPERA CIRRUS (EUMETNET, Open Radar Data /
MeteoGate) da usare come SECONDA FONTE a supporto dei candidati supercella
rilevati sulla VMI Radar-DPC.

STATO: PROTOTIPO NON ATTIVO. Questo modulo non e' importato da main.py/config.py
e non modifica la pipeline di produzione. Fornisce (a) l'accesso documentato e
verificato dal vivo a OPERA CIRRUS, (b) le primitive di allineamento alla griglia
VMI e (c) la logica PURA di consenso `source_agreement` (testabile senza rete).

FONTE / ACCESSO (verificato dal vivo il 2026-10-10):
  * S3 aperto (ANONIMO): https://s3.waw3-1.cloudferro.com/openradar-24h/
      - ListObjectsV2 e GET funzionano senza credenziali (HTTP 200).
  * Chiave prodotto:  YYYY/MM/DD/OPERA/COMP/OPERA@YYYYMMDDTHHMM@0@DBZH.{h5|tiff}
  * ORD API:      https://api.meteogate.eu/eu-eumetnet-weather-radar
      - accesso ANONIMO; nei test l'endpoint /collections ha restituito HTTP 429
        (rate limit anonimo). Per l'accesso affidabile: API key MeteoGate oppure
        la cache S3 aperta (usata qui).
  * Formati:      ODIM HDF5 (richiede h5py: NON installato) e Cloud-Optimized
      GeoTIFF (letto con rasterio, gia' dipendenza del motore) -> si usa il
      GeoTIFF `.tiff`.
  * Griglia:      Lambert Azimuthal Equal Area Europe, lat0=55, lon0=10,
      false_easting=1950000, false_northing=-2100000, pixel 1000 m, 4400x3800,
      aggiornamento 5 min, latenza < 10 min. Il GeoTIFF ha 2 bande:
      banda 1 = DBZH (dBZ, nodata -9999000), banda 2 = quality index (0..1).
  * Licenza:      CC BY 4.0 (attribuzione EUMETNET OPERA).

ATTRIBUZIONE (da riportare in qualunque uso a valle):
  - OPERA CIRRUS: "(c) EUMETNET OPERA, CC BY 4.0 (via Open Radar Data/MeteoGate)"
  - VMI:          "Radar-DPC — Dipartimento della Protezione Civile"

LIMITI DICHIARATI DEL PROTOTIPO:
  - La `source_agreement` opera su due raster GIA' allineati cella-per-cella
    (stessa forma/CRS/transform). L'allineamento OPERA->griglia VMI e' fornito
    da `align_opera_to_vmi` (rasterio.warp, nearest: conservativo sul max dBZ).
  - Se la reproiezione esatta non e' voluta, resta `value_at_lonlat` per il
    campionamento puntuale (lat/lon -> dBZ) senza allineamento di griglia.
"""

import datetime as _dt
import hashlib
import math
import os
import re
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

from .. import models


# ---------------------------------------------------------------------------
# Costanti sorgente
# ---------------------------------------------------------------------------
S3_BASE = "https://s3.waw3-1.cloudferro.com/openradar-24h/"
ORD_API_BASE = "https://api.meteogate.eu/eu-eumetnet-weather-radar"
PRODUCT_DBZH = "DBZH"
DEFAULT_NODATA = -9999000.0
USER_AGENT = "MeteoRisk-Light-fusion-prototype/1.2.3.0"

ATTRIBUTION_OPERA = ("OPERA CIRRUS composite - (c) EUMETNET OPERA, "
                     "CC BY 4.0 (via Open Radar Data / MeteoGate)")
ATTRIBUTION_VMI = ("Radar-DPC VMI - Dipartimento della Protezione Civile "
                   "(Radar-DPC)")

# OPERA@YYYYMMDDTHHMM@0@DBZH.tiff
_KEY_RE = re.compile(
    r"OPERA@(?P<stamp>\d{8}T\d{4})@0@(?P<product>[A-Z0-9]+)\.(?P<ext>h5|tiff)$"
)


# ---------------------------------------------------------------------------
# HTTP helper (stdlib: nessuna dipendenza extra)
# ---------------------------------------------------------------------------
def _http_get(url, timeout_s=60, headers=None):
    """GET binario. Ritorna (status, bytes, headers). Solleva SourceError."""
    req = urllib.request.Request(
        url, headers=headers or {"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        raise models.SourceError(f"http_{exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise models.SourceError(f"network:{exc.__class__.__name__}") from exc


def parse_key_nominal_time(key):
    """OPERA@YYYYMMDDTHHMM@0@DBZH.tiff -> datetime UTC nominale del prodotto.

    Solleva ValueError se la chiave non ha il formato atteso (mai inventare)."""
    m = _KEY_RE.search(os.path.basename(key))
    if not m:
        raise ValueError(f"unparseable_opera_key:{os.path.basename(key)}")
    return _dt.datetime.strptime(m.group("stamp"), "%Y%m%dT%H%M").replace(
        tzinfo=_dt.timezone.utc)


# ---------------------------------------------------------------------------
# Discovery (S3 aperto, ANONIMO)
# ---------------------------------------------------------------------------
def list_opera_keys(day, s3_base=S3_BASE, max_keys=1000, timeout_s=60):
    """Chiavi COMP di un giorno (YYYY/MM/DD) via ListObjectsV2 anonimo."""
    prefix = f"{day}/OPERA/COMP/"
    query = urllib.parse.urlencode(
        {"list-type": "2", "prefix": prefix, "max-keys": int(max_keys)})
    url = f"{s3_base}?{query}"
    status, body, _ = _http_get(url, timeout_s=timeout_s)
    text = body.decode("utf-8", "replace")
    return re.findall(r"<Key>([^<]+)</Key>", text)


def discover_latest(reference_dt=None, product=PRODUCT_DBZH, extension="tiff",
                    s3_base=S3_BASE, lookback_days=3, max_keys=1000,
                    timeout_s=60):
    """Trova la chiave OPERA COMP piu' recente per prodotto/formato.

    Scandisce a ritroso i giorni a partire da `reference_dt` (default: adesso
    UTC). Ritorna dict {key,url,product,extension,nominal_time_iso,status} oppure
    solleva SourceError se non trova nulla nel lookback."""
    ref = reference_dt or _dt.datetime.now(_dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=_dt.timezone.utc)
    suffix = f"@{product}.{extension}"
    for back in range(int(lookback_days) + 1):
        day_dt = ref - _dt.timedelta(days=back)
        day = day_dt.strftime("%Y/%m/%d")
        keys = [k for k in list_opera_keys(day, s3_base=s3_base,
                                           max_keys=max_keys,
                                           timeout_s=timeout_s)
                if k.endswith(suffix)]
        if keys:
            key = sorted(keys)[-1]
            return {
                "key": key,
                "url": s3_base + key,
                "product": product,
                "extension": extension,
                "nominal_time_iso": parse_key_nominal_time(key).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"),
                "status": "discovered_via_s3_listing",
            }
    raise models.SourceError(
        f"opera_no_product:{product}.{extension}:lookback={lookback_days}d")


def download(key, dest_path, s3_base=S3_BASE, timeout_s=180):
    """Scarica un oggetto S3 aperto su `dest_path`. Ritorna metadati + sha256."""
    url = s3_base + key
    status, data, headers = _http_get(url, timeout_s=timeout_s)
    os.makedirs(os.path.dirname(os.path.abspath(dest_path)), exist_ok=True)
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return {
        "path": dest_path,
        "url": url,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "status": status,
        "content_type": headers.get("Content-Type"),
    }


# ---------------------------------------------------------------------------
# Lettura raster (Cloud-Optimized GeoTIFF via rasterio)
# ---------------------------------------------------------------------------
class OperaRaster:
    """Raster OPERA CIRRUS letto dal GeoTIFF: DBZH (banda 1) + quality (banda 2).

    `data` e `quality` sono float64 con nodata MAPPATO a NaN (nessun sentinella
    numerico residuo). `crs` e' l'oggetto rasterio.crs.CRS del file."""

    def __init__(self, data, quality, transform, crs, rows, cols, path,
                 nominal_time_iso=None, nodata=DEFAULT_NODATA):
        self.data = data
        self.quality = quality
        self.transform = transform
        self.crs = crs
        self.rows = int(rows)
        self.cols = int(cols)
        self.path = path
        self.nominal_time_iso = nominal_time_iso
        self.nodata = nodata

    def summary(self):
        valid = np.isfinite(self.data)
        n = int(valid.sum())
        out = {
            "path": self.path,
            "rows": self.rows,
            "cols": self.cols,
            "crs": self.crs.to_string() if self.crs else None,
            "transform": list(self.transform)[:6],
            "nodata": self.nodata,
            "valid_pixels": n,
            "min_dbz": float(self.data[valid].min()) if n else None,
            "max_dbz": float(self.data[valid].max()) if n else None,
            "p99_dbz": float(np.percentile(self.data[valid], 99)) if n else None,
            "nominal_time_iso": self.nominal_time_iso,
        }
        if self.quality is not None:
            qv = np.isfinite(self.quality)
            out["quality_min"] = float(self.quality[qv].min()) if qv.any() else None
            out["quality_max"] = float(self.quality[qv].max()) if qv.any() else None
        return out


def open_opera(path, dbzh_band=1, quality_band=2):
    """Apre un GeoTIFF OPERA CIRRUS. Mappa nodata/sentinel -> NaN. Ritorna OperaRaster.

    Solleva ValidationError se il file non e' un raster leggibile."""
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover
        raise models.ValidationError("rasterio_missing") from exc
    try:
        with rasterio.open(path) as ds:
            raw = ds.read(dbzh_band).astype("float64")
            nodata = ds.nodata
            quality = (ds.read(quality_band).astype("float64")
                       if ds.count >= quality_band else None)
            transform = ds.transform
            crs = ds.crs
            rows, cols = ds.height, ds.width
    except models.ValidationError:
        raise
    except Exception as exc:
        raise models.ValidationError(f"opera_raster_corrupted:{exc}") from exc

    sentinels = [DEFAULT_NODATA]
    if nodata is not None:
        sentinels.append(float(nodata))
    bad = ~np.isfinite(raw)
    for s in sentinels:
        bad |= np.abs(raw - s) <= 1.0
    data = raw.copy()
    data[bad] = np.nan

    if quality is not None:
        qbad = ~np.isfinite(quality)
        for s in sentinels:
            qbad |= np.abs(quality - s) <= 1.0
        quality = quality.copy()
        quality[qbad] = np.nan

    nominal = None
    try:
        nominal = parse_key_nominal_time(path).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        nominal = None
    return OperaRaster(data, quality, transform, crs, rows, cols, str(path),
                       nominal_time_iso=nominal, nodata=nodata)


# ---------------------------------------------------------------------------
# Accesso per punto (nessuna riproiezione: utile se l'allineamento non serve)
# ---------------------------------------------------------------------------
def value_at_lonlat(opera, lon, lat):
    """dBZ OPERA nel pixel che contiene (lon, lat) EPSG:4326. NaN se fuori/nodata.

    Nessun valore inventato: punto fuori griglia o su nodata -> NaN."""
    from pyproj import Transformer
    try:
        tr = Transformer.from_crs("EPSG:4326", opera.crs, always_xy=True)
        x, y = tr.transform(float(lon), float(lat))
    except Exception:
        return float("nan")
    if not (math.isfinite(x) and math.isfinite(y)):
        return float("nan")
    col_f, row_f = ~opera.transform * (x, y)
    r, c = int(math.floor(row_f)), int(math.floor(col_f))
    if r < 0 or c < 0 or r >= opera.rows or c >= opera.cols:
        return float("nan")
    return float(opera.data[r, c])


# ---------------------------------------------------------------------------
# Allineamento alla griglia VMI (riproiezione cella-per-cella)
# ---------------------------------------------------------------------------
def _resampling(name):
    from rasterio.warp import Resampling
    return getattr(Resampling, str(name).lower())


def reproject_to_grid(src_array, src_transform, src_crs,
                      dst_transform, dst_crs, dst_shape, resampling="nearest"):
    """Riproietta `src_array` sulla griglia dst. Ritorna float64 con nodata=NaN.

    `src_array` deve avere nodata=NaN. `resampling='nearest'` e' il default
    CONSERVATIVO per il max reflectivity (nessuna interpolazione che generi
    valori non osservati)."""
    from rasterio.warp import reproject
    dst = np.full(tuple(int(v) for v in dst_shape), np.nan, dtype="float64")
    reproject(
        source=src_array.astype("float64"),
        destination=dst,
        src_transform=src_transform,
        src_crs=src_crs,
        dst_transform=dst_transform,
        dst_crs=dst_crs,
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=_resampling(resampling),
    )
    return dst


def align_opera_to_vmi(opera, vmi_raster, resampling="nearest"):
    """dBZ OPERA riproiettati sulla griglia esatta della VMI (stessa shape/CRS).

    `vmi_raster` e' un `radar_engine.models.RasterData` (preprocess.read_raster).
    Ritorna ndarray float64 (rows x cols) in dBZ, NaN dove OPERA non copre."""
    return reproject_to_grid(
        opera.data, opera.transform, opera.crs,
        vmi_raster.transform, vmi_raster.crs_wkt,
        (vmi_raster.rows, vmi_raster.cols), resampling=resampling)


# ---------------------------------------------------------------------------
# LOGICA PURA DI CONSENSO (senza rete, senza I/O: testabile offline)
# ---------------------------------------------------------------------------
def _presence(values, threshold):
    return np.isfinite(values) & (values >= float(threshold))


def source_agreement(vmi_dbz, opera_dbz, presence_threshold_dbz=20.0,
                     core_threshold_dbz=35.0, tolerance_dbz=10.0,
                     min_agreement_cells=5):
    """Consenso VMI vs OPERA su due raster ALLINEATI cella-per-cella.

    Input: `vmi_dbz`, `opera_dbz` sono ndarray 2-D della STESSA forma; i valori
    non finiti (NaN) sono celle non osservate. La classificazione di presenza e
    le statistiche di intensita' si valutano sul dominio CO-OSSERVATO (entrambe
    finiti); le celle osservate da UNA SOLA fonte sono contate a parte
    (`opera_missing_on_vmi`, `vmi_missing_on_opera`) e non inquinano
    only_vmi/only_opera. La logica e' pura (solo numpy) e deterministica.

    Classificazione di PRESENZA (cella co-osservata sopra `presence_threshold_dbz`):
      both        : almeno una cella presente in ENTRAMBE le fonti
      only_vmi    : VMI presente, OPERA no
      only_opera  : OPERA presente, VMI no
      neither     : nessuna delle due presenta (sul co-osservato)

    NOTA: per OPERA CIRRUS una cella NaN sul dominio italiano significa "nessun
    eco" oppure "fuori copertura radar". Se si vuole trattare NaN come "assente"
    (invece che "non osservato"), il chiamante deve pre-riempire quello con un
    valore sotto soglia PRIMA di chiamare questa funzione.

    `agreement_score` = IoU delle maschere di presenza (0..1).
    `confidence` dipende da IoU, dal numero di celle concordanti e (per i core)
    dalla frazione di celle core OPERA presenti dove la VMI ha un core.

    Ritorna dict (tutti i conteggi sono interi >= 0):
      presence_class, confidence, agreement_score, iou, overlap_fraction,
      counts{}, core{}, bias_dbz, mean_abs_diff_dbz, within_tolerance_count,
      within_tolerance_fraction, n_valid_cells
    """
    vmi = np.asarray(vmi_dbz, dtype="float64")
    opera = np.asarray(opera_dbz, dtype="float64")
    if vmi.shape != opera.shape:
        raise ValueError(
            f"grid_mismatch:{vmi.shape}!={opera.shape}")

    # Dominio CO-OSSERVATO: il consenso di PRESENZA si valuta solo dove ENTRAMBE
    # le fonti hanno un valore (finire). Dove una fonte manca si contabilizza a
    # parte (missing) e NON inquina only_vmi/only_opera: cosi' una cella senza
    # eco OPERA (NaN) non viene scambiata per "disaccordo".
    v_obs = np.isfinite(vmi)
    o_obs = np.isfinite(opera)
    co = v_obs & o_obs

    v_pres = co & (vmi >= float(presence_threshold_dbz))
    o_pres = co & (opera >= float(presence_threshold_dbz))
    both = v_pres & o_pres
    only_vmi = v_pres & ~o_pres
    only_opera = o_pres & ~v_pres
    union = v_pres | o_pres

    n_vmi = int(v_pres.sum())
    n_opera = int(o_pres.sum())
    n_both = int(both.sum())
    n_union = int(union.sum())
    n_co = int(co.sum())
    n_valid = n_co
    n_opera_missing = int((v_obs & ~o_obs).sum())
    n_vmi_missing = int((o_obs & ~v_obs).sum())

    iou = (n_both / n_union) if n_union else 0.0
    max_pres = max(n_vmi, n_opera)
    overlap_fraction = (n_both / max_pres) if max_pres else 0.0

    if n_vmi == 0 and n_opera == 0:
        presence_class = "neither"
    elif n_both > 0:
        presence_class = "both"
    elif n_vmi > 0:
        presence_class = "only_vmi"
    else:
        presence_class = "only_opera"

    # --- statistiche di intensita' sulle celle presenti in ENTRAMBE ---
    bias = None
    mean_abs = None
    within_tol = 0
    within_frac = None
    if n_both > 0:
        dv = vmi[both]
        do = opera[both]
        diff = do - dv
        bias = float(diff.mean())
        mean_abs = float(np.abs(diff).mean())
        within_tol = int((np.abs(diff) <= float(tolerance_dbz)).sum())
        within_frac = float(within_tol / n_both)

    # --- confronto sui CORE (celle sopra core_threshold_dbz, co-osservate) ---
    v_core = co & (vmi >= float(core_threshold_dbz))
    o_core = co & (opera >= float(core_threshold_dbz))
    n_vcore = int(v_core.sum())
    n_ocore = int(o_core.sum())
    n_core_both = int((v_core & o_core).sum())
    core_opera_confirms_vmi = ((n_core_both / n_vcore) if n_vcore else None)
    core_vmi_confirms_opera = ((n_core_both / n_ocore) if n_ocore else None)

    # --- confidenza ---
    if n_union == 0:
        confidence = "none"
    elif n_both < int(min_agreement_cells):
        confidence = "low"
    elif iou >= 0.5:
        confidence = "high"
    elif iou >= 0.25:
        confidence = "medium"
    else:
        confidence = "low"

    return {
        "presence_class": presence_class,
        "confidence": confidence,
        "agreement_score": round(float(iou), 4),
        "iou": round(float(iou), 4),
        "overlap_fraction": round(float(overlap_fraction), 4),
        "counts": {
            "vmi_observed": int(v_obs.sum()),
            "opera_observed": int(o_obs.sum()),
            "co_observed": n_co,
            "vmi_present": n_vmi,
            "opera_present": n_opera,
            "both": n_both,
            "only_vmi": int(only_vmi.sum()),
            "only_opera": int(only_opera.sum()),
            "union": n_union,
            "opera_missing_on_vmi": n_opera_missing,
            "vmi_missing_on_opera": n_vmi_missing,
        },
        "core": {
            "vmi_core": n_vcore,
            "opera_core": n_ocore,
            "core_both": n_core_both,
            "opera_confirms_vmi_core": (None if core_opera_confirms_vmi is None
                                        else round(core_opera_confirms_vmi, 4)),
            "vmi_confirms_opera_core": (None if core_vmi_confirms_opera is None
                                        else round(core_vmi_confirms_opera, 4)),
        },
        "bias_dbz": (None if bias is None else round(bias, 3)),
        "mean_abs_diff_dbz": (None if mean_abs is None else round(mean_abs, 3)),
        "within_tolerance_count": within_tol,
        "within_tolerance_fraction": (None if within_frac is None
                                      else round(within_frac, 4)),
        "n_valid_cells": n_valid,
    }


def candidate_agreement(vmi_dbz, opera_dbz, core_r0, core_r1, core_c0, core_c1,
                        margin_px=0, **kw):
    """Consenso su un CANDIDATO: ritaglia la finestra di footprint (inclusiva) e
    valuta `source_agreement` sul ritaglio. `core_*` sono gli estremi raster
    (r0,r1,c0,c1) del candidato su griglia VMI; `margin_px` allarga la finestra."""
    r0 = max(0, int(core_r0) - int(margin_px))
    c0 = max(0, int(core_c0) - int(margin_px))
    r1 = min(vmi_dbz.shape[0] - 1, int(core_r1) + int(margin_px))
    c1 = min(vmi_dbz.shape[1] - 1, int(core_c1) + int(margin_px))
    if r0 > r1 or c0 > c1:
        raise ValueError("empty_candidate_window")
    v = np.asarray(vmi_dbz)[r0:r1 + 1, c0:c1 + 1]
    o = np.asarray(opera_dbz)[r0:r1 + 1, c0:c1 + 1]
    out = source_agreement(v, o, **kw)
    out["window"] = [r0, r1, c0, c1]
    return out
