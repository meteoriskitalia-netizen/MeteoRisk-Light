#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE C (FIX2): coerenza poligoni vs vista continua.
// Verifica il CONTRATTO VALORE METRICO estratto dall'HTML:
//   getMetricValue(metric, agg) -> colorForMetricValue(metric, value)
// è l'UNICA scala colore; la surface interpola i VALORI e quantizza a valle
// (mai RGB), così il colore di una provincia coincide con quello della sua
// ancora nella vista continua. Casi A-D + invarianza sui colori ufficiali.
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.0.8.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- estrazione dal sorgente HTML ----------
const cb = src.indexOf('//#pure# BEGIN metricValueContract');
const ce = src.indexOf('//#pure# END metricValueContract');
failFast('blocco metricValueContract non trovato', cb >= 0 && ce >= 0);
let pure = src.slice(cb, ce + '//#pure# END metricValueContract'.length);
for (const name of [
  'function colorForMetric(metric, agg)',
  'function metricSurfaceValue(metric, agg)',
  'function surfaceColor(value, filteredShare)',
  'function hexToRgb(h)',
]) {
  const re = new RegExp(name.replace(/[()]/g, m => '\\' + m) + ' \\{[\\s\\S]*?\\n    \\}');
  const m = src.match(re);
  failFast(`${name} non estratta`, !!m);
  pure += '\n' + m[0];
}
// helper puro del valore celle V3 (no canvas/DOM) estratto dal sorgente
{
  const name = 'function computeV3CellValues(lattice, fr, points, valueReal, flagsReal, anchors)';
  const re = new RegExp(name.replace(/[()]/g, m => '\\' + m) + ' \\{[\\s\\S]*?\\n    \\}');
  const m = src.match(re);
  failFast(`${name} non estratta`, !!m);
  pure += '\n' + m[0];
}

// ---------- vm con profili controllati ----------
const RPROFILE = { level: 0, color: '#334155' };
const TPROFILE = { level: 0, color: '#334155' };
const CPROFILE = { level: 0, color: '#334155' };
const ctx = {
  isNewIndicesActive: false,
  thermalRiskIndex: 'heatindex',
  isHighRiskFilterActive: false,
  convCategory: 'no',
  currentMetric: 'risk',
  HAZARD_COLORS: ['#334155', '#22c55e', '#eab308', '#f97316', '#ef4444', '#7c1d6f'],
  getRiskProfile() { return RPROFILE; },
  getRiskProfileNew() { return RPROFILE; },
  computeThermalRisk() { return TPROFILE; },
  computeThunderProb() { return CPROFILE; },
  computeMesocycloneProb() { return CPROFILE; },
  computeVorticosiProb() { return CPROFILE; },
  computeDownburstProb() { return CPROFILE; },
  computeConvectiveIndex() { return CPROFILE; },
};
vm.runInNewContext(pure, ctx);

// ---------- 1. round-trip identità su OGNI colore ufficiale ----------
{
  const riskColors = ['#22c55e', '#a3e635', '#eab308', '#f97316', '#ef4444', '#a855f7'];
  let b1 = true;
  for (const c of riskColors) {
    const i = ctx.indexOfColorIn(ctx.RISK_BAND_COLORS, c);
    if (ctx.colorForMetricValue('risk', i) !== c) b1 = false;
  }
  ok('identità round-trip scale RISK (color->index->color)', b1);

  let b2 = true;
  for (const scaleKey of ['heatindex', 'humidex', 'windchill']) {
    ctx.thermalRiskIndex = scaleKey; // colorForMetricValue usa la scala ATTIVA
    const sc = ctx.THERMALRISK_SCALES[scaleKey];
    for (let i = 0; i < sc.length; i++) {
      if (ctx.colorForMetricValue('thermalrisk', i) !== sc[i]) b2 = false;
    }
  }
  ctx.thermalRiskIndex = 'heatindex';
  ok('identità round-trip scale THERMALRISK (tutte)', b2);

  let b3 = true;
  for (const c of ctx.HAZARD_COLORS) {
    const i = ctx.indexOfColorIn(ctx.HAZARD_COLORS, c);
    if (ctx.colorForMetricValue('conv', i) !== c) b3 = false;
  }
  ok('identità round-trip scala CONV/HAZARD', b3);
}

