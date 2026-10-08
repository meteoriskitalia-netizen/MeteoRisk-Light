# -*- coding: utf-8 -*-
"""Test PARTE 4a + S4b — phenomena-verify (HAIL + VORTEX), ENGINE-ONLY.

Copertura (NESSUN test tocca la rete: opener/client sempre iniettati o
network=False):
  - provider fulmini DPC: decode finestra, backoff 403, fail-fast su errore di
    rete, finestra stale, cache per run, network_disabled, schema risposta;
  - provider fulmini MLI EUMETSAT (S4b): URL GetMap, decode PNG RGBA sintetico
    (stdlib, MAI PIL), campionamento disco, backoff su slot mancanti, trend dal
    confronto con lo scan -5 min, schema risposta esteso (strength/note/
    attribution), cache dello scan per run;
  - gate fulmini provider-agnostico: soglie per-sorgente (dpc count>=10,
    mli strength>=0.01) in lightning_corroborates / lightning_strength;
  - registry provider: "dpc" e "mli" registrati, nome ignoto rifiutato, hook
    register_provider funzionante (ripulito a fine test);
  - logica HAIL: persistenza >=2 punti, finestra 15 min, CORROBORATED solo con
    fulmini, SUSPECT senza fulmini, sorgente duration, H0 come evidence sola,
    evidence MLI (nota AFA + attribution EUMETSAT) in labels e dict;
  - logica VORTEX: organization_score, morfologia hook-proxy, persistenza,
    CORROBORATED solo con fulmini, disclaimer Doppler sempre presente;
  - events store: merge per id, first_seen minore, prune, ANCORA ai dati,
    scrittura atomica (nessun tmp residuo), badges SOLO attivi;
  - NO-OP GUARD: content_digest ignora SOLO i campi volubili designati;
    write_json_if_changed non tocca i file a contenuto sostanziale invariato
    (anti commit-churn, campagne di commit piu' fitte di una scan);
  - engine e2e: run a rete spenta su input sintetici (default mli),
    promozione con opener DPC iniettato + H0, promozione con opener MLI
    iniettato (PNG sintetici, niente rete), CLI subprocess, provider ignoto ->
    exit 1, status != "ok" -> solo prune, idempotenza + no-op guard su file,
    piu' schema sui dati reali della release (skip se assente).

I valori soglia usati qui sono quelli di phenomena/{hail,vortex,lightning}.py
con THRESHOLDS_VERSION = pheno-1.1.0: se cambia una soglia, questo file deve
cambiare insieme (non e' un test che si misura da solo).
"""

import datetime as _dt
import json
import os
import re
import struct
import subprocess
import sys
import urllib.error

import numpy as np
import pytest

from phenomena import THRESHOLDS_VERSION, WINDOW_HOURS
from phenomena import engine
from phenomena import events as ev_store
from phenomena import hail
from phenomena import lightning as pltg
from phenomena import vortex
from radar_engine.phase2 import lightning as raw_ltg

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.dirname(os.path.dirname(TESTS_DIR))
RELEASE_ROOT = os.path.dirname(SCRIPTS_DIR)
REAL_RADAR_DIR = os.path.join(RELEASE_ROOT, "data", "radar")

# Slot radar usato da tutti i test sintetici: 2026-10-07T00:05:00Z, allineato
# alla griglia DPC da 5 minuti (i fulmini DPC sono slot da 5 minuti).
RADAR_ISO = "2026-10-07T00:05:00Z"
RADAR_MS = 1791331500000
PERIOD_MS = pltg.LTG_PERIOD_MS

REQUIRED_RESPONSE_KEYS = (
    "available", "source", "reason", "epoch_ms", "anchor_ms", "staleness_ms",
    "window_slots", "slots_ok", "radius_km", "center", "strikes",
    "count_total", "count_near", "per_slot_counts", "trend", "score",
    "strength", "note", "attribution",
)
REQUIRED_EVENT_KEYS = (
    "id", "type", "state", "score", "first_seen", "last_seen", "position",
    "evidence", "thresholds_version",
)
REQUIRED_BADGE_KEYS = (
    "id", "type", "state", "position", "area_id", "labels", "evidence",
)

LTG_OFF = {"available": False, "source": "dpc", "reason": "network_disabled",
           "count_near": 0, "count_total": 0, "radius_km": 30.0,
           "trend": {"direction": "flat"}, "score": None}
LTG_OK = {"available": True, "source": "dpc", "reason": None, "count_near": 12,
          "count_total": 12, "radius_km": 30.0,
          "trend": {"direction": "flat"}, "score": 5.0}
LTG_UP = {"available": True, "source": "dpc", "reason": None, "count_near": 6,
          "count_total": 6, "radius_km": 30.0,
          "trend": {"direction": "up"}, "score": 60.0}


# ---------------------------------------------------------------------------
# Helper: Blitz v2 sintetico (stessa codifica di scripts/tests/test_lightning.py,
# copiato localmente: nessun import fra suite)
# ---------------------------------------------------------------------------
def _blitz(records):
    """File Blitz v2 sintetico da (lon, lat) — verificato da decode qui sotto."""
    body = bytearray(b"\x02")
    body += struct.pack(">H", len(records))
    for lon, lat in records:
        lon_raw = int(round((float(lon) + 180.0) * 262144.0 / 360.0))
        lat_raw = int(round((float(lat) + 90.0) * 262144.0 / 180.0))
        rec = bytearray(6)
        rec[0:2] = struct.pack(">H", lon_raw & 0xFFFF)
        rec[2:4] = struct.pack(">H", lat_raw & 0xFFFF)
        rec[4] = (((lon_raw >> 16) & 3) << 6) | (((lat_raw >> 16) & 3) << 4)
        rec[5] = 0
        body += rec
    return bytes(body)


class FakeOpener:
    """Opener sintetico per fetch_ltg: (url, timeout_s) -> bytes | None (403).

    `payloads` = {epoch_ms: bytes|None}; `default` per gli epoch non elencati
    (None = 403 "non ancora pubblicato"); `error` solleva a ogni chiamata."""

    def __init__(self, payloads=None, default=None, error=None):
        self.payloads = dict(payloads or {})
        self.default = default
        self.error = error
        self.calls = []

    def __call__(self, url, timeout_s):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        match = re.search(r"lgt_5min_(\d+)\.bin", url)
        epoch = int(match.group(1)) if match else None
        return self.payloads.get(epoch, self.default)


def _payloads(base, records_by_offset):
    """{epoch: blitz} per epoch = base - offset*PERIOD_MS."""
    return {base - off * PERIOD_MS: _blitz(recs)
            for off, recs in records_by_offset.items()}


NEAR = [(12.0, 41.0), (12.1, 41.0), (11.9, 41.0)]   # tutti entro 30 km
# 6 strike vicini (per finestre in salita: 1-2-3-6 da piu' vecchio a recente)
NEAR6 = NEAR + [(12.0, 41.1), (12.0, 40.9), (12.05, 41.05)]


# ---------------------------------------------------------------------------
# Helper MLI: PNG RGBA sintetico (zlib+struct stdlib, MAI PIL) e opener fake
# ---------------------------------------------------------------------------
def _png_rgba(width, height, block=None):
    """PNG RGBA width x height (bitdepth 8) con alpha=255 dentro `block`.

    `block` = (x0, y0, x1, y1) esclusivo; writer PNG in pura stdlib perche'
    PIL NON e' dichiarato tra le dipendenze del modulo phenomena."""
    import binascii
    import zlib

    rows = bytearray()
    for y in range(height):
        rows.append(0)                              # filtro "None" per riga
        for x in range(width):
            a = 255 if (block and block[0] <= x < block[2]
                        and block[1] <= y < block[3]) else 0
            rows += bytes((a, a, a, a))             # RGBA (alpha nel byte 4)
    return _png_chunks(width, height, bytes(rows))


