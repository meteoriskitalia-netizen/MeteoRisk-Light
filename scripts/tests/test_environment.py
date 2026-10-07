# -*- coding: utf-8 -*-
"""Test A2 - environment.py (A1): formule pure + evaluate_environment.

Tutti i valori attesi sono quelli calcolati dallo scratch A2 eseguito
(_scratch_a2.py, radarvenv Python 3.13) su questo stesso codice A1.
Nessuna rete: il client Open-Meteo e' iniettato.
"""
import conftest_staging  # noqa: F401  bootstrap: 01_backend su sys.path

import pytest

import environment as env

N = 24
TIMES = [f"2026-10-07T{h:02d}:00" for h in range(N)]


def _seq(v):
    return [v] * N


def _payload():
    return {
        "hourly": {
            "time": list(TIMES),
            "temperature_2m": _seq(30.0),
            "dew_point_2m": _seq(20.0),
            "pressure_msl": _seq(1000.0),
            "cape": _seq(3000.0),
            "convective_inhibition": _seq(-100.0),
            "lifted_index": _seq(-6.0),
            "wind_speed_10m": _seq(10.0),
            "wind_direction_10m": _seq(270.0),
            "wind_speed_1000hPa": _seq(10.0),
            "wind_direction_1000hPa": _seq(270.0),
            "wind_speed_925hPa": _seq(12.0),
            "wind_direction_925hPa": _seq(270.0),
            "wind_speed_850hPa": _seq(15.0),
            "wind_direction_850hPa": _seq(270.0),
            "wind_speed_700hPa": _seq(20.0),
            "wind_direction_700hPa": _seq(270.0),
            "wind_speed_500hPa": _seq(25.0),
            "wind_direction_500hPa": _seq(270.0),
            "temperature_700hPa": _seq(10.0),
            "temperature_500hPa": _seq(-20.0),
            "geopotential_height_700hPa": _seq(3120.0),
            "geopotential_height_500hPa": _seq(5840.0),
            "freezing_level_height": _seq(3000.0),
        }
    }


def test_scp_stp_ship_pure_formulas():
    assert env.scp_env(3000, 200, 30, -100) == 7.2
    assert env.stp_env(3000, 800, 300, 20, -50) == 4.8
    assert env.ship_env(3000, 12, 7, -30, 20, 3000) == 3.6
    assert env.ship_env(None, 12, 7, -30, 20, 3000) is None


def test_env_score_scale():
    assert env.env_score(3, 1) == 76.2
    assert env.env_score(1, 1) == 32.2
    assert env.env_score(0.675, 1) == 22.1
    assert env.env_score(None, 1) == 0.0


def test_geo_to_metres_conversion():
    assert env.geo_to_metres(3120) == 3117.7231861167493
    assert env.geo_to_metres(5840) == 5838.2289941738
    assert env.geo_to_metres(None) is None


def test_mixing_ratio_bolton():
    assert env.mixing_ratio_gkg(20, 1000) == 14.864854763400624


def test_wind_to_uv_conventions():
    u, v = env.wind_to_uv(10, 270)          # vento DA ovest -> u positivo
    assert u == 10.0
    assert v == pytest.approx(0.0, abs=1e-12)


def test_build_openmeteo_url_deterministic():
    url = env.build_openmeteo_url(41.9, 12.5)
    assert url.startswith(env.OPENMETEO_URL)
    assert "latitude=41.90000" in url
    assert "longitude=12.50000" in url
    assert "timezone=GMT" in url
    assert "temperature_unit=celsius" in url


