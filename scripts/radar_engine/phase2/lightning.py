#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Phase 2 — lightning.py (FULMINI / LTG, EXPERIMENTAL)

Fonte fulmini DPC (lettera A0, 00_ricerca/lettera_a0.md, EVIDENZA):
  - il layer `radar:ltg*` NON ESISTE sul WMS/WMTS GeoWebCache DPC (caps =
    12 layer radar:<prodotto> senza LTG; GetMap `radar:ltg` -> HTTP 400
    "Unknown layer"): NON configurare alcun layer fulmini WMS;
  - la fonte reale e' il file binario Blitz v2 su S3, aggiornamento 5 minuti
    (documentazione DPC: LTG ogni 10 min, oggetti ogni 5 min):
        https://s3-prod-dpc-radar-webp-cache.s3.eu-south-1.amazonaws.com/
            LGT/lgt_5min_<epoch_ms>.bin
    verificato live in A0: 200 per l'ultimo slot pubblicato, 403 per slot
    non ancora pubblicati (bucket senza ListBucket -> 403 = oggetto assente).

Struttura del file Blitz v2 (DECODER VERIFICATO sui campioni reali
00_ricerca/lgt_sample.bin e lgt_sample2.bin: count 166/169, byte consumati
1007/1007 e 1025/1025, eta/lon ESATTAMENTE nei range della lettera A0):
  byte 0            = versione (2)
  uint16 BE         = count (numero fulmini)
  poi `count` record: 6 byte, oppure 8 byte se il byte d'indice 5 vale 255
      lon_raw = u16BE(b[0:2]) + ((b[4]>>6)&3)<<16   lon = lon_raw*360/2^18-180
      lat_raw = u16BE(b[2:4]) + ((b[4]>>4)&3)<<16   lat = lat_raw*180/2^18-90
      (griglia 2^18 = 262144)
  byte consumati totali DEVONO essere esattamente len(content): altrimenti
  ValueError (nessun parse parziale).

Utility WMS (non per i fulmini, che non esistono in WMS): build_getmap_url e
count_png_nontransparent restano a disposizione per frame generici (radar:vmi /
radar:ir108): la lettera A0 ha verificato HTTP 200 image/png SOLO con bbox
allineata alla griglia GWC (tile 256 px, span 11.25 gradi, risoluzione
0.0439453125 deg/px) e WIDTH=HEIGHT=256; il TIME NON viene onorato dal GWC.
Il decoder PNG e' stdlib (zlib), senza PIL; verificato sui PNG reali salvati in
00_ricerca (EUMETSAT RGBA 512x384, DPC RGBA/gray+alpha 256x256).

