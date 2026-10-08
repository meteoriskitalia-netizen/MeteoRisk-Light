#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — lightning.py: interfaccia provider fulmini PLUGGABLE.

Contratto (comune a tutti i provider):
    strikes_in_window(epoch_ms, window_slots=4, radius_km=30, center_lonlat=None)
        -> dict SEMPRE dello stesso schema, con available=False (MAI eccezione)
           quando il dato non e' raggiungibile (403/timeout/rete spenta).

Firme principali:
    LightningProvider     — classe base: conteggio/trend/score condivisi;
    DpcBlitzProvider      — DPC Blitz v2 su S3 (riuso radar_engine.phase2.
                            lightning: fetch_ltg/decode/count/trend), senza
                            secret, opener iniettabile;
    MliWmsProvider        — MLI EUMETSAT via WMS anonimo EUMETView (layer
                            mtg_fd:li_afa "Accumulated Flash Area", CC-BY-4.0,
                            NESSUN secret): UNA GetMap Italia per slot (cache
                            per run, campionata da tutti i candidati), proxy
                            su raster — NO flash puntuali;
    register_provider / get_provider / available_providers — registry.

Campi ESTESI dello schema (retro-compatibili: gia' presenti in unavailable()
e nella risposta base, quindi identici per ogni provider):
    strength    — intensita' 0..1 della sorgente (DPC: min(1, count_near/30);
                  MLI: pixel attivi / pixel del disco di raggio);
    note        — nota semantica per l'evidence (MLI: MLI_NOTE, "AFA proxy
                  (no flash puntuali)"; sorgenti puntuali: None);
    attribution — attribuzione licenza (MLI: EUMETSAT CC-BY-4.0; else None).

