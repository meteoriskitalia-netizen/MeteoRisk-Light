#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE A: fascia select "Previsioni" (fix root cause).
// Estrae il blocco //#pure# fasciaIndices + rebuildForecastSlotOptions dal
// sorgente HTML e li esegue in una vm con DOM stub (niente browser).
// In piu': verifica del wiring (no guard h.time, call sites, APP_VERSION).
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

// ---------- estrazione helper dal sorgente HTML ----------
const begin = src.indexOf('//#pure# BEGIN fasciaIndices');
const endTag = '//#pure# END fasciaIndices';
const end = src.indexOf(endTag);
if (begin < 0 || end < 0) { console.error('blocco //#pure# fasciaIndices non trovato'); process.exit(1); }
let pure = src.slice(begin, end + endTag.length);
const fb = src.indexOf('//#pure# BEGIN forecastSlotAvailability');
const fe = '//#pure# END forecastSlotAvailability';
const fend = src.indexOf(fe);
if (fb < 0 || fend < 0) { console.error('blocco forecastSlotAvailability non trovato'); process.exit(1); }
pure += '\n' + src.slice(fb, fend + fe.length);
const rb = src.match(/function rebuildForecastSlotOptions\(\) \{[\s\S]*?\n    \}/);
if (!rb) { console.error('rebuildForecastSlotOptions non trovata'); process.exit(1); }
pure += '\n' + rb[0];

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

function include(ctx) {
  return fn => {
    const script = pure + '\n(' + fn.toString() + ')();';
    return vm.runInNewContext(script, ctx, { filename: 'fascia.js' });
  };
}

// ---------- helper puri ----------
{
  const ctx = {};
  vm.runInNewContext(pure, ctx);
  const { fasciaIndices, hourlyLength } = ctx;

  ok('morning day0 -> {6,6}', JSON.stringify(fasciaIndices('morning', 0, 48)) === JSON.stringify({ startIdx: 6, count: 6 }));
  ok('afternoon day1 -> {36,6}', JSON.stringify(fasciaIndices('afternoon', 1, 60)) === JSON.stringify({ startIdx: 36, count: 6 }));
  ok('night day0 (offset +24) -> {24,6}', JSON.stringify(fasciaIndices('night', 0, 30)) === JSON.stringify({ startIdx: 24, count: 6 }));
  ok('night day2 fuori range (startIdx 72, 72+6>60) -> null', fasciaIndices('night', 2, 60) === null);
  ok('morning day2 hLen 40 (48+6>40) -> null', fasciaIndices('morning', 2, 40) === null);
  ok('slotKey ignoto (lunch) -> null', fasciaIndices('lunch', 0, 48) === null);
  ok('hLen 0 -> null', fasciaIndices('morning', 0, 0) === null);
  ok('hLen undefined -> null', fasciaIndices('morning', 0, undefined) === null);
  ok('hourlyLength(null) -> 0', hourlyLength(null) === 0);
  ok('hourlyLength({}) -> 0', hourlyLength({}) === 0);
  ok('hourlyLength con 48 valori -> 48', hourlyLength({ temperature_2m: new Array(48) }) === 48);
}