Nessuna dipendenza da `requests` (assente nel venv): urllib come in
source_dpc.py. Funzioni pure + un solo wrapper di rete (fetch_ltg).
"""

import struct
import urllib.error
import urllib.parse
import urllib.request
import zlib

# ---------------------------------------------------------------------------
# Costanti — DEFAULT (EVIDENZA: lettera A0, verifiche live)
# ---------------------------------------------------------------------------
LTG_S3_URL = ("https://s3-prod-dpc-radar-webp-cache.s3.eu-south-1.amazonaws.com"
              "/LGT/lgt_5min_{epoch_ms}.bin")
LTG_PERIOD_MS = 5 * 60 * 1000        # cadenza file (A0: lgt_5min_*)
# Backoff su 403 (slot non ancora pubblicato): misurato live 2026-10-07 il
# primo slot disponibile e' a 9 passi (45 min di ritardo di pubblicazione)
# -> 12 passi (1 ora) con margine. Ogni 403 e' una risposta piccola.
LTG_MAX_BACKOFF_STEPS = 12
LTG_HTTP_TIMEOUT_S = 30
# WMS DPC (solo frame generici, NON fulmini): URL e bbox VERIFICATI 200/GetMap
DEFAULT_WMS_URL = "https://radar-geowebcache.protezionecivile.it/service/wms"
DEFAULT_WMS_LAYER = "radar:vmi"
DEFAULT_BBOX = (0.0, 33.75, 11.25, 45.0)   # bbox allineata alla griglia GWC
DEFAULT_SIZE = 256                          # tile 256 px (GWC)
DEFAULT_SRS = "EPSG:4326"

# Score (EXPERIMENTAL: soglie non calibrate su casi reali)
LIGHTNING_THRESHOLD_RATE = 100.0    # fulmini per finestra 5 minuti di riferm.
LIGHTNING_JUMP_REF = 100.0          # diff cumulata di riferimento
LIGHTNING_SCORE_WEIGHTS = {"rate": 0.7, "jump": 0.3}   # somma 1.00
LIGHTNING_TREND_WINDOW = 4          # max sample per il diff cumulato (2-4)

_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class LightningFetchError(Exception):
    """S3 LTG / GetMap WMS non riuscito, o payload non interpretabile.

    Allineabile a radar_engine.models.SourceError a integrazione."""


# ---------------------------------------------------------------------------
# Pure: decodifica Blitz v2 (VERIFICATA sui 2 campioni reali in 00_ricerca)
# ---------------------------------------------------------------------------
def decode_blitz_v2(content):
    """Decodifica un file Fulmini DPC Blitz v2 in lista (lon, lat).

    Ritorna {"version", "count", "strikes": [(lon, lat)...], "consumed"}.
    Solleva ValueError per: versione != 2, record troncato, byte finali non
    consumati (parse parziale vietato: nessuna posizione inventata).
    Verifica di regressione: 00_ricerca/lgt_sample.bin -> count 166,
    consumed 1007/1007, lon [0.935..12.686], lat [36.759..45.887];
    lgt_sample2.bin -> 169, 1025/1025, lon [2.210..12.203],
    lat [38.154..43.615] (range della lettera A0)."""
    if not isinstance(content, (bytes, bytearray)) or len(content) < 5:
        raise ValueError("blitz_too_short")
    if content[0] != 2:
        raise ValueError(f"blitz_version_unsupported:{content[0]}")
    count = struct.unpack(">H", bytes(content[1:3]))[0]
    total = len(content)
    off = 3
    strikes = []
    for _ in range(count):
        if off + 6 > total:
            raise ValueError(f"blitz_truncated_record:off={off}")
        b = bytes(content[off:off + 6])
        lon_raw = struct.unpack(">H", b[0:2])[0] + (((b[4] >> 6) & 3) << 16)
        lat_raw = struct.unpack(">H", b[2:4])[0] + (((b[4] >> 4) & 3) << 16)
        rec_len = 8 if b[5] == 255 else 6
        if off + rec_len > total:
            raise ValueError(f"blitz_truncated_record:off={off}")
        lon = lon_raw * 360.0 / 262144.0 - 180.0
        lat = lat_raw * 180.0 / 262144.0 - 90.0
        strikes.append((round(lon, 6), round(lat, 6)))
        off += rec_len
    if off != total:
        raise ValueError(f"blitz_bytes_mismatch:consumed={off}/total={total}")
    return {"version": 2, "count": count, "strikes": strikes,
            "consumed": off}


def ltg_epoch_floor(now=None, backoff_steps=0):
    """epoch_ms dell'ultimo slot LTG (multiplo di 5 minuti) <= `now` (UTC).

    backoff_steps: slot indietro (0 = slot corrente, 1 = quello precedente...).
    Deterministico dato l'istante; nessuna I/O."""
    import datetime as _dt
    if now is None:
        now = _dt.datetime.now(_dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    epoch_ms = int(now.timestamp() * 1000)
    floor = epoch_ms - (epoch_ms % LTG_PERIOD_MS)
    return floor - backoff_steps * LTG_PERIOD_MS


# ---------------------------------------------------------------------------
# Pure: URL GetMap + decoder PNG (utility per frame generici, NON fulmini)
# ---------------------------------------------------------------------------
def build_getmap_url(wms_url=None, layer=None, bbox=None, width=None,
                     height=None, srs=None, time_iso=None):
    """Costruisce l'URL GetMap WMS 1.1.1 PNG trasparente (deterministico).

    bbox: (lon_min, lat_min, lon_max, lat_max). Default = bbox allineata alla
    griglia GWC DPC VERIFICATA (200 image/png); bbox arbitrarie danno 400
    ("exceeds 10% threshold", lettera A0). time_iso: parametro TIME (sul GWC
    DPC NON viene onorato, caps priva di Dimension time: documentato, non
    inventato)."""
    base = wms_url or DEFAULT_WMS_URL
    lay = layer or DEFAULT_WMS_LAYER
    bb = bbox or DEFAULT_BBOX
    w = int(width or DEFAULT_SIZE)
    h = int(height or DEFAULT_SIZE)
    srv = srs or DEFAULT_SRS
    params = [
        ("SERVICE", "WMS"),
        ("VERSION", "1.1.1"),
        ("REQUEST", "GetMap"),
        ("LAYERS", lay),
        ("STYLES", ""),
        ("SRS", srv),
        ("BBOX", ",".join(f"{float(v):.5f}" for v in bb)),
        ("WIDTH", str(w)),
        ("HEIGHT", str(h)),
        ("FORMAT", "image/png"),
        ("TRANSPARENT", "TRUE"),
    ]
    if time_iso:
        params.append(("TIME", str(time_iso)))
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params)


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw, height, stride, bpp):
    """De-filtra le scanline PNG (filter 0-4). Ritorna bytearray contiguo."""
    out = bytearray(height * stride)
    prev_start = -1
    pos = 0
    for row in range(height):
        if pos + 1 + stride > len(raw):
            raise ValueError("png_truncated_scanlines")
        ftype = raw[pos]
        line = raw[pos + 1:pos + 1 + stride]
        pos += 1 + stride
        start = row * stride
        if ftype == 0:                      # None
            out[start:start + stride] = line
        elif ftype == 1:                    # Sub
            for i in range(stride):
                left = out[start + i - bpp] if i >= bpp else 0
                out[start + i] = (line[i] + left) & 0xFF
        elif ftype == 2:                    # Up
            if prev_start >= 0:
                for i in range(stride):
                    out[start + i] = (line[i] + out[prev_start + i]) & 0xFF
            else:
                out[start:start + stride] = line
        elif ftype == 3:                    # Average
            for i in range(stride):
                left = out[start + i - bpp] if i >= bpp else 0
                up = out[prev_start + i] if prev_start >= 0 else 0
                out[start + i] = (line[i] + ((left + up) >> 1)) & 0xFF
        elif ftype == 4:                    # Paeth
            for i in range(stride):
                left = out[start + i - bpp] if i >= bpp else 0
                up = out[prev_start + i] if prev_start >= 0 else 0
                ul = out[prev_start + i - bpp] \
                    if (prev_start >= 0 and i >= bpp) else 0
                out[start + i] = (line[i] + _paeth(left, up, ul)) & 0xFF
        else:
            raise ValueError(f"png_filter_unknown:{ftype}")
        prev_start = start
    return out


