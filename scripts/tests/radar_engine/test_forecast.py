#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Test forecast.py: forecast <=2h per candidati supercell (ENGINE-ONLY).

Fixture sintetiche, nessuna rete: track lineari/curvate a velocita' nota su
griglia Italia, bundle minimo con radar_timestamp. Il motion della track e'
valorizzato con dati DELIBERATAMENTE errati (999 km/h, 180 deg): il forecast
deve derivare velocita'/direzione dai centroidi, non dal motion pubblicato."""

import json
import math

from radar_engine import forecast
from radar_engine import models
from radar_engine import output
from radar_engine import supercell as sc_mod
from radar_engine import tracking
from radar_engine.config import CONFIG

from conftest import make_cell, make_track

T0 = 1_777_027_200_000
STEP_S = 300
KM_PER_DEG = 111.1949


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _km_per_deg_lon(lat_deg):
    return KM_PER_DEG * math.cos(math.radians(lat_deg))


def _wrong_motion():
    """Motion NON usato dal forecast: valori di troppo, apposta."""
    return {
        "organization_score": 90,
        "classification": "Organized Convective Cell",
        "velocity_kmh": 999.0,
        "direction_toward_deg": 180.0,
        "distance_km": 1.0,
        "duration_min": 5.0,
        "area_growth_pct": 40.0,
        "intensity_delta_dbz": 0.0,
    }


def _moving_track(n=8, v_kmh=60.0, heading_deg=90.0, lon0=12.0, lat0=41.0,
                  t0=T0, dbz=54.0, mean_dbz=48.0, area0=300.0, growth=0.4,
                  track_id=1, dbz_series=None, lat_wobble_deg=0.0):
    """Track lineare a velocita' costante (heading toward: 0=N, 90=E)."""
    km_step = v_kmh * STEP_S / 3600.0
    dlat = km_step * math.cos(math.radians(heading_deg)) / KM_PER_DEG
    dlon = km_step * math.sin(math.radians(heading_deg)) / _km_per_deg_lon(lat0)
    cells = []
    for i in range(n):
        wobble = lat_wobble_deg * math.sin(2.0 * math.pi * i / 6.0)
        area = area0 * (1.0 + growth * i / max(n - 1, 1))
        cells.append(make_cell(
            lon0 + i * dlon, lat0 + i * dlat + wobble, t0 + i * STEP_S * 1000,
            area_km2=area,
            max_dbz=(dbz_series[i] if dbz_series else dbz),
            mean_dbz=mean_dbz, frame_index=i,
            cell_id="fc-{}-{}".format(track_id, i)))
    track = make_track(cells, track_id)
    track.motion = _wrong_motion()
    return track


def _curved_track(track_id=3):
    """8 punti su due tratti: 3 segmenti a 60 deg poi 4 a 90 deg (5 km/passi)."""
    km_step = 60.0 * STEP_S / 3600.0
    headings = [60.0] * 3 + [90.0] * 4
    lat, lon = 41.0, 12.0
    cells = [make_cell(lon, lat, T0, area_km2=300.0, max_dbz=54.0,
                       mean_dbz=48.0, frame_index=0,
                       cell_id="cur-{}".format(track_id))]
    for i, heading in enumerate(headings):
        lat += km_step * math.cos(math.radians(heading)) / KM_PER_DEG
        lon += km_step * math.sin(math.radians(heading)) / _km_per_deg_lon(lat)
        cells.append(make_cell(
            lon, lat, T0 + (i + 1) * STEP_S * 1000, area_km2=300.0,
            max_dbz=54.0, mean_dbz=48.0, frame_index=i + 1,
            cell_id="cur-{}-{}".format(track_id, i + 1)))
    track = make_track(cells, track_id)
    track.motion = _wrong_motion()
    return track


