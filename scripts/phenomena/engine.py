#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phenomena — engine.py: motore phenomena-verify (HAIL + VORTEX), ENGINE-ONLY.

    python scripts/phenomena/engine.py --radar-dir data/radar \
        --out-dir data/phenomena [--lightning-provider mli] [--no-network]

Lette dai derivati radar (sola lettura: data/radar/{latest.json,
supercells.json, tracks.json, storms.geojson}) e scritte ATOMICHE in
data/phenomena/{events.json, badges.json}.

Decisioni documentate:
  - latest.json assente o status != "ok" o radar_timestamp assente ->
    NESSUN nuovo evento: si riscrive events.json solo con il prune delle 24h e
    badges.json con badges vuoti (con radar degradato si preferisce non
    pubblicare piuttosto che badge costruiti su dati incompleti); exit 0.
  - provider fulmini DEFAULT "mli" (MLI EUMETSAT via WMS anonimo EUMETView,
    nessun secret); "dpc" resta selezionabile con --lightning-provider.
    provider unavailable / --no-network -> available=False -> SOLO tier SUSPECT
    (nessuna promozione, nessun crash); H0 (freezing level) saltato.
  - H0 e' un singolo fetch NON bloccante per run: se manca, l'evidence cambia
    solo il campo freezing_level_* / una label, MAI il tier.
  - idempotente: stesso radar_timestamp + stessi input -> stessi eventi e badge
    (merge per id mai duplicato, eventi/badge ordinati per id).
  - NO-OP GUARD: events.json/badges.json vengono riscritti SOLO se il
    contenuto sostanziale cambia (events.write_json_if_changed, campi volubili
    generated_at/radar_timestamp ignorati) -> run consecutive senza variazioni
    non toccano i file e il workflow non committa (~144 commit/giorno senza
    variazioni, il problema del churn di commit S4a).
  - il prune della finestra 24h e' ancorato AL RADAR_TIMESTAMP (tempo dei
    dati), non all'orologio: una run non puo' buttare gli eventi che sta
    appena scrivendo solo perche' l'orologio va piu' avanti dei dati (dev/
    stallo pipeline). I badge restano comunque vuoti se il radar e' degradato
    (status != ok) o se last_seen non coincide col frame corrente.
  - provider lightning ignoto -> UnknownProviderError -> exit 1 (nessun
    fallback silenzioso); ogni altro errore imprevisto -> stderr + exit 1
    (nessun output scritto a meta': gli atomi sono events.json/badges.json).

NOTE DI RETE: l'unico modo per fare I/O di rete e' run(network=True). I test
iniettano opener (DPC/MLI) e h0_client (Open-Meteo) e non toccano la rete; la
CLI `--no-network` disattiva entrambi.
"""

import argparse
import json
import math
import os
import sys

# Bootstrap: rende importabili `radar_engine` e `phenomena` anche quando il
# file viene eseguito direttamente (python scripts/phenomena/engine.py).
_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from phenomena import WINDOW_HOURS
from phenomena import events as ev_store
from phenomena import hail as hail_mod
from phenomena import lightning as lightning_mod
from phenomena import vortex as vortex_mod

LOG = "[phenomena]"
DEFAULT_RADAR_DIR = "data/radar"
DEFAULT_OUT_DIR = "data/phenomena"
DEFAULT_PROVIDER = "mli"  # default S4b: provider senza secret, retroattivo via gitignore
OBS_DEDUP_KM = 8.0  # dedup spaziale: osservazioni entro questa distanza = stessa tempesta


def _num(value):
    """float finite oppure None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _haversine_km(a, b):
    """Distanza sferica (km) fra due [lon, lat]; None se un punto non e' valido.

    Funzione pura, nessuna dipendenza esterna: usata solo per la dedup spaziale
    delle osservazioni (cella/storm_object coincidenti = stesso fenomeno)."""
    if not a or not b or len(a) < 2 or len(b) < 2:
        return None
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (math.sin(dlat / 2.0) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2.0) ** 2)
    return 2.0 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


