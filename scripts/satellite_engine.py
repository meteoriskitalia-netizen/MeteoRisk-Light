#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Satellite Engine (satellite_engine.py): scarica i frame satellitari EUMETSAT
(WMS view.eumetsat.int) e mantiene una finestra rolling di 25 slot per sorgente
(2 ore a passo di 5 minuti) in satellite/<sourceId>/<slotISO>.png.

Faithful port della logica dell'app (mri-light-1.1.0.3.html):
  - buildEumetsatLiveGetMap: GetMap EPSG:3857 su SATELLITE_EUROPE_BOUNDS
    [[22,-28],[72,55]] (width 2048, height proporzionale, stessa sequenza di
    parametri layers/styles/format/transparent/version/time/width/height/srs/bbox);
  - buildSyncTimeline + EUMETSAT_DATA_LAG_MS: time = ora corrente - 15 minuti,
    floor a 5 minuti (setUTCMinutes(floor(m/5)*5, 0, 0)), formato .000Z.

Output:
  satellite/<sourceId>/<slotISO>.png   slotISO = 2026-10-06T19-45-00Z
  satellite/manifest.json              generated_at + slot + sorgenti -> slot ISO
  - idempotente: frame gia' presente e valido -> skip (nessun download);
  - pruning: restano solo gli ultimi 25 slot per sorgente, i non-PNG sono rimossi;
  - il download e' indipendente per sorgente (un errore NON blocca le altre).

Exit codes:
   0 = tutte le sorgenti scaricate o gia' presenti
   3 = degradato: almeno una sorgente OK e almeno una in errore
   4 = errore: nessuna sorgente disponibile (run visibilmente fallita)
"""

import argparse
import datetime as _dt
import json
import math
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

SOURCES = {
    "eumetsat": "mtg_fd:ir105_hrfi",
    "eumetsat_airmass": "msg_fes:rgb_airmass",
    "eumetsat_geocolour": "mtg_fd:rgb_geocolour",
}
WMS_ENDPOINT = "https://view.eumetsat.int/geoserver/wms"
EU_WEST, EU_EAST = -28.0, 55.0
EU_SOUTH, EU_NORTH = 22.0, 72.0
WORLD_M = 40075016.685578488
FRAME_WIDTH = 2048
EUMETSAT_DATA_LAG_MS = 15 * 60 * 1000
SLOT_MINUTES = 5
WINDOW_SLOTS = 25
SLOT_STEP_S = SLOT_MINUTES * 60
HTTP_TIMEOUT_S = 60
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_BASE_S = 5.0
RETRY_JITTER_MAX_S = 1.5
USER_AGENT = common.APP_NAME + "/satellite-engine (non-commercial)"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def compute_slot(now=None):
    """Slot corrente: ora - EUMETSAT_DATA_LAG_MS, floor a 5 minuti UTC
    (buildSyncTimeline: setUTCMinutes(Math.floor(m/5)*5, 0, 0))."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    lagged = now - _dt.timedelta(milliseconds=EUMETSAT_DATA_LAG_MS)
    epoch = int(lagged.timestamp())
    epoch -= epoch % SLOT_STEP_S
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)


def slot_name(slot):
    """Nome filesystem-safe, identico per tutte le sorgenti (frame allineati)."""
    return slot.strftime("%Y-%m-%dT%H-%M-%SZ")


def slot_time(slot):
    return slot.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _q(value):
    return urllib.parse.quote(value, safe="!*'()")


def build_getmap_url(layer, iso, width=FRAME_WIDTH):
    x0 = (EU_WEST / 360.0) * WORLD_M
    x1 = (EU_EAST / 360.0) * WORLD_M
    radius = WORLD_M / (2.0 * math.pi)
    y0 = radius * math.log(math.tan(math.pi / 4.0 + EU_SOUTH * math.pi / 360.0))
    y1 = radius * math.log(math.tan(math.pi / 4.0 + EU_NORTH * math.pi / 360.0))
    height = max(64, int(round(width * (y1 - y0) / (x1 - x0))))
    bbox = ",".join(repr(v) for v in (x0, y0, x1, y1))
    return (WMS_ENDPOINT + "?&service=WMS&request=GetMap"
            + "&layers=" + _q(layer)
            + "&styles="
            + "&format=" + _q("image/png")
            + "&transparent=true&version=1.1.0"
            + "&time=" + _q(iso)
            + "&width=" + str(width) + "&height=" + str(height)
            + "&srs=" + _q("EPSG:3857")
            + "&bbox=" + bbox)


def is_png(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(8) == PNG_MAGIC
    except OSError:
        return False


def fetch_png(url):
    last_err = None
    for attempt in range(RETRY_ATTEMPTS):
        if attempt:
            delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1)) + random.uniform(0.0, RETRY_JITTER_MAX_S)
            print("[satellite_engine] retry %d/%d tra %.1fs: %s"
                  % (attempt, RETRY_ATTEMPTS - 1, delay, last_err))
            time.sleep(delay)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "image/png"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = resp.read()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        if not body.startswith(PNG_MAGIC):
            last_err = "risposta non-PNG (%d byte): %r" % (len(body), body[:160])
            continue
        return body
    raise RuntimeError("download fallito dopo %d tentativi: %s" % (RETRY_ATTEMPTS, last_err))