def _png_chunks(width, height, raw):
    import binascii
    import struct
    import zlib

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", int(width), int(height), 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# Box sintetici AFA centrati su (12.0, 41.0): il blocchetto recente e' piu'
# ampio di quello a -5 min (trend in salita) ed e' dentro il disco 30 km.
_MLI_XY = (452, 516, 460, 524)                      # 8x8 px
_MLI_PREV_XY = (454, 518, 458, 522)                 # 4x4 px
MLI_PNG_NOW = _png_rgba(pltg.MLI_WIDTH, pltg.MLI_HEIGHT, _MLI_XY)
MLI_PNG_PREV = _png_rgba(pltg.MLI_WIDTH, pltg.MLI_HEIGHT, _MLI_PREV_XY)
# Box lontano dal centro Italia (per count_total senza centro).
MLI_PNG_FAR = _png_rgba(pltg.MLI_WIDTH, pltg.MLI_HEIGHT, (900, 40, 1000, 60))


class MliFakeOpener:
    """Opener sintetico per MliWmsProvider: (url, timeout_s) -> bytes PNG.

    `slots` = {epoch_ms: bytes}; `fail_http_codes` = {epoch_ms: code} alza
    HTTPError (slot non ancora pubblicato); `fail_urlerror` con `error_times`
    alza URLError (errore di rete, fail-fast). Numero di chiamate
    registrato in `calls` (per verificare la cache condivisa dello scan)."""

    def __init__(self, slots=None, fail_http_codes=None, error_times=None):
        self.slots = dict(slots or {})
        self.fail_http_codes = dict(fail_http_codes or {})
        self.error_times = set(error_times or ())
        self.calls = []

    def __call__(self, url, timeout_s):
        self.calls.append(url)
        # urlencode trasforma i ":" in "%3A": il TIME_ISO puo' essere sia
        # "2026-10-07T00:05:00Z" sia "2026-10-07T00%3A05%3A00Z"
        m = re.search(r"time=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
                      r"|\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A\d{2}Z)", url)
        if m is None:
            return None
        iso = m.group(1).replace("%3A", ":")
        ms = int(_dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
        ms = ms - ms % PERIOD_MS
        if ms in self.error_times:
            raise urllib.error.URLError("dns fail")
        if ms in self.fail_http_codes:
            raise urllib.error.HTTPError(url, self.fail_http_codes[ms],
                                         "nope", {}, None)
        return self.slots.get(ms)


# ---------------------------------------------------------------------------
# Helper: osservazioni e input radar sintetici
# ---------------------------------------------------------------------------
def _points(values, lon=12.0, lat=41.0):
    """Point di track ordinati oldest->newest che terminano su RADAR_MS."""
    n = len(values)
    out = []
    for i, dbz in enumerate(values):
        ms = RADAR_MS - (n - 1 - i) * PERIOD_MS
        out.append({"timestamp_ms": ms, "timestamp": ev_store.ms_to_iso(ms),
                    "lonlat": [lon, lat], "max_dbz": float(dbz),
                    "mean_dbz": float(dbz) - 2.0, "area_km2": 60.0})
    return out


def _obs(points, max_dbz=None, source="points", **extra):
    """Osservazione normalizzata (forma attesa da evaluate_*)."""
    dbz = [p["max_dbz"] for p in points]
    obs = {
        "anchor": "cell-1",
        "type": "cell",
        "position": [12.0, 41.0],
        "first_seen": (ev_store.ms_to_iso(points[0]["timestamp_ms"])
                       if points else RADAR_ISO),
        "last_seen": RADAR_ISO,
        "persistence_source": source,
        "recent_points": list(points),
        "max_dbz": max(dbz) if (max_dbz is None and dbz) else max_dbz,
        "duration_min": 15.0,
        "on_latest_frame": True,
        "organization_score": 65,
        "classification": "Organized Convective Cell",
        "n_frames": 4,
        "eccentricity_max": 0.70,
        "solidity_min": 0.95,
        "compactness_max": 1.8,
    }
    obs.update(extra)
    return obs


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_radar(radar_dir, track_dbz=(58.0, 58.0, 58.0, 58.0),
                 track_org=80, status="ok"):
    """Input radar sintetici: 1 track cell + 1 candidato storm_object."""
    radar_dir.mkdir(parents=True, exist_ok=True)
    _write_json(radar_dir / "latest.json",
                {"status": status, "radar_timestamp": RADAR_ISO,
                 "radar_timestamp_ms": RADAR_MS, "warnings": []})
    _write_json(radar_dir / "tracks.json",
                {"radar_timestamp": RADAR_ISO, "generated_at": RADAR_ISO,
                 "tracks": [{
                     "track_id": 1, "status": "active",
                     "classification": "Organized Convective Cell",
                     "organization_score": track_org,
                     "duration_min": 15.0, "n_frames": len(track_dbz),
                     "velocity_kmh": 30.0, "direction_toward_deg": 45.0,
                     "points": _points(list(track_dbz)),
                 }]})
    _write_json(radar_dir / "supercells.json",
                {"radar_timestamp": RADAR_ISO, "candidates": [{
                    "supercell_id": "sc-7", "track_id": 7,
                    "track_type": "storm_object",
                    "first_seen": "2026-10-06T23:45:00Z",
                    "last_seen": RADAR_ISO, "position": [12.5, 41.5],
                    "intensity": {"max_dbz": 62.0},
                    "organization": {"organization_score": 72,
                                     "classification": "Organized"},
                    "motion": {"velocity_kmh": 40.0, "duration_min": 12.0},
                    "on_latest_frame": True,
                }]})
    _write_json(radar_dir / "storms.geojson",
                {"type": "FeatureCollection", "features": [{
                    "type": "Feature",
                    "geometry": {"type": "Point", "coordinates": [12.0, 41.0]},
                    "properties": {
                        "track_id": 1, "cell_id": "cell-1",
                        "timestamp": RADAR_ISO, "timestamp_ms": RADAR_MS,
                        "area_km2": 60.0, "max_dbz": 58.0, "mean_dbz": 56.0,
                        "eccentricity": 0.95, "solidity": 0.80,
                        "compactness": 4.0, "source": "test",
                    },
                }]})
    return radar_dir


@pytest.fixture
def radar_case(tmp_path):
    """Coppia (radar_dir, out_dir) con input sintetici e out pulito."""
    radar = _write_radar(tmp_path / "radar")
    return {"radar": str(radar), "out": str(tmp_path / "phenomena")}


def _h0_client(value=2500.0):
    def client(url):
        assert "freezing_level_height" in url
        return {"hourly": {"time": ["2020-01-01T00:00", "2020-01-01T01:00"],
                           "freezing_level_height": [5000.0, value]}}
    return client


# ===========================================================================
# Provider fulmini DPC
# ===========================================================================
def test_blitz_helper_roundtrip():
    """Il costruttore di payload Blitz va e viene dal decoder reale."""
    parsed = raw_ltg.decode_blitz_v2(_blitz(NEAR))
    assert parsed["version"] == 2
    assert parsed["count"] == 3
    assert abs(parsed["strikes"][0][0] - 12.0) < 0.002
    assert abs(parsed["strikes"][0][1] - 41.0) < 0.002


def test_radar_slot_is_five_minutes_aligned():
    assert RADAR_MS % PERIOD_MS == 0


def test_dpc_window_counts_trend_and_center():
    opener = FakeOpener(payloads=_payloads(RADAR_MS, {0: NEAR, 1: NEAR,
                                                      2: NEAR, 3: NEAR}),
                        default=None)
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS, window_slots=4,
                                      radius_km=30.0,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is True
    assert resp["source"] == "dpc"
    assert resp["slots_ok"] == 4
    assert resp["window_slots"] == 4
    assert resp["anchor_ms"] == RADAR_MS
    assert resp["staleness_ms"] == 0
    assert resp["count_near"] == 12
    assert resp["count_total"] == 12
    assert resp["per_slot_counts"] == [3, 3, 3, 3]
    assert resp["trend"]["direction"] == "flat"
    assert resp["center"] == [12.0, 41.0]
    assert resp["score"] is not None


def test_dpc_count_near_ignores_far_strike_and_needs_center():
    far = NEAR + [(0.0, 0.0)]
    opener = FakeOpener(payloads=_payloads(RADAR_MS, {0: far, 1: NEAR,
                                                      2: NEAR, 3: NEAR}))
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    with_center = provider.strikes_in_window(
        epoch_ms=RADAR_MS, center_lonlat=[12.0, 41.0])
    assert with_center["count_total"] == 13
    assert with_center["count_near"] == 12       # (0,0) e' fuori 30 km
    without = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert without["center"] is None
    assert without["count_near"] == 0            # nessun anchor = nessun raggio
    assert without["count_total"] == 13
    assert without["per_slot_counts"] == [3, 3, 3, 4]  # lo slot recente ultimo


def test_dpc_all_403_is_unavailable_with_reason():
    opener = FakeOpener(default=None)
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert resp["available"] is False
    assert "ltg_unavailable" in resp["reason"]
    assert "not_published_403" in resp["reason"]
    assert resp["count_near"] == 0
    assert resp["strikes"] == []
    assert len(opener.calls) == raw_ltg.LTG_MAX_BACKOFF_STEPS + 1


def test_dpc_network_error_fails_fast_without_backoff():
    opener = FakeOpener(error=urllib.error.URLError("dns fail"))
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert resp["available"] is False
    assert "URLError" in resp["reason"]
    assert len(opener.calls) == 1                 # nessun retry su errore rete


def test_dpc_backoff_to_published_slot():
    anchor = RADAR_MS - 3 * PERIOD_MS
    opener = FakeOpener(payloads={anchor: _blitz(NEAR)}, default=None)
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS, window_slots=4,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is True
    assert resp["anchor_ms"] == anchor
    assert resp["staleness_ms"] == 3 * PERIOD_MS
    assert resp["slots_ok"] == 1                  # solo lo slot ancorato
    assert resp["count_near"] == 3
    assert len(opener.calls) == 7      # 3 slot 403 + anchor + 3 slot precedenti


def test_dpc_stale_window_is_declined():
    stale_anchor = RADAR_MS - 7 * PERIOD_MS      # 35 min > 30 min massimi
    opener = FakeOpener(payloads={stale_anchor: _blitz(NEAR)}, default=None)
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert resp["available"] is False
    assert resp["reason"].startswith("stale_ltg:")
    assert resp["count_near"] == 0


def test_dpc_network_disabled_never_calls_transport():
    opener = FakeOpener(default=_blitz(NEAR))
    provider = pltg.get_provider("dpc", network=False, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert resp["available"] is False
    assert resp["reason"] == "network_disabled"
    assert opener.calls == []


def test_dpc_window_is_cached_per_run():
    opener = FakeOpener(default=_blitz(NEAR))
    provider = pltg.get_provider("dpc", network=True, opener=opener)
    first = provider.strikes_in_window(epoch_ms=RADAR_MS, window_slots=4)
    second = provider.strikes_in_window(epoch_ms=RADAR_MS, window_slots=4)
    # senza centro count_near e' 0 (nessun anchor): si conta il totale
    assert first["count_total"] == second["count_total"] == 12
    assert len(opener.calls) == 4                 # una sola finestra scaricata


def test_response_schema_is_stable_available_or_not():
    for provider in (pltg.get_provider("dpc", network=False),
                     pltg.get_provider("mli", network=False)):
        for resp in (provider.strikes_in_window(epoch_ms=RADAR_MS),
                     pltg.unavailable(provider.name, "reason_test")):
            assert set(resp) == set(REQUIRED_RESPONSE_KEYS)
            assert isinstance(resp["available"], bool)
            assert isinstance(resp["count_near"], int)
            assert isinstance(resp["trend"], dict)
            assert resp["note"] is None or isinstance(resp["note"], str)
            assert resp["attribution"] is None or isinstance(
                resp["attribution"], str)
        unavailable = pltg.unavailable(provider.name, "not_configured")
        assert unavailable["available"] is False
        assert unavailable["source"] == provider.name
        assert unavailable["count_near"] == 0
        assert unavailable["strength"] is None
        assert unavailable["note"] is None
        assert unavailable["attribution"] is None


# ===========================================================================
# Registry provider (gancio S4b)
# ===========================================================================
def test_registry_has_dpc_and_mli_and_unknown_rejected():
    assert "dpc" in pltg.available_providers()
    assert "mli" in pltg.available_providers()      # registrato da S4b
    with pytest.raises(pltg.UnknownProviderError) as exc:
        pltg.get_provider("nope")
    assert "unknown_lightning_provider:nope" in str(exc.value)


def test_registry_register_hook_is_usable_and_cleaned_up():
    class DummyProvider(pltg.LightningProvider):
        name = "dummy-test"

        def fetch_window(self, epoch_ms=None, window_slots=4):
            return pltg.unavailable(self.name, "test_only")

    try:
        pltg.register_provider("dummy-test", DummyProvider)
        assert "dummy-test" in pltg.available_providers()
        provider = pltg.get_provider("dummy-test", network=False)
        resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
        assert resp["available"] is False
        assert resp["reason"] == "test_only"
    finally:
        pltg._PROVIDERS.pop("dummy-test", None)
    assert "dummy-test" not in pltg.available_providers()


def test_unknown_provider_name_is_rejected():
    with pytest.raises(pltg.UnknownProviderError):
        pltg.get_provider("")
    with pytest.raises(pltg.UnknownProviderError):
        pltg.get_provider(None)


# ===========================================================================
# Logica HAIL
# ===========================================================================
def test_hail_suspect_from_two_recent_points():
    event = hail.evaluate_hail(_obs(_points([58.0, 58.0])), LTG_OFF, None)
    assert event is not None
    assert event["type"] == "HAIL"
    assert event["state"] == "SUSPECT"
    assert event["thresholds_version"] == THRESHOLDS_VERSION
    assert 0.0 <= event["score"] <= 69.0
    assert event["id"] == "HAIL-cell-1"
    assert event["evidence"]["points_ge_55"] == 2
    assert event["evidence"]["max_dbz"] == 58.0
    assert event["evidence"]["persistence_source"] == "points"
    assert event["evidence"]["lightning"]["available"] is False


def test_hail_single_point_is_not_enough():
    assert hail.evaluate_hail(_obs(_points([62.0])), LTG_OK, None) is None


def test_hail_point_outside_window_is_ignored():
    old = {"timestamp_ms": RADAR_MS - 20 * 60 * 1000,
           "timestamp": ev_store.ms_to_iso(RADAR_MS - 20 * 60 * 1000),
           "lonlat": [12.0, 41.0], "max_dbz": 70.0}
    recent = _points([58.0, 58.0])
    windowed = hail.recent_points(recent + [old], RADAR_MS)
    # il punto da 70 dBZ di 20 minuti prima NON entra nella finestra di 15 min
    assert [p["timestamp_ms"] for p in windowed] == \
        [p["timestamp_ms"] for p in recent]
    event = hail.evaluate_hail(_obs(windowed), LTG_OFF, None)
    assert event is not None
    assert event["evidence"]["recent_points"] == 2
    assert event["evidence"]["max_dbz"] == 58.0     # il 70 dBZ vecchio non conta


def test_hail_corroborated_with_dbz60_and_lightning():
    event = hail.evaluate_hail(_obs(_points([58.0, 62.0])), LTG_OK, None)
    assert event is not None
    assert event["state"] == "CORROBORATED"
    assert 70.0 <= event["score"] <= 99.0


def test_hail_stays_suspect_when_lightning_unavailable():
    event = hail.evaluate_hail(_obs(_points([62.0, 62.0])), LTG_OFF, None)
    assert event is not None
    assert event["state"] == "SUSPECT"
    labels = event["evidence"]["labels"]
    assert any("fulmini non disponibili" in lab for lab in labels)


def test_hail_positive_dbz_trend_promotes():
    event = hail.evaluate_hail(_obs(_points([55.0, 55.0, 58.0])), LTG_OK,
                               None)
    assert event is not None
    assert event["state"] == "CORROBORATED"
    assert event["evidence"]["dbz_trend_per_frame"] == 1.5


def test_hail_duration_source_for_targets_without_points():
    obs = _obs([], max_dbz=62.0, source="duration", recent_points=[],
               duration_min=12.0, on_latest_frame=True, anchor="storm_object-7",
               type="storm_object")
    event = hail.evaluate_hail(obs, LTG_OFF, None)
    assert event is not None
    assert event["state"] == "SUSPECT"
    assert event["id"] == "HAIL-storm_object-7"
    assert event["evidence"]["persistence_source"] == "duration"


def test_hail_duration_source_requires_persistence_and_freshness():
    base = dict(anchor="storm_object-7", type="storm_object",
                position=[12.5, 41.5], first_seen=RADAR_ISO,
                last_seen=RADAR_ISO, persistence_source="duration",
                recent_points=[], classification=None, n_frames=None)
    too_short = hail.evaluate_hail(
        dict(base, max_dbz=62.0, duration_min=5.0, on_latest_frame=True),
        LTG_OFF, None)
    stale = hail.evaluate_hail(
        dict(base, max_dbz=62.0, duration_min=12.0, on_latest_frame=False),
        LTG_OFF, None)
    assert too_short is None
    assert stale is None


def test_hail_freezing_level_is_evidence_not_a_gate():
    obs = _obs(_points([58.0, 58.0]))
    without = hail.evaluate_hail(obs, LTG_OFF, None)
    with_h0 = hail.evaluate_hail(obs, LTG_OFF, 2500.0)
    high_h0 = hail.evaluate_hail(obs, LTG_OFF, 4200.0)
    assert without["state"] == with_h0["state"] == high_h0["state"] == "SUSPECT"
    assert with_h0["evidence"]["freezing_level_low"] is True
    assert high_h0["evidence"]["freezing_level_low"] is False
    assert any(lab.startswith("H0 ") for lab in with_h0["evidence"]["labels"])
    assert not any(lab.startswith("H0 ")
                   for lab in high_h0["evidence"]["labels"])


def test_fetch_freezing_level_never_raises():
    assert hail.fetch_freezing_level(41.0, 12.0, client=_h0_client()) == 2500.0

    def boom(url):
        raise RuntimeError("rete spenta")

    assert hail.fetch_freezing_level(41.0, 12.0, client=boom) is None
    assert hail.fetch_freezing_level(41.0, 12.0,
                                     client=lambda url: None) is None
    assert hail.fetch_freezing_level(41.0, 12.0,
                                     client=lambda url: {"hourly": {}}) is None

    class Client:
        def get_json(self, url):
            return _h0_client()(url)

    assert hail.fetch_freezing_level(41.0, 12.0, client=Client()) == 2500.0


def test_hail_score_is_bounded():
    assert hail.hail_score("SUSPECT", 55.0, LTG_OFF, None) == 40.0
    assert hail.hail_score("CORROBORATED", 70.0, LTG_OK, 2500.0) <= 99.0
    assert hail.hail_score("VERIFIED", None, LTG_OFF, None) == 100.0


# ===========================================================================
# Logica VORTEX
# ===========================================================================
def test_vortex_suspect_from_organization_score():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=65), LTG_OFF)
    assert event is not None
    assert event["type"] == "VORTEX"
    assert event["state"] == "SUSPECT"
    assert event["id"] == "VORTEX-cell-1"
    assert event["evidence"]["organization_score"] == 65
    assert event["evidence"]["doppler"] is False


