"""TEST 1.0.0.8 — FIX PIPELINE: fetch STATELESS rispetto a data/_raw (casi A/C)
e budget check PRIMA di ogni retry API (casi D/E).

  CASO A: nuovo runner, ECMWF invariato + Best Match cambiato -> decision engine
          sceglie coordinated e il fetch riscarica ENTRAMBI i leg SENZA alcun
          raw predecessore (data/_raw parte vuota: stateless, runner GitHub
          ephemeral). Nessun merge_best_match_only / best_match_only.
  CASO C: ECMWF nuovo -> fetch completo coordinato Best Match + ECMWF (stesso
          flusso di A: entrambi i leg, leg_timestamps identici).
  CASO D: errore retryable + budget residuo disponibile -> retry selettivo
          eseguito (fetch_source_batch richiamato di nuovo sul batch fallito).
  CASO E: errore retryable + budget esaurito -> NESSUN retry (gate budget prima
          del re-tentativo; nessuna prenotazione preventiva, controllo SOLO al
          momento del retry).

Nessuna rete: common.fetch_source_batch e pacings vengono re-indirizzati.
Tutti i percorsi di scrittura (data/_raw, api_usage.json, report efficienza)
vengono re-indirizzati su una directory temporanea: il repo non viene toccato.
"""

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import common  # noqa: E402
import fetch_source_data  # noqa: E402


def _sample_points():
    return [
        {"index": 0, "provinceIdx": 0, "sigla": "RM", "coordIdx": 0,
         "lat": 41.89, "lon": 12.48},
        {"index": 1, "provinceIdx": 1, "sigla": "MI", "coordIdx": 1,
         "lat": 45.46, "lon": 9.19},
    ]


def _ok_element(elevation=10.0):
    return {"elevation": elevation, "hourly": {}, "daily": {}}


def _day_record(used):
    return {"requests": used, "successful": 0, "failed": 0, "checks": 0,
            "canary_requests": 0, "forecast_requests": 0, "retries": 0,
            "batches": 0, "locations": 0, "bytes": 0}


def _usage_state(used, ceiling, hard_stop=True):
    return {
        "api_usage_guardrails": {
            "enabled": True,
            "daily_safety_ceiling": ceiling,
            "warn_threshold_fraction": 0.8,
            "hard_stop_enabled": hard_stop,
        },
        "days": {common.usage_day_key(): _day_record(used)},
        "last_update": None,
    }


def _run_fetch(points_path):
    with mock.patch.object(sys, "argv", ["fetch_source_data", "--points-json", points_path]):
        return fetch_source_data.main()


@contextmanager
def _isolated_env(used=0, ceiling=100, points=None):
    """Re-indirizza i percorsi di scrittura in una tmp dir e blocca la rete.
    Restituisce un oggetto con i percorsi temp (raw_path/usage_path e affini)."""
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    raw_dir = root / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    usage_path = root / "api_usage.json"
    eff_dir = root / "api_efficiency"
    points_path = root / "points.json"
    points_path.write_text(
        json.dumps(points if points is not None else _sample_points(),
                   ensure_ascii=False), encoding="utf-8")
    usage_path.write_text(json.dumps(_usage_state(used, ceiling), indent=2),
                          encoding="utf-8")

    class Env:
        pass

    env = Env()
    env.points = str(points_path)
    env.raw = raw_dir / "source_raw.json"
    env.usage = usage_path
    env.efficiency = eff_dir
    with mock.patch.object(common, "DATA_RAW", raw_dir), \
         mock.patch.object(common, "API_USAGE_JSON", usage_path), \
         mock.patch.object(common, "API_EFFICIENCY_DIR", eff_dir), \
         mock.patch.object(common, "is_bootstrap_pending", return_value=False), \
         mock.patch.object(common, "_pace_next_request", lambda: None), \
         mock.patch.object(common.time, "sleep", lambda s: None):
        yield env
    tmp.cleanup()