def count_png_nontransparent(content):
    """Conta i pixel NON trasparenti di un PNG 8-bit non interlacciato.

    Supporta color type 0 (gray), 2 (RGB), 3 (palette), 4 (gray+alpha),
    6 (RGBA) con eventuale tRNS. Ritorna intero >= 0. ValueError per PNG non
    validi o non supportati (bit depth != 8, Adam7, deflate corrotto):
    nessun conteggio inventato. Verificato sui PNG reali in 00_ricerca
    (EUMETSAT 512x384 RGBA 8 bit; DPC 256x256 RGBA e gray+alpha 8 bit)."""
    if not isinstance(content, (bytes, bytearray)) or \
            content[:8] != _PNG_SIG:
        raise ValueError("not_a_png")
    pos = 8
    ihdr = None
    idat = []
    plte = None
    trns = None
    n = len(content)
    while pos + 8 <= n:
        length = int.from_bytes(content[pos:pos + 4], "big")
        ctype = bytes(content[pos + 4:pos + 8])
        data = content[pos + 8:pos + 8 + length]
        pos += 12 + length
        if ctype == b"IHDR":
            if len(data) < 13:
                raise ValueError("png_bad_ihdr")
            ihdr = {
                "width": int.from_bytes(data[0:4], "big"),
                "height": int.from_bytes(data[4:8], "big"),
                "bit_depth": data[8],
                "color_type": data[9],
                "interlace": data[12],
            }
        elif ctype == b"IDAT":
            idat.append(bytes(data))
        elif ctype == b"PLTE":
            plte = bytes(data)
        elif ctype == b"tRNS":
            trns = bytes(data)
        elif ctype == b"IEND":
            break
    if ihdr is None:
        raise ValueError("png_missing_ihdr")
    if ihdr["bit_depth"] != 8:
        raise ValueError(f"png_bit_depth_unsupported:{ihdr['bit_depth']}")
    if ihdr["interlace"] != 0:
        raise ValueError("png_interlaced_unsupported")
    ct = ihdr["color_type"]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ct)
    if channels is None:
        raise ValueError(f"png_color_type_unsupported:{ct}")
    if not idat:
        raise ValueError("png_missing_idat")
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise ValueError(f"png_zlib_error:{exc}") from exc

    width, height = ihdr["width"], ihdr["height"]
    stride = width * channels
    pixels = _unfilter(raw, height, stride, channels)

    count = 0
    if ct == 6:                              # RGBA
        for i in range(3, len(pixels), 4):
            if pixels[i] != 0:
                count += 1
    elif ct == 4:                            # Gray + alpha
        for i in range(1, len(pixels), 2):
            if pixels[i] != 0:
                count += 1
    elif ct == 3:                            # Palette (+ tRNS)
        if plte is None:
            raise ValueError("png_missing_plte")
        alpha = list(trns) if trns else []
        for idx in pixels:
            a = alpha[idx] if idx < len(alpha) else 255
            if a != 0:
                count += 1
    elif ct == 2:                            # RGB (+ tRNS colore)
        trans = tuple(trns[:3]) if trns and len(trns) >= 3 else None
        if trans is None:
            count = width * height
        else:
            for i in range(0, len(pixels), 3):
                if (pixels[i], pixels[i + 1], pixels[i + 2]) != trans:
                    count += 1
    else:                                    # ct == 0, gray (+ tRNS)
        if trns and len(trns) >= 2:
            trans = int.from_bytes(trns[:2], "big")
            for g in pixels:
                if g != trans:
                    count += 1
        else:
            count = width * height
    return int(count)