def _bundle(tracks, radar_ms=None, generated_at="2026-04-30T10:00:00Z"):
    """Bundle minimo: track + radar_timestamp all'ultimo punto."""
    bundle = models.EngineBundle("ok", generated_at, output.SOURCE_LABEL)
    bundle.tracks = list(tracks)
    bundle.storm_tracks = []
    bundle.supercells = []
    ms = radar_ms
    if ms is None and tracks:
        ms = max(t.points[-1].timestamp_ms for t in tracks)
    bundle.radar_timestamp_ms = ms
    bundle.radar_timestamp_iso = _iso(ms) if ms is not None else None
    bundle.cells_by_frame = [[]]
    return bundle


def _ctx(track, on_latest=True):
    return {"track": track, "track_id": track.track_id,
            "track_type": "cell", "on_latest_frame": on_latest}


def _ang_diff(a, b):
    return abs((a - b + 180.0) % 360.0 - 180.0)


# ---------------------------------------------------------------------------
# Vincoli di computabilita'
# ---------------------------------------------------------------------------
def test_forecast_null_se_track_meno_di_2_punti():
    track = _moving_track(n=1)
    bundle = _bundle([track])
    assert forecast.build_forecast(_ctx(track), bundle) is None


def test_forecast_null_se_non_on_latest_frame():
    track = _moving_track(n=6)
    bundle = _bundle([track])
    assert forecast.build_forecast(_ctx(track, on_latest=False), bundle) is None


def test_bundle_vuoto_non_fallisce():
    bundle = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    bundle.tracks = []
    bundle.storm_tracks = []
    bundle.supercells = []
    assert forecast.attach_forecasts(bundle) == 0
    assert forecast.build_forecast({}, bundle) is None
    assert forecast.build_forecast({"track_id": 99, "on_latest_frame": True},
                                   bundle) is None
    # track fornita nel ctx ma bundle senza track: media regionale di default
    track = _moving_track(n=6)
    block = forecast.build_forecast(_ctx(track), bundle)
    assert block is not None and len(block["steps"]) == 4
    assert all(20.0 <= s["max_dbz"] <= 75.0 for s in block["steps"])


def test_attach_forecasts_risolve_track_da_track_id():
    track = _moving_track(n=6, track_id=7)
    bundle = _bundle([track])
    bundle.supercells = [{"track_id": 7, "track_type": "cell",
                          "on_latest_frame": True, "supercell_id": "SC-X"}]
    assert forecast.attach_forecasts(bundle) == 1
    assert len(bundle.supercells[0]["forecast"]["steps"]) == 4
    bundle.supercells.append({"track_id": 7, "track_type": "cell",
                              "on_latest_frame": False})
    assert forecast.build_forecast(bundle.supercells[1], bundle) is None


# ---------------------------------------------------------------------------
# Proiezione, cono, direzione
# ---------------------------------------------------------------------------
def test_avanzamento_coerente_con_velocita():
    # 60 km/h reali (dal fit sui centroidi), motion del bundle = 999 km/h:
    # a +30 min la distanza deve essere ~30 km entro il 10%.
    track = _moving_track(n=8, v_kmh=60.0, heading_deg=90.0)
    bundle = _bundle([track])
    block = forecast.build_forecast(_ctx(track), bundle)
    origin = track.points[-1].lonlat
    dist = tracking.haversine_km(origin, tuple(block["steps"][0]["position"]))
    assert abs(dist - 30.0) <= 3.0


def test_cono_cresce_con_offset():
    track = _moving_track(n=8)
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    cones = [s["cone_km"] for s in block["steps"]]
    assert cones == sorted(cones)
    assert cones[-1] > cones[0]
    assert min(cones) >= 6.0


def test_cono_cella_giovane_piu_largo():
    young = _moving_track(n=4)        # durata 15 min -> growth x1.5
    mature = _moving_track(n=8)       # durata 35 min
    fc_y = forecast.build_forecast(_ctx(young), _bundle([young]))
    fc_m = forecast.build_forecast(_ctx(mature), _bundle([mature]))
    assert fc_y["steps"][-1]["cone_km"] > fc_m["steps"][-1]["cone_km"]


