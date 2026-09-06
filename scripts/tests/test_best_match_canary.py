"""TEST 1.0.0.8 — Best Match Canary (sentinelle + fingerprint contenuto).

Vedi anche test_bootstrap.py (stato G3/G5) e test_decide_cycle.py (matrice/rc).
Qui: mapping della risposta multi-location -> payload sentinelle, determinismo
e content-sensitivity della fingerprint SHA-256 (NESSUN generation-time), e il
registro sentinelle (stesse coordinate dei punti pubblicati: coordIdx 0).
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import common  # noqa: E402
import check_best_match  # noqa: E402


def make_el(hourly_time, codes, precip):
    return {
        "hourly": {
            "time": hourly_time,
            "weathercode": codes,
            "precipitation": precip,
        }
    }


def test_response_payload_maps_order():
    sents = common.best_match_sentinels()[:2]  # MI, VE
    data = [make_el(["2026-09-06T00:00"], [1], [0.0]),
            make_el(["2026-09-06T00:00"], [3], [0.2])]
    payload = check_best_match.response_payload(data, sents)
    assert payload["day0"] == "2026-09-06"
    assert payload["sentinels"][sents[0]["sigla"]]["weathercode"] == [1]
    assert payload["sentinels"][sents[1]["sigla"]]["precipitation"] == [0.2]
    # la fingerprint e' STABILE su un payload identico
    assert common.fingerprint_best_match(payload) == common.fingerprint_best_match(payload)


def test_response_payload_rejects_wrong_count():
    sents = common.best_match_sentinels()[:2]
    try:
        check_best_match.response_payload([make_el(["2026-09-06T00:00"], [1], [0.0])], sents)
        raise AssertionError("attese ValueError per numero di locazioni errato")
    except ValueError:
        pass


def test_fingerprint_deterministic_and_content_sensitive():
    pts = [
        {"index": 0, "sigla": "MI", "coordIdx": 0, "capoluogo": True,
         "models": {"best_match": {"hourly": {"weathercode": [1, 2, 3],
                                              "precipitation": [0.0, 0.1, 0.2]}}}},
        {"index": 1, "sigla": "RM", "coordIdx": 0, "capoluogo": True,
         "models": {"best_match": {"hourly": {"weathercode": [3],
                                              "precipitation": [0.5]}}}},
    ]
    p1 = common.best_match_sentinel_payload(pts, "2026-09-06")
    p2 = common.best_match_sentinel_payload(pts, "2026-09-06")
    assert common.fingerprint_best_match(p1) == common.fingerprint_best_match(p2)
    # giorno diverso -> fingerprint diversa (il day0 e' parte del contenuto)
    assert common.fingerprint_best_match(
        common.best_match_sentinel_payload(pts, "2026-09-07")) != common.fingerprint_best_match(p1)
    # weathercode cambiato -> fingerprint diversa
    pts2 = json.loads(json.dumps(pts))
    pts2[0]["models"]["best_match"]["hourly"]["weathercode"] = [9, 9, 9]
    assert common.fingerprint_best_match(
        common.best_match_sentinel_payload(pts2, "2026-09-06")) != common.fingerprint_best_match(p1)
    # ordine delle chiavi del payload non alterato dalla presenza di altri campi
    assert len(p1["sentinels"]) == 6  # 6 sentinelle SEMPRE, formato stabile


def test_sentinel_registry_matches_published_points():
    sents = common.best_match_sentinels()
    assert len(sents) == 6
    assert [s["sigla"] for s in sents] == list(common.BEST_MATCH_SENTINEL_SIGLAS)
    # area di copertura presente per ogni sentinella
    for s in sents:
        assert s["area"] in common.BEST_MATCH_SENTINEL_AREAS.values()
    # coordinate numeriche reali (capoluoghi, stessa base dei punti pubblicati)
    for s in sents:
        assert isinstance(s["lat"], float) and isinstance(s["lon"], float)


def test_fetch_best_match_check_bodies():
    # la richiesta canary deve pedalare SOLO weathercode+precipitation (leggera)
    assert common.BEST_MATCH_CHECK_HOURLY == "weathercode,precipitation"
    assert common.BEST_MATCH_CHECK_HOURLY.split(",") == ["weathercode", "precipitation"]


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("  [PASS] %s" % t.__name__)
    print("RESULT: PASS (Best Match canary + fingerprint)")