// ---------- rebuildForecastSlotOptions (DOM stub) ----------
function baseCtx(sel, stores) {
  return Object.assign({
    sel,
    document: {
      getElementById: () => sel,
      createElement() { return { value: '', textContent: '' }; },
    },
    modelStores: {},
    rawPointStores: {},
    selectedWeatherModel: 'best_match',
    isDualModelMode() { return false; },
    forecastTimeSlot: 'all', forecastActive: false,
    updateForecastMarkers() {},
  }, stores);
}
{
  const sel = makeSelect([]);
  const ctx = baseCtx(sel, { weatherStore: { a: { hourly: { temperature_2m: new Array(48) } } } });
  const run = include(ctx);
  ok('rebuild: 5 opzioni con dati >=48h',
    JSON.stringify(run(function rebuild() {
      rebuildForecastSlotOptions();
      return Array.from(sel.options).map(o => o.value);
    })) === JSON.stringify(['all', 'morning', 'afternoon', 'evening', 'night']));

  const sel3 = makeSelect(['all', 'morning', 'afternoon', 'evening', 'night']);
  const ctx3 = baseCtx(sel3, { weatherStore: { a: { daily: { temperature_2m_max: [27], weather_code: [1] } } } });
  const run3 = include(ctx3);
  const r3 = run3(function rebuild3() {
    rebuildForecastSlotOptions();
    return { v: Array.from(sel.options).map(o => o.value), t: forecastTimeSlot };
  });
  ok('rebuild: 1 opzione (all) senza dati orari', JSON.stringify(r3.v) === JSON.stringify(['all']));
  ok('rebuild: forecastTimeSlot resettato a all', r3.t === 'all');

  // 24h reali -> la fascia mattina del giorno 0 è rappresentabile (finestra 06-12).
  const sel24 = makeSelect([]);
  const ctx24 = baseCtx(sel24, { weatherStore: { a: { hourly: { temperature_2m: new Array(24) } } } });
  const run24 = include(ctx24);
  const r24 = run24(function rebuild24() {
    rebuildForecastSlotOptions();
    return Array.from(sel.options).map(o => o.value);
  });
  ok('rebuild: 24h reali -> all/morning/afternoon/evening (night esclusa)',
    JSON.stringify(r24) === JSON.stringify(['all', 'morning', 'afternoon', 'evening']), r24.join(','));
}

// ---------- getAvailableHourlyLength multi-store (regressione root cause) ----------
{
  const ctx = {
    weatherStore: { a: { hourly: { temperature_2m: new Array(24) } } },
    modelStores: { best_match: { b: { hourly: { temperature_2m: new Array(48) } } } },
    rawPointStores: { best_match: { c: { hourly: { temperature_2m: new Array(72) } } } },
    selectedWeatherModel: 'best_match',
    isDualModelMode() { return false; },
  };
  vm.runInNewContext(pure, ctx);
  ok('getAvailableHourlyLength: weatherStore 24h ma rawPointStores 72h -> 72',
    ctx.getAvailableHourlyLength() === 72);
  const ctxEmpty = { weatherStore: { a: { daily: {} } }, modelStores: {}, rawPointStores: {}, selectedWeatherModel: 'best_match', isDualModelMode() { return false; } };
  vm.runInNewContext(pure, ctxEmpty);
  ok('getAvailableHourlyLength: 0 senza hourly ovunque', ctxEmpty.getAvailableHourlyLength() === 0);
  ok('canRepresentSlot(all, 0) -> false', ctx.canRepresentSlot('all', 0) === false);
  ok('canRepresentSlot(all, 24) -> true', ctx.canRepresentSlot('all', 24) === true);
  ok('canRepresentSlot(afternoon, 17) -> false (18+6>17)', ctx.canRepresentSlot('afternoon', 17) === false);
  ok('canRepresentSlot(night, 23) -> false (serve giorno 1)', ctx.canRepresentSlot('night', 23) === false);
  ok('canRepresentSlot(evening, 24) -> true (finestra giorno 0)', ctx.canRepresentSlot('evening', 24) === true);
  ok('canRepresentSlot(evening, 48) -> true', ctx.canRepresentSlot('evening', 48) === true);
}

// ---------- wiring HTML ----------
{
  const g = src.indexOf('function getForecastSlotData(');
  const nextFn = src.indexOf('\n    function ', g + 30);
  const body = src.slice(g, nextFn > 0 ? nextFn : g + 6000);
  ok('getForecastSlotData (corpo intero) senza guard h.time', !/\bh\.time\b/.test(body));
  ok('rebuildForecastSlotOptions() chiamata >= 3 volte', (src.match(/rebuildForecastSlotOptions\(\)/g) || []).length >= 3);
  ok('APP_VERSION = 1.0.0.8', /APP_VERSION\s*=\s*['"]1\.0\.0\.8['"]/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);