def _load_json(path, label):
    """Legge un input JSON; assente/corrotto/non-dict -> {} + warn su stderr."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"{LOG} WARN input mancante: {label}", file=sys.stderr)
        return {}
    except (OSError, ValueError) as exc:
        print(f"{LOG} WARN input illeggibile {label}: "
              f"{exc.__class__.__name__}", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"{LOG} WARN input non-dict {label}", file=sys.stderr)
        return {}
    return data


def _morph_from_storms(storms):
    """Morfologia per track_id cella dai Point di storms.geojson.

    Max eccentricity, min solidity, max compattita' su TUTTI i frame della
    cella (i valori medi sono gia' eccentrici: il gate serve a individuare le
    code, non a contare celle ovali)."""
    morph = {}
    for feat in (storms or {}).get("features") or []:
        if not isinstance(feat, dict):
            continue
        if (feat.get("geometry") or {}).get("type") != "Point":
            continue
        props = feat.get("properties") or {}
        tid = props.get("track_id")
        if tid is None:
            continue
        cell = morph.setdefault(str(tid), {"eccentricity_max": None,
                                           "solidity_min": None,
                                           "compactness_max": None})
        ecc = _num(props.get("eccentricity"))
        if ecc is not None and (cell["eccentricity_max"] is None
                                or ecc > cell["eccentricity_max"]):
            cell["eccentricity_max"] = ecc
        sol = _num(props.get("solidity"))
        if sol is not None and (cell["solidity_min"] is None
                                or sol < cell["solidity_min"]):
            cell["solidity_min"] = sol
        comp = _num(props.get("compactness"))
        if comp is not None and (cell["compactness_max"] is None
                                 or comp > cell["compactness_max"]):
            cell["compactness_max"] = comp
    return morph


def _point_iso(point, radar_ts):
    """timestamp ISO di un punto: suo timestamp -> da timestamp_ms -> radar_ts."""
    if not isinstance(point, dict):
        return radar_ts
    if point.get("timestamp"):
        return point.get("timestamp")
    return ev_store.ms_to_iso(point.get("timestamp_ms")) or radar_ts


def _on_latest_frame(point, radar_ts_ms):
    if point is None or radar_ts_ms is None:
        return False
    try:
        return int(point.get("timestamp_ms")) == int(radar_ts_ms)
    except (TypeError, ValueError):
        return False


def build_observations(supercells, tracks, storms, radar_ts_ms, radar_ts=None):
    """Normalizza gli input radar in osservazioni per hail/vortex.

    Una cella con track in tracks.json -> osservazione "points" (persistenza
    REALE dalla finestra recente); i candidati cella gia' coperti dalla track
    sono saltati (stessa tempesta). Candidati senza track e gli storm_object ->
    osservazione "duration" (on_latest_frame + duration_min), con id anchor
    "{track_type}-{track_id}" per non collidere i track_id fra domini."""
    obs = []
    morph = _morph_from_storms(storms or {})
    covered = set()
    cell_candidates = {}
    for cand in ((supercells or {}).get("candidates") or []):
        if not isinstance(cand, dict) or cand.get("track_id") is None:
            continue
        if (cand.get("track_type") or "cell") == "cell":
            cell_candidates[str(cand["track_id"])] = cand

    for track in ((tracks or {}).get("tracks") or []):
        if not isinstance(track, dict) or track.get("track_id") is None:
            continue
        key = str(track["track_id"])
        pts = [p for p in (track.get("points") or []) if isinstance(p, dict)]
        if not pts:
            continue                      # il candidato (se c'e') copre sotto
        first, last = pts[0], pts[-1]
        recent = hail_mod.recent_points(pts, radar_ts_ms)
        recent_dbz = [v for v in (_num(p.get("max_dbz")) for p in recent)
                      if v is not None]
        duration_min = _num(track.get("duration_min"))
        if duration_min is None:
            try:
                duration_min = (int(last.get("timestamp_ms"))
                                - int(first.get("timestamp_ms"))) / 60000.0
            except (TypeError, ValueError):
                duration_min = None
        item = {
            "anchor": f"cell-{key}",
            "type": "cell",
            "position": list(last.get("lonlat") or []),
            "first_seen": _point_iso(first, radar_ts),
            "last_seen": _point_iso(last, radar_ts),
            "persistence_source": "points",
            "recent_points": recent,
            "max_dbz": max(recent_dbz) if recent_dbz else None,
            "duration_min": duration_min,
            "on_latest_frame": _on_latest_frame(last, radar_ts_ms),
            "organization_score": _num(track.get("organization_score")),
            "classification": track.get("classification"),
            "n_frames": track.get("n_frames"),
        }
        item.update(morph.get(key) or {})
        cand = cell_candidates.get(key)
        if cand is not None and item["organization_score"] is None:
            item["organization_score"] = _num(
                (cand.get("organization") or {}).get("organization_score"))
        obs.append(item)
        covered.add(key)

    anchors = {o["anchor"] for o in obs}
    for cand in ((supercells or {}).get("candidates") or []):
        if not isinstance(cand, dict) or cand.get("track_id") is None:
            continue
        track_type = cand.get("track_type") or "cell"
        key = str(cand["track_id"])
        if track_type == "cell" and key in covered:
            continue                      # gia' valutata via tracks.json
        anchor = f"{track_type}-{key}"
        if anchor in anchors:
            continue
        intensity = cand.get("intensity") or {}
        organization = cand.get("organization") or {}
        motion = cand.get("motion") or {}
        item = {
            "anchor": anchor,
            "type": track_type,
            "position": list(cand.get("position") or []),
            "first_seen": cand.get("first_seen") or radar_ts,
            "last_seen": cand.get("last_seen") or radar_ts,
            "persistence_source": "duration",
            "recent_points": [],
            "max_dbz": _num(intensity.get("max_dbz")),
            "duration_min": _num(motion.get("duration_min")),
            "on_latest_frame": bool(cand.get("on_latest_frame")),
            "organization_score": _num(organization.get("organization_score")),
            "classification": organization.get("classification"),
            "n_frames": None,
        }
        if track_type == "cell":
            item.update(morph.get(key) or {})
        obs.append(item)
        anchors.add(anchor)

    # Dedup spaziale finale: le track ("points") precedono i candidati, quindi
    # una cella con track vince su un candidato coincidente (storm_object o
    # cella) sullo stesso fenomeno, evitando badge doppi (es. VORTEX-cell-2 e
    # VORTEX-storm_object-2). Ordine preservato -> risultato deterministico.
    deduped = []
    for item in obs:
        pos = item.get("position") or []
        duplicate = False
        for kept in deduped:
            dist = _haversine_km(pos, kept.get("position") or [])
            if dist is not None and dist <= OBS_DEDUP_KM:
                duplicate = True
                break
        if not duplicate:
            deduped.append(item)
    return deduped


def run(radar_dir=DEFAULT_RADAR_DIR, out_dir=DEFAULT_OUT_DIR,
        provider_name=DEFAULT_PROVIDER, network=True,
        h0_client=None, opener=None, now=None,
        radius_km=None, window_slots=None):
    """Esegue una verifica e scrive events.json + badges.json.

    Ritorna il riassunto {ok, degraded, events, active, hail, vortex, source,
    radar_timestamp, generated_at}. NON solleva per dati mancanti, finestra
    fulmini unavailable o sorgenti corrotte (solo warn/stderr + degrada);
    solleva UnknownProviderError per un provider non registrato."""
    radius = lightning_mod.DEFAULT_RADIUS_KM if radius_km is None else radius_km
    slots = (lightning_mod.DEFAULT_WINDOW_SLOTS
             if window_slots is None else window_slots)
    generated_at = ev_store.utc_iso(now)
    radar_dir = str(radar_dir)
    out_dir = str(out_dir)

    # Provider PRIMA del lavoro: nome ignoto -> errore esplicito, exit 1.
    provider = lightning_mod.get_provider(provider_name, network=network,
                                          opener=opener)

    latest = _load_json(os.path.join(radar_dir, "latest.json"), "latest.json")
    radar_ts = latest.get("radar_timestamp")
    radar_ts_ms = (latest.get("radar_timestamp_ms")
                   or ev_store.iso_to_ms(radar_ts))
    status = latest.get("status")
    ok = bool(status == "ok" and radar_ts and radar_ts_ms is not None)

    events_path = os.path.join(out_dir, ev_store.EVENTS_NAME)
    badges_path = os.path.join(out_dir, ev_store.BADGES_NAME)
    existing = ev_store.load_events(events_path)

    if not ok:
        merged = ev_store.merge_events(existing, [], generated_at,
                                       WINDOW_HOURS, prune_anchor=radar_ts)
        events_written = ev_store.write_json_if_changed(
            events_path, merged, ev_store.VOLATILE_EVENT_KEYS)
        badges_written = ev_store.write_json_if_changed(
            badges_path,
            ev_store.build_badges([], radar_ts, generated_at),
            ev_store.VOLATILE_BADGE_KEYS)
        print(f"{LOG} status={status or 'missing'} "
              f"radar_timestamp={radar_ts or 'none'} -> solo prune; "
              f"events={len(merged['events'])} active=0 "
              f"(hail=0 vortex=0) source={provider.name} "
              f"events_written={events_written} "
              f"badges_written={badges_written}")
        return {"ok": True, "degraded": True, "events": len(merged["events"]),
                "active": 0, "hail": 0, "vortex": 0, "source": provider.name,
                "radar_timestamp": radar_ts, "generated_at": generated_at,
                "events_written": events_written,
                "badges_written": badges_written}

    supercells = _load_json(os.path.join(radar_dir, "supercells.json"),
                            "supercells.json")
    tracks = _load_json(os.path.join(radar_dir, "tracks.json"), "tracks.json")
    storms = _load_json(os.path.join(radar_dir, "storms.geojson"),
                        "storms.geojson")
    observations = build_observations(supercells, tracks, storms,
                                      radar_ts_ms, radar_ts)

    # H0: UN fetch per run, non bloccante, solo se qualcosa da valutare.
    freezing_level_m = None
    if observations and (h0_client is not None or network):
        for item in observations:
            pos = item.get("position") or []
            if len(pos) >= 2:
                freezing_level_m = hail_mod.fetch_freezing_level(
                    pos[1], pos[0], client=h0_client)
                break

    incoming = []
    for item in observations:
        try:
            response = provider.strikes_in_window(
                epoch_ms=radar_ts_ms, window_slots=slots, radius_km=radius,
                center_lonlat=item.get("position") or None)
        except Exception as exc:            # nessuna eccezione verso l'alto
            response = lightning_mod.unavailable(
                provider.name, f"error:{exc.__class__.__name__}", radius)
        event = hail_mod.evaluate_hail(item, response, freezing_level_m)
        if event is not None:
            incoming.append(event)
        event = vortex_mod.evaluate_vortex(item, response)
        if event is not None:
            incoming.append(event)

    merged = ev_store.merge_events(existing, incoming, generated_at,
                                   WINDOW_HOURS, prune_anchor=radar_ts)
    events_written = ev_store.write_json_if_changed(
        events_path, merged, ev_store.VOLATILE_EVENT_KEYS)
    badges = ev_store.build_badges(merged["events"], radar_ts, generated_at)
    badges_written = ev_store.write_json_if_changed(
        badges_path, badges, ev_store.VOLATILE_BADGE_KEYS)
    n_hail = sum(1 for b in badges["badges"] if b.get("type") == "HAIL")
    n_vortex = sum(1 for b in badges["badges"] if b.get("type") == "VORTEX")
    print(f"{LOG} events={len(merged['events'])} "
          f"active={len(badges['badges'])} "
          f"(hail={n_hail} vortex={n_vortex}) source={provider.name} "
          f"events_written={events_written} badges_written={badges_written}")
    return {"ok": True, "degraded": False, "events": len(merged["events"]),
            "active": len(badges["badges"]), "hail": n_hail,
            "vortex": n_vortex, "source": provider.name,
            "radar_timestamp": radar_ts, "generated_at": generated_at,
            "events_written": events_written,
            "badges_written": badges_written}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="phenomena-verify",
        description="Verifica eventi HAIL/VORTEX dai derivati radar "
                    "(ENGINE-ONLY).")
    parser.add_argument("--radar-dir", default=DEFAULT_RADAR_DIR,
                        help="cartella input radar (default: data/radar)")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR,
                        help="cartella output (default: data/phenomena)")
    parser.add_argument("--lightning-provider", default=DEFAULT_PROVIDER,
                        help="provider fulmini registrato (default: mli)")
    parser.add_argument("--no-network", action="store_true",
                        help="nessun I/O di rete (fulmini/H0 unavailable)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        run(radar_dir=args.radar_dir, out_dir=args.out_dir,
            provider_name=args.lightning_provider,
            network=not args.no_network)
    except Exception as exc:
        print(f"{LOG} ERROR {exc.__class__.__name__}: {exc}",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
