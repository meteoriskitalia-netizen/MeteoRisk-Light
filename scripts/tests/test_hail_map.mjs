#!/usr/bin/env node
// TEST 1.2.3.0 — Mappa Rischio Grandine (metrica 'hail').
// Due sottolivelli: 'prob' (probabilità POTENZIALE, euristica non calibrata) e
// 'size' (dimensione stimata da size_mm). Sorgente: agg.hailMetrics.
// Estrae il blocco puro //#pure# metricValueContract + colorForMetric e lo esegue
// in vm.runInNewContext con ctx.hailLevel / ctx.HAIL_SCALES.
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.2.3.0.html');
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
for (const name of ['function colorForMetric(metric, agg)']) {
  const re = new RegExp(name.replace(/[()]/g, m => '\\' + m) + ' \\{[\\s\\S]*?\\n    \\}');
  const m = src.match(re);
  failFast(`${name} non estratta`, !!m);
  pure += '\n' + m[0];
}

// ---------- vm con free-variable controllate ----------
const ctx = {
  hailLevel: 'prob',
  isNewIndicesActive: false,
  thermalRiskIndex: 'heatindex',
  isHighRiskFilterActive: false,
  convCategory: 'no',
  currentMetric: 'hail',
  getRiskProfile() { return { level: 0, color: '#334155' }; },
  getRiskProfileNew() { return { level: 0, color: '#334155' }; },
  computeThermalRisk() { return { level: 0, color: '#334155' }; },
  computeThunderProb() { return { level: 0, color: '#334155' }; },
  computeMesocycloneProb() { return { level: 0, color: '#334155' }; },
  computeVorticosiProb() { return { level: 0, color: '#334155' }; },
  computeDownburstProb() { return { level: 0, color: '#334155' }; },
  computeConvectiveIndex() { return { level: 0, color: '#334155' }; },
};
vm.runInNewContext(pure, ctx);

function agg(o) {
  return {
    hailMetrics: o.hm === undefined ? { ship: o.ship, wmaxshear: o.wms, fzl_agl: o.fzl, cin: o.cin, size_mm: o.size_mm, category: o.category } : o.hm,
    cape: o.cape,
    code: o.code || 0,
    rain: o.rain || 0,
    showers: o.showers || 0,
    severeIndices: { shear06: o.shear06 },
  };
}

// ---------- 1. round-trip identità delle DUE scale ----------
{
  let bP = true;
  ctx.hailLevel = 'prob';
  const scP = ctx.HAIL_SCALES.prob;
  for (let i = 0; i < scP.length; i++) {
    if (ctx.indexOfColorIn(scP, scP[i]) !== i) bP = false;
    if (ctx.colorForMetricValue('hail', i) !== scP[i]) bP = false;
  }
  ok('round-trip identità scala hail/prob (color->index->color)', bP);

  let bS = true;
  ctx.hailLevel = 'size';
  const scS = ctx.HAIL_SCALES.size;
  for (let i = 0; i < scS.length; i++) {
    if (ctx.indexOfColorIn(scS, scS[i]) !== i) bS = false;
    if (ctx.colorForMetricValue('hail', i) !== scS[i]) bS = false;
  }
  ok('round-trip identità scala hail/size (color->index->color)', bS);
}

// ---------- 2. size_mm 0/10/20/35/50/80 -> 0..5 ----------
{
  ctx.hailLevel = 'size';
  const cases = [[0, 0], [9.9, 0], [10, 1], [20, 2], [35, 3], [50, 4], [80, 5], [120, 5]];
  let b = true;
  for (const [mm, lv] of cases) {
    const v = ctx.getMetricValue('hail', agg({ size_mm: mm }));
    if (v !== lv) b = false;
    if (ctx.colorForMetricValue('hail', v) !== ctx.HAIL_SCALES.size[lv]) b = false;
  }
  ok('size_mm -> livello 0..5 (soglie 10/20/35/50/80)', b);
}

// ---------- 3. hailMetrics assente ----------
{
  ctx.hailLevel = 'size';
  ok('size: hailMetrics null -> getMetricValue == null',
    ctx.getMetricValue('hail', { hailMetrics: null }) === null);
  ok('size: hailMetrics null -> colore #334155 (N/D)',
    ctx.colorForMetric('hail', { hailMetrics: null }) === '#334155');

  ctx.hailLevel = 'prob';
  const miss = agg({ hm: null });
  ok('prob: nessun dato (cape/ship/wms/shear nulli) -> livello 0',
    ctx.getMetricValue('hail', miss) === 0);
}