def test_vortex_morphology_only_is_suspect():
    event = vortex.evaluate_vortex(
        _obs(_points([50.0, 50.0]), organization_score=None,
             eccentricity_max=0.95, solidity_min=0.80, compactness_max=4.0),
        LTG_OFF)
    assert event is not None
    assert event["state"] == "SUSPECT"
    assert event["evidence"]["hookish_morphology"] is True


def test_vortex_without_gate_is_not_an_event():
    assert vortex.evaluate_vortex(
        _obs(_points([50.0, 50.0]), organization_score=None,
             eccentricity_max=0.70, solidity_min=1.0, compactness_max=1.8),
        LTG_OFF) is None
    assert vortex.evaluate_vortex(
        _obs(_points([50.0]), organization_score=80, duration_min=5.0),
        LTG_OFF) is None                       # persistenza insufficiente


def test_vortex_corroborated_needs_org_and_lightning():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=80), LTG_UP)
    assert event is not None
    assert event["state"] == "CORROBORATED"
    assert 70.0 <= event["score"] <= 99.0


def test_vortex_corroborated_blocked_without_lightning():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=80), LTG_OFF)
    assert event is not None
    assert event["state"] == "SUSPECT"
    assert event["evidence"]["lightning"]["available"] is False


def test_vortex_corroborated_blocked_with_low_org():
    event = vortex.evaluate_vortex(
        _obs(_points([50.0, 50.0]), organization_score=55,
             eccentricity_max=0.95, solidity_min=0.80, compactness_max=4.0),
        LTG_UP)
    assert event is not None
    assert event["state"] == "SUSPECT"        # org < 70: niente promozione