def process_source(source_id, layer, slot, out_root):
    src_dir = out_root / source_id
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / (slot_name(slot) + ".png")
    if is_png(target):
        size = target.stat().st_size
        print("[satellite_engine] slot=%s source=%s status=skip bytes=%d"
              % (slot_name(slot), source_id, size))
        return {"status": "skip", "bytes": size}
    url = build_getmap_url(layer, slot_time(slot))
    try:
        body = fetch_png(url)
        with open(target, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        if target.exists() and not is_png(target):
            target.unlink()
        print("[satellite_engine] slot=%s source=%s status=errore bytes=0 url=%s motivo=%s"
              % (slot_name(slot), source_id, url, exc))
        return {"status": "errore", "bytes": 0, "detail": str(exc)}
    print("[satellite_engine] slot=%s source=%s status=ok bytes=%d"
          % (slot_name(slot), source_id, len(body)))
    return {"status": "ok", "bytes": len(body)}


def prune_source(source_id, out_root):
    src_dir = out_root / source_id
    if not src_dir.is_dir():
        return
    valid, invalid = [], []
    for path in sorted(src_dir.glob("*.png")):
        (valid if is_png(path) else invalid).append(path)
    for path in invalid:
        path.unlink()
        print("[satellite_engine] source=%s status=rimosso file=%s motivo=non-PNG"
              % (source_id, path.name))
    for path in valid[:-WINDOW_SLOTS]:
        path.unlink()
        print("[satellite_engine] source=%s status=pruned file=%s" % (source_id, path.name))


def write_manifest(out_root, slot, generated_at):
    sources = {}
    for source_id in SOURCES:
        src_dir = out_root / source_id
        slots = []
        if src_dir.is_dir():
            slots = sorted(p.stem for p in src_dir.glob("*.png") if is_png(p))
        sources[source_id] = slots
    manifest = {"generated_at": generated_at, "slot": slot_name(slot), "sources": sources}
    with open(out_root / "manifest.json", "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return manifest


def parse_now(value, parser):
    if not value:
        return _dt.datetime.now(_dt.timezone.utc)
    try:
        parsed = _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now non è un ISO-8601 valido: %s" % value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.astimezone(_dt.timezone.utc)


def main():
    parser = argparse.ArgumentParser(
        description="Frame EUMETSAT (WMS) + finestra rolling 25 slot per sorgente.")
    parser.add_argument("--out-dir", default=None,
                        help="Directory output (default: <repo>/satellite).")
    parser.add_argument("--now", default=None,
                        help="Istante di riferimento ISO-8601 UTC (default: ora corrente).")
    parser.add_argument("--print-slot", action="store_true",
                        help="Stampa lo slot calcolato ed esce (nessuna richiesta di rete).")
    args = parser.parse_args()

    now = parse_now(args.now, parser)
    slot = compute_slot(now)
    if args.print_slot:
        print(slot_name(slot))
        return 0

    out_root = Path(args.out_dir) if args.out_dir else (common.REPO_ROOT / "satellite")
    out_root.mkdir(parents=True, exist_ok=True)
    print("[satellite_engine] slot=%s time=%s out=%s"
          % (slot_name(slot), slot_time(slot), out_root))

    results = {}
    for source_id, layer in SOURCES.items():
        results[source_id] = process_source(source_id, layer, slot, out_root)
    for source_id in SOURCES:
        prune_source(source_id, out_root)

    generated_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = write_manifest(out_root, slot, generated_at)

    ok = sum(1 for r in results.values() if r["status"] in ("ok", "skip"))
    failed = len(SOURCES) - ok
    print("[satellite_engine] esito slot=%s ok=%d fallite=%d frame=%s"
          % (slot_name(slot), ok, failed,
             {k: len(v) for k, v in manifest["sources"].items()}))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## Meteorisk Satellite Engine — EUMETSAT WMS", "",
                 "slot: `%s` — time WMS: `%s`" % (slot_name(slot), slot_time(slot)), ""]
        for source_id in SOURCES:
            res = results[source_id]
            lines.append("- `%s`: **%s** — %d byte — %d frame nella finestra"
                         % (source_id, res["status"], res["bytes"],
                            len(manifest["sources"][source_id])))
        lines.append("")
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    if failed == 0:
        return 0
    if ok:
        print("[satellite_engine] DEGRADATO: %d sorgenti OK, %d in errore (run comunque valida)."
              % (ok, failed))
        return 3
    print("[satellite_engine] ERRORE: nessuna sorgente disponibile per lo slot %s."
          % slot_name(slot))
    return 4


if __name__ == "__main__":
    sys.exit(main())
