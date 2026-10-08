#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test history.py: archivio rolling 2h (slot, index, merge idempotente,
prune della finestra, payload scan da bundle, scrittura atomica)."""

import json
import os
from types import SimpleNamespace

import pytest

from radar_engine import history
from radar_engine import models
from radar_engine import output

from conftest import make_cell, make_track

T0 = 1_777_027_200_000  # 2026-04-24T10:40:00Z (multiplo di 5 min)
SLOT0 = "2026-04-24T10-40-00Z"

_SUPERCELL = {
    "supercell_id": "SC-2026-04-24T10-cell001",
    "track_id": 1,
    "track_type": "cell",
    "ssi": 72,
    "level": "possible",
    "candidate": True,
    "phase": "birth",
    "on_latest_frame": True,
    "position": [12.1, 41.0],
    "intensity": {"max_dbz": 52.0, "mean_dbz": 41.0, "delta_dbz": 3.0},
    "motion": {"velocity_kmh": 48.5, "direction_toward_deg": 90.0,
               "distance_km": 8.0, "duration_min": 5.0,
               "area_growth_pct": 6.7},
}


def _bundle(ts_ms=T0, supers=None, dead_track=False):
    """Bundle con 2 frame, 1 track viva (ultimo frame) e candidato SC."""
    c1 = make_cell(12.0, 41.0, ts_ms - 300000, cell_id="c1")
    c2 = make_cell(12.1, 41.0, ts_ms, cell_id="c2")
    tr = make_track([c1, c2], 1)
    tr.motion = {"velocity_kmh": 48.5, "direction_toward_deg": 90.0,
                 "organization_score": 61}
    b = models.EngineBundle("ok", "2026-04-24T10:40:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = c2.timestamp_iso
    b.radar_timestamp_ms = ts_ms
    b.cells_by_frame = [[c1], [c2]]
    b.tracks = [tr]
    if dead_track:
        b.tracks.append(make_track([c1], 2))
    b.supercells = [_SUPERCELL] if supers is None else list(supers)
    return b


def _slot_payload(slot=SLOT0, ts_iso="2026-04-24T10:40:00Z"):
    return {"slot": slot, "radar_timestamp": ts_iso,
            "radar_timestamp_ms": 1777027200000, "tracks": [],
            "supercells": []}


def _slot_iso(offset_s):
    """ISO 5 minuti (multiplo) a offset in secondi da T0."""
    from datetime import datetime, timezone
    return datetime.fromtimestamp((T0 // 1000) + offset_s,
                                  timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# slot_name: floor a 5 min UTC
# ---------------------------------------------------------------------------
def test_slot_name_floor_5min():
    assert history.slot_name("2026-04-24T10:42:31Z") == SLOT0
    assert history.slot_name("2026-04-24T10:44:59.999Z") == SLOT0
    assert history.slot_name("2026-04-24T10:45:00Z") == "2026-04-24T10-45-00Z"
    assert history.slot_name("2026-04-24T10:40:00+00:00") == SLOT0
    assert history.slot_name("2026-05-01T00:02:10Z") == "2026-05-01T00-00-00Z"
    with pytest.raises(ValueError):
        history.slot_name("non-una-data")


# ---------------------------------------------------------------------------
# letture di index/slot assenti o corrotti -> default senza errori
# ---------------------------------------------------------------------------
def test_read_absent_returns_defaults(tmp_path):
    root = str(tmp_path / "history")
    index = history.read_index(root)
    assert index == {"as_of": None, "window_slots": 25, "next_uid": 1,
                     "slots": []}
    assert history.read_slot(root, SLOT0) is None
    os.makedirs(root)
    with open(os.path.join(root, "index.json"), "w", encoding="utf-8") as fh:
        fh.write("{corrotto")
    assert history.read_index(root)["slots"] == []
    with open(os.path.join(root, "index.json"), "w", encoding="utf-8") as fh:
        json.dump({"slots": "non-una-lista"}, fh)
    assert history.read_index(root)["slots"] == []


# ---------------------------------------------------------------------------
# merge: scrittura atomica (cartella inesistente creata, nessun tmp residuo)
# ---------------------------------------------------------------------------
def test_merge_slot_creates_dirs_atomically(tmp_path):
    root = str(tmp_path / "nested" / "history")   # cartella INESISTENTE
    history.merge_slot(root, _slot_payload())
    slot_path = os.path.join(root, "slots", SLOT0 + ".json")
    assert os.path.exists(slot_path)
    with open(slot_path, encoding="utf-8") as fh:
        assert json.load(fh)["slot"] == SLOT0
    assert history.read_index(root)["slots"] == [SLOT0]
    leftovers = [f for f in os.listdir(os.path.join(root, "slots"))
                 if f.endswith(".tmp")]
    assert leftovers == []
    assert not any(f.endswith(".tmp")
                   for f in os.listdir(root) if f.endswith(".tmp"))


def test_atomic_write_text_creates_missing_parent(tmp_path):
    path = str(tmp_path / "deep" / "dir" / "index.json")
    history._atomic_write_text(path, '{"a": 1}')
    with open(path, encoding="utf-8") as fh:
        assert json.load(fh) == {"a": 1}
    bad = str(tmp_path / "other" / "dir" / "index.json")
    with pytest.raises(ValueError):
        history._atomic_write_text(bad, "{non-json")
    assert not os.path.exists(bad)


# ---------------------------------------------------------------------------
# merge idempotente + ordinamento dell'index
# ---------------------------------------------------------------------------
def test_merge_slot_idempotent_same_slot(tmp_path):
    root = str(tmp_path / "history")
    history.merge_slot(root, _slot_payload())
    with open(os.path.join(root, "index.json"), encoding="utf-8") as fh:
        before = fh.read()
    history.merge_slot(root, _slot_payload())
    with open(os.path.join(root, "index.json"), encoding="utf-8") as fh:
        after = fh.read()
    assert after == before                       # index NON cresce
    assert history.read_index(root)["slots"] == [SLOT0]


def test_index_sorted_after_out_of_order_merge(tmp_path):
    root = str(tmp_path / "history")
    for offset in (600, -600, 0):                # 10:50, 10:30, 10:40
        iso = _slot_iso(offset)
        history.merge_slot(root, _slot_payload(history.slot_name(iso), iso))
    slots = history.read_index(root)["slots"]
    assert slots == sorted(slots)
    assert slots == ["2026-04-24T10-30-00Z", SLOT0, "2026-04-24T10-50-00Z"]


# ---------------------------------------------------------------------------
# prune: ultimi N slot (default 25 e custom) + file orfani
# ---------------------------------------------------------------------------
def _merge_n(root, n):
    for i in range(n):
        iso = _slot_iso(i * 300)
        history.merge_slot(root, _slot_payload(history.slot_name(iso), iso))


def test_prune_keeps_last_25_default(tmp_path):
    root = str(tmp_path / "history")
    _merge_n(root, 30)
    history.prune_slots(root)
    index = history.read_index(root)
    assert index["window_slots"] == 25
    assert len(index["slots"]) == 25
    assert index["slots"][0] == history.slot_name(_slot_iso(5 * 300))
    assert not os.path.exists(
        os.path.join(root, "slots", history.slot_name(_slot_iso(0)) + ".json"))


def test_prune_custom_keep(tmp_path):
    root = str(tmp_path / "history")
    _merge_n(root, 6)
    history.prune_slots(root, keep=3)
    index = history.read_index(root)
    assert index["window_slots"] == 3
    assert index["slots"] == [history.slot_name(_slot_iso(3 * 300)),
                              history.slot_name(_slot_iso(4 * 300)),
                              history.slot_name(_slot_iso(5 * 300))]
    files = os.listdir(os.path.join(root, "slots"))
    assert len(files) == 3


def test_prune_removes_orphan_files(tmp_path):
    root = str(tmp_path / "history")
    _merge_n(root, 2)
    orphan = os.path.join(root, "slots", "1999-01-01T00-00-00Z.json")
    with open(orphan, "w", encoding="utf-8") as fh:
        fh.write('{"slot": "1999-01-01T00-00-00Z"}')
    history.prune_slots(root, keep=10)
    assert not os.path.exists(orphan)
    assert len(history.read_index(root)["slots"]) == 2


# ---------------------------------------------------------------------------
# payload del blob: campi attesi e coerenti
# ---------------------------------------------------------------------------
def test_build_slot_payload_schema():
    payload = history.build_slot_payload(_bundle(dead_track=True))
    assert payload["slot"] == SLOT0
    assert payload["radar_timestamp"].endswith("Z")
    assert isinstance(payload["radar_timestamp_ms"], int)
    # solo la track VIVA all'ultimo frame (la "morta" al frame 0 e' esclusa)
    assert len(payload["tracks"]) == 1
    t = payload["tracks"][0]
    assert isinstance(t["track_id"], int) and t["track_id"] == 1
    assert t["track_type"] == "cell"
    assert isinstance(t["status"], str)
    assert isinstance(t["lonlat"], list) and len(t["lonlat"]) == 2
    assert all(isinstance(v, float) for v in t["lonlat"])
    for key in ("area_km2", "max_dbz", "mean_dbz"):
        assert isinstance(t[key], float)
    assert t["velocity_kmh"] == 48.5
    assert t["direction_toward_deg"] == 90.0
    assert len(payload["supercells"]) == 1


def test_build_slot_payload_supercells_on_latest_only():
    stale = dict(_SUPERCELL, on_latest_frame=False)
    payload = history.build_slot_payload(_bundle(supers=[_SUPERCELL, stale]))
    assert len(payload["supercells"]) == 1
    sc = payload["supercells"][0]
    assert sc["supercell_id"] == _SUPERCELL["supercell_id"]
    assert isinstance(sc["track_id"], int) and sc["track_id"] == 1
    assert isinstance(sc["ssi"], int) and sc["ssi"] == 72
    assert sc["level"] == "possible" and sc["phase"] == "birth"
    assert isinstance(sc["position"], list) and len(sc["position"]) == 2
    assert sc["intensity"]["max_dbz"] == 52.0
    assert sc["motion"]["velocity_kmh"] == 48.5


def test_build_slot_payload_includes_storm_tracks():
    point = SimpleNamespace(frame_index=1, lonlat=(12.2, 41.1),
                            area_km2=250.0, max_dbz=47.5, mean_dbz=40.0)
    storm = SimpleNamespace(track_id=7, status="active",
                            motion={"velocity_kmh": 30.0},
                            points=[point])
    b = _bundle()
    b.storm_tracks = [storm]
    payload = history.build_slot_payload(b)
    assert [t["track_type"] for t in payload["tracks"]] == ["cell",
                                                            "storm_object"]
    assert payload["tracks"][1]["track_id"] == 7
    assert payload["tracks"][1]["velocity_kmh"] == 30.0


# ---------------------------------------------------------------------------
# build_history: window custom, ri-run senza duplichi, nessuno scan
# ---------------------------------------------------------------------------
def test_build_history_window_and_rerun(tmp_path):
    root = str(tmp_path / "history")
    index = history.build_history(_bundle(), root, window_slots=3)
    assert index["slots"] == [SLOT0]
    assert index["window_slots"] == 3
    index2 = history.build_history(_bundle(), root, window_slots=3)
    assert index2["slots"] == [SLOT0]            # nessun duplicato
    for i in range(1, 5):                        # 4 scan successive
        history.build_history(_bundle(ts_ms=T0 + i * 300000), root,
                              window_slots=3)
    final = history.read_index(root)
    assert len(final["slots"]) == 3              # finestra mantenuta
    assert final["slots"][-1] == "2026-04-24T11-00-00Z"
    assert final["slots"][0] == "2026-04-24T10-50-00Z"


def test_build_history_skips_valid_slot(tmp_path, monkeypatch):
    root = str(tmp_path / "history")
    history.build_history(_bundle(), root)
    writes = []
    monkeypatch.setattr(history, "_atomic_write_text",
                        lambda path, text: writes.append(path))
    history.build_history(_bundle(), root)        # stesso scan
    assert writes == []                           # slot valido: zero riscritture


def test_build_history_without_scan_returns_none(tmp_path):
    root = str(tmp_path / "history")
    b = models.EngineBundle("ok", "2026-04-24T10:40:00Z", output.SOURCE_LABEL)
    assert history.build_history(b, root) is None
    assert not os.path.exists(root)               # nessuna cartella creata


# ---------------------------------------------------------------------------
# uid stabile cross-run: continuita' delle tracce tra slot consecutivi
# ---------------------------------------------------------------------------
def _bundle_at(lon, lat, ts_ms, track_id=1):
    """Bundle con 1 track viva a (lon, lat) all'ultimo frame + SC coerente."""
    c1 = make_cell(lon, lat, ts_ms - 300000, cell_id="c1-{}-{}".format(track_id, ts_ms))
    c2 = make_cell(lon, lat, ts_ms, cell_id="c2-{}-{}".format(track_id, ts_ms))
    tr = make_track([c1, c2], track_id)
    tr.motion = {"velocity_kmh": 30.0, "direction_toward_deg": 90.0}
    b = models.EngineBundle("ok", "2026-04-24T10:40:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = c2.timestamp_iso
    b.radar_timestamp_ms = ts_ms
    b.cells_by_frame = [[c1], [c2]]
    b.tracks = [tr]
    b.supercells = [dict(_SUPERCELL, track_id=track_id)]
    return b


def _bundle_multi(specs, ts_ms):
    """Bundle con piu' track vive (specs = [(lon, lat, track_id), ...])."""
    first, last, tracks = [], [], []
    for i, (lon, lat, track_id) in enumerate(specs):
        c1 = make_cell(lon, lat, ts_ms - 300000, cell_id="m1-{}-{}".format(i, ts_ms))
        c2 = make_cell(lon, lat, ts_ms, cell_id="m2-{}-{}".format(i, ts_ms))
        first.append(c1)
        last.append(c2)
        tr = make_track([c1, c2], track_id)
        tr.motion = {"velocity_kmh": 30.0, "direction_toward_deg": 90.0}
        tracks.append(tr)
    b = models.EngineBundle("ok", "2026-04-24T10:40:00Z", output.SOURCE_LABEL)
    b.radar_timestamp_iso = last[0].timestamp_iso if last else None
    b.radar_timestamp_ms = ts_ms
    b.cells_by_frame = [first, last]
    b.tracks = tracks
    b.supercells = []
    return b


def _uid(root, slot):
    return history.read_slot(root, slot)["tracks"][0]["uid"]


def test_build_slot_payload_has_uid_placeholders():
    payload = history.build_slot_payload(_bundle())
    assert "uid" in payload["tracks"][0]
    assert payload["tracks"][0]["uid"] is None    # assegnato in build_history
    assert "track_uid" in payload["supercells"][0]
    assert payload["supercells"][0]["track_uid"] is None


def test_uid_stable_when_cell_moves_little(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0), root)
    history.build_history(_bundle_at(12.05, 41.0, T0 + 300000), root)
    slot0 = history.slot_name(_slot_iso(0))
    slot1 = history.slot_name(_slot_iso(300))
    assert _uid(root, slot0) == _uid(root, slot1)
    assert history.read_index(root)["next_uid"] == 2