// ---------- 4. monotonia SHIP (prob) ----------
{
  ctx.hailLevel = 'prob';
  const ships = [0, 0.5, 1, 1.5, 2, 2.5, 3];
  let prev = -1, mono = true, seq = [];
  for (const s of ships) {
    const v = ctx.getMetricValue('hail', agg({ ship: s, wms: 1000, cape: 1000, shear06: 15, code: 95, category: 'moderate' }));
    seq.push(v);
    if (v < prev) mono = false;
    prev = v;
  }
  ok('prob: livello monotono crescente con SHIP', mono, 'seq=' + seq.join(','));
}

// ---------- 5. monotonia WMAXSHEAR (prob) ----------
{
  ctx.hailLevel = 'prob';
  const wmss = [300, 600, 900, 1200, 1500, 1800, 2100];
  let prev = -1, mono = true, seq = [];
  for (const w of wmss) {
    const v = ctx.getMetricValue('hail', agg({ ship: 1.5, wms: w, cape: 1200, shear06: 12, code: 95, category: 'moderate' }));
    seq.push(v);
    if (v < prev) mono = false;
    prev = v;
  }
  ok('prob: livello monotono crescente con WMAXSHEAR', mono, 'seq=' + seq.join(','));
}

// ---------- 6. codice WMO 96/99 alza il livello ----------
{
  ctx.hailLevel = 'prob';
  const l95 = ctx.getMetricValue('hail', agg({ ship: 0.5, wms: 400, cape: 300, shear06: 6, code: 95, category: 'moderate' }));
  const l96 = ctx.getMetricValue('hail', agg({ ship: 0.5, wms: 400, cape: 300, shear06: 6, code: 96, category: 'moderate' }));
  const l99 = ctx.getMetricValue('hail', agg({ ship: 0.5, wms: 400, cape: 300, shear06: 6, code: 99, category: 'moderate' }));
  ok('prob: code 96 alza il livello rispetto a 95', l96 > l95, `95=${l95} 96=${l96}`);
  ok('prob: code 99 alza il livello rispetto a 95', l99 > l95, `95=${l95} 99=${l99}`);
}

// ---------- 7. quota gelo alta (>4500 m AGL) abbassa il livello ----------
{
  ctx.hailLevel = 'prob';
  const base = agg({ ship: 2.5, wms: 1500, cape: 2000, shear06: 25, code: 95, category: 'strong' });
  const highFzl = agg({ ship: 2.5, wms: 1500, cape: 2000, shear06: 25, code: 95, fzl: 5000, category: 'strong' });
  const vBase = ctx.getMetricValue('hail', base);
  const vHigh = ctx.getMetricValue('hail', highFzl);
  ok('prob: fzl_agl > 4500 m abbassa il livello', vHigh < vBase, `base=${vBase} high=${vHigh}`);
}

// ---------- 8. regressioni strutturali sul sorgente ----------
{
  ok('HTML: id="metric-tab-hail" presente', /id="metric-tab-hail"/.test(src));
  ok('HTML: id="hail-level-select" presente', /id="hail-level-select"/.test(src));

  const mSel = src.match(/function selectMetric\(metric\) \{[\s\S]*?\n    \}/);
  failFast('function selectMetric non estratta', !!mSel);
  ok("selectMetric: gestisce 'hail-level-select'", mSel[0].includes('hail-level-select'));

  const mTog = src.match(/function toggleSviluppo\(force\) \{[\s\S]*?\n    \}/);
  failFast('function toggleSviluppo non estratta', !!mTog);
  ok("toggleSviluppo: gestisce 'metric-tab-hail'", mTog[0].includes('metric-tab-hail'));

  const mSurf = src.match(/function metricSurfaceValue\(metric, agg\) \{[\s\S]*?\n    \}/);
  failFast('function metricSurfaceValue non estratta', !!mSurf);
  ok("metricSurfaceValue: ramo hail presente", mSurf[0].includes("metric === 'hail'"));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