// ---------- 2. CASO A: provincia uniforme livello 3 ----------
{
  const agg = { prob: 40, rain: 8, wind: 32, code: 95, cape: 900, severeIndices: { showalter: 0, li: 0 } };
  RPROFILE.level = 3; RPROFILE.color = '#f97316';
  const v = ctx.getMetricValue('risk', agg);
  ok('A: getMetricValue(risk)=3 (banda #f97316)', v === 3, 'v=' + v);
  ok('A: colorForMetricValue(risk,3)=#f97316', ctx.colorForMetricValue('risk', v) === '#f97316');
  ok('A: colorForMetric ≡ colorForMetricValue su aggregato provincia',
    ctx.colorForMetric('risk', agg) === ctx.colorForMetricValue('risk', v));
  ok('A: surfaceColor(anchor provincia) = colore poligono',
    ctx.surfaceColor(ctx.getMetricValue('risk', agg), 0) === ctx.colorForMetric('risk', agg));
}

// ---------- 3. CASO B: punto critico (2 x liv1 + 1 x liv4) ----------
{
  RPROFILE.level = 4; RPROFILE.color = '#ef4444';
  const worstAgg = { prob: 70, rain: 30, wind: 66, code: 99, cape: 2200, severeIndices: {} };
  const mildAgg = { prob: 10, rain: 0, wind: 10, code: 0, cape: 200, severeIndices: {} };
  RPROFILE.level = 1; RPROFILE.color = '#22c55e';  // Basso (indice 0)
  const vMild = ctx.getMetricValue('risk', mildAgg);
  RPROFILE.level = 4; RPROFILE.color = '#ef4444';  // Alto (indice 3)
  const vWorst = ctx.getMetricValue('risk', worstAgg);
  // Poligono: worst-point -> colore ufficiale livello 4
  ok('B: poligono provincia critica = #ef4444 (liv4)', ctx.colorForMetric('risk', worstAgg) === '#ef4444');
  // Surface pura sul punto critico (ancora) = stesso colore ufficiale
  ok('B: surfaceColor nel punto critico = #ef4444', ctx.surfaceColor(vWorst, 0) === '#ef4444');
  // Media dei VALORI degli indici (2*0 + 1*3)/3 = 1 -> fascia ufficiale #a3e635
  const mix = (2 * vMild + 1 * vWorst) / 3;
  const colMix = ctx.colorForMetricValue('risk', mix);
  ok('B: mix valori quantizzato su palette ufficiale (mai RGB blending)',
    ctx.RISK_BAND_COLORS.includes(colMix) && colMix === '#a3e635', `mix=${mix} color=${colMix}`);
  ok('B: il mix NON resta sul verde puro dei punti liv1', colMix !== '#22c55e' && colMix !== '#ef4444');
}

// ---------- 4. CASO C: confine tra province (monotonia della scala) ----------
{
  let prev = -1, mono = true;
  for (let v = 0; v <= 5.001; v += 0.25) {
    const col = ctx.colorForMetricValue('risk', v);
    const idx = ctx.indexOfColorIn(ctx.RISK_BAND_COLORS, col);
    if (idx < prev) mono = false;
    if (idx < 0) mono = false;
    prev = idx;
  }
  ok('C: quantizzazione monotona sulla scala RISK (any value -> palette ufficiale)', mono);
}

// ---------- 5. CASO D: dual model (merged) poligono ≡ surface ----------
{
  RPROFILE.level = 4; RPROFILE.color = '#ef4444';
  const merged = { prob: 66, rain: 24, wind: 58, code: 96, cape: 1800, severeIndices: {} };
  const valD = ctx.getMetricValue('risk', merged);
  ok('D: poligono (merged dual) = #ef4444', ctx.colorForMetric('risk', merged) === '#ef4444');
  ok('D: surfaceColor(merged) = stessa tinta ufficiale',
    ctx.surfaceColor(valD, 0) === ctx.colorForMetric('risk', merged) && ctx.surfaceColor(valD, 0) === '#ef4444');
}