def test_uid_differs_for_distant_same_raw_id(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0, track_id=1), root)
    history.build_history(_bundle_at(30.0, 41.0, T0 + 300000, track_id=1), root)
    slot0 = history.slot_name(_slot_iso(0))
    slot1 = history.slot_name(_slot_iso(300))
    assert _uid(root, slot0) != _uid(root, slot1)   # niente stitching


def test_uid_new_when_jump_beyond_threshold(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0, track_id=1), root)
    history.build_history(_bundle_at(12.5, 41.0, T0 + 300000, track_id=1), root)
    slot0 = history.slot_name(_slot_iso(0))
    slot1 = history.slot_name(_slot_iso(300))
    assert _uid(root, slot0) != _uid(root, slot1)


def test_uid_monotonic_and_next_uid_persists(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0), root)
    assert history.read_index(root)["next_uid"] == 2
    history.build_history(
        _bundle_multi([(30.0, 41.0, 1), (31.0, 41.0, 2)], T0 + 300000), root)
    slot1 = history.slot_name(_slot_iso(300))
    uids = [t["uid"] for t in history.read_slot(root, slot1)["tracks"]]
    assert uids == sorted(uids) and len(set(uids)) == 2
    assert history.read_index(root)["next_uid"] == 4
    history.build_history(_bundle_at(12.0, 41.0, T0 + 600000), root)
    slot2 = history.slot_name(_slot_iso(600))
    assert _uid(root, slot2) == 4
    assert history.read_index(root)["next_uid"] == 5