def test_vortex_requires_persistence():
    assert vortex.evaluate_vortex(
        _obs([], source="duration", recent_points=[], duration_min=4.0,
             on_latest_frame=True, organization_score=80), LTG_OFF) is None


def test_vortex_always_disclaims_doppler():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=80), LTG_UP)
    labels = event["evidence"]["labels"]
    assert any(vortex.DOPPLER_DISCLAIMER in lab for lab in labels)
    assert any("Doppler" in lab for lab in labels)


def test_vortex_score_is_bounded():
    assert vortex.vortex_score("SUSPECT", 60, LTG_OFF) == 40.0
    assert vortex.vortex_score("CORROBORATED", 80, LTG_UP) <= 99.0
    assert vortex.vortex_score("VERIFIED", None, LTG_OFF) == 100.0


# ===========================================================================
# Events store + badges
# ===========================================================================
def test_merge_updates_same_id_and_keeps_earliest_first_seen():
    existing = ev_store.empty_events("2026-10-07T00:00:00Z")
    first = {"id": "HAIL-cell-1", "type": "HAIL", "state": "SUSPECT",
             "score": 40.0, "first_seen": "2026-10-06T23:00:00Z",
             "last_seen": "2026-10-07T00:00:00Z", "position": [12.0, 41.0],
             "evidence": {"anchor": "cell-1"}}
    merged = ev_store.merge_events(existing, [first], "2026-10-07T00:00:00Z")
    second = dict(first, last_seen=RADAR_ISO, state="CORROBORATED", score=75.0,
                  first_seen="2026-10-07T00:00:00Z")
    merged = ev_store.merge_events(merged, [second], RADAR_ISO)
    assert [e["id"] for e in merged["events"]] == ["HAIL-cell-1"]
    event = merged["events"][0]
    assert event["first_seen"] == "2026-10-06T23:00:00Z"
    assert event["last_seen"] == RADAR_ISO
    assert event["state"] == "CORROBORATED"
    assert merged["window_hours"] == WINDOW_HOURS


