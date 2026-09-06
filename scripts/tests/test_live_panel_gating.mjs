#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE D (FIX3): gating Live Panel / Blitzortung.
// Con PUBLIC_EDITION_FEATURES.lightningBlitzortung = false:
//   - l'UI #live-panel (pannello informativo Blitzortung) NON resta nel DOM;
//   - refreshBlitzTile / refreshLiveLightningMarkers / flashLightningStrike
//     ritornano PRIMA di creare layer o scrivere stato (nessuna UI orfana);
//   - il testo di status del LIVE panel è costruito dalle SOLA fonti attive
//     (registro dinamico LIVE_SOURCES);
//   - classify FETCH / LAYER / UI / LEGEND / ATTRIBUTION / DEAD CODE.
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

// ---------- estrazione dalle sorgente HTML ----------
function extractFn(name) {
  const base = name.replace('()', '');            // es. 'function flashLightningStrike'
  const esc = base.replace(/[()]/g, c => '\\' + c);
  const re = new RegExp(esc + '\\([^)]*\\) \\{[\\s\\S]*?\\n    \\}');
  const m = src.match(re);
  failFast(`${base}() non estratta`, !!m);
  return m[0];
}
let pure = '';
pure += '\n' + (src.match(/var LIVE_SOURCES = \{[\s\S]*?\n    \};/) || ['var LIVE_SOURCES = {};'])[0];
const vsl = src.match(/function visibleLiveSources\(\) \{[\s\S]*?\n    \}/);
failFast('visibleLiveSources non estratta', !!vsl);
pure += '\n' + vsl[0];
for (const n of ['function refreshBlitzTile', 'function refreshLiveLightningMarkers', 'function flashLightningStrike', 'function applyPublicEditionFeatureVisibility']) {
  pure += '\n' + extractFn(n);
}
// startLivePanel per grep strutturali sullo stato WS (non eseguito qui)
const slp = extractFn('async function startLivePanel()');

// ---------- vm con flag MUTABILI ----------
const FLAGS = { lightningBlitzortung: false, satelliteEumetsat: true, radarRainViewer: true, lightningLimaps: false, satelliteSat24: false };
let isLivePanelActive = true;
let blitzWsBuffer = [];
let liveLightningMarkerLayer = null;
let liveBlitzTileLayer = null;
let liveFlashLayer = null;
let tileCalls = 0, layerCalls = 0;

function makeDoc() {
  const status = { innerText: 'prima' };
  const livePanelNode = { classList: { add() {}, remove() {} } };
  const els = { 'live-panel': livePanelNode, 'status-msg': status, 'sync-timeline-panel': { classList: { remove() {} } }, 'btn-live': { classList: { toggle() {} }, style: {} } };
  livePanelNode.parentNode = { removeChild() { delete els['live-panel']; } };
  return {
    els,
    getElementById(id) { return els[id] || null; },
    createElement() { return {}; },
  };
}

const ctx = {
  document: makeDoc(),
  isLivePanelActive,
  blitzWsBuffer,
  liveLightningMarkerLayer,
  liveBlitzTileLayer,
  liveFlashLayer,
  blitzWsConnected: false,
  map: {
    hasLayer() { return false; },
    removeLayer() {},
    addLayer() {},
    getZoom() { return 8; },
  },
  L: {
    tileLayer() { tileCalls++; return { addTo() { return this; } }; },
    layerGroup() { layerCalls++; return { addLayer() { return this; }, addTo() { return this; }, remove() {} }; },
    marker() { return {}; },
    divIcon() { return {}; },
  },
  makeLightningCross() { return {}; },
  blitzColorForAge() { return '#ffc000'; },
  pruneBlitzBuffer() {},
  isFeatureEnabled(f) { return FLAGS[f] === true; },
  enabledRadarProviderIds() { return FLAGS.radarRainViewer ? ['rainviewer'] : []; },
  enabledSatSourceIds() { return FLAGS.satelliteEumetsat ? ['eumetsat'] : []; },
  enabledLightningSourceIds() { return []; },
  addLayerStub() {},
};
vm.runInNewContext(pure, ctx);

const doc = ctx.document;

