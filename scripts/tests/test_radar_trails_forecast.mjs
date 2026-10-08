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
  'scTrailStepOk', 'scTrailWindow', 'scTrailSegments', 'scTrailUidIndex',
  'scBuildForecastCone', 'scMatchBadgeCandidate', 'scPhenomenaSummary',
  'scPhenomenaGlyph', 'scPhenomenaIsProxy', 'scPhenomenaStateLabel',
  'scPhenomenaTypeLabel', 'scPhenomenaDetailText', 'scPhenomenaRowHtml',
  'scCandidateDirectionDeg',
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
  ok('A13: opacity low leggibile (> 0.10)', ctx.scForecastConfidenceOpacity('low') > 0.10,
    String(ctx.scForecastConfidenceOpacity('low')));
}

// ---------- A2. DIREZIONE CANDIDATO: forecast preferito, fallback motion ----------
{
  ok('A2-1: scCandidateDirectionDeg preferisce forecast (stessa direzione del cono)',
    ctx.scCandidateDirectionDeg({ forecast: { direction_toward_deg: 90 }, motion: { direction_toward_deg: 180 } }) === 90);
  ok('A2-2: fallback su motion se forecast assente/non finito',
    ctx.scCandidateDirectionDeg({ motion: { direction_toward_deg: 45 } }) === 45 &&
    ctx.scCandidateDirectionDeg({ forecast: { direction_toward_deg: NaN }, motion: { direction_toward_deg: 45 } }) === 45);
  ok('A2-3: null se entrambi assenti/non numerici',
    ctx.scCandidateDirectionDeg({}) === null &&
    ctx.scCandidateDirectionDeg(null) === null &&
    ctx.scCandidateDirectionDeg({ forecast: { direction_toward_deg: '90' }, motion: {} }) === null);
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

  // UID stabile: chiave preferita quando presente; fallback per slot vecchi.
  ok('B9: scTrailKey preferisce uid (voce-track) -> u#7',
    ctx.scTrailKey({ uid: 7, track_id: 1, track_type: 'cell' }) === 'u#7');
  ok('B10: scTrailKey fallback track_type#id senza uid',
    ctx.scTrailKey({ track_id: 1, track_type: 'cell' }) === 'cell#1' &&
    ctx.scTrailKey({ track_id: 2, track_type: 'storm_object' }) === 'storm_object#2');

  // Due slot con lo STESSO uid ma track_id diversi -> stessa scia per uid.
  {
    const s1 = { radar_timestamp_ms: 1000, tracks: [
      { uid: 5, track_id: 1, track_type: 'cell', lonlat: [12.0, 42.0] } ] };
    const s2 = { radar_timestamp_ms: 2000, tracks: [
      { uid: 5, track_id: 9, track_type: 'cell', lonlat: [12.1, 42.0] } ] };
    const trUid = ctx.scAssembleTrails([s1, s2]);
    ok('B11: stesso uid (track_id diverso) -> scia unica u#5 di 2 punti',
      trUid['u#5'] && trUid['u#5'].length === 2 &&
      trUid['u#5'][0].lonlat[0] === 12.0 && trUid['u#5'][1].lonlat[0] === 12.1);
  }
  // Due uid diversi con lo STESSO track_id grezzo -> nessuno stitching.
  {
    const s1 = { radar_timestamp_ms: 1000, tracks: [
      { uid: 5, track_id: 1, track_type: 'cell', lonlat: [12.0, 42.0] } ] };
    const s2 = { radar_timestamp_ms: 2000, tracks: [
      { uid: 5, track_id: 1, track_type: 'cell', lonlat: [12.1, 42.0] },
      { uid: 7, track_id: 1, track_type: 'cell', lonlat: [13.0, 43.0] } ] };
    const s3 = { radar_timestamp_ms: 3000, tracks: [
      { uid: 7, track_id: 1, track_type: 'cell', lonlat: [13.1, 43.0] } ] };
    const trSep = ctx.scAssembleTrails([s1, s2, s3]);
    ok('B12: uid diversi con stesso track_id -> due scie separate (no stitching)',
      trSep['u#5'] && trSep['u#5'].length === 2 &&
      trSep['u#7'] && trSep['u#7'].length === 2 &&
      trSep['u#5'].every(p => p.lonlat[0] < 13) && trSep['u#7'].every(p => p.lonlat[0] >= 13));
  }
  // Validazione: punti non finiti / fuori range Italia scartati.
  {
    const sv1 = { radar_timestamp_ms: 1000, tracks: [
      { track_id: 1, track_type: 'cell', lonlat: [12.0, 42.0] },
      { track_id: 2, track_type: 'cell', lonlat: [999, 42.0] },
      { track_id: 3, track_type: 'cell', lonlat: [12.0, NaN] },
      { track_id: 4, track_type: 'cell', lonlat: [2.0, 42.0] },
    ] };
    const sv2 = { radar_timestamp_ms: 2000, tracks: [
      { track_id: 1, track_type: 'cell', lonlat: [12.05, 42.0] },
      { track_id: 2, track_type: 'cell', lonlat: [12.0, 42.0] },
      { track_id: 3, track_type: 'cell', lonlat: [12.0, 42.0] },
      { track_id: 4, track_type: 'cell', lonlat: [12.0, 42.0] },
    ] };
    const cv = ctx.scAssembleTrails([sv1, sv2]);
    ok('B13: scarta punti fuori range/non finiti (cell#2/#3/#4 esclusi, cell#1 ok)',
      cv['cell#1'] && cv['cell#1'].length === 2 &&
      !('cell#2' in cv) && !('cell#3' in cv) && !('cell#4' in cv));
  }
  // Gate di continuità sul singolo passo.
  ok('B14: scTrailStepOk true per moto plausibile (~8 km in 5 min)',
    ctx.scTrailStepOk({ lonlat: [12.0, 42.0], t: 0 }, { lonlat: [12.1, 42.0], t: 300000 }) === true);
  ok('B15: scTrailStepOk false per salto enorme ([8,45]->[17,38] in 5 min)',
    ctx.scTrailStepOk({ lonlat: [8, 45], t: 0 }, { lonlat: [17, 38], t: 300000 }) === false);
  ok('B16: scTrailStepOk false con coordinate non valide',
    ctx.scTrailStepOk({ lonlat: [12.0, 42.0] }, { lonlat: [NaN, 42.0] }) === false);

  // Indice uid: track_type#track_id -> u#uid (slot più recente vince).
  {
    const idx = ctx.scTrailUidIndex([
      { radar_timestamp_ms: 1000, tracks: [{ uid: 5, track_id: 1, track_type: 'cell', lonlat: [12.0, 42.0] }] },
      { radar_timestamp_ms: 2000, tracks: [{ uid: 6, track_id: 1, track_type: 'cell', lonlat: [12.1, 42.0] }] },
    ]);
    ok('B17: scTrailUidIndex mappa cell#1 -> u#6 (ultimo slot)', idx['cell#1'] === 'u#6');
  }

  // Finestra temporale 2h (120 min) e fallback senza timestamp.
  {
    const ptsW = [];
    for (let i = 0; i < 40; i++) ptsW.push({ lonlat: [12 + i * 0.01, 42], t: i * 600000 });
    const win = ctx.scTrailWindow(ptsW, 120 * 60000);
    ok('B18: scTrailWindow tiene solo t >= ultimo-120min (13 punti: i=27..39)',
      win.length === 13 && win[0].t === 27 * 600000 && win[win.length - 1].t === 39 * 600000,
      String(win.length));
    const noT = [];
    for (let i = 0; i < 40; i++) noT.push({ lonlat: [12, 42] });
    ok('B19: scTrailWindow senza t -> fallback slice(-25)', ctx.scTrailWindow(noT, 120 * 60000).length === 25);
    ok('B20: scTrailWindow(null) -> array vuoto', Array.isArray(ctx.scTrailWindow(null, 120 * 60000)) &&
      ctx.scTrailWindow(null, 120 * 60000).length === 0);
  }

  // Segmenti contigui: spezzati solo da salti reali o coordinate invalide.
  {
    const segPts = [
      { lonlat: [12.00, 42.0], t: 0 },
      { lonlat: [12.05, 42.0], t: 300000 },
      { lonlat: [12.10, 42.0], t: 600000 },
      { lonlat: [17.00, 38.0], t: 900000 },
      { lonlat: [17.05, 38.0], t: 1200000 },
    ];
    const segs = ctx.scTrailSegments(segPts);
    ok('B21: scTrailSegments -> 2 segmenti contigui al salto reale (3 + 2 punti)',
      segs.length === 2 && segs[0].length === 3 && segs[1].length === 2, String(segs.length));
    const segsBad = ctx.scTrailSegments([
      { lonlat: [12, 42], t: 0 },
      { lonlat: [12.05, 42], t: 300000 },
      { lonlat: [NaN, 42], t: 600000 },
      { lonlat: [12.1, 42], t: 900000 },
      { lonlat: [12.15, 42], t: 1200000 },
    ]);
    ok('B22: coordinata non valida spezza il segmento (2 segmenti da 2 punti)',
      segsBad.length === 2 && segsBad[0].length === 2 && segsBad[1].length === 2);
    ok('B23: scTrailSegments(null) -> array vuoto', ctx.scTrailSegments(null).length === 0);
  }
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
  ok('D6: state label SUSPECT->Sospetto, CORROBORATED->Confermato',
    ctx.scPhenomenaStateLabel('SUSPECT') === 'Sospetto' &&
    ctx.scPhenomenaStateLabel('CORROBORATED') === 'Confermato');
  ok('D7: state label ignoto -> stringa originale; assente -> —',
    ctx.scPhenomenaStateLabel('FOO') === 'FOO' && ctx.scPhenomenaStateLabel(null) === '—');
  ok('D8: detail text unisce labels con · e nota proxy; null -> vuoto',
    ctx.scPhenomenaDetailText({ labels: ['a', 'b'] }) === 'a · b' &&
    ctx.scPhenomenaDetailText({ labels: ['a'], evidence: { source: 'MLI' } }) === 'a — (proxy riflettività)' &&
    ctx.scPhenomenaDetailText(null) === '');
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
  ok('F7: trail continuo (polilinea per segmento, dashArray 6,6, opacity 0.55, niente frac)',
    /L\.polyline\(latlngs, \{ pane: 'trailPane', color: color, weight: 2, dashArray: '6,6', opacity: 0\.55, interactive: false \}\)/.test(src) &&
    !/0\.2 \+ 0\.6 \* frac/.test(src));
  ok('F8: renderScRadar ridisegna gli strati derivati (scRedrawDerived)',
    /if \(markers\.length\) \{[\s\S]{0,120}scRadarLayer = L\.layerGroup\(markers\)\.addTo\(map\);[\s\S]{0,80}scRedrawDerived\(\);/.test(src));
  ok('F9: badge glyph HAIL=▲ / VORTEX=↻ nel rendering fenomeni',
    /function scPhenomenaMarkerHtml[\s\S]{0,600}scPhenomenaGlyph\(badge\)/.test(src));

  const rr = extractFn('renderScRadar');
  ok('F10: renderScRadar costruisce i pennant (SVG) e NON usa L.circleMarker',
    /scBuildCandidateMarkers|scPennantIcon/.test(rr) && !/L\.circleMarker/.test(rr) &&
    /function scPennantIcon\([\s\S]*?<svg/.test(src));
  ok('F11: scRefreshMarkerIcons ricostruisce i pennant e scRedrawPhenomena lo invoca',
    /function scRefreshMarkerIcons\(\)/.test(src) &&
    /function scRedrawPhenomena\([\s\S]{0,4000}scRefreshMarkerIcons\(\);/.test(src));
  ok('F12: scTrailStepOk applicato in scTrailSegments, invocato da scRedrawTrails',
    /function scTrailSegments\([\s\S]{0,900}scTrailStepOk\(/.test(src) &&
    /function scRedrawTrails\([\s\S]{0,1600}scTrailSegments\(/.test(src));
  ok('F13: scPhenomenaRowHtml usa .sc-phenomena-detail e scPhenomenaStateLabel; niente labels.join nella riga compatta',
    /sc-phenomena-detail/.test(extractFn('scPhenomenaRowHtml')) &&
    /scPhenomenaStateLabel\(/.test(extractFn('scPhenomenaRowHtml')) &&
    !/labels\.join\(' · '\)/.test(extractFn('scPhenomenaRowHtml')));
  ok('F14: scRedrawTrails accumula segmenti contigui e NON crea polyline per-coppia',
    /scTrailSegments\(/.test(extractFn('scRedrawTrails')) &&
    /dashArray: '6,6'/.test(extractFn('scRedrawTrails')) &&
    !/recent\[i \+ 1\]/.test(extractFn('scRedrawTrails')));
  ok('F15: finestra temporale 120 min applicata in scRedrawTrails (scTrailWindow pts,120*60000)',
    /scTrailWindow\(pts, 120 \* 60000\)/.test(src));
  ok('F16: toggle espansione delegato idempotente su #sc-phenomena',
    /function scEnsurePhenomenaClickHandler\([\s\S]{0,700}data-sc-click-bound[\s\S]{0,500}classList\.toggle\('open'\)/.test(src) &&
    /function scRedrawPhenomena\([\s\S]{0,4000}scEnsurePhenomenaClickHandler\(\);/.test(src));

  const rp = extractFn('scRedrawPhenomena');
  ok('F17: scRedrawPhenomena separa verified; i sospetti in blocco compresso <details class="sc-phenomena-suspects"> con conteggio',
    /b\.state === 'CORROBORATED' \|\| b\.state === 'VERIFIED'/.test(rp) &&
    /verified\.push\(/.test(rp) && /suspects\.push\(/.test(rp) &&
    /<details class="sc-phenomena-suspects">/.test(rp) &&
    /<summary>' \+ suspects\.length \+ ' sospetti \(/.test(rp) &&
    /scPhenomenaRowHtml\(/.test(rp));
  ok('F18: pennant e testo "verso" usano scCandidateDirectionDeg (nessun accesso diretto a motion.direction_toward_deg)',
    /scCandidateDirectionDeg\(c\)/.test(extractFn('scPennantIcon')) &&
    !/motion\.direction_toward_deg/.test(extractFn('scPennantIcon')) &&
    /scCompass\(scCandidateDirectionDeg\(c\)\)/.test(src) &&
    !/scCompass\(c\.motion\.direction_toward_deg\)/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
