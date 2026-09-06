#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE B (FIX1): end-to-end forecast slot availability.
// Percorso reale: fixture meteorisk-points.json (schema 1.0.0.8) -> applyStaticDataset()
// -> popolamento store -> rebuildForecastSlotOptions() -> DOM (select Previsioni).
// Non ci sono soglie rigide (>=48): la disponibilità deriva dai dati realmente
// caricati su TUTTI gli store (weatherStore + modelStores + rawPointStores).
// Si stubbano solo le dipendenze pesanti non in gioco per la durata oraria
// (densifyVirtualPoints, selezione worst-point deterministica, merge dual,
// updateMapColors/renderContinuousOverlay/updateUI).
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.0.8.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond) {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- estrazione dal sorgente HTML ----------
function extractPure(beginTag, endTag) {
  const b = src.indexOf(beginTag);
  const e = src.indexOf(endTag);
  failFast(`blocco ${beginTag} non trovato`, b >= 0 && e >= 0);
  return src.slice(b, e + endTag.length);
}
let pure = extractPure('//#pure# BEGIN fasciaIndices', '//#pure# END fasciaIndices');
pure += '\n' + extractPure('//#pure# BEGIN forecastSlotAvailability', '//#pure# END forecastSlotAvailability');
const rb = src.match(/function rebuildForecastSlotOptions\(\) \{[\s\S]*?\n    \}/);
failFast('rebuildForecastSlotOptions non trovata', !!rb);
pure += '\n' + rb[0];
const asd = src.match(/function applyStaticDataset\(meta, payload\) \{[\s\S]*?\n    \}/);
failFast('applyStaticDataset non trovata', !!asd);
pure += '\n' + asd[0];
// Stub interni COMPILATI nella vm (i riferimenti a provinceSamplePoints/weatherStore
// devono risolvere i global della vm, non l'host).
pure += `
    function densifyVirtualPoints(m) { return 0; }
    function worstPointForProvince(rawPts, provIdx) {
        for (var i = 0; i < provinceSamplePoints.length; i++) {
            var slot = provinceSamplePoints[i];
            if (slot.provinceIdx !== provIdx) continue;
            var d = rawPts[slot.index];
            if (d && d.daily && d.hourly) return d;
        }
        return null;
    }
    function assembleDualModelStores(storeA, storeB) {
        var mergedStore = {};
        for (var p = 0; p < regionsData.length; p++) mergedStore[p] = storeA[p] || storeB[p];
        weatherStore = mergedStore;
        modelStores['dual_best_ecmwf'] = Object.assign({}, mergedStore);
    }
`;

// ---------- fixture reale (schema meteorisk-points.json) ----------
function series(len, gen) {
  const a = [];
  for (let i = 0; i < len; i++) a.push(gen(i));
  return a;
}
function buildModel(hours) {
  return {
    daily: {
      weather_code: [1, 2, 3],
      temperature_2m_max: [27, 28, 29],
      temperature_2m_min: [16, 17, 18],
      precipitation_sum: [0, 0, 0],
      wind_speed_10m_max: [20, 22, 18],
      wind_gusts_10m_max: [40, 44, 36],
    },
    hourly: {
      weathercode: series(hours, i => [0, 3, 51, 61, 95, 1, 2, 63][i % 8]),
      temperature_2m: series(hours, i => 20 + 6 * Math.sin((2 * Math.PI * (i % 24)) / 24 - Math.PI / 2)),
      precipitation: series(hours, () => 0),
    },
  };
}
function buildPoint(id, provinceIdx, sigla, lat, lon, hoursBm, hoursEcm) {
  return {
    id, provinceIdx, sigla, coordIdx: 0, lat, lon, elevation: 100,
    models: {
      best_match: buildModel(hoursBm),
      ecmwf_ifs: buildModel(hoursEcm),
    },
  };
}
function buildFixture(hoursBm, hoursEcm) {
  return {
    status: 'live',
    points: [
      buildPoint(0, 0, 'RM', 41.89, 12.48, hoursBm, hoursEcm),
      buildPoint(1, 1, 'MI', 45.46, 9.18, hoursBm, hoursEcm),
      buildPoint(2, 2, 'PA', 38.12, 13.36, hoursBm, hoursEcm),
    ],
  };
}

// ---------- stub DOM ----------
function makeSelect(initialValues) {
  const o = { options: (initialValues || []).map(v => ({ value: v, textContent: v })) };
  Object.defineProperty(o, 'innerHTML', {
    enumerable: true,
    get() { return o._inner || ''; },
    set(v) { o._inner = v; o.options.length = 0; },
  });
  o.appendChild = el => { o.options.push(el); };
  return o;
}
function makeDoc(sel) {
  const els = {
    'forecast-slot': sel,
    'status-msg': { innerText: '' },
  };
  return {
    getElementById: id => els[id] || null,
    createElement() { return { value: '', textContent: '' }; },
    querySelector() { return { innerHTML: '' }; },
  };
}

// ---------- scenario A: fixture 72h -> applyStaticDataset -> 5 opzioni ----------
{
  const sel = makeSelect([]);
  const ctx = {
    document: makeDoc(sel),
    regionsData: [{}, {}, {}],
    provinceGeoJsonData: null,
    DATASET_COVERED_MODELS: ['dual_best_ecmwf', 'best_match', 'ecmwf_ifs'],
    weatherFetchGeneration: 0,
    selectedWeatherModel: 'dual_best_ecmwf',
    provinceSamplePoints: [],
    provinceSamplesReady: false,
    rawPointStores: {},
    modelStores: {},
    weatherStore: {},
    zonePreview: null,
    datasetState: { loaded: false, generatedAt: '', day0: '', pointCount: 0, virtualCount: 0 },
    DENSIFY: { _status: null, _confOf() { return 0.5; } },
    isDualModelMode() { return true; },
    forecastTimeSlot: 'all',
    forecastActive: false,
    updateForecastMarkers() {},
    updateMapColors() {},
    renderContinuousOverlay() {},
    updateUI() {},
  };
  vm.runInNewContext(pure, ctx);
  const meta = { status: 'live', point_count: 3, forecast_days: 3, generated_at: '2026-09-06T00:00:00Z', day0: '2026-09-06' };

  let threw = null;
  try { ctx.applyStaticDataset(meta, buildFixture(72, 72)); } catch (e) { threw = e; }
  ok('A: applyStaticDataset non lancia (' + (threw && threw.message) + ')', threw === null);
  ok('A: 3 province popolate in weatherStore', Object.keys(ctx.weatherStore).length === 3);
  const lens = Object.keys(ctx.weatherStore).map(k => ctx.weatherStore[k].hourly.temperature_2m.length);
  ok('A: weatherStore con hourly 72h per provincia', lens.every(l => l === 72), lens.join(','));
  ok('A: rawPointStores.best_match popolato 72h',
    Object.keys(ctx.rawPointStores.best_match).length === 3 &&
    Object.values(ctx.rawPointStores.best_match).every(r => r.hourly.temperature_2m.length === 72));
  const values = sel.options.map(o => o.value);
  ok('A: select = 5 opzioni all/morning/afternoon/evening/night',
    JSON.stringify(values) === JSON.stringify(['all', 'morning', 'afternoon', 'evening', 'night']), values.join('|'));
}

// ---------- scenario B: nessun dato orario (daily-only) -> 1 opzione 'all' ----------
{
  const sel = makeSelect(['all', 'morning', 'afternoon', 'evening', 'night']);
  const ctx = {
    document: makeDoc(sel),
    modelStores: {},
    rawPointStores: {},
    isDualModelMode() { return false; },
    selectedWeatherModel: 'best_match',
    weatherStore: { 0: { daily: { temperature_2m_max: [27], weather_code: [1] }, elevation: 100 } },
    forecastTimeSlot: 'morning',
    forecastActive: false,
    updateForecastMarkers() {},
  };
  vm.runInNewContext(pure, ctx);
  const before = sel.options.length;
  ctx.rebuildForecastSlotOptions();
  ok('B: getAvailableHourlyLength()=0 senza hourly', ctx.getAvailableHourlyLength() === 0);
  ok('B: select passa da 5 a 1 opzione (all)',
    before === 5 && sel.options.length === 1 && sel.options[0].value === 'all');
  ok('B: forecastTimeSlot resettato a all', ctx.forecastTimeSlot === 'all');
}

// ---------- scenario C: regressione root cause (weatherStore corto, raw store 72h) ----------
{
  const sel = makeSelect([]);
  const ctx = {
    document: makeDoc(sel),
    modelStores: {},
    rawPointStores: {},
    isDualModelMode() { return false; },
    selectedWeatherModel: 'best_match',
    // weatherStore collassato corto (24h), ma rawPointStores del modello selezionato ha 72h
    weatherStore: { 0: buildModel(24), 1: buildModel(24) },
    forecastTimeSlot: 'all',
    forecastActive: false,
    updateForecastMarkers() {},
  };
  ctx.rawPointStores.best_match = { 0: buildModel(72), 1: buildModel(72), 2: buildModel(72) };
  ctx.modelStores.best_match = { 0: buildModel(24), 1: buildModel(24) };
  vm.runInNewContext(pure, ctx);
  const len = ctx.getAvailableHourlyLength();
  ok('C: getAvailableHourlyLength()=72 anche se weatherStore=24h (scan multi-store)', len === 72);
  ctx.rebuildForecastSlotOptions();
  const vals = sel.options.map(o => o.value);
  ok('C: select = 5 opzioni (fix root cause, niente soglia fissa)',
    JSON.stringify(vals) === JSON.stringify(['all', 'morning', 'afternoon', 'evening', 'night']));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);