def test_direzione_forecast_entro_20gradi_dai_movimenti_reali():
    track = _curved_track()
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    fc_brg = tracking.bearing_deg(tuple(block["steps"][0]["position"]),
                                  tuple(block["steps"][-1]["position"]))
    seg_brgs = [tracking.bearing_deg(a.lonlat, b.lonlat)
                for a, b in zip(track.points, track.points[1:])]
    mean_brg = sum(seg_brgs) / len(seg_brgs)
    assert _ang_diff(fc_brg, mean_brg) <= 20.0


# ---------------------------------------------------------------------------
# Intensita': decay vs gate trend
# ---------------------------------------------------------------------------
def test_intensita_decade_verso_media_regionale():
    strong = _moving_track(n=8, dbz=55.0, mean_dbz=48.0, track_id=1)
    weak = _moving_track(n=8, dbz=25.0, mean_dbz=22.0, track_id=2, lon0=9.0)
    block = forecast.build_forecast(_ctx(strong), _bundle([strong, weak]))
    values = [s["max_dbz"] for s in block["steps"]]
    assert values == sorted(values, reverse=True)
    assert all(v < 55.0 for v in values)


def test_gate_trend_puo_non_far_decadere():
    ramp = [30.0 + 5.0 * i for i in range(8)]   # slope +5 -> cap +1.5/scan
    strong = _moving_track(n=8, dbz_series=ramp, mean_dbz=45.0, track_id=1)
    weak = _moving_track(n=8, dbz=25.0, mean_dbz=22.0, track_id=2, lon0=9.0)
    block = forecast.build_forecast(_ctx(strong), _bundle([strong, weak]))
    values = [s["max_dbz"] for s in block["steps"]]
    assert values[-1] > values[0]        # il gate trend vince sul decay
    assert values[-1] <= 75.0


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------
def test_confidence_high_solo_con_basis_e_residui_bassi():
    track = _moving_track(n=8)           # basis 8, linea perfetta
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block["basis_frames"] >= 6
    assert block["confidence"] == "high"
    # traiettoria ondulata: residui > 6 km, nessun outlier -> mai high
    wiggle = _moving_track(n=8, lat_wobble_deg=0.11, track_id=4)
    fit = forecast.fit_motion(wiggle.points)
    assert fit["n_outliers"] == 0
    assert fit["resid_std_km"] > 6.0
    block2 = forecast.build_forecast(_ctx(wiggle), _bundle([wiggle]))
    assert block2["confidence"] == "medium"


def test_confidence_low_cella_giovane_e_medium_sotto_6_frame():
    young = _moving_track(n=4)           # durata 15 min < 20 min
    block = forecast.build_forecast(_ctx(young), _bundle([young]))
    assert block["confidence"] == "low"
    mid = _moving_track(n=5, track_id=5)  # durata 20 min: non giovane, basis 5
    block2 = forecast.build_forecast(_ctx(mid), _bundle([mid]))
    assert block2["confidence"] == "medium"


def test_due_punti_vector_avg_e_confidence_low():
    track = _moving_track(n=2, v_kmh=60.0)
    fit = forecast.fit_motion(track.points)
    assert fit["fit_used"] is False
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block is not None
    assert block["basis_frames"] == 2
    assert block["confidence"] == "low"
    dist = tracking.haversine_km(track.points[-1].lonlat,
                                 tuple(block["steps"][1]["position"]))
    assert abs(dist - 60.0) <= 6.0        # +60 min a 60 km/h (vector-avg)


def test_outlier_isolato_incrementa_n_outliers_e_mai_high():
    track = _moving_track(n=8)
    spike = track.points[4]
    spike.lonlat = (spike.lonlat[0], spike.lonlat[1] + 2.0)  # ~222 km a nord
    fit = forecast.fit_motion(track.points)
    assert fit["n_outliers"] == 1
    assert fit["basis_frames"] == 7
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block["basis_frames"] == 7
    assert block["confidence"] != "high"


