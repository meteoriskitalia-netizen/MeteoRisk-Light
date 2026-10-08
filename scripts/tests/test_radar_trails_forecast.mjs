#!/usr/bin/env node
// TEST PARTE 5 — TRACCE RADAR 2h / FORECAST CONO ≤2h / BADGE FENOMENI.
// Estrae le funzioni PURE dal sorgente HTML (regex + vm) e verifica:
//   A) scBuildForecastCone: chiusura anello, numero vertici, margine
//      perpendicolare (caso geometrico noto: moto puro est e puro nord,
//      cone_km costante, verifica via haversine dal punto sul path)
//   B) scAssembleTrails: ordine cronologico, dedup per slot, filtro <2 punti
//      (track di tipo diverso con stesso id = trail separato)
//   C) scMatchBadgeCandidate: inside/outside della soglia 30 km
//   D) scPhenomenaSummary/Glyph/IsProxy: conteggi, glyph, disclaimer proxy
//   E) graceful: input nulli/malcampionati -> nessun throw, output vuoti
//   F) structural: guardie document.hidden, panes, pulizia al toggle OFF
// Documentato limite: verifica la LOGICA PURA e la presenza degli agganci,
// non il rendering Leaflet reale (validato in-browser).
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.2.0.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- estrazione funzioni PURE dal sorgente HTML ----------
function extractFn(name) {
  const esc = name.replace(/[()]/g, c => '\\' + c);
  const re = new RegExp('function ' + esc + '\\([^)]*\\) \\{[\\s\\S]*?\\n    \\}');
  const m = src.match(re);
  failFast(`${name}() non estratta`, !!m);
  return m[0];
}

const PURE_FNS = [
  'scTrailKey', 'scHaversineKm', 'scBearingDeg', 'scAssembleTrails',
  'scBuildForecastCone', 'scMatchBadgeCandidate', 'scPhenomenaSummary',
  'scPhenomenaGlyph', 'scPhenomenaIsProxy',
  'scForecastConfidenceColor', 'scForecastConfidenceOpacity',
];
let pure = '';
for (const n of PURE_FNS) pure += '\n' + extractFn(n);

const ctx = {};
vm.runInNewContext(pure, ctx);

// ---------- A. CONO FORECAST: caso geometrico noto ----------
{
  // Moto puro EST (bearing 90°) a lat 42, cone_km costante 11.132 km
  // (= 0.1° di latitudine). Origine margin 0, step margin 11.132.
  const fcEast = { steps: [
    { offset_min: 30, position: [12.1, 42.0], cone_km: 11.132 },
    { offset_min: 60, position: [12.2, 42.0], cone_km: 11.132 },
  ] };
  const ring = ctx.scBuildForecastCone(fcEast, [12.0, 42.0]);
  ok('A1: anello chiuso (primo punto == ultimo)', Array.isArray(ring) &&
    ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1]);
  ok('A2: 3 punti path (origine+2 step) -> 3+3+1 = 7 vertici', ring && ring.length === 7,
    ring && String(ring.length));
  // ring = [l0, l1, l2, r2, r1, r0, l0]: lato NORD (+0.1° lat), lato SUD (-0.1°)
  ok('A3: lato sinistro (nord di un moto verso EST) lat = 42.1', ring && Math.abs(ring[1][0] - 42.1) < 1e-9);
  ok('A4: lato destro (sud) lat = 41.9 sui due vertici step', ring &&
    Math.abs(ring[3][0] - 41.9) < 1e-9 && Math.abs(ring[4][0] - 41.9) < 1e-9);
  const d1 = ctx.scHaversineKm([ring[1][1], ring[1][0]], [12.1, 42.0]);
  ok('A5: margine perpendicolare == cone_km (haversine 11.132 km)',
    Math.abs(d1 - 11.132) < 0.05, d1 && d1.toFixed(3));

  // Moto puro NORD (bearing 0°): perp left = ovest, perp right = est
  const fcNorth = { steps: [ { offset_min: 30, position: [12.0, 42.1], cone_km: 11.132 } ] };
  const ringN = ctx.scBuildForecastCone(fcNorth, [12.0, 42.0]);
  ok('A6: origine+1 step -> 2+2+1 = 5 vertici', ringN && ringN.length === 5,
    ringN && String(ringN.length));
  // ringN = [l0, l1, r1, r0, l0]: l1 = ovest, r1 = est
  ok('A7: lato sinistro (ovest di un moto verso NORD) lon < 12', ringN && ringN[1][1] < 12.0);
  ok('A8: lato destro (est) lon > 12', ringN && ringN[2][1] > 12.0);
  const dN = ctx.scHaversineKm([ringN[1][1], ringN[1][0]], [12.0, 42.1]);
  ok('A9: margine perpendicolare == cone_km su moto nord', Math.abs(dN - 11.132) < 0.05,
    dN && dN.toFixed(3));

  // Senza origine: il cono parte dal primo step (apice = primo step)
  const ringNo = ctx.scBuildForecastCone(fcEast, null);
  ok('A10: senza origine parte dal primo step -> 5 vertici', ringNo && ringNo.length === 5,
    ringNo && String(ringNo.length));

  // Confidence helpers (usate dal rendering del cono)
  ok('A11: confidence color high/medium/low',
    ctx.scForecastConfidenceColor('high') === '#22c55e' &&
    ctx.scForecastConfidenceColor('medium') === '#f59e0b' &&
    ctx.scForecastConfidenceColor('low') === '#94a3b8');
  ok('A12: opacità crescente high > medium > low',
    ctx.scForecastConfidenceOpacity('high') > ctx.scForecastConfidenceOpacity('medium') &&
    ctx.scForecastConfidenceOpacity('medium') > ctx.scForecastConfidenceOpacity('low'));
}