def test_caso_a_new_runner_stateless_full_fetch():
    """Runner nuovo: data/_raw VUOTA (nessun raw precedente persistito).
    Il fetch completo coordinated va comunque a buon fine e scrive un raw
    dual completo: la pipeline non dipende da dati di cicli precedenti."""
    with _isolated_env(used=0, ceiling=100) as env, \
         mock.patch.object(common, "fetch_source_batch",
                           side_effect=lambda lats, lons, models=None,
                           forecast_days=None: [_ok_element() for _ in range(len(lats))]) as fb:
        rc = _run_fetch(env.points)
        assert rc == 0
        assert fb.call_count == 2  # 1 batch x 2 leg (best_match + ecmwf_ifs)
        raw = json.loads(env.raw.read_text(encoding="utf-8"))
    assert raw["cycle_mode"] == "coordinated"
    assert raw["models"] == ["best_match", "ecmwf_ifs"]
    assert raw["leg_timestamps"]["best_match_fetched_at"] == raw["leg_timestamps"]["ecmwf_fetched_at"]
    pts = dict(raw["points"])
    assert set(pts.keys()) == {0, 1}
    assert "best_match" in pts[0] and "ecmwf_ifs" in pts[0]
    assert "best_match" in pts[1] and "ecmwf_ifs" in pts[1]


def test_caso_c_ecmwf_new_full_fetch():
    # CASO C a livello decision engine: ECMWF nuovo -> coordinated (fetch
    # completo), identico flusso di fetch del caso A (verificato sopra).
    import decide_cycle
    assert decide_cycle.cycle_mode(True, False, False) == "coordinated"
    assert decide_cycle.cycle_mode(True, True, False) == "coordinated"
    assert decide_cycle.fetch_request_count(
        "coordinated", {"batches": [1], "n_model_legs": 2}) == 2


def test_caso_a_no_best_match_only_api():
    # l'API del refresh parziale e il merge del raw precedente NON esistono piu'
    assert not hasattr(fetch_source_data, "merge_best_match_only")
    # il mode best_match_only e' rimosso dai choices -> argparse SystemExit
    try:
        with mock.patch.object(sys, "argv",
                               ["fetch_source_data", "--mode", "best_match_only",
                                "--dry-run", "--points-json", _sample_points_path()]):
            fetch_source_data.main()
        raise AssertionError("attesa SystemExit per --mode best_match_only (rimosso)")
    except SystemExit:
        pass


