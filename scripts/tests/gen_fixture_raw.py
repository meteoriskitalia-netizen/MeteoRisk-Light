#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TEST-ONLY fixture: genera un dataset raw SINTETICO deterministico nella forma
che produci il fetch_source_data.py, SOLO per testare la pipeline end-to-end
(build -> validate -> publish) senza consumare quota Open-Meteo (nessuna rete).

NON è dato reale: è esplicitamente escluso da data/latest e dal repository di
pubblicazione. I valori sono plausibili ma inventati (gradiente orografico,
una cella convettiva su Lazio/Campania, un flusso da SW).

Output: data/_workdir/fixture_raw.json (nella forma source_raw.json).
"""

import datetime as _dt
import json
import math
import random
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, "..")
import common

HOURLY = common.HOURLY_FIELDS
DAILY = common.DAILY_FIELDS
SEED = 20260101


def synth_point(lat, lon, elev):
    rnd = random.Random(SEED + int(lat * 1000) + int(lon * 1000) + int((elev or 0)))
    base_t = 30.0 - (elev or 0) / 150.0 - (abs(lat - 42.0)) * 1.2
    # cella convettiva concentrica su 41.4N, 12.7E
    d = math.hypot(lat - 41.4, (lon - 12.7) * math.cos(math.radians(41.4)))
    conv = max(0.0, 1.0 - d / 2.0)

    def cell():
        min_dl, max_dl = [], []
        dl = {}
        # daily
        mins = []
        maxs = []
        for d in range(3):
            tmin = base_t - 6 + rnd.uniform(-2, 2)
            tmax = base_t + 4 + rnd.uniform(-2, 2) + conv * 1.5
            mins.append(round(tmin, 2))
            maxs.append(round(tmax, 2))
        return mins, maxs

    mins, maxs = cell()
    hourly = {}
    for f in HOURLY:
        if f == "temperature_2m":
            arr = []
            for h in range(72):
                day = h // 24
                hour = h % 24
                day_amp = (maxs[day] - mins[day]) / 2.0
                arr.append(round(mins[day] + day_amp + day_amp * math.sin((hour - 9) * math.pi / 12) + conv * 1.2, 2))
            hourly[f] = arr
        elif f == "relativehumidity_2m":
            hourly[f] = [round(_clip(58 - 6 * math.sin(h % 24 / 24 * 2 * math.pi) + conv * 25 + rnd.uniform(-5, 5), 10, 100), 1) for h in range(72)]
        elif f == "dew_point_2m":
            hourly[f] = [round(hourly["temperature_2m"][h] - (100 - hourly["relativehumidity_2m"][h]) / 5.0, 2) for h in range(72)]
        elif f == "pressure_msl":
            hourly[f] = [round(1013 - (elev or 0) * 0.12 + conv * 4 + rnd.uniform(-2, 2), 1) for h in range(72)]
        elif f == "cape":
            hourly[f] = [round(max(0, (1500 * conv ** 2 * (1 if (h % 24) > 8 else 0.3)) * (1 + rnd.uniform(0, 0.4))), 1) for h in range(72)]
        elif f == "convective_inhibition":
            hourly[f] = [round(-max(0, 60 * conv * (0.2 if (h % 24) > 12 else 0.8)) * (1 + rnd.uniform(0, 0.3)), 1) for h in range(72)]
        elif f == "lifted_index":
            hourly[f] = [round(6 - 9 * conv * (1 if (h % 24) > 8 else 0.3) + rnd.uniform(-1.5, 1.5), 2) for h in range(72)]
        elif f == "k_index":
            hourly[f] = [round(20 + 16 * conv + rnd.uniform(-3, 3), 1) for h in range(72)]
        elif f == "precipitation_probability":
            hourly[f] = [round(_clip(conv * 95 + (0.15 if h % 24 in (15, 16, 17) else 0.05) * 100, 0, 100), 0) for h in range(72)]
        elif f in ("windspeed_10m", "wind_speed_1000hPa", "wind_speed_975hPa", "wind_speed_950hPa", "wind_speed_925hPa", "wind_speed_900hPa", "wind_speed_850hPa", "wind_speed_800hPa", "wind_speed_700hPa", "wind_speed_600hPa", "wind_speed_500hPa"):
            w = 8 + rnd.uniform(0, 6) + conv * 6
            hourly[f] = [round(w + math.sin(h % 24 / 24 * math.pi * 2) * 2, 2) for h in range(72)]
        elif f == "windspeed_100m":
            hourly[f] = [round(v * 1.6, 2) for v in hourly["windspeed_10m"]]
        elif f in ("winddirection_10m", "winddirection_100m"):
            base_dir = 230 + rnd.uniform(-15, 15)
            hourly[f] = [round(base_dir + math.sin(h % 24) * 8, 0) for h in range(72)]
        elif f.startswith("wind_direction_"):
            base_dir = 230 + rnd.uniform(-15, 15)
            hourly[f] = [round(base_dir + math.sin(h % 24) * 8, 0) for h in range(72)]
        elif f == "windgusts_10m":
            hourly[f] = [round(g * 1.9 * (1 + conv * 0.6), 1) for g in hourly["windspeed_10m"]]
        elif f == "weathercode":
            hourly[f] = [_weathercode(h % 24, conv, rnd) for h in range(72)]
        elif f == "precipitation":
            hourly[f] = [round((_precip(h % 24, conv, rnd)), 2) for h in range(72)]
        elif f == "showers":
            hourly[f] = [round(pv * (0.8 if conv > 0.5 else 0.1), 2) for pv in hourly["precipitation"]]
        elif f == "freezing_level_height":
            hourly[f] = [round(3200 + (elev or 0) + conv * 300 + math.sin(h % 24 / 24 * math.pi * 2) * 200, 1) for h in range(72)]
        elif f.startswith("temperature_850hPa") or f == "temperature_850hPa":
            hourly[f] = [round(t - (elev or 0) * 0.006 - 8, 2) for t in hourly["temperature_2m"]]
        elif f == "temperature_700hPa":
            hourly[f] = [round(t - (elev or 0) * 0.006 - 20, 2) for t in hourly["temperature_2m"]]
        elif f == "temperature_500hPa":
            hourly[f] = [round(t - (elev or 0) * 0.006 - 38, 2) for t in hourly["temperature_2m"]]
        elif f in ("relative_humidity_850hPa", "relative_humidity_700hPa"):
            hourly[f] = [round(_clip(hv + 5, 10, 100), 1) for hv in hourly["relativehumidity_2m"]]
        elif f in ("dew_point_850hPa", "dew_point_700hPa"):
            hourly[f] = [round(dv - 4, 2) for dv in hourly["dew_point_2m"]]
        elif f in ("geopotential_height_850hPa", "geopotential_height_700hPa", "geopotential_height_500hPa"):
            lvl = {"geopotential_height_850hPa": 1400, "geopotential_height_700hPa": 3100, "geopotential_height_500hPa": 5700}[f]
            hourly[f] = [round(lvl + conv * 90 + math.sin(h % 24) * 15, 1) for h in range(72)]
        else:
            hourly[f] = [round(rnd.uniform(0, 1), 3) for h in range(72)]

    wcodes = [_weathercode(h % 24, conv, rnd) for h in range(72)]
    daily = {
        "weathercode": [max(wcodes[d * 24: (d + 1) * 24]) for d in range(3)],
        "temperature_2m_max": maxs,
        "temperature_2m_min": mins,
        "precipitation_sum": [round(sum(hourly["precipitation"][d * 24: (d + 1) * 24]), 2) for d in range(3)],
        "windspeed_10m_max": [round(max(hourly["windspeed_10m"][d * 24: (d + 1) * 24]), 1) for d in range(3)],
        "precipitation_probability_max": [max(hourly["precipitation_probability"][d * 24: (d + 1) * 24]) for d in range(3)],
    }

    def model_rec(jitter):
        h2 = {}
        for f, arr in hourly.items():
            h2[f] = [round(v + rnd.uniform(-jitter, jitter), 2) for v in arr]
        d2 = {k: [round(v + rnd.uniform(-jitter, jitter), 2) for v in arr] for k, arr in daily.items()}
        d2["weathercode"] = list(daily["weathercode"])
        return {"daily": d2, "hourly": h2}

    return {
        "best_match": model_rec(0.15),
        "ecmwf_ifs": model_rec(0.35),
        "elevation": (elev or 0),
    }


def _weathercode(hour, conv, rnd):
    if conv > 0.55 and hour in (14, 15, 16, 17) and rnd.random() < 0.7:
        return rnd.choice([95, 96, 99])
    if conv > 0.45 and rnd.random() < 0.4:
        return 61 + int(rnd.choice([0, 0, 3]))
    if conv > 0.3 and hour in (15, 16) and rnd.random() < 0.3:
        return 80
    if hour > 21 or hour < 5:
        return rnd.choice([0, 1, 3])
    if rnd.random() < 0.12:
        return 3
    return 0


def _precip(hour, conv, rnd):
    if conv > 0.5 and hour in (13, 14, 15, 16, 17, 18):
        return rnd.uniform(1, 9) * conv
    if conv > 0.3 and rnd.random() < 0.3:
        return rnd.uniform(0.1, 1.5)
    return 0.0


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def main():
    points = common.generate_real_points()
    out = {"fetched_at": "2026-01-01T00:00:00Z", "forecast_days": 3, "timezone": "Europe/Rome",
           "driver_model": "ecmwf_ifs",
           "models": ["best_match", "ecmwf_ifs"],
           "points": []}
    # elevation fake deterministica (gradiente orografico sintetico)
    for p in points:
        elev = 0 + (abs(p["lat"] - 46.5) * 120) + (abs(p["lon"] - 11.3) * 60)
        out["points"].append([p["index"], synth_point(p["lat"], p["lon"], elev)])
    common.DATA_WORK.mkdir(parents=True, exist_ok=True)
    dst = common.DATA_WORK / "fixture_raw.json"
    dst.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    print("[fixture] TEST-ONLY raw sintetico: %d punti → data/_workdir/fixture_raw.json (%.0f KB)"
          % (len(out["points"]), dst.stat().st_size / 1024.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())