Evidence: MLI non ha punti lat/lon (strikes sempre []) -> il campo
evidence.lightning.note dichiara "AFA proxy (no flash puntuali)".
"""

import datetime as _dt
import io
import math
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings

import numpy as np

from radar_engine.phase2 import lightning as _ltg

from . import DEFAULT_LIGHTNING_THRESHOLD, LIGHTNING_SOURCE_THRESHOLDS

LTG_PERIOD_MS = _ltg.LTG_PERIOD_MS            # 300000 (slot da 5 min)
DEFAULT_WINDOW_SLOTS = 4                      # = LIGHTNING_TREND_WINDOW
DEFAULT_RADIUS_KM = _ltg.LIGHTNING_RADIUS_KM  # 30.0
# Massima latenza accettabile fra radar_timestamp e lo slot fulmini piu'
# recente pubblicato: oltre -> dato NON usato come evidenza (stale = nullo).
LTG_MAX_STALENESS_MS = 30 * 60 * 1000
# Tentativi per lo STESSO slot prima di arretrare a quello precedente:
# assorbe errori transitori (5xx intermittenti, DNS/connect reset) senza
# perdere l'intera run. Delay fra tentativi via MliWmsProvider._sleep.
LIGHTNING_RETRY_PER_SLOT = 3
LTG_RETRY_BASE_DELAY_S = 1.0

# ---------------------------------------------------------------------------
# MLI EUMETSAT (WMS anonimo EUMETView) — GetMap VERIFICATO live 2026-10-08:
# HTTP 200 image/png RGBA 1024x898 senza auth per layer+style sotto riportati.
# ---------------------------------------------------------------------------
MLI_WMS_URL = "https://view.eumetsat.int/geoserver/wms"
MLI_LAYER = "mtg_fd:li_afa"                   # Accumulated Flash Area
MLI_STYLE = "mtg_li_afa"
MLI_BBOX = (6.6, 36.5, 18.8, 47.2)            # Italia + margini (satellite_engine)
MLI_WIDTH = 1024
MLI_HEIGHT = 898                              # = round(width * Δlat/Δlon)
MLI_HTTP_TIMEOUT_S = 30.0
# Lag di pubblicazione MTG verificato live ~10 min (una GetMap "ora corrente"
# restituisce 502) + 5xx intermittenti di EUMETView: backoff di slot da 5 min
# fino a 8 passi (~40 min) con retry per-slot su errori transitori.
MLI_MAX_BACKOFF_STEPS = 8
# Staleness MLI COERENTE con la finestra di backoff: un campo pubblicato entro
# il backoff (<= 40 min) resta utilizzabile come evidenza. DPC conserva il
# proprio LTG_MAX_STALENESS_MS (30 min) invariato.
MLI_MAX_STALENESS_MS = MLI_MAX_BACKOFF_STEPS * LTG_PERIOD_MS
MLI_NOTE = "AFA proxy (no flash puntuali)"
MLI_ATTRIBUTION = ("Contains modified EUMETSAT Meteosat data © EUMETSAT "
                   "(CC-BY-4.0)")
MLI_USER_AGENT = "MeteoRisk-Light/phenomena (EUMETSAT MLI, non-commercial)"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# Rif. normalizzazione count_near -> strength per le sorgenti puntuali:
# 30 = stessa base usata da hail_score/vortex_score (10 fulmini -> 0.33).
STRENGTH_REF_DPC = 30.0


class UnknownProviderError(ValueError):
    """Nome provider non registrato (nessun fallback silenzioso)."""


def _is_slot_missing(exc):
    """True se l'errore e' 'slot non ancora pubblicato' (403): vale la pena
    arretrare di uno slot; per timeout/DNS NO (fail fast, nessun retry inutile)."""
    msg = str(exc)
    return "not_published_403" in msg or "http_403" in msg


def _retry_delay(attempt):
    """Delay deterministico (s) dopo il tentativo 1-based `attempt` dello slot.

    Base 1s con crescita esponenziale, tetto a 8s: piccolo abbastanza da non
    bloccare una run, sufficiente a coprire un 5xx transitorio."""
    return min(8.0, LTG_RETRY_BASE_DELAY_S * (2 ** max(0, int(attempt) - 1)))


def _flat_trend():
    return _ltg.lightning_trend([])


def unavailable(source, reason, radius_km=DEFAULT_RADIUS_KM):
    """Risposta canonica "non disponibile" (stessa forma di strikes_in_window)."""
    return {
        "available": False,
        "source": source,
        "reason": reason,
        "epoch_ms": None,
        "anchor_ms": None,
        "staleness_ms": None,
        "window_slots": 0,
        "slots_ok": 0,
        "radius_km": float(radius_km),
        "center": None,
        "strikes": [],
        "count_total": 0,
        "count_near": 0,
        "per_slot_counts": [],
        "trend": _flat_trend(),
        "score": None,
        "strength": None,
        "note": None,
        "attribution": None,
    }


def _slot_floor(epoch_ms):
    """epoch_ms -> inizio slot da 5 minuti (piu' vicino non-successivo)."""
    ms = int(epoch_ms) - int(epoch_ms) % LTG_PERIOD_MS
    return ms


def _slot_iso(epoch_ms):
    """epoch_ms -> ISO8601 Z per il parametro TIME del WMS."""
    return _dt.datetime.fromtimestamp(
        int(epoch_ms) / 1000.0, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_center(center_lonlat):
    """[lon, lat] finite -> lista, altrimenti None (mai coordinate inventate)."""
    if center_lonlat is None:
        return None
    try:
        lon, lat = float(center_lonlat[0]), float(center_lonlat[1])
    except (TypeError, ValueError, IndexError):
        return None
    if lon != lon or lat != lat:
        return None
    return [lon, lat]


def lightning_strength(ltg):
    """Intensita' fulmini 0..1 per hail_score/vortex_score (mai NaN).

    Usa `strength` della sorgente se presente; altrimenti fallback DPC
    count_near/30 (identico allo score storico); unavailable -> 0.0."""
    if not isinstance(ltg, dict) or not ltg.get("available"):
        return 0.0
    strength = ltg.get("strength")
    if isinstance(strength, (int, float)) and strength == strength \
            and strength not in (float("inf"), float("-inf")):
        return max(0.0, min(1.0, float(strength)))
    try:
        count = float(ltg.get("count_near") or 0.0)
    except (TypeError, ValueError):
        count = 0.0
    return max(0.0, min(1.0, count / STRENGTH_REF_DPC))


def lightning_corroborates(ltg):
    """Gate fulmini provider-agnostico (soglie per sorgente, phenomena.__init__).

    strength della sorgente >= strength_min -> OK; sorgente senza `strength`
    (DPC e mock legacy) -> fallback count_near >= count_min. Provider
    unavailable -> False (nessuna promozione senza evidenza)."""
    if not isinstance(ltg, dict) or not ltg.get("available"):
        return False
    source = str(ltg.get("source") or "dpc").strip().lower()
    thr = LIGHTNING_SOURCE_THRESHOLDS.get(source) or DEFAULT_LIGHTNING_THRESHOLD
    strength = ltg.get("strength")
    if isinstance(strength, (int, float)) and strength == strength \
            and strength not in (float("inf"), float("-inf")):
        return float(strength) >= float(thr["strength_min"])
    try:
        count = int(ltg.get("count_near") or 0)
    except (TypeError, ValueError):
        count = 0
    return count >= int(thr["count_min"])


class LightningProvider:
    """Classe base: fetch del finestra (sottoclasse) + risposta normalizzata."""

    name = "abstract"

    def __init__(self, network=True):
        self.network = bool(network)

    def fetch_window(self, epoch_ms=None, window_slots=DEFAULT_WINDOW_SLOTS):
        """Scarica la finestra di slot fulmini. Deve ritornare:
        {available, source, reason, epoch_ms, anchor_ms, staleness_ms,
         slots_requested, slots_ok, slots:[{epoch_ms, strikes, ok} oldest->newest]}
        e NON sollevare mai per errori di rete/dato."""
        raise NotImplementedError

    def strikes_in_window(self, epoch_ms=None, window_slots=DEFAULT_WINDOW_SLOTS,
                          radius_km=DEFAULT_RADIUS_KM, center_lonlat=None):
        """Risposta pubblica del provider (schema stabile, v. modulo).

        count_near = strike entro radius_km dal centro; senza centro non c'e'
        raggio -> 0 e "center": None (0 = nessun anchor, NON una misura) e
        per_slot_counts/trend si riferiscono ai totali per slot.
        """
        win = self.fetch_window(epoch_ms=epoch_ms, window_slots=window_slots)
        try:
            radius = float(radius_km)
        except (TypeError, ValueError):
            radius = DEFAULT_RADIUS_KM
        center = _normalize_center(center_lonlat)
        out = {
            "available": bool(win.get("available")),
            "source": win.get("source", self.name),
            "reason": win.get("reason"),
            "epoch_ms": win.get("epoch_ms"),
            "anchor_ms": win.get("anchor_ms"),
            "staleness_ms": win.get("staleness_ms"),
            "window_slots": int(win.get("slots_requested") or 0),
            "slots_ok": int(win.get("slots_ok") or 0),
            "radius_km": radius,
            "center": center,
            "strikes": [],
            "count_total": 0,
            "count_near": 0,
            "per_slot_counts": [],
            "trend": _flat_trend(),
            "score": None,
            "strength": None,
            "note": None,
            "attribution": None,
        }
        if not out["available"]:
            return out
        lon = lat = None
        if center is not None:
            lon, lat = center
        strikes = []
        per_slot = []
        for slot in (win.get("slots") or []):
            recs = slot.get("strikes")
            if recs is None:
                per_slot.append(None)          # slot non scaricato
                continue
            if center is not None:
                per_slot.append(_ltg.count_strikes_in_radius(
                    recs, lon, lat, radius))
            else:
                per_slot.append(len(recs))
            when = slot.get("epoch_ms")
            for rec in recs:
                try:
                    strikes.append({"lon": float(rec[0]), "lat": float(rec[1]),
                                    "time_ms": when})
                except (TypeError, ValueError, IndexError):
                    continue
        counts = [c for c in per_slot if c is not None]
        out["strikes"] = strikes
        out["count_total"] = len(strikes)
        out["count_near"] = int(sum(counts)) if center is not None else 0
        out["per_slot_counts"] = per_slot
        out["trend"] = _ltg.lightning_trend(counts)
        out["score"] = _ltg.lightning_spatial_score(counts)
        out["strength"] = round(min(1.0, out["count_near"] / STRENGTH_REF_DPC), 4)
        return out


class DpcBlitzProvider(LightningProvider):
    """Fulmini DPC Blitz v2 su S3 (riuso radar_engine.phase2.lightning).

    Finestra ancorata a epoch_ms: cerca lo slot piu' recente PUBBLICATO <=
    epoch_ms (backoff graceful, identico a fetch_ltg(epoch_ms=None)); se la
    latenza supera LTG_MAX_STALENESS_MS il dato e' dichiarato stale ->
    available=False (mai evidenza fulmini vecchia). Ogni singolo slot puo'
    fallire (403/timeout): nessun errore si propaga, available=False solo se
    NESSUN slot e' scaricabile. La finestra scaricata viene cachata per run.
    """

    name = "dpc"

    def __init__(self, network=True, opener=None, timeout_s=None):
        super().__init__(network=network)
        self.opener = opener          # iniettabile (test); None -> urllib
        self.timeout_s = timeout_s
        self._cache = {}

    def fetch_window(self, epoch_ms=None, window_slots=DEFAULT_WINDOW_SLOTS):
        base = int(epoch_ms) if epoch_ms is not None else _ltg.ltg_epoch_floor()
        try:
            n = max(1, int(window_slots))
        except (TypeError, ValueError):
            n = DEFAULT_WINDOW_SLOTS
        if not self.network:
            return _window(self.name, base, [], n, False, "network_disabled")
        key = (base, n)
        if key in self._cache:
            return self._cache[key]
        out = self._fetch(base, n)
        if len(self._cache) > 8:
            self._cache.clear()
        self._cache[key] = out
        return out

    def _fetch(self, base, n):
        anchor = None
        anchor_payload = None
        last_reason = "no_slot"
        for k in range(_ltg.LTG_MAX_BACKOFF_STEPS + 1):
            slot = base - k * LTG_PERIOD_MS
            try:
                payload = _ltg.fetch_ltg(epoch_ms=slot, opener=self.opener,
                                         timeout_s=self.timeout_s)
            except Exception as exc:            # mai eccezione verso l'alto
                last_reason = f"{exc.__class__.__name__}:{exc}"
                if _is_slot_missing(exc):
                    continue                    # slot non ancora pubblicato
                return _window(self.name, base, [], n, False, last_reason)
            anchor = slot
            anchor_payload = payload
            break
        if anchor is None:
            return _window(self.name, base, [], n, False,
                           f"ltg_unavailable:{last_reason}")
        staleness = base - anchor
        if staleness > LTG_MAX_STALENESS_MS:
            return _window(self.name, base, [], n, False,
                           f"stale_ltg:{staleness}ms")
        slots = []
        for i in range(n):
            slot = anchor - i * LTG_PERIOD_MS
            payload = anchor_payload if i == 0 else None
            if i > 0:
                try:
                    payload = _ltg.fetch_ltg(epoch_ms=slot,
                                             opener=self.opener,
                                             timeout_s=self.timeout_s)
                except Exception:
                    payload = None
            slots.append({"epoch_ms": slot,
                          "strikes": list(payload["strikes"])
                          if payload else None,
                          "ok": payload is not None})
        slots.reverse()                          # oldest -> newest
        return _window(self.name, base, slots, n, True, None,
                       anchor_ms=anchor, staleness_ms=staleness)


def _window(source, epoch_ms, slots, slots_requested, available, reason,
            anchor_ms=None, staleness_ms=None):
    return {
        "available": bool(available),
        "source": source,
        "reason": reason,
        "epoch_ms": epoch_ms,
        "anchor_ms": anchor_ms,
        "staleness_ms": staleness_ms,
        "slots_requested": slots_requested,
        "slots_ok": sum(1 for s in slots if s.get("ok")),
        "slots": slots,
    }


# ---------------------------------------------------------------------------
# MLI EUMETSAT — utility pure (URL, decodifica, campionamento)
# ---------------------------------------------------------------------------
def build_mli_getmap_url(time_iso=None, bbox=MLI_BBOX, width=MLI_WIDTH,
                         height=MLI_HEIGHT):
    """URL GetMap WMS 1.1.1 anonimo EUMETView per il layer MLI (deterministico).

    Pattern VERIFICATO live (HTTP 200 image/png, nessuna auth):
    layers=mtg_fd:li_afa, styles=mtg_li_afa, srs=EPSG:4326, transparent=true,
    bbox = (minx, miny, maxx, maxy) su MLI_BBOX; time = scan (dimensione del
    layer PT5M, default "latest")."""
    params = [("service", "WMS"), ("version", "1.1.1"), ("request", "GetMap"),
              ("layers", MLI_LAYER), ("styles", MLI_STYLE),
              ("format", "image/png"), ("transparent", "true"),
              ("srs", "EPSG:4326"),
              ("bbox", ",".join(str(float(v)) for v in bbox)),
              ("width", str(int(width))), ("height", str(int(height)))]
    if time_iso:
        params.append(("time", str(time_iso)))
    return MLI_WMS_URL + "?" + urllib.parse.urlencode(params)


def decode_afa_png(body):
    """PNG GetMap (bytes) -> mask numpy bool dei pixel ATTIVI (alpha > 0).

    Formati accettati (quelli che EUMETView restituisce con transparent=true):
    RGBA, gray+alpha, palette+tRNS; altri -> ValueError (nessun conteggio
    inventato). rasterio e' importato qui (lazy): GDAL pesante e percorso
    solo MLI, il percorso DPC non lo tocca."""
    if not isinstance(body, (bytes, bytearray)) or bytes(body[:8]) != PNG_MAGIC:
        raise ValueError("mli_not_png")
    import rasterio                                    # lazy (GDAL)
    with warnings.catch_warnings():                    # NotGeoreferenced (BytesIO)
        warnings.simplefilter("ignore")
        with rasterio.open(io.BytesIO(bytes(body))) as ds:
            bands = ds.count
            if bands >= 4:
                alpha = ds.read(4)
            elif bands == 2:
                alpha = ds.read(2)
            elif bands == 1:
                lut = np.zeros(256, dtype=np.uint8)
                for idx, rgba in ds.colormap(1).items():
                    lut[int(idx)] = int(rgba[3])
                alpha = lut[np.clip(ds.read(1), 0, 255)]
            else:
                raise ValueError(f"mli_png_no_alpha:{bands}")
    return np.asarray(alpha) > 0


def sample_afa_field(mask, bbox, center_lonlat, radius_km=DEFAULT_RADIUS_KM):
    """Pixel ATTIVI nel disco di `radius_km` attorno al centro (campionatore MLI).

    mask: 2D array bool (True = attivo); bbox = (minx, miny, maxx, maxy);
    centro -> griglia pixel lineare sulla bbox, raggio in km convertito in
    ellisse di pixel (111.19 km/grad con Raggio IUGG, lon scalata su cos(lat);
    distorsione trascurabile a 30 km). Centro FUORI dalla bbox ->
    in_coverage=False (nessun campionamento inventato, il chiamante degrada).
    Ritorna {"in_coverage", "count_near", "count_disk"}; strength si calcola
    come count_near / count_disk (frazione del disco coperta da AFA)."""
    arr = np.asarray(mask)
    if arr.ndim != 2:
        raise ValueError("mli_mask_not_2d")
    height, width = arr.shape
    lon0, lat0, lon1, lat1 = (float(v) for v in bbox)
    lon, lat = _normalize_center(center_lonlat) or (None, None)
    if lon is None:
        raise ValueError("mli_center_invalid")
    px_lon = (lon1 - lon0) / width
    px_lat = (lat1 - lat0) / height
    cx = (lon - lon0) / px_lon - 0.5
    cy = (lat1 - lat) / px_lat - 0.5
    if not (-0.5 <= cx <= width - 0.5 and -0.5 <= cy <= height - 0.5):
        return {"in_coverage": False, "count_near": 0, "count_disk": 0}
    km_lat = _ltg.EARTH_RADIUS_KM * math.pi / 180.0
    km_lon = km_lat * max(1e-6, math.cos(math.radians(lat)))
    rx = float(radius_km) / (px_lon * km_lon)
    ry = float(radius_km) / (px_lat * km_lat)
    i0 = max(0, int(math.floor(cx - rx)))
    i1 = min(width - 1, int(math.ceil(cx + rx)))
    j0 = max(0, int(math.floor(cy - ry)))
    j1 = min(height - 1, int(math.ceil(cy + ry)))
    if i1 < i0 or j1 < j0:
        return {"in_coverage": True, "count_near": 0, "count_disk": 0}
    xs = (np.arange(i0, i1 + 1) - cx) / rx
    ys = (np.arange(j0, j1 + 1) - cy) / ry
    disk = (ys[:, None] ** 2 + xs[None, :] ** 2) <= 1.0
    sub = arr[j0:j1 + 1, i0:i1 + 1]
    return {"in_coverage": True,
            "count_near": int(np.count_nonzero(sub & disk)),
            "count_disk": int(np.count_nonzero(disk))}


class MliWmsProvider(LightningProvider):
    """Fulmini MLI EUMETSAT via WMS anonimo EUMETView (mtg_fd:li_afa).

    Accumulated Flash Area (CC-BY-4.0, nessun secret): UNA GetMap Italia per
    slot scaricata e cachata per run (fetch_field), campionata da TUTTI i
    candidati (nessuna richiesta per candidato) + UNA GetMap del slot
    precedente per il trend. Proxy = pixel attivi nel disco di radius_km
    attorno al centro: strikes=[] e evidence con MLI_NOTE ("AFA proxy (no
    flash puntuali)") — nessuna posizione lat/lon e' inventata.

    Robustezza: slot da 5 min, backoff di MLI_MAX_BACKOFF_STEPS slot con retry
    per-slot (LIGHTNING_RETRY_PER_SLOT) su TUTTI gli errori transitori
    (HTTPError 5xx/403/..., URLError, TimeoutError, OSError, PNG non valido):
    nessun errore aborta la ricerca, si arretra al slot precedente. Delay fra
    tentativi via `_sleep` iniettabile (default time.sleep). Un campo
    decodificato entro MLI_MAX_STALENESS_MS -> available=True; MAI eccezione
    verso l'alto. `opener` iniettabile: (url, timeout_s) -> bytes, come DPC.
    """

    name = "mli"

    def __init__(self, network=True, opener=None, timeout_s=None, sleep=None):
        super().__init__(network=network)
        self.opener = opener          # iniettabile (test); None -> urllib
        self.timeout_s = timeout_s
        self._sleep = sleep if sleep is not None else time.sleep
        self._fields = {}             # cache per run: slot -> field dict

    def fetch_field(self, epoch_ms=None):
        """Campo AFA Italia per lo slot (UN fetch condiviso per run).

        Ritorna {available, reason, epoch_ms, anchor_ms, staleness_ms, mask};
        cache sia dei successi sia dei fallimenti (niente retry a ogni
        candidato). NON solleva per errori di rete/dato."""
        if not self.network:
            return {"available": False, "reason": "network_disabled",
                    "epoch_ms": None, "anchor_ms": None, "staleness_ms": None,
                    "mask": None}
        base = _slot_floor(epoch_ms if epoch_ms is not None
                           else _ltg.ltg_epoch_floor())
        if base in self._fields:
            return self._fields[base]
        try:
            out = self._fetch(base)
        except Exception as exc:                       # mai eccezione verso l'alto
            out = {"available": False,
                   "reason": f"error:{exc.__class__.__name__}",
                   "epoch_ms": base, "anchor_ms": None, "staleness_ms": None,
                   "mask": None}
        if len(self._fields) > 16:
            self._fields.clear()
        self._fields[base] = out
        return out

    def _http(self, url):
        """GET del PNG: opener iniettabile, default urllib con User-Agent."""
        timeout = (MLI_HTTP_TIMEOUT_S if self.timeout_s is None
                   else float(self.timeout_s))
        if self.opener is not None:
            return self.opener(url, timeout)
        req = urllib.request.Request(
            url, headers={"User-Agent": MLI_USER_AGENT, "Accept": "image/png"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _fetch(self, base):
        """Backoff di slot con retry per-slot su errori TRANSITORI (lag MTG/5xx).

        Ogni slot e' tentato fino a LIGHTNING_RETRY_PER_SLOT volte; un errore
        (HTTPError, URLError, TimeoutError, OSError, PNG non valido) NON aborta
        la ricerca: si registra l'ultimo errore, si attende `_sleep` e si passa
        allo slot precedente. Diagnostica (tentativi, slot provati, ultimo
        errore) nel `reason`. MAI eccezione verso l'alto."""
        attempts = 0
        slots_tried = 0
        last = "no_slot"
        for k in range(MLI_MAX_BACKOFF_STEPS + 1):
            slot = base - k * LTG_PERIOD_MS
            slots_tried += 1
            url = build_mli_getmap_url(_slot_iso(slot))
            mask = None
            for attempt in range(LIGHTNING_RETRY_PER_SLOT):
                attempts += 1
                try:
                    body = self._http(url)
                except urllib.error.HTTPError as exc:
                    last = f"http_{exc.code}"          # slot non pubblicato/5xx
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last = f"network:{exc.__class__.__name__}"
                except Exception as exc:               # opener inatteso
                    last = f"http_error:{exc.__class__.__name__}"
                else:
                    try:
                        mask = decode_afa_png(body)
                    except Exception as exc:
                        last = f"png_invalid:{exc}"
                if mask is not None:
                    break
                if attempt < LIGHTNING_RETRY_PER_SLOT - 1:
                    self._sleep(_retry_delay(attempt + 1))
            if mask is None:
                continue
            staleness = base - slot
            if staleness > MLI_MAX_STALENESS_MS:
                return {"available": False,
                        "reason": f"stale_mli:{staleness}ms",
                        "epoch_ms": base, "anchor_ms": slot,
                        "staleness_ms": staleness, "mask": None}
            return {"available": True, "reason": None, "epoch_ms": base,
                    "anchor_ms": slot, "staleness_ms": staleness, "mask": mask}
        return {"available": False,
                "reason": (f"mli_unavailable:{last}:attempts={attempts}"
                           f":slots_tried={slots_tried}"),
                "epoch_ms": base, "anchor_ms": None, "staleness_ms": None,
                "mask": None}

    def strikes_in_window(self, epoch_ms=None, window_slots=DEFAULT_WINDOW_SLOTS,
                          radius_km=DEFAULT_RADIUS_KM, center_lonlat=None):
        """Risposta pubblica MLI (stesso schema del contratto, v. modulo).

        window_slots e' IGNORATO: AFA e' gia' accumulato -> UNO scan per run
        (window_slots=slots_ok=1). count_near = pixel attivi nel disco;
        count_total = pixel attivi su tutta l'Italia; strength =
        count_near/count_disk; trend dal confronto con lo scan a
        -5 min (fetch_field cached; scan precedente mancante -> trend=None)."""
        try:
            radius = float(radius_km)
        except (TypeError, ValueError):
            radius = DEFAULT_RADIUS_KM
        try:
            return self._respond(epoch_ms, radius, _normalize_center(center_lonlat))
        except Exception as exc:                       # mai eccezione verso l'alto
            return unavailable(self.name, f"error:{exc.__class__.__name__}",
                               radius)

    def _respond(self, epoch_ms, radius, center):
        base = _slot_floor(epoch_ms if epoch_ms is not None
                           else _ltg.ltg_epoch_floor())
        cur = self.fetch_field(base)
        if not cur.get("available"):
            return unavailable(self.name, cur.get("reason"), radius)
        mask = cur["mask"]
        anchor = cur["anchor_ms"]
        total = int(np.count_nonzero(mask))
        out = unavailable(self.name, None, radius)
        out.update({
            "available": True,
            "reason": None,
            "epoch_ms": base,
            "anchor_ms": anchor,
            "staleness_ms": cur.get("staleness_ms"),
            "window_slots": 1,
            "slots_ok": 1,
            "center": center,
            "count_total": total,
            "note": MLI_NOTE,
            "attribution": MLI_ATTRIBUTION,
        })
        prev = self.fetch_field(anchor - LTG_PERIOD_MS)
        prev_mask = prev.get("mask") if prev.get("available") else None
        if center is None:
            # nessun anchor -> count_near/strength sono 0 (come per DPC: 0
            # = nessuna misura, non "nessun fulmine"); trend sui totali Italia.
            out["per_slot_counts"] = ([int(np.count_nonzero(prev_mask)), total]
                                      if prev_mask is not None else [total])
            counts = out["per_slot_counts"]
            out["trend"] = (_ltg.lightning_trend(counts)
                            if len(counts) == 2 else None)
            out["strength"] = 0.0
            return out
        sample = sample_afa_field(mask, MLI_BBOX, center, radius)
        if not sample["in_coverage"]:
            return unavailable(self.name, "center_outside_coverage", radius)
        count_near = sample["count_near"]
        out["count_near"] = count_near
        out["strength"] = (round(count_near / sample["count_disk"], 4)
                           if sample["count_disk"] else 0.0)
        out["score"] = round(100.0 * (out["strength"] or 0.0), 1)
        if prev_mask is not None:
            prev_count = sample_afa_field(prev_mask, MLI_BBOX, center,
                                          radius)["count_near"]
            out["per_slot_counts"] = [prev_count, count_near]
            out["trend"] = _ltg.lightning_trend([prev_count, count_near])
        else:
            out["per_slot_counts"] = [count_near]
            out["trend"] = None                      # secondo scan mancante
        return out


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_PROVIDERS = {}


def register_provider(name, factory):
    """Registra un provider (factory callable -> LightningProvider)."""
    key = str(name).strip().lower()
    if not key:
        raise UnknownProviderError("empty_provider_name")
    _PROVIDERS[key] = factory
    return key


def available_providers():
    """Nomi provider registrati (ordinati)."""
    return sorted(_PROVIDERS)


def get_provider(name="dpc", **kwargs):
    """Istanzia il provider registrato `name` (kwargs passati alla factory)."""
    key = str(name).strip().lower()
    if key not in _PROVIDERS:
        raise UnknownProviderError(
            f"unknown_lightning_provider:{key} "
            f"(disponibili: {', '.join(available_providers()) or 'nessuno'})")
    return _PROVIDERS[key](**kwargs)


register_provider("dpc", DpcBlitzProvider)
register_provider("mli", MliWmsProvider)   # default del motore (S4b)
