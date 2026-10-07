#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Satellite Engine (satellite_engine.py): scarica i frame satellitari EUMETSAT
(WMS view.eumetsat.int) e mantiene una finestra rolling di 25 slot per sorgente
(2 ore a passo di 5 minuti) in satellite/<sourceId>/<slotISO>.png.

Faithful port della logica dell'app (mri-light-1.1.0.7.html):
  - buildEumetsatLiveGetMap: GetMap EPSG:3857 su SATELLITE_EUROPE_BOUNDS
    [[22,-28],[72,55]] (width 2048, height proporzionale, stessa sequenza di
    parametri layers/styles/format/transparent/version/time/width/height/srs/bbox);
  - frame Italia: stessa formula su SATELLITE_ITALY_BOUNDS [[36.5,6.6],[47.2,18.8]]
    con ITALY_FRAME_WIDTH = 1024;
  - buildSyncTimeline + EUMETSAT_DATA_LAG_MS: time = ora corrente - 15 minuti,
    floor a 5 minuti (setUTCMinutes(floor(m/5)*5, 0, 0)), formato .000Z.

6 sorgenti: 3 Europa (2048px, ~4,2 MP per frame) + 3 Italia (1024px, ~1,3 MP
per frame, pixel -70% rispetto all'Europa).

Backfill: ogni run scarica tutti gli slot mancanti della finestra di 2 ore
(25 slot x 6 sorgenti), dallo slot corrente al piu' vecchio, su MAX_WORKERS
thread paralleli:
  - 0 richieste se la finestra e' gia' tutta presente e valida;
  - steady-state 0-6 richieste (0 con la finestra piena, fino a 6 quando manca
    solo lo slot corrente);
  - fino a 150 richieste al primo run (25 slot x 6 sorgenti).

Output:
  satellite/<sourceId>/<slotISO>.png   slotISO = 2026-10-06T19-45-00Z
  satellite/manifest.json              generated_at + slot + sorgenti -> slot ISO
  - idempotente: frame gia' presente e valido -> skip (nessun download);
  - pruning: restano solo gli ultimi 25 slot per sorgente, i non-PNG sono rimossi;
  - il download e' indipendente per sorgente (un errore NON blocca le altre);
  - i fallimenti sugli slot di backfill sono loggati e conteggiati ma non
    influenzano l'exit code (uno slot storico morto non fa fallire la run).

Exit codes (valutati sullo slot corrente):
   0 = tutte e 6 le sorgenti dello slot corrente scaricate o gia' presenti
   3 = degradato: sullo slot corrente almeno una sorgente OK e almeno una in errore
   4 = errore: sullo slot corrente nessuna sorgente disponibile (run visibilmente
       fallita)
"""

import argparse
import datetime as _dt
import json
import math
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common

WMS_ENDPOINT = "https://view.eumetsat.int/geoserver/wms"
EU_WEST, EU_EAST = -28.0, 55.0
EU_SOUTH, EU_NORTH = 22.0, 72.0
IT_WEST, IT_EAST = 6.6, 18.8
IT_SOUTH, IT_NORTH = 36.5, 47.2
WORLD_M = 40075016.685578488
FRAME_WIDTH = 2048
ITALY_FRAME_WIDTH = 1024
SOURCES = {
    "eumetsat_italy":           {"layer": "mtg_fd:ir105_hrfi",   "bounds": (IT_WEST, IT_EAST, IT_SOUTH, IT_NORTH), "width": ITALY_FRAME_WIDTH},
    "eumetsat_airmass_italy":   {"layer": "msg_fes:rgb_airmass", "bounds": (IT_WEST, IT_EAST, IT_SOUTH, IT_NORTH), "width": ITALY_FRAME_WIDTH},
    "eumetsat_geocolour_italy": {"layer": "mtg_fd:rgb_geocolour","bounds": (IT_WEST, IT_EAST, IT_SOUTH, IT_NORTH), "width": ITALY_FRAME_WIDTH},
    "eumetsat":                 {"layer": "mtg_fd:ir105_hrfi",   "bounds": (EU_WEST, EU_EAST, EU_SOUTH, EU_NORTH), "width": FRAME_WIDTH},
    "eumetsat_airmass":         {"layer": "msg_fes:rgb_airmass", "bounds": (EU_WEST, EU_EAST, EU_SOUTH, EU_NORTH), "width": FRAME_WIDTH},
    "eumetsat_geocolour":       {"layer": "mtg_fd:rgb_geocolour","bounds": (EU_WEST, EU_EAST, EU_SOUTH, EU_NORTH), "width": FRAME_WIDTH},
}
EUMETSAT_DATA_LAG_MS = 15 * 60 * 1000
SLOT_MINUTES = 5
WINDOW_SLOTS = 25
SLOT_STEP_S = SLOT_MINUTES * 60
HTTP_TIMEOUT_S = 60
RETRY_ATTEMPTS = 4
RETRY_BACKOFF_BASE_S = 5.0
RETRY_JITTER_MAX_S = 1.5
MAX_WORKERS = 4
USER_AGENT = common.APP_NAME + "/satellite-engine (non-commercial)"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

_LOG_LOCK = threading.Lock()


def _log(msg):
    """print con lock: i download girano su thread concorrenti."""
    with _LOG_LOCK:
        print(msg)


def compute_slot(now=None):
    """Slot corrente: ora - EUMETSAT_DATA_LAG_MS, floor a 5 minuti UTC
    (buildSyncTimeline: setUTCMinutes(Math.floor(m/5)*5, 0, 0))."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    lagged = now - _dt.timedelta(milliseconds=EUMETSAT_DATA_LAG_MS)
    epoch = int(lagged.timestamp())
    epoch -= epoch % SLOT_STEP_S
    return _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)