# ---------------------------------------------------------------------------
# Fallback con cap di velocita', potatura estremi, campi esposti
# ---------------------------------------------------------------------------
def test_fallback_ultimo_punto_assurdo_clamp_e_low():
    track = _moving_track(n=2, v_kmh=60.0, heading_deg=90.0)
    last = track.points[-1]
    last.lonlat = (last.lonlat[0] + 3.0, last.lonlat[1])   # ~250 km in un passo
    fit = forecast.fit_motion(track.points)
    assert fit["fit_used"] is False
    assert fit["clamped"] is True
    assert fit["velocity_kmh"] <= 110.0
    assert abs(fit["velocity_kmh"] - 110.0) <= 1e-9
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block["fit_used"] is False
    assert block["clamped"] is True
    assert block["velocity_kmh"] <= 110.0
    assert block["confidence"] == "low"
    origin = track.points[-1].lonlat
    dist = tracking.haversine_km(origin, tuple(block["steps"][-1]["position"]))
    assert dist <= 110.0 * (120.0 / 60.0) + 2.0


def test_spike_chiusura_potato_confidenza_non_medium_high():
    track = _moving_track(n=8, v_kmh=60.0, heading_deg=90.0)
    spike = track.points[-1]
    spike.lonlat = (spike.lonlat[0] + 2.0, spike.lonlat[1])   # spike di coda
    fit = forecast.fit_motion(track.points)
    assert fit["n_outliers"] >= 1
    assert fit["basis_frames"] <= 7
    assert fit["velocity_kmh"] <= 110.0
    assert _ang_diff(fit["direction_toward_deg"], 90.0) <= 5.0
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block["confidence"] not in ("medium", "high")


def test_fit_used_false_implica_confidence_low():
    track = _moving_track(n=2, v_kmh=60.0)
    fit = forecast.fit_motion(track.points)
    assert fit["fit_used"] is False
    assert forecast._confidence(fit, young=False) == "low"


def test_output_espone_direzione_velocita_e_flag():
    track = _moving_track(n=8, v_kmh=60.0, heading_deg=110.0)
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    assert block["fit_used"] is True
    assert block["clamped"] is False
    assert abs(block["velocity_kmh"] - 60.0) <= 1.0
    brg = tracking.bearing_deg(track.points[-1].lonlat,
                               tuple(block["steps"][0]["position"]))
    assert _ang_diff(brg, block["direction_toward_deg"]) <= 3.0


def test_tracce_corte_restano_valide_e_non_esplodono():
    for n in (2, 3):
        track = _moving_track(n=n, v_kmh=40.0, heading_deg=45.0, track_id=n)
        block = forecast.build_forecast(_ctx(track), _bundle([track]))
        assert block is not None
        assert block["confidence"] == "low"
        assert block["velocity_kmh"] <= 110.0
        origin = track.points[-1].lonlat
        dist = tracking.haversine_km(origin, tuple(block["steps"][-1]["position"]))
        assert dist <= 40.0 * 2.0 + 5.0


# ---------------------------------------------------------------------------
# Schema e regressione
# ---------------------------------------------------------------------------
def test_schema_steps_esattamente_4():
    track = _moving_track(n=8)
    bundle = _bundle([track])
    block = forecast.build_forecast(_ctx(track), bundle)
    assert set(block) == {"horizon_min", "step_min", "generated_at", "method",
                          "basis_frames", "confidence", "steps",
                          "direction_toward_deg", "velocity_kmh",
                          "fit_used", "clamped"}
    assert block["horizon_min"] == 120
    assert block["step_min"] == 30
    assert block["method"] == "linear_weighted_haversine_decay"
    assert block["generated_at"] == bundle.generated_at_iso
    assert [s["offset_min"] for s in block["steps"]] == [30, 60, 90, 120]
    for step in block["steps"]:
        assert set(step) == {"offset_min", "at", "position", "cone_km",
                             "max_dbz", "mean_dbz", "area_km2"}
        lon, lat = step["position"]
        assert isinstance(lon, float) and isinstance(lat, float)
        assert 4.0 <= lon <= 21.0 and 34.0 <= lat <= 48.0
        expected_at = _iso(bundle.radar_timestamp_ms + step["offset_min"] * 60000)
        assert step["at"] == expected_at