def test_build_history_idempotent_keeps_uid_and_next_uid(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0), root)
    slot0 = history.slot_name(_slot_iso(0))
    uid_before = _uid(root, slot0)
    next_before = history.read_index(root)["next_uid"]
    history.build_history(_bundle_at(12.9, 41.0, T0), root)   # stesso slot
    assert _uid(root, slot0) == uid_before
    assert history.read_index(root)["next_uid"] == next_before


def test_supercell_track_uid_matches_track(tmp_path):
    root = str(tmp_path / "history")
    history.build_history(_bundle_at(12.0, 41.0, T0), root)
    payload = history.read_slot(root, history.slot_name(_slot_iso(0)))
    track = payload["tracks"][0]
    assert payload["supercells"][0]["track_uid"] == track["uid"]
    assert track["uid"] == 1


def test_previous_slot_without_uid_assigns_new(tmp_path):
    root = str(tmp_path / "history")
    legacy = _slot_payload(history.slot_name(_slot_iso(0)), _slot_iso(0))
    legacy["tracks"] = [{"track_id": 1, "track_type": "cell",
                         "lonlat": [12.0, 41.0]}]        # slot vecchio: senza uid
    history.merge_slot(root, legacy)
    history.build_history(_bundle_at(12.05, 41.0, T0 + 300000), root)
    slot1 = history.slot_name(_slot_iso(300))
    assert _uid(root, slot1) == 1
    assert history.read_index(root)["next_uid"] == 2