def test_evaluate_environment_full_payload():
    calls = []

    def client(url):
        calls.append(url)
        return _payload()

    res = env.evaluate_environment(41.9, 12.5, openmeteo_client=client)
    assert len(calls) == 1
    assert calls[0].startswith(env.OPENMETEO_URL)

    assert res["scp"] == 0.675
    assert res["stp"] == 0.075
    assert res["ship"] == 3.214
    assert res["srh_0_3"] == 37.5
    assert res["srh_1km"] == 15.0
    assert res["ebwd_ms"] == 15.0
    assert res["sb_lcl_m"] == 1250.0
    assert res["q_gkg"] == 14.865
    assert res["lr_700_500"] == 11.027
    assert res["env_score"] == 22.1
    assert res["completeness"] == 1.0
    assert res["partial"] is False
    assert res["missing"] == []
    assert res["time_iso"] in TIMES
    assert res["flags"]["storm_motion"] == "bunkers_rm_mean_arith"
    assert res["flags"]["q_source"] == "dewpoint_pressure"
    assert res["flags"]["cin_term_neutral"] is False


def test_evaluate_environment_missing_cape_is_partial():
    payload = _payload()
    payload["hourly"]["cape"] = _seq(None)
    res = env.evaluate_environment(41.9, 12.5,
                                   openmeteo_client=lambda url: payload)
    assert res["scp"] is None
    assert res["env_score"] == 0.0
    assert res["completeness"] == 0.9167
    assert res["missing"] == ["cape"]
    assert res["partial"] is True
    assert res["flags"]["cin_term_neutral"] is False


def test_evaluate_environment_client_failure_raises():
    def bad_client(url):
        raise RuntimeError("boom")

    with pytest.raises(env.EnvironmentFetchError,
                       match="client_failed:RuntimeError"):
        env.evaluate_environment(41.9, 12.5, openmeteo_client=bad_client)


# ---------------------------------------------------------------------------
# B2 (PHASE2_VERSION 0.4.0): cache per bucket dei candidati
# ---------------------------------------------------------------------------

def test_cache_bucket_rounding_by_grid_deg():
    # grid 0.1 -> 1 decimale: due candidati nello stesso bucket = 1 chiamata
    assert env.cache_bucket(41.94, 12.51) == (41.9, 12.5)
    assert env.cache_bucket(41.91, 12.54) == (41.9, 12.5)
    assert env.cache_bucket(41.94, 12.51) == env.cache_bucket(41.91, 12.54)
    assert env.cache_bucket(41.5, 13.5) == (41.5, 13.5)
    assert env.cache_bucket(41.94, 12.51, grid_deg=1.0) == (42.0, 13.0)
    with pytest.raises(ValueError, match="cache_bucket_non_numeric"):
        env.cache_bucket("not-a-lat", 12.5)


def test_evaluate_environment_cached_dedup_and_failure_not_cached():
    calls = []

    def evaluator(lat, lon, timeout_s=None):
        calls.append((lat, lon, timeout_s))
        if lon > 13.0:
            raise RuntimeError("boom")
        return {"env_score": 42.0, "scp": 1.0, "partial": False}

    cache = {}
    first = env.evaluate_environment_cached(41.94, 12.51, cache=cache,
                                            timeout_s=7, evaluator=evaluator)
    second = env.evaluate_environment_cached(41.91, 12.54, cache=cache,
                                             timeout_s=7, evaluator=evaluator)
    # stesso bucket -> UNA sola richiesta, stesso dict restituito
    assert first == {"env_score": 42.0, "scp": 1.0, "partial": False}
    assert second is first
    assert len(calls) == 1
    assert calls[0][2] == 7
    assert len(cache) == 1

    # bucket diverso con errore -> None (nessun dato inventato)
    assert env.evaluate_environment_cached(41.5, 13.5, cache=cache,
                                           evaluator=evaluator) is None
    assert len(calls) == 2
    assert len(cache) == 1              # il fallimento NON viene memorizzato
    # stesso bucket fallito -> riprova (quindi e' proprio non cachato)
    assert env.evaluate_environment_cached(41.5, 13.5, cache=cache,
                                           evaluator=evaluator) is None
    assert len(calls) == 3

    # risposta non-dict -> None, non cachata
    assert env.evaluate_environment_cached(41.0, 14.0, cache=cache,
                                           evaluator=lambda *a, **k: []) is None
    assert len(cache) == 1
    # nessun cache dict -> funziona comunque (cache opzionale)
    assert env.evaluate_environment_cached(41.9, 12.5, cache=None,
                                           evaluator=evaluator)["env_score"] == 42.0