// ---------- B. TRAILS: assemblaggio da slot fake ----------
{
  const slotA = { slot: 'S1', radar_timestamp_ms: 1000, tracks: [
    { track_id: 1, track_type: 'cell', lonlat: [12.0, 42.0], max_dbz: 40 },
    { track_id: 1, track_type: 'cell', lonlat: [12.5, 42.5], max_dbz: 45 }, // stesso track+slot: dedup
    { track_id: 9, track_type: 'cell', lonlat: [10.0, 44.0], max_dbz: 30 }, // 1 solo punto: esclusa
  ] };
  const slotB = { slot: 'S0', radar_timestamp_ms: 500, tracks: [
    { track_id: 1, track_type: 'cell', lonlat: [11.9, 41.9], max_dbz: 38 },
  ] };
  const slotC = { slot: 'S2', radar_timestamp_ms: 1500, tracks: [
    { track_id: 1, track_type: 'cell', lonlat: [12.1, 42.1], max_dbz: 50 },
    { track_id: 1, track_type: 'storm_object', lonlat: [13.0, 43.0], max_dbz: 60 }, // tipo diverso: 1 pt
  ] };
  const trails = ctx.scAssembleTrails([slotA, slotB, slotC]); // input FUORI ordine
  const t1 = trails['cell#1'];
  ok('B1: trail assemblato per cell#1', Array.isArray(t1));
  ok('B2: 3 punti (dedup del doppione nello slot S1)', t1 && t1.length === 3, t1 && String(t1.length));
  ok('B3: ordinamento cronologico (t crescente)', t1 && t1[0].t === 500 && t1[1].t === 1000 && t1[2].t === 1500);
  ok('B4: dedup conserva il PRIMO arrivo (lonlat 12.0, non 12.5)', t1 && t1[1].lonlat[0] === 12.0);
  ok('B5: track con 1 solo punto filtrata (cell#9 assente)', !('cell#9' in trails));
  ok('B6: storm_object id diverso e 1 punto -> filtrato', !('storm_object#1' in trails));
  ok('B7: max_dbz propagato nel punto', t1 && t1[2].max_dbz === 50);
  ok('B8: scTrailKey distingue i track_type', ctx.scTrailKey(1, 'cell') !== ctx.scTrailKey(1, 'storm_object'));
}

// ---------- C. BADGE -> CANDIDATO (soglia 30 km) ----------
{
  const cands = [
    { supercell_id: 'SC-A', track_id: 1, track_type: 'cell', position: [12.0, 42.0] },
    { supercell_id: 'SC-B', track_id: 2, track_type: 'cell', position: [13.0, 43.0] },
  ];
  const inside = ctx.scMatchBadgeCandidate({ position: [12.0, 42.1] }, cands); // ~11.1 km
  ok('C1: inside 30 km -> match sul candidato più vicino (SC-A)',
    inside && inside.candidate.supercell_id === 'SC-A');
  ok('C2: distance_km coerente (~11.13)', inside && Math.abs(inside.distance_km - 11.13) < 0.2,
    inside && String(inside.distance_km));
  ok('C3: outside 30 km -> null', ctx.scMatchBadgeCandidate({ position: [15.0, 40.0] }, cands) === null);
  const far = { position: [12.0, 42.3] }; // ~33.4 km
  ok('C4: default maxKm=30 esclude ~33 km', ctx.scMatchBadgeCandidate(far, cands) === null);
  ok('C5: maxKm esplicito 50 include ~33 km', ctx.scMatchBadgeCandidate(far, cands, 50) !== null);
}