# ---------------------------------------------------------------------------
# Pure: trend e score
# ---------------------------------------------------------------------------
def lightning_trend(rate_list):
    """Trend/jump del tasso fulmini (famiglia Gatlin): diff cumulato.

    rate_list = conteggi/numeri per finestra 5 minuti (dal piu' vecchio al
    piu' recente). Finestra = ultimi 2..4 sample (LIGHTNING_TREND_WINDOW=4).
    Ritorna dict:
      jump      = rate[-1] - rate[0] della finestra (diff CUMULATO)
      trend     = jump / (n_sample - 1) (variazione media per sample)
      window    = n sample usati, n_samples = input validi
      direction = 'up' | 'down' | 'flat'
    Meno di 2 sample validi -> jump/trend 0, direction 'flat' (dato
    insufficiente, nessun trend inventato)."""
    vals = []
    for r in (rate_list or []):
        try:
            v = float(r)
        except (TypeError, ValueError):
            continue
        if v == v and v not in (float("inf"), float("-inf")):
            vals.append(v)
    n = len(vals)
    if n < 2:
        return {"jump": 0.0, "trend": 0.0, "window": 0, "n_samples": n,
                "direction": "flat"}
    win = vals[-min(n, LIGHTNING_TREND_WINDOW):]
    jump = win[-1] - win[0]
    trend = jump / (len(win) - 1)
    if jump > 0:
        direction = "up"
    elif jump < 0:
        direction = "down"
    else:
        direction = "flat"
    return {"jump": round(jump, 2), "trend": round(trend, 2),
            "window": len(win), "n_samples": n, "direction": direction}