def test_merge_keeps_absent_ids_until_they_expire():
    existing = {"generated_at": RADAR_ISO, "window_hours": 24, "events": [
        {"id": "HAIL-cell-1", "last_seen": RADAR_ISO,
         "first_seen": RADAR_ISO},
        {"id": "HAIL-cell-9", "last_seen": "2026-10-01T00:00:00Z",
         "first_seen": "2026-10-01T00:00:00Z"},
    ]}
    merged = ev_store.merge_events(existing, [], RADAR_ISO)
    assert [e["id"] for e in merged["events"]] == ["HAIL-cell-1"]


def test_merge_prune_anchor_is_data_time_not_wall_clock():
    incoming = [{"id": "HAIL-cell-1", "type": "HAIL", "state": "SUSPECT",
                 "score": 40.0, "first_seen": "2026-10-06T23:00:00Z",
                 "last_seen": RADAR_ISO, "position": [12.0, 41.0],
                 "evidence": {}}]
    clock = "2026-10-08T18:00:00Z"                # 42h avanti dai dati
    by_clock = ev_store.merge_events(None, incoming, clock, 24)
    assert by_clock["events"] == []               # finestra a orologio: vuota
    by_data = ev_store.merge_events(None, incoming, clock, 24,
                                    prune_anchor=RADAR_ISO)
    assert [e["id"] for e in by_data["events"]] == ["HAIL-cell-1"]


def test_merge_orders_events_by_id():
    incoming = [{"id": "VORTEX-cell-2", "last_seen": RADAR_ISO},
                {"id": "HAIL-cell-1", "last_seen": RADAR_ISO}]
    merged = ev_store.merge_events(None, incoming, RADAR_ISO)
    assert [e["id"] for e in merged["events"]] == ["HAIL-cell-1",
                                                   "VORTEX-cell-2"]


def test_atomic_write_json_is_valid_and_leaves_no_tmp(tmp_path):
    path = tmp_path / "out" / "events.json"
    ev_store.atomic_write_json(str(path), {"a": 1})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert list(path.parent.glob(".phenomena-*.tmp")) == []
    with pytest.raises(TypeError):
        ev_store.atomic_write_json(str(path), {"bad": object()})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 1}
    assert list(path.parent.glob(".phenomena-*.tmp")) == []


def test_load_events_corrupt_or_missing_returns_empty(tmp_path):
    assert ev_store.load_events(str(tmp_path / "nope.json"))["events"] == []
    broken = tmp_path / "events.json"
    broken.write_text("{not json", encoding="utf-8")
    assert ev_store.load_events(str(broken))["events"] == []
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert ev_store.load_events(str(wrong))["events"] == []


def test_build_badges_only_active_and_promotes_labels():
    active = {"id": "HAIL-cell-1", "type": "HAIL", "state": "SUSPECT",
              "score": 40.0, "first_seen": RADAR_ISO, "last_seen": RADAR_ISO,
              "position": [12.0, 41.0],
              "evidence": {"anchor": "cell-1", "labels": ["tier SUSPECT"]}}
    stale = dict(active, id="HAIL-cell-2", last_seen="2026-10-06T23:00:00Z")
    payload = ev_store.build_badges([stale, active], RADAR_ISO, RADAR_ISO)
    assert [b["id"] for b in payload["badges"]] == ["HAIL-cell-1"]
    assert payload["radar_timestamp"] == RADAR_ISO
    badge = payload["badges"][0]
    assert set(badge) == set(REQUIRED_BADGE_KEYS)
    assert badge["area_id"] == "cell-1"
    assert badge["labels"] == ["tier SUSPECT"]
    assert "labels" not in badge["evidence"]
    assert ev_store.is_active(stale, RADAR_ISO) is False
    assert ev_store.is_active(active, RADAR_ISO) is True


# ===========================================================================
# build_observations
# ===========================================================================
def test_build_observations_dedupes_cell_candidate_and_keeps_storm_object(tmp_path):
    radar_dir = _write_radar(tmp_path / "radar")
    # il candidato "cell" con lo stesso track_id della track va saltato
    data = json.loads((radar_dir / "supercells.json").read_text(
        encoding="utf-8"))
    data["candidates"].append({
        "track_id": 1, "track_type": "cell", "position": [12.0, 41.0],
        "first_seen": RADAR_ISO, "last_seen": RADAR_ISO,
        "intensity": {"max_dbz": 70.0},
        "organization": {"organization_score": 90},
        "motion": {"duration_min": 15.0}, "on_latest_frame": True})
    _write_json(radar_dir / "supercells.json", data)
    tracks = json.loads((radar_dir / "tracks.json").read_text(encoding="utf-8"))
    supercells = json.loads((radar_dir / "supercells.json").read_text(
        encoding="utf-8"))
    storms = json.loads((radar_dir / "storms.geojson").read_text(
        encoding="utf-8"))
    obs = engine.build_observations(supercells, tracks, storms, RADAR_MS,
                                    RADAR_ISO)
    anchors = [o["anchor"] for o in obs]
    assert anchors == ["cell-1", "storm_object-7"]
    cell = obs[0]
    assert cell["persistence_source"] == "points"
    assert len(cell["recent_points"]) == 4
    assert cell["max_dbz"] == 58.0            # dal track, non dal candidato
    assert cell["eccentricity_max"] == 0.95   # morfologia da storms.geojson
    assert cell["solidity_min"] == 0.80
    assert cell["compactness_max"] == 4.0
    storm_object = obs[1]
    assert storm_object["type"] == "storm_object"
    assert storm_object["persistence_source"] == "duration"
    assert storm_object["max_dbz"] == 62.0
    assert storm_object["duration_min"] == 12.0


def test_build_observations_track_without_points_falls_back_to_candidate(tmp_path):
    radar_dir = _write_radar(tmp_path / "radar")
    tracks = {"tracks": [{"track_id": 1, "points": [],
                          "organization_score": 40}]}
    supercells = {"candidates": [{
        "track_id": 1, "track_type": "cell", "position": [12.0, 41.0],
        "first_seen": RADAR_ISO, "last_seen": RADAR_ISO,
        "intensity": {"max_dbz": 57.0},
        "organization": {"organization_score": 66},
        "motion": {"duration_min": 11.0}, "on_latest_frame": True}]}
    storms = {"features": []}
    obs = engine.build_observations(supercells, tracks, storms, RADAR_MS,
                                    RADAR_ISO)
    assert len(obs) == 1
    assert obs[0]["anchor"] == "cell-1"
    assert obs[0]["persistence_source"] == "duration"
    assert obs[0]["max_dbz"] == 57.0
    assert obs[0]["organization_score"] == 66


# ===========================================================================
# Engine e2e
# ===========================================================================
def test_engine_run_no_network_writes_events_and_badges(radar_case, capsys):
    summary = engine.run(radar_dir=radar_case["radar"],
                         out_dir=radar_case["out"], network=False)
    out = capsys.readouterr().out
    assert re.search(r"\[phenomena\] events=4 active=4 "
                     r"\(hail=2 vortex=2\) source=mli "
                     r"events_written=True badges_written=True", out)
    assert summary["ok"] is True
    assert summary["degraded"] is False
    assert summary["events"] == 4
    assert summary["active"] == 4
    assert "mli" == summary["source"]
    assert summary["events_written"] is True
    assert summary["badges_written"] is True

    events_path = os.path.join(radar_case["out"], "events.json")
    badges_path = os.path.join(radar_case["out"], "badges.json")
    with open(events_path, encoding="utf-8") as fh:
        events = json.load(fh)
    with open(badges_path, encoding="utf-8") as fh:
        badges = json.load(fh)
    assert set(events) == {"generated_at", "window_hours", "events"}
    assert set(badges) == {"generated_at", "radar_timestamp", "badges"}
    assert [e["id"] for e in events["events"]] == [
        "HAIL-cell-1", "HAIL-storm_object-7", "VORTEX-cell-1",
        "VORTEX-storm_object-7"]
    for event in events["events"]:
        assert set(event) == set(REQUIRED_EVENT_KEYS)
        assert event["state"] == "SUSPECT"      # nessun fulmini: nessun salto
        assert event["thresholds_version"] == THRESHOLDS_VERSION
        assert event["last_seen"] == RADAR_ISO
    for badge in badges["badges"]:
        assert set(badge) == set(REQUIRED_BADGE_KEYS)
    assert badges["radar_timestamp"] == RADAR_ISO
    assert len(badges["badges"]) == 4
    events_by_id = {e["id"]: e for e in events["events"]}
    for badge in badges["badges"]:
        assert badge["id"] in events_by_id
        assert ev_store.is_active(events_by_id[badge["id"]], RADAR_ISO)
    vortex_badges = [b for b in badges["badges"] if b["type"] == "VORTEX"]
    assert all(any("Doppler" in lab for lab in b["labels"])
               for b in vortex_badges)


