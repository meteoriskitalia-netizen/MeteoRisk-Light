#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Phase 7 (build_meteorisk_dataset.py): METEO-RISK DATA ENGINE.
Trasforma i dati RAW della fonte (Open-Meteo) nel DATASET DERIVATO MeteoRisk:
non copie dell'API ma aggregazioni e indicatori calcolati dalla pipeline.

Contenuto derivato (per punto reale):
  - collasso giornaliero "day-wide" (port di getHourOrDailyData(...,'all'))
  - riepilogo derivato giornaliero (temp max/min, precip, prob, vento, raffica,
    giorni/ore di temporale-grandine-neve, intensità max pioggia, CAPE, etc.)
  - le serie orarie/giornaliere sono i VALORI INTERMEDI STRETTAMENTE NECESSARI
    al motore di rischio MeteoRisk client-side (densificazione IDW, vista oraria,
    indici severi, merge dual): array numerici spogliati di ogni envelope API
    (unità, unit, time, api_response_mask, timezone).

Per provincia:
  - worst-point convettivo (port di scorePointForProvince) con stesso tie-break
    dell'app (primo massimo in ordine di slot, iterazione in indice crescente)
  - riepilogo derivato per giorno del punto peggiore.

Output (in data/_staging, poi validate_dataset.py e publish_dataset.py):
  data/latest/metadata.json, meteorisk-points.json, meteorisk-provinces.json