def test_regressione_dati_lineari_perfetti():
    v_kmh, heading = 45.0, 30.0
    track = _moving_track(n=8, v_kmh=v_kmh, heading_deg=heading)
    fit = forecast.fit_motion(track.points)
    assert fit["fit_used"] is True
    assert abs(fit["velocity_kmh"] - v_kmh) <= 1.0
    assert _ang_diff(fit["direction_toward_deg"], heading) <= 2.0
    block = forecast.build_forecast(_ctx(track), _bundle([track]))
    fc_brg = tracking.bearing_deg(tuple(block["steps"][0]["position"]),
                                  tuple(block["steps"][-1]["position"]))
    assert _ang_diff(fc_brg, heading) <= 2.0
    dist = tracking.haversine_km(track.points[-1].lonlat,
                                 tuple(block["steps"][0]["position"]))
    assert abs(dist - v_kmh * 0.5) <= v_kmh * 0.5 * 0.10


# ---------------------------------------------------------------------------
# Integrazione: hook in supercell.evaluate + supercells.json
# ---------------------------------------------------------------------------
def test_evaluate_e_supercells_json_contengono_forecast(tmp_path):
    bundle = models.EngineBundle("ok", "2026-04-30T10:00:00Z", output.SOURCE_LABEL)
    dlon = 5.0 / _km_per_deg_lon(41.0)          # 5 km per 5 min -> 60 km/h
    cells = [make_cell(
        12.0 + i * dlon, 41.0, T0 + i * STEP_S * 1000,
        area_km2=300.0 * (1.0 + 0.4 * i / 5), max_dbz=54.0, mean_dbz=48.0,
        frame_index=i, cell_id="it-{}".format(i)) for i in range(6)]
    alive = make_track(cells, 1)
    alive.motion = _wrong_motion()
    dead = make_track([make_cell(
        p.lonlat[0], p.lonlat[1], p.timestamp_ms, area_km2=p.area_km2,
        max_dbz=p.max_dbz, mean_dbz=p.mean_dbz, frame_index=p.frame_index,
        cell_id="dead-{}".format(i)) for i, p in enumerate(alive.points[:3])], 2)
    dead.motion = _wrong_motion()
    bundle.tracks = [alive, dead]
    bundle.radar_timestamp_ms = alive.points[-1].timestamp_ms
    bundle.radar_timestamp_iso = _iso(bundle.radar_timestamp_ms)
    bundle.cells_by_frame = [[c] for c in cells]
    sc_mod.evaluate(bundle, CONFIG["supercell"])
    assert len(bundle.supercells) == 2
    by_id = {c["track_id"]: c for c in bundle.supercells}
    assert by_id[1]["on_latest_frame"] is True
    assert isinstance(by_id[1]["forecast"], dict)
    assert [s["offset_min"] for s in by_id[1]["forecast"]["steps"]] == \
        [30, 60, 90, 120]
    assert by_id[2]["on_latest_frame"] is False
    assert by_id[2]["forecast"] is None
    paths = output.write_outputs(bundle, str(tmp_path / "radar"))
    with open(paths["supercells.json"], encoding="utf-8") as fh:
        payload = json.load(fh)
    forecasts = {c["track_id"]: c["forecast"] for c in payload["candidates"]}
    assert forecasts[1]["basis_frames"] >= 4
    assert forecasts[2] is None
    assert output.validate_outputs(str(tmp_path / "radar")) == []