// ---------- 1. UI: #live-panel rimosso quando Blitzortung OFF ----------
ok('UI: #live-panel presente nel DOM prima della guardia', !!doc.getElementById('live-panel'));
ctx.applyPublicEditionFeatureVisibility();
ok('UI: #live-panel RIMOSSO dal DOM (lightningBlitzortung=false)', doc.getElementById('live-panel') === null);
let noThrow = true;
try { ctx.applyPublicEditionFeatureVisibility(); } catch (e) { noThrow = false; }
ok('UI: chiamata ripetuta non lancia (null-safe)', noThrow);

// ---------- 2. LAYER: nessun tile/marker creato con flag OFF ----------
tileCalls = 0; layerCalls = 0;
ctx.refreshBlitzTile();
ok('LAYER: refreshBlitzTile non crea tile con flag OFF', tileCalls === 0 && layerCalls === 0);
ctx.refreshLiveLightningMarkers();
ok('LAYER: refreshLiveLightningMarkers non crea layerGroup con flag OFF', layerCalls === 0);
ok('LAYER: nessuno stato "Fulmini: 0 scariche" scritto (UI orfana)', doc.els['status-msg'].innerText === 'prima');
ctx.flashLightningStrike(41.9, 12.4);
ok('LAYER: flashLightningStrike non crea layer con flag OFF', layerCalls === 0 && ctx.liveFlashLayer === null);

// ---------- 3. LIVE_SOURCES: registro dinamico coerente ----------
{
  const off = ctx.visibleLiveSources();
  ok('REGISTRY: blitzortung escluso con flag OFF', off.indexOf('lightningBlitzortung') === -1,
    off.join(','));
  ok('REGISTRY: satelliteEumetsat e radar attivi', off.indexOf('satelliteEumetsat') !== -1 && off.indexOf('radar') !== -1,
    off.join(','));
  FLAGS.lightningBlitzortung = true;
  const on = ctx.visibleLiveSources();
  ok('REGISTRY: blitzortung incluso con flag ON', on.indexOf('lightningBlitzortung') !== -1, on.join(','));
  FLAGS.lightningBlitzortung = false;
}

// ---------- 4. FETCH/WS: startLivePanel le guardia strutturalmente ----------
{
  const sslp = src.slice(src.indexOf('async function startLivePanel'), src.indexOf('async function startLivePanel') + slp.length + 2400);
  ok('FETCH: connectBlitzWs chiamato solo sotto isFeatureEnabled(lightningBlitzortung)',
    /isFeatureEnabled\('lightningBlitzortung'\)/.test(sslp) && /connectBlitzWs\(\)/.test(sslp));
  ok('FETCH: refreshBlitzTile non contiene fetch() (nessun download con flag OFF — early return)',
    !/fetch\(/.test(extractFn('function refreshBlitzTile')));
  ok('FETCH: refreshLiveLightningMarkers non contiene fetch()', !/fetch\(/.test(extractFn('function refreshLiveLightningMarkers') || '{}'));
}

// ---------- 5. ATTRIBUTION + CLASSIFICAZIONE: coerenza con i guard ----------
{
  const rbBody = src.slice(src.indexOf('function refreshBlitzTile'), src.indexOf('function refreshBlitzTile') + 1100);
  const guardIdx = rbBody.indexOf("!isFeatureEnabled('lightningBlitzortung')");
  const attrIdx = rbBody.indexOf('Blitzortung.org');
  ok('ATTRIBUTION: "© Blitzortung.org" aggiunta SOLO dopo il guard (mai con flag OFF)',
    guardIdx > 0 && attrIdx > guardIdx);
  for (const f of ['function refreshBlitzTile', 'function refreshLiveLightningMarkers', 'function flashLightningStrike']) {
    const body = extractFn(f);
    ok(`GUARD: ${f} ha early-return isFeatureEnabled(lightningBlitzortung)`,
      /if \(!isFeatureEnabled\('lightningBlitzortung'\)\) return;/.test(body));
  }
  ok('DEAD CODE: toggleLivePanel usa accesso guardato a #live-panel (null-safe)',
    /var livePanelEl = document\.getElementById\('live-panel'\);?[\s\S]{0,120}if \(livePanelEl\)/.test(src));
  ok('DEAD CODE: panes fulmini creati solo dai path con guardia (liveBlitzTilePane/flashMarkerPane dentro funzioni gated)',
    /pane: 'liveBlitzTilePane'/.test(extractFn('function refreshBlitzTile')) &&
    /pane: 'flashMarkerPane'/.test(extractFn('function flashLightningStrike')));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);