"""

import argparse
import datetime as _dt
import json
import os
import sys

sys.path.insert(0, __file__ and __file__[: __file__.rfind("\\")] or ".")
import common

ATTR = ("Meteorological source data: Open-Meteo.com / Data license: CC BY 4.0 / "
        "Processed and aggregated by MeteoRisk Light.")


def dedupe_models(record):
    """record: {best_match: {daily,hourly}, ecmwf_ifs: {...}, elevation}."""
    out = {}
    for m in ("best_match", "ecmwf_ifs"):
        sub = record.get(m)
        if sub and sub.get("daily") and sub.get("hourly"):
            out[m] = {"daily": sub["daily"], "hourly": sub["hourly"]}
    return out


def day_summary(hourly, daily, day_index, day_label):
    """Derived daily summary from the day-wide collapse + daily arrays."""
    c = common.collapse_day(hourly, day_index)
    daily_arr = {k: (daily.get(k) or []) for k in common.DAILY_FIELDS}
    codes = hourly.get("weathercode") or []
    prec = hourly.get("precipitation") or []
    s = day_index * 24
    thunder = hail = snow = rain_int = 0
    for i in range(24):
        wc = codes[s + i] if (s + i) < len(codes) else None
        if wc is None:
            continue
        wc = int(wc)
        if wc >= 95:
            thunder += 1
        if wc in (96, 99):
            hail += 1
        if 71 <= wc <= 77:
            snow += 1
        pv = prec[s + i] if (s + i) < len(prec) else 0
        if pv is not None and pv > rain_int:
            rain_int = pv
    return {
        "day": day_label,
        "temp_max": common.r2(_at(daily_arr["temperature_2m_max"], day_index)),
        "temp_min": common.r2(_at(daily_arr["temperature_2m_min"], day_index)),
        "precip_sum": common.r2(_at(daily_arr["precipitation_sum"], day_index)),
        "prob_max": common.r2(_at(daily_arr["precipitation_probability_max"], day_index)),
        "wind_max": common.r2(_at(daily_arr["windspeed_10m_max"], day_index)),
        "weathercode": common.r2(c["code"]),
        "humidity": common.r2(c["humidity"]),
        "pressure": common.r2(c["pressure"]),
        "precip_hourly": common.r2(c["precip"]),
        "showers": common.r2(c["showers"]),
        "gusts": common.r2(c["gusts"]),
        "wind_avg": common.r2(c["wind"]),
        "wind100_max": common.r2(c["wind100"]),
        "cape_max": common.r2(c["cape"]),
        "freezing_max": common.r2(c["freezingLevel"]),
        "rain_intensity_max": common.r2(rain_int),
        "thunder_hours": thunder,
        "hail_hours": hail,
        "snow_hours": snow,
    }


def _at(arr, i):
    return arr[i] if i < len(arr) else None


def main():
    parser = argparse.ArgumentParser(description="Build derived MeteoRisk dataset.")
    parser.add_argument("--points-json", default=str(common.REPO_ROOT / "data" / "_workdir" / "real_points.json"))
    parser.add_argument("--raw-json", default=str(common.DATA_RAW / "source_raw.json"))
    parser.add_argument("--force", action="store_true", help="Build anche senza raw (placeholder/empty).")
    parser.add_argument("--empty", action="store_true", help="Genera dataset placeholder (status=empty).")
    args = parser.parse_args()

    if not args.empty and not os.path.exists(args.raw_json):
        print("[build] RAW non trovato (--raw-json). Usare --empty per il placeholder o eseguire prima fetch_source_data.py.")
        return 2

    regions = common._load_regions()
    state = common.load_run_state()
    run_info = {
        "run_key": state.get("run_key"),
        "driver_model": state.get("driver_model") or common.DRIVER_MODEL,
        "run_init_ts": state.get("run_init_ts"),
        "run_available_ts": state.get("run_available_ts"),
        "fetched_at": state.get("checked_at"),
    }
    fetch_timestamps = None
    points_meta = json.load(open(args.points_json, encoding="utf-8")) if os.path.exists(args.points_json) else []
    points_meta_map = {p["index"]: p for p in points_meta}

    # giorno di partenza del forecast (giorno 0) per le etichette
    init_ts = int(state.get("run_init_ts") or 0)
    day0 = _dt.datetime.fromtimestamp(init_ts, _dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=1)))  # Europe/Rome e' UTC+1 in inverno (approssimazione sicura: si
    # usa il day label SOLO informativo)
    if init_ts:
        from datetime import timezone
        tz_rome = timezone(_dt.timedelta(hours=1))
        day0 = _dt.datetime.fromtimestamp(init_ts, tz_rome).date()
    else:
        day0 = _dt.datetime.now(_dt.timezone.utc).date() + _dt.timedelta(days=0)

    def day_label(i):
        return (day0 + _dt.timedelta(days=i)).isoformat()

    if args.empty:
        print("[build] EMTPY placeholder dataset (status=empty).")
        common.mkdirs(common.DATA_STAGING)
        metadata = {
            "schema_version": 4,
            "dataset_type": common.APP_DATA_TYPE,
            "status": "empty",
            "application": common.APP_NAME,
            "source_data": [{"name": common.SOURCE_NAME, "role": common.SOURCE_ROLE,
                             "license": common.SOURCE_LICENSE}],
            "attribution": ATTR,
            "generated_at": common.now_iso(),
            "models_covered": ["best_match", "ecmwf_ifs"],
            "forecast_days": common.FORECAST_DAYS,
            "timezone": common.TIMEZONE,
            "point_count": 0,
            "province_count": 0,
            "run_info": run_info,
            "files": {"points": "meteorisk-points.json", "provinces": "meteorisk-provinces.json",
                      "validation": "validation.json"},
            "note": "Placeholder: il dataset reale viene generato dalla GitHub Action "
                    "(o da un run locale riuscito) con i dati della fonte Open-Meteo.",
        }
        if fetch_timestamps is not None:
            metadata["fetch_timestamps"] = fetch_timestamps
        with open(common.DATA_STAGING / "metadata.json", "w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, ensure_ascii=False)
        with open(common.DATA_STAGING / "meteorisk-points.json", "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 4, "status": "empty", "generated_at": metadata["generated_at"],
                       "points": []}, fh, ensure_ascii=False)
        with open(common.DATA_STAGING / "meteorisk-provinces.json", "w", encoding="utf-8") as fh:
            json.dump({"schema_version": 4, "status": "empty", "generated_at": metadata["generated_at"],
                       "province_count": 0, "provinces": []}, fh, ensure_ascii=False)
        print("[build] Staging placeholder pronto in data/_staging.")
        return 0

    raw = json.load(open(args.raw_json, encoding="utf-8"))
    raw_map = {idx: payload for idx, payload in raw.get("points", [])}
    fetch_ts = raw.get("fetched_at")
    run_info["fetched_at"] = fetch_ts
    leg_ts = raw.get("leg_timestamps") or {}
    bm_ts = leg_ts.get("best_match_fetched_at") or fetch_ts
    ecm_ts = leg_ts.get("ecmwf_fetched_at") or fetch_ts
    cycle_mode = raw.get("cycle_mode") or "coordinated"
    if cycle_mode == "best_match_only":
        coord_note = ("best_match aggiornata da sola (canary sentinelle 1.0.0.8); ecmwf_ifs invariata "
                      "dal ciclo precedente (leg_timestamps espliciti).")
    else:
        coord_note = ("best_match e ecmwf_ifs scaricati nello stesso ciclo di fetch comandato "
                      "dal run ECMWF IFS (coerenza temporale del dataset).")
    fetch_timestamps = {
        "dataset_generation_timestamp": fetch_ts,
        "ecmwf_fetch_timestamp": ecm_ts,
        "best_match_fetch_timestamp": bm_ts,
        "coordinated_cycle": coord_note,
    }

    provinces = []
    for i, r in enumerate(regions):
        provinces.append({"idx": r["idx"], "sigla": r["sigla"], "prov": r["prov"],
                          "region": r["region"], "selected_point": None, "days": []})

    points_out = []
    pidx_map = {p["sigla"]: p["idx"] for p in regions}
    for meta in sorted(points_meta_map.values(), key=lambda p: p["index"]):
        idx = meta["index"]
        payload = raw_map.get(idx)
        if not payload:
            continue
        models = dedupe_models(payload)
        if not models:
            continue
        record = models.get("best_match")
        hourly = record["hourly"]
        daily = record["daily"]
        elev = payload.get("elevation")
        summary = [day_summary(hourly, daily, d, day_label(d)) for d in range(common.FORECAST_DAYS)]
        entry = {
            "id": idx,
            "provinceIdx": meta["provinceIdx"],
            "sigla": meta["sigla"],
            "coordIdx": meta["coordIdx"],
            "lat": common.r2(meta["lat"], 6),
            "lon": common.r2(meta["lon"], 6),
            "elevation": common.r2(elev, 1) if elev is not None else None,
            "models": models,
            "summary": summary,
        }
        points_out.append(entry)

    # Worst-point per provincia (port fedele di scorePointForProvince, tie-break primo max in ordine slot)
    points_by_prov = {}
    for e in points_out:
        points_by_prov.setdefault(e["provinceIdx"], []).append(e)

    for prov in provinces:
        cands = points_by_prov.get(prov["idx"], [])
        if not cands:
            continue
        best = None
        best_score = -1.0
        for e in cands:  # gia' ordinati per id crescente
            rec = {"hourly": e["models"]["best_match"]["hourly"],
                   "daily": e["models"]["best_match"]["daily"]}
            sc = common.score_point_for_province(rec)
            if sc > best_score:
                best_score = sc
                best = e
        if best is not None:
            prov["selected_point"] = {
                "id": best["id"], "score": common.r2(best_score, 1),
                "coordIdx": best["coordIdx"], "lat": best["lat"], "lon": best["lon"],
            }
            prov["days"] = best["summary"]

    points_write = {"schema_version": 4, "status": "live",

                    "generated_at": raw["fetched_at"],
                    "forecast_days": common.FORECAST_DAYS,
                    "timezone": common.TIMEZONE,
                    "models": ["best_match", "ecmwf_ifs"],
                    "day0": day_label(0),
                    "points": points_out}
    provinces_write = {"schema_version": 4, "status": "live",
                       "generated_at": raw["fetched_at"],
                       "province_count": len(provinces),
                       "provinces": provinces}

    common.mkdirs(common.DATA_STAGING)
    common.DATA_STAGING.joinpath("meteorisk-points.json").write_text(
        json.dumps(points_write, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
    common.DATA_STAGING.joinpath("meteorisk-provinces.json").write_text(
        json.dumps(provinces_write, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")

    metadata = {
        "schema_version": 4,
        "dataset_type": common.APP_DATA_TYPE,
        "status": "live",
        "application": common.APP_NAME,
        "source_data": [{"name": common.SOURCE_NAME, "role": common.SOURCE_ROLE,
                         "license": common.SOURCE_LICENSE}],
        "attribution": ATTR,
        "generated_at": raw["fetched_at"],
        "models_covered": ["best_match", "ecmwf_ifs"],
        "forecast_days": common.FORECAST_DAYS,
        "timezone": common.TIMEZONE,
        "point_count": len(points_out),
        "province_count": len(provinces),
        "day0": day_label(0),
        "run_info": run_info,
        "fetch_timestamps": fetch_timestamps,
        "update_strategy": cycle_mode,
        "files": {"points": "meteorisk-points.json", "provinces": "meteorisk-provinces.json",
                  "validation": "validation.json"},
    }
    with open(common.DATA_STAGING / "metadata.json", "w", encoding="utf-8") as fh:
        json.dump(metadata, fh, indent=2, ensure_ascii=False)

    print("[build] Staging: %d punti · %d province (%.0f KB points, %.0f KB provinces)"
          % (len(points_out), len(provinces),
             (common.DATA_STAGING / "meteorisk-points.json").stat().st_size / 1024.0,
             (common.DATA_STAGING / "meteorisk-provinces.json").stat().st_size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())