// ---------- D. FENOMENI: summary / glyph / proxy ----------
{
  const sum = ctx.scPhenomenaSummary([{ type: 'HAIL' }, { type: 'VORTEX' }, { type: 'HAIL' }, { type: 'X' }]);
  ok('D1: summary conta HAIL/VORTEX (ignora tipi ignoti)', sum.total === 3 && sum.hail === 2 && sum.vortex === 1);
  ok('D2: glyph HAIL=▲, VORTEX=↻',
    ctx.scPhenomenaGlyph({ type: 'HAIL' }) === '▲' && ctx.scPhenomenaGlyph({ type: 'VORTEX' }) === '↻');
  ok('D3: proxy via lightning.note AFA', ctx.scPhenomenaIsProxy({ lightning: { note: 'AFA proxy (no flash puntuali)' } }) === true);
  ok('D4: proxy via source MLI', ctx.scPhenomenaIsProxy({ source: 'MLI' }) === true);
  ok('D5: nessun proxy con evidence pulita (fonte DPC)', ctx.scPhenomenaIsProxy({ source: 'dpc', lightning: { note: 'flash DPC' } }) === false);
}

// ---------- E. GRACEFUL: nessun throw su input malformati ----------
{
  let noThrow = true;
  let r1, r2, r3, r4, r5;
  try {
    r1 = ctx.scAssembleTrails(null);
    r2 = ctx.scAssembleTrails([null, {}, { tracks: 'x' }, { tracks: [null] }]);
    r3 = ctx.scBuildForecastCone(null, [12, 42]);
    r4 = ctx.scBuildForecastCone({ steps: [] }, [12, 42]);
    r5 = ctx.scBuildForecastCone({ steps: [{ position: [12.1, 42.0], cone_km: 5 }] }, null);
  } catch (e) { noThrow = false; }
  ok('E1: input nulli/malcampionati -> nessun throw', noThrow);
  ok('E2: scAssembleTrails(null) -> oggetto vuoto', r1 && Object.keys(r1).length === 0);
  ok('E3: slot malformati tutti ignorati', r2 && Object.keys(r2).length === 0);
  ok('E4: forecast null -> null', r3 === null);
  ok('E5: steps vuoti -> null', r4 === null);
  ok('E6: 1 solo step senza origine -> null (path < 2 punti)', r5 === null);
  ok('E7: scMatchBadgeCandidate({}) -> null', ctx.scMatchBadgeCandidate({}, null) === null);
  ok('E8: scPhenomenaSummary(null) -> zeros', ctx.scPhenomenaSummary(null).total === 0);
  ok('E9: scPhenomenaIsProxy(null) -> false', ctx.scPhenomenaIsProxy(null) === false);
}

// ---------- F. STRUCTURAL: agganci UI/robustezza nel sorgente ----------
{
  ok('F1: refreshScHistory con guardie isScRadarActive + document.hidden',
    /function refreshScHistory\(\)[\s\S]{0,200}if \(!isScRadarActive\) return;[\s\S]{0,120}if \(document\.hidden\) return;/.test(src));
  ok('F2: refreshScHistory fetcha index.json + slot con cache/concorrenza 6',
    /data\/radar\/history\/index\.json/.test(src) && /data\/radar\/history\/slots\//.test(src) &&
    /for \(var w = 0; w < 6; w\+\+\)/.test(src));
  ok('F3: refreshScPhenomena fetcha badges.json (graceful 404)',
    /function refreshScPhenomena\(\)[\s\S]{0,500}data\/phenomena\/badges\.json/.test(src) &&
    /function refreshScPhenomena\(\)[\s\S]{0,700}scPhenomenaData = null;/.test(src));
  ok('F4: panes trailPane (520) e forecastPane (540) creati in initMap',
    /map\.createPane\('trailPane'\);\s*map\.getPane\('trailPane'\)\.style\.zIndex = 520;/.test(src) &&
    /map\.createPane\('forecastPane'\);\s*map\.getPane\('forecastPane'\)\.style\.zIndex = 540;/.test(src));
  ok('F5: toggleScRadar OFF pulisce trail + forecast + fenomeni',
    /function toggleScRadar\(\)[\s\S]{0,3000}scClearTrails\(\);[\s\S]{0,150}scClearForecasts\(\);[\s\S]{0,150}scClearPhenomena\(\);/.test(src));
  ok('F6: timer coordinato: refreshScAll su SC_REFRESH_MS (5 min)',
    /scRefreshTimer = setInterval\(refreshScAll, SC_REFRESH_MS\);/.test(src) &&
    /var SC_REFRESH_MS = 5 \* 60 \* 1000;/.test(src));
  ok('F7: trail tratteggiato con gradiente di opacità verso il presente',
    /dashArray: '4,6'[\s\S]{0,120}opacity: Math\.round\(\(0\.2 \+ 0\.6 \* frac\)/.test(src));
  ok('F8: renderScRadar ridisegna gli strati derivati (scRedrawDerived)',
    /if \(markers\.length\) \{[\s\S]{0,120}scRadarLayer = L\.layerGroup\(markers\)\.addTo\(map\);[\s\S]{0,80}scRedrawDerived\(\);/.test(src));
  ok('F9: badge glyph HAIL=▲ / VORTEX=↻ nel rendering fenomeni',
    /function scPhenomenaMarkerHtml[\s\S]{0,600}scPhenomenaGlyph\(badge\)/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