def test_engine_cli_subprocess_no_network(radar_case):
    proc = subprocess.run(
        [sys.executable,
         os.path.join(SCRIPTS_DIR, "phenomena", "engine.py"),
         "--radar-dir", radar_case["radar"],
         "--out-dir", radar_case["out"], "--no-network"],
        cwd=RELEASE_ROOT, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    assert "[phenomena] events=4 active=4" in proc.stdout
    assert os.path.isfile(os.path.join(radar_case["out"], "events.json"))
    assert os.path.isfile(os.path.join(radar_case["out"], "badges.json"))


def test_engine_injected_lightning_and_h0_promote(tmp_path, capsys):
    radar_dir = _write_radar(tmp_path / "radar",
                             track_dbz=(58.0, 60.0, 62.0, 62.0))
    opener = FakeOpener(payloads=_payloads(
        RADAR_MS, {0: NEAR6, 1: NEAR, 2: NEAR[:2], 3: NEAR[:1]}))
    summary = engine.run(radar_dir=str(radar_dir),
                         out_dir=str(tmp_path / "phenomena"),
                         provider_name="dpc",     # opener DPC iniettato
                         network=True, opener=opener, h0_client=_h0_client())
    capsys.readouterr()
    events = json.loads(open(os.path.join(tmp_path, "phenomena",
                                          "events.json"),
                             encoding="utf-8").read())
    by_id = {e["id"]: e for e in events["events"]}
    assert by_id["HAIL-cell-1"]["state"] == "CORROBORATED"
    assert by_id["VORTEX-cell-1"]["state"] == "CORROBORATED"
    assert by_id["HAIL-cell-1"]["evidence"]["lightning"]["count_near"] == 12
    assert by_id["HAIL-cell-1"]["evidence"]["freezing_level_m"] == 2500.0
    assert by_id["HAIL-cell-1"]["evidence"]["freezing_level_low"] is True
    assert summary["hail"] >= 1
    assert len(opener.calls) == 4               # una sola finestra DPC


def test_engine_status_not_ok_prunes_only_and_clears_badges(tmp_path):
    radar_dir = _write_radar(tmp_path / "radar", status="error")
    out_dir = tmp_path / "phenomena"
    stale = {"id": "HAIL-cell-9", "type": "HAIL", "state": "SUSPECT",
             "score": 40.0, "first_seen": "2026-10-01T00:00:00Z",
             "last_seen": "2026-10-01T00:00:00Z", "position": [12.0, 41.0],
             "evidence": {"anchor": "cell-9"}}
    fresh = dict(stale, id="HAIL-cell-1", first_seen=RADAR_ISO,
                 last_seen=RADAR_ISO)
    _write_json(out_dir / "events.json",
                {"generated_at": RADAR_ISO, "window_hours": 24,
                 "events": [stale, fresh]})
    summary = engine.run(radar_dir=str(radar_dir), out_dir=str(out_dir),
                         network=False)
    assert summary["degraded"] is True
    assert summary["active"] == 0
    events = json.loads((out_dir / "events.json").read_text(encoding="utf-8"))
    badges = json.loads((out_dir / "badges.json").read_text(encoding="utf-8"))
    assert [e["id"] for e in events["events"]] == ["HAIL-cell-1"]
    assert events["events"][0]["state"] == "SUSPECT"   # invariato
    assert badges["badges"] == []
    assert badges["radar_timestamp"] == RADAR_ISO


def test_engine_missing_latest_is_degraded_not_a_crash(tmp_path, capsys):
    out_dir = tmp_path / "phenomena"
    (tmp_path / "radar").mkdir()
    summary = engine.run(radar_dir=str(tmp_path / "radar"),
                         out_dir=str(out_dir), network=False)
    capsys.readouterr()
    assert summary["degraded"] is True
    assert summary["events"] == 0
    assert json.loads((out_dir / "badges.json").read_text(
        encoding="utf-8"))["badges"] == []
    assert os.path.isfile(str(out_dir / "events.json"))


def test_engine_unknown_provider_exits_1_without_writing(tmp_path):
    out_dir = tmp_path / "phenomena"
    code = engine.main(["--radar-dir", str(tmp_path / "radar"),
                        "--out-dir", str(out_dir),
                        "--lightning-provider", "nope", "--no-network"])
    assert code == 1
    assert list(out_dir.glob("*.json")) == []


def test_engine_is_idempotent_on_same_inputs(radar_case, capsys):
    engine.run(radar_dir=radar_case["radar"], out_dir=radar_case["out"],
               network=False)
    capsys.readouterr()
    first_events = json.loads(open(os.path.join(radar_case["out"],
                                                "events.json"),
                                   encoding="utf-8").read())
    first_badges = json.loads(open(os.path.join(radar_case["out"],
                                                "badges.json"),
                                   encoding="utf-8").read())
    engine.run(radar_dir=radar_case["radar"], out_dir=radar_case["out"],
               network=False)
    capsys.readouterr()
    second_events = json.loads(open(os.path.join(radar_case["out"],
                                                 "events.json"),
                                    encoding="utf-8").read())
    second_badges = json.loads(open(os.path.join(radar_case["out"],
                                                 "badges.json"),
                                    encoding="utf-8").read())
    assert first_events["events"] == second_events["events"]
    assert first_badges["badges"] == second_badges["badges"]
    assert [e["id"] for e in second_events["events"]] == sorted(
        e["id"] for e in second_events["events"])


@pytest.mark.skipif(not os.path.isdir(REAL_RADAR_DIR),
                    reason="data/radar della release assente")
def test_engine_on_real_release_data_respects_schema(tmp_path, capsys):
    summary = engine.run(radar_dir=REAL_RADAR_DIR,
                         out_dir=str(tmp_path / "phenomena"), network=False)
    capsys.readouterr()
    assert summary["ok"] is True
    events = json.loads(open(os.path.join(tmp_path, "phenomena",
                                          "events.json"),
                             encoding="utf-8").read())
    badges = json.loads(open(os.path.join(tmp_path, "phenomena",
                                          "badges.json"),
                             encoding="utf-8").read())
    ids = [e["id"] for e in events["events"]]
    assert ids == sorted(ids) and len(ids) == len(set(ids))
    for event in events["events"]:
        assert set(event) == set(REQUIRED_EVENT_KEYS)
        assert event["state"] in {"SUSPECT", "CORROBORATED"}   # mai VERIFIED
        assert 0.0 <= event["score"] <= 100.0
        assert event["thresholds_version"] == THRESHOLDS_VERSION
        assert "labels" in event["evidence"]
        assert event["evidence"]["anchor"]
    assert events["window_hours"] == WINDOW_HOURS
    assert badges["radar_timestamp"] == summary["radar_timestamp"] == RADAR_ISO
    assert [b["id"] for b in badges["badges"]] == sorted(
        b["id"] for b in badges["badges"])
    events_by_id = {e["id"]: e for e in events["events"]}
    for badge in badges["badges"]:
        assert set(badge) == set(REQUIRED_BADGE_KEYS)
        assert badge["id"] in events_by_id
        assert ev_store.is_active(events_by_id[badge["id"]], RADAR_ISO)
    assert len(events["events"]) == summary["events"]
    assert len(badges["badges"]) == summary["active"]


# ===========================================================================
# MLI EUMETSAT (S4b) — utility pure, provider, gate per-sorgente
# ===========================================================================
def test_mli_getmap_url_and_slot_helpers():
    url = pltg.build_mli_getmap_url("2026-10-07T00:00:00Z")
    assert url.startswith(pltg.MLI_WMS_URL + "?")
    assert "request=GetMap" in url
    assert "layers=mtg_fd%3Ali_afa" in url
    assert "time=2026-10-07T00%3A00%3A00Z" in url
    assert "bbox=6.6%2C36.5%2C18.8%2C47.2" in url
    assert pltg.build_mli_getmap_url() == pltg.build_mli_getmap_url(None)
    assert "time=" not in pltg.build_mli_getmap_url()
    assert pltg._slot_floor(RADAR_MS + 90 * 1000) == RADAR_MS
    assert pltg._slot_iso(RADAR_MS) == "2026-10-07T00:05:00Z"


def test_mli_decode_png_counts_active_pixels():
    mask = pltg.decode_afa_png(MLI_PNG_NOW)
    assert mask.shape == (pltg.MLI_HEIGHT, pltg.MLI_WIDTH)
    assert int(mask.sum()) == 64
    assert int(pltg.decode_afa_png(MLI_PNG_PREV).sum()) == 16
    assert int(pltg.decode_afa_png(MLI_PNG_FAR).sum()) == 2000
    with pytest.raises(ValueError):
        pltg.decode_afa_png(b"PNG-not-really")
    with pytest.raises(ValueError):
        pltg.decode_afa_png(None)


def test_mli_sample_field_radius_disk_and_coverage():
    mask = pltg.decode_afa_png(MLI_PNG_NOW)
    sample = pltg.sample_afa_field(mask, pltg.MLI_BBOX, [12.0, 41.0], 30.0)
    assert sample["in_coverage"] is True
    assert sample["count_near"] == 64
    assert abs(sample["count_disk"] - 2134) < 10     # disco 30 km ~2134 px
    outside = pltg.sample_afa_field(mask, pltg.MLI_BBOX, [0.0, 0.0], 30.0)
    assert outside["in_coverage"] is False
    assert outside["count_near"] == 0
    with pytest.raises(ValueError):
        pltg.sample_afa_field(np.zeros(4), pltg.MLI_BBOX, [12.0, 41.0])
    with pytest.raises(ValueError):
        pltg.sample_afa_field(mask, pltg.MLI_BBOX, None, 12.0)


def test_mli_provider_network_disabled_makes_no_calls():
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW})
    provider = pltg.get_provider("mli", network=False, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is False
    assert resp["reason"] == "network_disabled"
    assert opener.calls == []


def test_mli_provider_counts_trend_and_shared_cache():
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW,
                                  RADAR_MS - PERIOD_MS: MLI_PNG_PREV})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is True
    assert resp["source"] == "mli"
    assert resp["window_slots"] == resp["slots_ok"] == 1   # AFA accumulato
    assert resp["anchor_ms"] == RADAR_MS
    assert resp["staleness_ms"] == 0
    assert resp["count_total"] == 64
    assert resp["count_near"] == 64
    assert resp["center"] == [12.0, 41.0]
    assert resp["per_slot_counts"] == [16, 64]
    assert resp["trend"]["direction"] == "up"
    assert resp["strength"] == 0.03
    assert resp["score"] == pytest.approx(3.0, abs=0.3)
    assert resp["strikes"] == []
    assert resp["note"] == pltg.MLI_NOTE
    assert resp["attribution"] == pltg.MLI_ATTRIBUTION
    assert len(opener.calls) == 2        # scan corrente + precedente, cachati
    following = provider.strikes_in_window(epoch_ms=RADAR_MS + PERIOD_MS,
                                           center_lonlat=[12.0, 41.0])
    assert following["available"] is True
    assert len(opener.calls) == 4        # nuovo scan + precedente gia' in cache