def lightning_score(rate, jump, threshold=None):
    """Score fulmini 0-100 (float, 1 decimale) da tasso e jump.

    rate = fulmini nella finestra 5 minuti (count Blitz). score = 100 *
    (w_rate * min(rate/threshold, 1) + w_jump * min(max(jump,0)/JUMP_REF, 1)),
    pesi LIGHTNING_SCORE_WEIGHTS (somma 1.00). rate <= 0 -> 0 (nessun
    fulmine = nessun merito). Soglie EXPERIMENTAL (non calibrate)."""
    thr = float(LIGHTNING_THRESHOLD_RATE if threshold is None
                else threshold)
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return 0.0
    if not (r == r) or r <= 0.0:        # None/NaN/<=0 -> 0
        return 0.0
    try:
        j = float(jump)
    except (TypeError, ValueError):
        j = 0.0
    if not (j == j):
        j = 0.0
    rate_t = min(r / max(thr, 1e-9), 1.0)
    jump_t = min(max(j, 0.0) / LIGHTNING_JUMP_REF, 1.0)
    w = LIGHTNING_SCORE_WEIGHTS
    return round(100.0 * (w["rate"] * rate_t + w["jump"] * jump_t), 1)


# ---------------------------------------------------------------------------
# Wrapper I/O
# ---------------------------------------------------------------------------
def fetch_ltg(epoch_ms=None, url_template=None, timeout_s=None,
              max_backoff_steps=None, opener=None):
    """WRAPPER DI RETE: scarica l'ultimo frame Fulmini Blitz v2 da S3 DPC.

    epoch_ms: slot esplicito (tentativo singolo). Se None, parte dallo slot
    corrente (multiplo di 5 minuti) e, su HTTP 403 (oggetto non ancora
    pubblicato: bucket senza ListBucket -> 403 = assente), arretra fino a
    max_backoff_steps slot (default LTG_MAX_BACKOFF_STEPS).
    opener: callabile (url, timeout_s) -> bytes, per test/Injection; default
    urllib. Ritorna {"epoch_ms", "url", "count", "strikes": [(lon,lat)...],
    "version", "attempts"}. Solleva LightningFetchError se tutti i tentativi
    falliscono o il payload non e' Blitz v2 valido (nessun conteggio fittizio).
    NOTA: il decoder fa da guardia -> ValueError -> LightningFetchError."""
    tpl = url_template or LTG_S3_URL
    to = float(LTG_HTTP_TIMEOUT_S if timeout_s is None else timeout_s)
    if opener is None:
        def opener(url, timeout_s):  # noqa: F811
            req = urllib.request.Request(url, method="GET",
                                         headers={"Accept": "*/*"})
            try:
                with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 403:
                    return None          # assente (non ancora pubblicato)
                raise LightningFetchError(f"http_{exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise LightningFetchError(
                    f"network:{exc.__class__.__name__}") from exc

    if epoch_ms is not None:
        slots = [int(epoch_ms)]
    else:
        steps = (LTG_MAX_BACKOFF_STEPS if max_backoff_steps is None
                 else int(max_backoff_steps))
        slots = [ltg_epoch_floor(backoff_steps=k) for k in range(steps + 1)]

    last_reason = "no_slot"
    for i, slot in enumerate(slots):
        url = tpl.format(epoch_ms=slot)
        content = opener(url, to)
        if content is None:
            last_reason = "not_published_403"
            continue
        try:
            parsed = decode_blitz_v2(content)
        except ValueError as exc:
            raise LightningFetchError(f"blitz_invalid:{exc}") from exc
        return {"epoch_ms": slot, "url": url, "count": parsed["count"],
                "strikes": parsed["strikes"], "version": parsed["version"],
                "attempts": i + 1}
    raise LightningFetchError(f"ltg_unavailable:{last_reason}")