// ---------- 6. metriche continue: delegazione identica alle vecchie soglie ----------
{
  ok('humidity 90 -> #ef4444', ctx.colorForMetric('humidity', { humidity: 90 }) === '#ef4444');
  ok('tempmax 44 -> #7c1d6f', ctx.colorForMetric('tempmax', { tempMax: 44 }) === '#7c1d6f');
  ok('tempmax 31 -> #ef4444', ctx.colorForMetric('tempmax', { tempMax: 31 }) === '#ef4444');
  ok('tempmax 29 -> #fb923c (28-30)', ctx.colorForMetric('tempmax', { tempMax: 29 }) === '#fb923c');
  ok('tempmin 28 -> #ef4444', ctx.colorForMetric('tempmin', { tempMin: 28 }) === '#ef4444');
  ok('wind gust 45 -> #f97316', ctx.colorForMetric('wind', { gusts: 45 }) === '#f97316');
  ok('pressure 1005 -> #f97316', ctx.colorForMetric('pressure', { pressure: 1005 }) === '#f97316');
  ok('valori nulli -> #334155', ctx.colorForMetricValue('tempmax', null) === '#334155' &&
                            ctx.colorForMetricValue('risk', NaN) === '#334155');
}

// ---------- 7. filtro alto rischio nella surface ----------
{
  ctx.isHighRiskFilterActive = true;
  ctx.currentMetric = 'risk';
  RPROFILE.level = 2; RPROFILE.color = '#eab308';
  const aggL2 = { prob: 30, rain: 4, wind: 20, code: 1, cape: 600, severeIndices: {} };
  const sv2 = ctx.metricSurfaceValue('risk', aggL2);
  ok('filter: liv2 filtrato -> grigio #1e293b (surfaceColor con share>=0.5)',
    sv2.filtered === true && ctx.surfaceColor(sv2.v, 1) === '#1e293b');
  ok('filter: liv2 valore puro resta sulla scala', ctx.surfaceColor(sv2.v, 0) === ctx.colorForMetricValue('risk', sv2.v));
  RPROFILE.level = 3; RPROFILE.color = '#f97316';
  const aggL3 = { prob: 45, rain: 10, wind: 34, code: 95, cape: 1000, severeIndices: {} };
  const sv3 = ctx.metricSurfaceValue('risk', aggL3);
  ok('filter: liv3 NON filtrato', sv3.filtered === false && ctx.surfaceColor(sv3.v, 0) === '#f97316');
  ctx.isHighRiskFilterActive = false;
}