def test_mli_trend_none_when_previous_scan_missing():
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW},
                           error_times={RADAR_MS - PERIOD_MS})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is True
    assert resp["trend"] is None
    assert resp["per_slot_counts"] == [64]
    assert resp["count_near"] == 64


def test_mli_all_slots_http_error_backoffs_to_unavailable():
    opener = MliFakeOpener(slots={}, fail_http_codes={
        RADAR_MS - k * PERIOD_MS: 502 for k in range(5)})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is False
    assert "mli_unavailable" in resp["reason"]
    assert "http_502" in resp["reason"]
    assert len(opener.calls) == pltg.MLI_MAX_BACKOFF_STEPS + 1


def test_mli_network_error_fails_fast_without_backoff():
    opener = MliFakeOpener(slots={}, error_times={RADAR_MS})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[12.0, 41.0])
    assert resp["available"] is False
    assert resp["reason"] == "network:URLError"
    assert len(opener.calls) == 1                      # nessun retry


def test_mli_without_center_reports_total_only():
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW,
                                  RADAR_MS - PERIOD_MS: MLI_PNG_PREV})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS)
    assert resp["available"] is True
    assert resp["center"] is None
    assert resp["count_near"] == 0                     # 0 = nessuna misura
    assert resp["strength"] == 0.0
    assert resp["count_total"] == 64
    assert resp["per_slot_counts"] == [16, 64]         # totali Italia per slot


def test_mli_center_outside_coverage_is_declined():
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW})
    provider = pltg.get_provider("mli", network=True, opener=opener)
    resp = provider.strikes_in_window(epoch_ms=RADAR_MS,
                                      center_lonlat=[0.0, 0.0])
    assert resp["available"] is False
    assert resp["reason"] == "center_outside_coverage"


# ===========================================================================
# Gate fulmini provider-agnostico
# ===========================================================================
def test_lightning_strength_fallback_and_clamp():
    assert pltg.lightning_strength({"available": True, "strength": 0.05}) == 0.05
    assert pltg.lightning_strength({"available": True, "strength": 1.7}) == 1.0
    assert pltg.lightning_strength({"available": True, "count_near": 30}) == 1.0
    assert pltg.lightning_strength({"available": True, "count_near": 15}) == 0.5
    assert pltg.lightning_strength({"available": False}) == 0.0
    assert pltg.lightning_strength({}) == 0.0
    assert pltg.lightning_strength(None) == 0.0


def test_lightning_corroborates_per_source_thresholds():
    assert pltg.lightning_corroborates(
        {"available": True, "source": "mli", "strength": 0.02}) is True
    assert pltg.lightning_corroborates(
        {"available": True, "source": "mli", "strength": 0.005}) is False
    assert pltg.lightning_corroborates(
        {"available": True, "source": "dpc", "count_near": 10}) is True
    assert pltg.lightning_corroborates(
        {"available": True, "source": "dpc", "count_near": 9}) is False
    # DPC con strength esplicito: vale la soglia strength (0.33), NON count
    assert pltg.lightning_corroborates(
        {"available": True, "source": "dpc", "count_near": 25,
         "strength": 0.10}) is False
    assert pltg.lightning_corroborates({"available": False}) is False
    assert pltg.lightning_corroborates(None) is False
    assert pltg.lightning_corroborates({}) is False
    # sorgente ignota -> soglia di DEFAULT (dpc): si promuove, non si muore
    unknown = {"available": True, "source": "wms-foo", "count_near": 10}
    assert pltg.lightning_corroborates(unknown) is True


