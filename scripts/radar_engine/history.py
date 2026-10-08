#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Meteorisk Radar Engine — history.py (archivio rolling 2h)

Archivio rolling DERIVATO a finestra di 25 slot da 5 minuti (2 ore), mantenuto
in data/radar/history/ (FUORI dai 4 file derivati committati su main):

    index.json            as_of + window_slots + slot ordinati (cronologico,
                          il piu' recente in fondo)
    slots/<SLOT>.json     payload dello scan: tracks (punto all'ultimo frame
                          per track viva) + supercelle on_latest_frame

Pattern replicato dalla finestra rolling satellite (satellite_engine.py /
branch satellite-cache):
    - slot con floor a 5 min UTC, nome 'YYYY-MM-DDTHH-MM-00Z';
    - merge IDEMPOTENTE: slot gia' presente e valido -> saltato (nessuna
      riscrittura), slot assente/corrotto -> riscritto;
    - prune della finestra agli ultimi N slot + rimozione file orfani;
    - scrittura ATOMICA (tmp nella stessa dir + json.loads + os.replace).

L'archivio e' BEST-EFFORT: un suo errore non cambia mai l'rc del run
(main.py logga un warning e l'output derivato resta comunque scritto).
"""

import datetime as _dt
import json
import math
import os
import tempfile

from . import models

WINDOW_SLOTS = 25       # 25 slot * 5 min = 2 h (stessa finestra satellite)
SLOT_STEP_S = 300       # passo slot: 5 min
INDEX_NAME = "index.json"
SLOTS_DIR = "slots"


def _parse_iso(value):
    """datetime UTC da ISO-8601 ('...Z' o con offset) o da datetime."""
    if isinstance(value, _dt.datetime):
        dt = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = _dt.datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError("iso_non_valida:{}".format(value)) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return dt.astimezone(_dt.timezone.utc)


def slot_name(iso):
    """Nome slot dello scan: floor a 5 min UTC ('YYYY-MM-DDTHH-MM-00Z')."""
    epoch = int(_parse_iso(iso).timestamp())
    epoch -= epoch % SLOT_STEP_S
    slot = _dt.datetime.fromtimestamp(epoch, tz=_dt.timezone.utc)
    return slot.strftime("%Y-%m-%dT%H-%M-%SZ")


def _index_empty():
    return {"as_of": None, "window_slots": WINDOW_SLOTS, "slots": []}


def read_index(root):
    """index.json dell'archivio; default vuoto se assente/corrotto."""
    try:
        with open(os.path.join(root, INDEX_NAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return _index_empty()
    if not isinstance(data, dict) or not isinstance(data.get("slots"), list):
        return _index_empty()
    try:
        window = int(data.get("window_slots") or WINDOW_SLOTS)
    except (TypeError, ValueError):
        window = WINDOW_SLOTS
    slots = sorted({str(s) for s in data["slots"] if isinstance(s, str)})
    return {"as_of": data.get("as_of"), "window_slots": window, "slots": slots}


def read_slot(root, slot):
    """Payload di slots/<slot>.json; None se assente/corrotto."""
    path = os.path.join(root, SLOTS_DIR, "{}.json".format(slot))
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_text(path, text):
    """Scrive text in modo atomico (tmp nella stessa dir + os.replace)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    json.loads(text)  # validazione JSON PRIMA della replace
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".history-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise models.OutputError("atomic_write_failed:{}".format(exc)) from exc


def _write_index(root, slots, window_slots):
    """Riscrive index.json con slot ordinati (no-op se gia' aggiornato)."""
    slots = sorted({str(s) for s in slots})
    window_slots = int(window_slots)
    current = read_index(root)
    if (os.path.exists(os.path.join(root, INDEX_NAME))
            and current["slots"] == slots
            and current["window_slots"] == window_slots):
        return current
    payload = {
        "as_of": models.utcnow_iso(),
        "window_slots": window_slots,
        "slots": slots,
    }
    _atomic_write_text(os.path.join(root, INDEX_NAME),
                       json.dumps(payload, ensure_ascii=False))
    return payload


def merge_slot(root, slot_payload):
    """Scrive/sovrascrive slots/<slot>.json (atomico) e aggiorna l'index."""
    slot = str(slot_payload["slot"])
    _atomic_write_text(os.path.join(root, SLOTS_DIR, slot + ".json"),
                       json.dumps(_safe(slot_payload), ensure_ascii=False))
    index = read_index(root)
    _write_index(root, list(index["slots"]) + [slot], index["window_slots"])


def prune_slots(root, keep=WINDOW_SLOTS):
    """Taglia la finestra agli ultimi `keep` slot e rimuove i file orfani."""
    keep = max(int(keep), 0)
    index = read_index(root)
    slots_dir = os.path.join(root, SLOTS_DIR)
    try:
        names = sorted(n for n in os.listdir(slots_dir)
                       if n.endswith(".json"))
    except OSError:
        names = []
    kept = sorted(index["slots"])[-keep:] if keep else []
    kept_set = set(kept)
    for name in names:
        if name[:-len(".json")] not in kept_set:
            try:
                os.unlink(os.path.join(slots_dir, name))
            except OSError:
                pass  # file gia' sparito: la pulizia prosegue
    if not names and not index["slots"]:
        return  # nessun archivio: non creare la directory
    _write_index(root, kept, keep)


def _round(value, ndigits):
    """float arrotondato; None se assente/non numerico/non finito."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return round(out, ndigits) if math.isfinite(out) else None


def _as_int(value):
    """int arrotondato; None se assente/non numerico/non finito."""
    out = _round(value, 6)
    return None if out is None else int(round(out))


def _safe(value):
    """Conversione ricorsiva in tipi JSON-safe (numpy -> nativi)."""
    if isinstance(value, dict):
        return {str(k): _safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(v) for v in value]
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if hasattr(value, "tolist"):
        return _safe(value.tolist())
    if hasattr(value, "item"):
        return _safe(value.item())
    return str(value)


def _track_entry(track, latest_frame_index, track_type):
    """Voce track sul frame piu' recente; None se non viva a quel frame."""
    if latest_frame_index is None:
        return None
    point = None
    for p in reversed(getattr(track, "points", None) or []):
        if p.frame_index == latest_frame_index:
            point = p
            break
    if point is None:
        return None
    motion = getattr(track, "motion", None) or {}
    return {
        "track_id": _as_int(getattr(track, "track_id", None)),
        "track_type": track_type,
        "status": getattr(track, "status", None),
        "lonlat": [_round(point.lonlat[0], 5), _round(point.lonlat[1], 5)],
        "area_km2": _round(point.area_km2, 2),
        "max_dbz": _round(point.max_dbz, 2),
        "mean_dbz": _round(point.mean_dbz, 2),
        "velocity_kmh": _round(motion.get("velocity_kmh"), 2),
        "direction_toward_deg": _round(motion.get("direction_toward_deg"), 1),
    }


def _supercell_entry(candidate):
    """Voce supercella sul frame corrente (intensity/motion dal bundle)."""
    position = candidate.get("position") or []
    if len(position) == 2:
        position = [_round(position[0], 5), _round(position[1], 5)]
    else:
        position = None
    return {
        "supercell_id": candidate.get("supercell_id"),
        "track_id": _as_int(candidate.get("track_id")),
        "track_type": candidate.get("track_type"),
        "ssi": _as_int(candidate.get("ssi")),
        "level": candidate.get("level"),
        "phase": candidate.get("phase"),
        "position": position,
        "intensity": candidate.get("intensity") or {},
        "motion": candidate.get("motion") or {},
    }


def build_slot_payload(bundle_like):
    """Payload dello scan corrente per slots/<SLOT>.json (None se assente).

    tracks: una voce per track VIVA all'ultimo frame (punto del frame piu'
    recente + velocity/direction dal motion radice); supercells: soli
    candidati con on_latest_frame=True. I campi mancanti restano None/omessi
    (nessun dato inventato)."""
    ts_iso = getattr(bundle_like, "radar_timestamp_iso", None)
    if not ts_iso:
        return None
    cells_by_frame = getattr(bundle_like, "cells_by_frame", None) or []
    latest = len(cells_by_frame) - 1 if cells_by_frame else None
    if latest is None:
        pts = [p.frame_index
               for trk in (getattr(bundle_like, "tracks", None) or [])
               for p in (getattr(trk, "points", None) or [])]
        latest = max(pts) if pts else None
    tracks = []
    for trk in getattr(bundle_like, "tracks", None) or []:
        entry = _track_entry(trk, latest, "cell")
        if entry is not None:
            tracks.append(entry)
    for trk in getattr(bundle_like, "storm_tracks", None) or []:
        entry = _track_entry(trk, latest, "storm_object")
        if entry is not None:
            tracks.append(entry)
    supercells = [_supercell_entry(c)
                  for c in (getattr(bundle_like, "supercells", None) or [])
                  if c.get("on_latest_frame")]
    return _safe({
        "slot": slot_name(ts_iso),
        "radar_timestamp": ts_iso,
        "radar_timestamp_ms": getattr(bundle_like, "radar_timestamp_ms", None),
        "tracks": tracks,
        "supercells": supercells,
    })


def _slot_valid(payload, slot):
    """True se lo slot e' un payload leggibile e coerente con lo slot."""
    return (isinstance(payload, dict) and payload.get("slot") == slot
            and isinstance(payload.get("radar_timestamp"), str)
            and isinstance(payload.get("tracks"), list)
            and isinstance(payload.get("supercells"), list))


def build_history(bundle_like, out_root, window_slots=WINDOW_SLOTS):
    """Archivio rolling: merge dello slot corrente + prune della finestra.

    IDEMPOTENTE (come satellite_engine): uno slot gia' presente e valido viene
    saltato senza riscrittura; slot assente/corrotto -> riscritto. Ritorna il
    nuovo index oppure None se non c'e' scan da archiviare."""
    payload = build_slot_payload(bundle_like)
    if payload is None:
        return None
    root = os.path.abspath(out_root)
    slot = payload["slot"]
    if _slot_valid(read_slot(root, slot), slot):
        index = read_index(root)
        if slot not in index["slots"] or index["window_slots"] != int(window_slots):
            _write_index(root, list(index["slots"]) + [slot], window_slots)
    else:
        merge_slot(root, payload)
    prune_slots(root, keep=window_slots)
    return read_index(root)