def _sample_points_path():
    tmp = tempfile.mkdtemp(prefix="mri_pts_")
    p = Path(tmp) / "points.json"
    p.write_text(json.dumps(_sample_points(), ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_caso_d_retry_runs_when_budget_available():
    """Errore retryable al primo giro + budget residuo disponibile -> il retry
    selettivo PARTE (fetch_source_batch richiamato di nuovo sui batch falliti)."""
    calls = {"n": 0}

    def flaky(lats, lons, models=None, forecast_days=None):
        calls["n"] += 1
        if calls["n"] == 1:  # primo giro: errore retryable (primo leg del batch)
            raise RuntimeError("Open-Meteo API error: Minutely API request limit")
        return [_ok_element() for _ in range(len(lats))]

    with _isolated_env(used=90, ceiling=100) as env, \
         mock.patch.object(common, "fetch_source_batch", side_effect=flaky) as fb:
        rc = _run_fetch(env.points)
        assert rc == 0
        assert fb.call_count == 3  # 1 primo giro + 2 retry selettivo (entrambi i leg)
        raw = json.loads(env.raw.read_text(encoding="utf-8"))
    assert len(raw["points"]) == 2
    assert raw["cycle_mode"] == "coordinated"


def test_caso_e_no_retry_without_budget():
    """Errore retryable ma budget GIORNALIERO esaurito -> NESSUN retry: il gate
    budget (prima del re-tentativo) ferma i retry; capoluoghi mancanti -> rc 3
    (safe skip, no partial publish). Nessuna prenotazione preventiva."""
    def always_flaky(lats, lons, models=None, forecast_days=None):
        raise RuntimeError("Open-Meteo API error: Minutely API request limit")

    with _isolated_env(used=98, ceiling=100) as env, \
         mock.patch.object(common, "fetch_source_batch", side_effect=always_flaky) as fb:
        rc = _run_fetch(env.points)
        assert rc == 3  # capoluoghi mancanti in steady-state: no-publish, safe skip
        assert fb.call_count == 1  # SOLO primo giro: nessun retry (budget esaurito)
        assert not env.raw.exists()  # nessun raw parziale scritto
        usage = json.loads(env.usage.read_text(encoding="utf-8"))
    assert usage["days"][common.usage_day_key()]["requests"] == 100  # 98 + 2 primo giro


def test_budget_ok_for_retry_gate():
    with tempfile.TemporaryDirectory() as tmp:
        p1 = Path(tmp) / "g1.json"
        p1.write_text(json.dumps(_usage_state(90, 100), indent=2), encoding="utf-8")
        with mock.patch.object(common, "API_USAGE_JSON", p1):
            assert common.budget_ok_for(2) is True    # CASO D: retry consentito
            assert common.budget_ok_for(11) is False  # oltre il tetto: MAI

        p2 = Path(tmp) / "g2.json"
        p2.write_text(json.dumps(_usage_state(100, 100), indent=2), encoding="utf-8")
        with mock.patch.object(common, "API_USAGE_JSON", p2):
            assert common.budget_ok_for(1) is False   # CASO E: budget esaurito

        p2b = Path(tmp) / "g2b.json"
        p2b.write_text(json.dumps(_usage_state(99, 100), indent=2), encoding="utf-8")
        with mock.patch.object(common, "API_USAGE_JSON", p2b):
            assert common.budget_ok_for(1) is True    # esattamente al limite: ok

        # guardrails disabilitati -> nessun blocco (sola osservabilita')
        p3 = Path(tmp) / "g3.json"
        p3.write_text(json.dumps(_usage_state(200, 100, hard_stop=False), indent=2),
                      encoding="utf-8")
        with mock.patch.object(common, "API_USAGE_JSON", p3):
            assert common.budget_ok_for(1) is True


def test_fetch_source_batch_internal_retry_budget_gate():
    """Il client applica il CONTROLLO BUDGET PRIMA di ogni re-tentativo API
    (caso D/E a livello di singola batch): con budget disponibile il retry
    prosegue; esaurito il giorno, il retry si ferma PRIMA della chiamata."""
    import urllib.error
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "u1.json"
        p.write_text(json.dumps(_usage_state(50, 100), indent=2), encoding="utf-8")
        calls = {"n": 0}

        def flaky(url, timeout_s=None):
            calls["n"] += 1
            if calls["n"] < common.RETRY_LIMIT:
                raise urllib.error.URLError("_ssl: handshake timeout")
            return {"latitude": [41.0]}

        with mock.patch.object(common, "API_USAGE_JSON", p), \
             mock.patch.object(common, "_http_json", side_effect=flaky), \
             mock.patch.object(common.time, "sleep", lambda s: None), \
             mock.patch.object(common, "_pace_next_request", lambda: None):
            data = common.fetch_source_batch([41.0], [12.0])
        assert data["latitude"] == [41.0]
        assert calls["n"] == common.RETRY_LIMIT  # budget OK: retry fino al successo

        p2 = Path(tmp) / "u2.json"
        p2.write_text(json.dumps(_usage_state(100, 100), indent=2), encoding="utf-8")
        calls2 = {"n": 0}

        def always(url, timeout_s=None):
            calls2["n"] += 1
            raise urllib.error.URLError("_ssl: handshake timeout")

        with mock.patch.object(common, "API_USAGE_JSON", p2), \
             mock.patch.object(common, "_http_json", side_effect=always), \
             mock.patch.object(common.time, "sleep", lambda s: None), \
             mock.patch.object(common, "_pace_next_request", lambda: None):
            try:
                common.fetch_source_batch([41.0], [12.0])
                raise AssertionError("atteso arresto per budget esaurito")
            except RuntimeError as exc:
                assert "budget" in str(exc)
        assert calls2["n"] == 1  # giorno esaurito: NESSUN retry dopo il primo errore


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (fetch stateless + budget-before-retry, casi A/C/D/E)")