# ===========================================================================
# MLI — evidence in HAIL/VORTEX (nota AFA + attribution EUMETSAT)
# ===========================================================================
MLI_OK = {"available": True, "source": "mli", "reason": None,
          "count_near": 64, "count_total": 64, "per_slot_counts": [16, 64],
          "radius_km": 30.0, "center": [12.0, 41.0],
          "trend": {"direction": "up"}, "score": 3.0, "strength": 0.03,
          "note": pltg.MLI_NOTE, "attribution": pltg.MLI_ATTRIBUTION}
MLI_WEAK = dict(MLI_OK, count_near=4, strength=0.002, score=0.2,
                trend={"direction": "flat"})


def test_hail_corroborates_with_mli_gate_and_attribution():
    event = hail.evaluate_hail(_obs(_points([62.0, 62.0])), MLI_OK, None)
    assert event["state"] == "CORROBORATED"
    lg = event["evidence"]["lightning"]
    assert lg["source"] == "mli"
    assert lg["strength"] == 0.03
    assert lg["note"] == pltg.MLI_NOTE
    assert lg["attribution"] == pltg.MLI_ATTRIBUTION
    labels = event["evidence"]["labels"]
    assert any("px AFA nel raggio 30.0 km (trend up)" in lab for lab in labels)
    assert any(pltg.MLI_ATTRIBUTION in lab for lab in labels)


def test_hail_stays_suspect_below_mli_strength_threshold():
    event = hail.evaluate_hail(_obs(_points([62.0, 62.0])), MLI_WEAK, None)
    assert event is not None
    assert event["state"] == "SUSPECT"               # sotto 0.01: nessun salto


def test_vortex_corroborates_with_mli_gate_and_attribution():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=80), MLI_OK)
    assert event is not None
    assert event["state"] == "CORROBORATED"
    lg = event["evidence"]["lightning"]
    assert lg["source"] == "mli"
    assert lg["strength"] == 0.03
    assert lg["note"] == pltg.MLI_NOTE
    assert any("px AFA" in lab for lab in event["evidence"]["labels"])


def test_vortex_blocked_when_mli_strength_below_gate():
    event = vortex.evaluate_vortex(_obs(_points([50.0, 50.0]),
                                        organization_score=80), MLI_WEAK)
    assert event is not None
    assert event["state"] == "SUSPECT"


# ===========================================================================
# Engine e2e con provider MLI iniettato (PNG sintetici, ZERO rete)
# ===========================================================================
def test_engine_mli_promotes_full_run_with_synthetic_pngs(tmp_path, capsys):
    radar_dir = _write_radar(tmp_path / "radar",
                             track_dbz=(58.0, 60.0, 62.0, 62.0))
    opener = MliFakeOpener(slots={RADAR_MS: MLI_PNG_NOW,
                                  RADAR_MS - PERIOD_MS: MLI_PNG_PREV})
    summary = engine.run(radar_dir=str(radar_dir),
                         out_dir=str(tmp_path / "phenomena"),
                         network=True, opener=opener)
    capsys.readouterr()
    events = json.loads(open(os.path.join(tmp_path, "phenomena",
                                          "events.json"),
                             encoding="utf-8").read())
    by_id = {e["id"]: e for e in events["events"]}
    assert summary["source"] == "mli"
    assert summary["events_written"] is True
    assert by_id["HAIL-cell-1"]["state"] == "CORROBORATED"
    assert by_id["VORTEX-cell-1"]["state"] == "CORROBORATED"
    lg_hail = by_id["HAIL-cell-1"]["evidence"]["lightning"]
    assert lg_hail["source"] == "mli"
    assert lg_hail["strength"] == 0.03
    assert lg_hail["count_near"] == 64
    assert lg_hail["note"] == pltg.MLI_NOTE
    assert lg_hail["attribution"] == pltg.MLI_ATTRIBUTION
    assert len(opener.calls) == 2        # cur + prev, condivisi da tutti i target


# ===========================================================================
# NO-OP GUARD (anti commit-churn)
# ===========================================================================
def test_content_digest_ignores_only_volatile_keys():
    a = {"generated_at": "A", "events": [1, 2], "nested": {"x": 1}}
    b = {"generated_at": "B", "events": [1, 2], "nested": {"x": 1}}
    assert ev_store.content_digest(a) == ev_store.content_digest(b)
    assert (ev_store.content_digest(a, volatile_keys=())
            != ev_store.content_digest(b, volatile_keys=()))
    c = dict(b, events=[1, 2, 3])
    assert ev_store.content_digest(a) != ev_store.content_digest(c)


def test_write_json_if_changed_noop_guard(tmp_path):
    path = tmp_path / "out" / "events.json"
    base = {"generated_at": "2026-10-07T00:00:00Z", "events": [{"id": "X"}]}
    assert ev_store.write_json_if_changed(str(path), base) is True
    first_txt = path.read_text(encoding="utf-8")
    same = {"generated_at": "2026-10-07T00:30:00Z", "events": [{"id": "X"}]}
    assert ev_store.write_json_if_changed(str(path), same) is False
    assert path.read_text(encoding="utf-8") == first_txt    # intatto, no diff
    changed = dict(same, events=[{"id": "X"}, {"id": "Y"}])
    assert ev_store.write_json_if_changed(str(path), changed) is True
    assert json.loads(path.read_text(encoding="utf-8"))["events"] == [
        {"id": "X"}, {"id": "Y"}]
    # volatile_keys vuoti: generated_at conta -> riscrittura
    assert ev_store.write_json_if_changed(str(path), same,
                                          volatile_keys=()) is True


def test_write_json_if_changed_badges_ignore_radar_timestamp(tmp_path):
    path = tmp_path / "out" / "badges.json"
    badge = {"generated_at": "2026-10-07T00:05:00Z",
             "radar_timestamp": "2026-10-07T00:00:00Z", "badges": []}
    assert ev_store.write_json_if_changed(
        str(path), badge, ev_store.VOLATILE_BADGE_KEYS) is True
    assert ev_store.write_json_if_changed(
        str(path), dict(badge, radar_timestamp="2026-10-07T00:05:00Z"),
        ev_store.VOLATILE_BADGE_KEYS) is False
    assert ev_store.write_json_if_changed(
        str(path), dict(badge, badges=[{"id": "B"}], radar_timestamp=RADAR_ISO),
        ev_store.VOLATILE_BADGE_KEYS) is True


def test_write_json_if_changed_recovers_corrupt_or_missing(tmp_path):
    missing = tmp_path / "nope.json"
    assert ev_store.write_json_if_changed(str(missing), {"a": 1}) is True
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert ev_store.write_json_if_changed(str(broken), {"a": 2}) is True
    assert json.loads(broken.read_text(encoding="utf-8")) == {"a": 2}


def test_engine_guard_does_not_rewrite_unchanged_output(radar_case, capsys):
    engine.run(radar_dir=radar_case["radar"], out_dir=radar_case["out"],
               network=False)
    capsys.readouterr()
    events_path = os.path.join(radar_case["out"], "events.json")
    badges_path = os.path.join(radar_case["out"], "badges.json")
    st_events = os.stat(events_path)
    st_badges = os.stat(badges_path)
    body_events = open(events_path, "rb").read()
    body_badges = open(badges_path, "rb").read()
    summary = engine.run(radar_dir=radar_case["radar"], out_dir=radar_case["out"],
                         network=False)
    capsys.readouterr()
    assert summary["events_written"] is False
    assert summary["badges_written"] is False
    assert os.stat(events_path).st_mtime_ns == st_events.st_mtime_ns
    assert os.stat(badges_path).st_mtime_ns == st_badges.st_mtime_ns
    assert open(events_path, "rb").read() == body_events
    assert open(badges_path, "rb").read() == body_badges
    assert summary["events"] == 4
    assert summary["active"] == 4