// ---------- 8. regressioni strutturali (nessuna interpolazione RGB residua) ----------
{
  ok('nessun RGB-mix nella cella V3 (cr += w*cc[0] assente)', !/cr \+= w \* cc\[0\]/.test(src));
  ok('nessun colore in colorAll (solo valori)', !/colorAll\[/ .test(src));
  ok('V1/V2 pixel-loop: surfaceColor + hexToRgb a valle', /var rgbEnd = hexToRgb\(colEnd\)/.test(src));
  ok('V3 chiamata con ancore provinciali', /renderContinuousV3\(points, value, flags, v3Anchors\)/.test(src));
  ok('surfaceColor presente e condivisa', /function surfaceColor\(value, filteredShare\)/.test(src));
  ok('metricSurfaceValue presente', /function metricSurfaceValue\(metric, agg\)/.test(src));
}

// ---------- 9. V3: ogni cella parte dalla provincia che la contiene (sfumatura ≡ poligoni) ----------
{
  const mm = lat => Math.log(Math.tan(Math.PI / 4 + lat * Math.PI / 360));
  const fr = { W: 256, H: 316, minLon: 0, maxLon: 12, minLat: 36, maxLat: 47,
               lonSpan: 12, mSpan: mm(47) - mm(36), mnM: mm(36), mxM: mm(47) };
  const px = (lon, lat) => ({
    x: (lon - fr.minLon) / fr.lonSpan * (fr.W - 1),
    y: (fr.mxM - mm(lat)) / fr.mSpan * (fr.H - 1),
  });
  const cellA = { lat: 36.3, lon: 0.3, zone: 5 };   // interno di provincia 5
  const rawFar = { lat: 36.3, lon: 11.8 };          // punto reale LONTANO (prov. 9, verde)
  const rawNear = { lat: 36.3, lon: 0.3 };          // punto reale SULLA cella
  const pC = px(cellA.lon, cellA.lat), pF = px(rawFar.lon, rawFar.lat), pN = px(rawNear.lon, rawNear.lat);
  const d2Far = (pC.x - pF.x) ** 2 + (pC.y - pF.y) ** 2;
  const d2Near = (pC.x - pN.x) ** 2 + (pC.y - pN.y) ** 2;
  ok('9: punto lontano è fuori dal raggio adattivo (>=180px)', d2Far >= 180 * 180, 'd2=' + d2Far.toFixed(0));
  ok('9: punto sulla cella è a distanza 0', d2Near === 0);
  const anchors = [
    { lat: 45.9, lon: 6, value: 4, filtered: false, zone: 5 },  // prov 5: livello 4 (rosso)
    { lat: 46.5, lon: 6, value: 1, filtered: false, zone: 9 },  // prov 9: livello 1 (verde)
  ];
  const resFar = ctx.computeV3CellValues([cellA], fr, [rawFar], [1], [false], anchors);
  ok('9: cella in provincia 5 lontana da punti → valore 4 (≠ verde del vicino)',
    Math.abs(resFar.cellv[0] - 4) < 1e-6, 'v=' + resFar.cellv[0].toFixed(6));
  ok('9: flag filtrato base della provincia (0 senza filtro)', Math.abs(resFar.cellf[0]) < 1e-9);
  const resOld = ctx.computeV3CellValues([{ lat: cellA.lat, lon: cellA.lon, zone: -1 }], fr,
    [rawFar], [1], [false], anchors);
  ok('9: senza zona la cella tirava dal punto più vicino (vecchio bug: verde)',
    resOld.cellv[0] !== 4 && Math.abs(resOld.cellv[0] - 1) < 1e-9, 'v=' + resOld.cellv[0].toFixed(6));
  const resMod = ctx.computeV3CellValues([cellA], fr, [rawNear], [1], [false], anchors);
  const expMod = (4 * 3 + 1 * 1) / (3 + 1);
  ok('9: punto reale SULLA cella modula localmente (PZ=3) → mix pesato',
    Math.abs(resMod.cellv[0] - expMod) < 1e-9, 'v=' + resMod.cellv[0].toFixed(4));
  const resFil = ctx.computeV3CellValues([{ lat: 36.3, lon: 0.3, zone: 5 }], fr,
    [rawFar], [4], [false],
    [{ lat: 45.9, lon: 6, value: 4, filtered: true, zone: 5 }]);
  ok('9: filtro alto rischio della provincia propagato alla cella',
    Math.abs(resFil.cellf[0] - 1) < 1e-6 && Math.abs(resFil.cellv[0] - 4) < 1e-6);
  ok('9: V3 usa computeV3CellValues (helper puro a valore provinciale)',
    /var cellRes = computeV3CellValues\(continuousLattice, fr, points, valueReal, flagsReal, anchorsList\)/.test(src));
  ok('9: celle del reticolo portano la zona (rasterZoneField)',
    /function rasterZoneField\(nx, ny, fr\)/.test(src) && /cell\.zone = zoneIds\[j \* nx \+ i\]/.test(src));
  ok('9: le ancore portano lo zone index (mappa provincia → valore)',
    /zone: vzA\.zoneIdx/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);