def window_slots(slot):
    """Finestra di WINDOW_SLOTS slot: dal corrente indietro di SLOT_STEP_S
    (slot corrente per primo, poi dal piu' recente al piu' vecchio)."""
    return [slot - _dt.timedelta(seconds=SLOT_STEP_S * i)
            for i in range(WINDOW_SLOTS)]


def slot_name(slot):
    """Nome filesystem-safe, identico per tutte le sorgenti (frame allineati)."""
    return slot.strftime("%Y-%m-%dT%H-%M-%SZ")


def slot_time(slot):
    return slot.strftime("%Y-%m-%dT%H:%M:%S") + ".000Z"


def _q(value):
    return urllib.parse.quote(value, safe="!*'()")


def build_getmap_url(layer, iso, width=FRAME_WIDTH, bounds=None):
    if bounds is None:
        bounds = (EU_WEST, EU_EAST, EU_SOUTH, EU_NORTH)
    west, east, south, north = bounds
    x0 = (west / 360.0) * WORLD_M
    x1 = (east / 360.0) * WORLD_M
    radius = WORLD_M / (2.0 * math.pi)
    y0 = radius * math.log(math.tan(math.pi / 4.0 + south * math.pi / 360.0))
    y1 = radius * math.log(math.tan(math.pi / 4.0 + north * math.pi / 360.0))
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
            _log("[satellite_engine] retry %d/%d tra %.1fs: %s"
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


def process_source(source_id, src, slot, out_root):
    src_dir = out_root / source_id
    src_dir.mkdir(parents=True, exist_ok=True)
    target = src_dir / (slot_name(slot) + ".png")
    if is_png(target):
        size = target.stat().st_size
        _log("[satellite_engine] slot=%s source=%s status=skip bytes=%d"
             % (slot_name(slot), source_id, size))
        return {"status": "skip", "bytes": size}
    url = build_getmap_url(src["layer"], slot_time(slot), src["width"], src["bounds"])
    try:
        body = fetch_png(url)
        with open(target, "wb") as fh:
            fh.write(body)
    except Exception as exc:  # noqa: BLE001
        if target.exists() and not is_png(target):
            target.unlink()
        _log("[satellite_engine] slot=%s source=%s status=errore bytes=0 url=%s motivo=%s"
             % (slot_name(slot), source_id, url, exc))
        return {"status": "errore", "bytes": 0, "detail": str(exc)}
    _log("[satellite_engine] slot=%s source=%s status=ok bytes=%d"
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
        _log("[satellite_engine] source=%s status=rimosso file=%s motivo=non-PNG"
             % (source_id, path.name))
    for path in valid[:-WINDOW_SLOTS]:
        path.unlink()
        _log("[satellite_engine] source=%s status=pruned file=%s" % (source_id, path.name))


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
        description="Frame EUMETSAT (WMS) + backfill finestra rolling 25 slot per sorgente.")
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
    slots = window_slots(slot)
    _log("[satellite_engine] slot=%s time=%s out=%s backfill=%d slot x %d sorgenti"
         % (slot_name(slot), slot_time(slot), out_root, len(slots), len(SOURCES)))

    results = {}
    backfill = {"ok": 0, "skip": 0, "errore": 0}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        pending = {pool.submit(process_source, source_id, src, slot, out_root): source_id
                   for source_id, src in SOURCES.items()}
        for future in as_completed(pending):
            results[pending[future]] = future.result()
        pending = {pool.submit(process_source, source_id, src, s, out_root): source_id
                   for s in slots[1:] for source_id, src in SOURCES.items()}
        for future in as_completed(pending):
            backfill[future.result()["status"]] += 1
    for source_id in SOURCES:
        prune_source(source_id, out_root)

    generated_at = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest = write_manifest(out_root, slot, generated_at)

    counts = {"ok": 0, "skip": 0, "errore": 0}
    for res in results.values():
        counts[res["status"]] += 1
    ok = counts["ok"] + counts["skip"]
    failed = len(SOURCES) - ok
    _log("[satellite_engine] esito slot=%s ok=%d fallite=%d frame=%s"
         % (slot_name(slot), ok, failed,
            {k: len(v) for k, v in manifest["sources"].items()}))
    _log("[satellite_engine] run: ok=%d skip=%d falliti=%d (slot corrente) | "
         "backfill slot=%d: ok=%d skip=%d falliti=%d"
         % (counts["ok"], counts["skip"], counts["errore"], len(slots) - 1,
            backfill["ok"], backfill["skip"], backfill["errore"]))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = ["## Meteorisk Satellite Engine — EUMETSAT WMS", "",
                 "slot: `%s` — time WMS: `%s`" % (slot_name(slot), slot_time(slot)), ""]
        for source_id, src in SOURCES.items():
            res = results[source_id]
            lines.append("- `%s` (%s): **%s** — %d byte — %d frame nella finestra"
                         % (src["layer"], source_id, res["status"], res["bytes"],
                            len(manifest["sources"][source_id])))
        lines.append("")
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    if failed == 0:
        return 0
    if ok:
        _log("[satellite_engine] DEGRADATO: %d sorgenti OK, %d in errore (run comunque valida)."
             % (ok, failed))
        return 3
    _log("[satellite_engine] ERRORE: nessuna sorgente disponibile per lo slot %s."
         % slot_name(slot))
    return 4


if __name__ == "__main__":
    sys.exit(main())
