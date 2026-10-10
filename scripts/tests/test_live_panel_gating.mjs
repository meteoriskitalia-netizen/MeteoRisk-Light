#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE D (FIX3): gating Live Panel / Blitzortung.
// 1.2.3.0: il pannello #live-panel ospita la funzione interna "Supercelle radar"
// (#live-toggle-supercells + #live-sc-summary/list/phenomena), quindi RESTA nel DOM
// anche con PUBLIC_EDITION_FEATURES.lightningBlitzortung = false. Con flag OFF viene
// neutralizzata SOLO la UI specifica dei fulmini (#live-legend/#live-hint), mentre i
// layer fulmini restano gated nei rispettivi handler. Con flag OFF:
//   - #live-panel RESTA nel DOM (il toggle supercelle è raggiungibile);
//   - #live-legend / #live-hint nascosti (display:none);
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
for (const n of ['function refreshBlitzTile', 'function refreshLiveLightningMarkers', 'function flashLightningStrike', 'function applyPublicEditionFeatureVisibility', 'function initBlitzortung']) {
  pure += '\n' + extractFn(n);
}
// Toggle interno "Supercelle radar" del pannello LIVE + guardia condivisa
// scDataActive(): estratti per essere ESEGUITI nel vm (comportamento ON/OFF e
// gating del fetch supercelle condiviso con il pannello #sc-radar-panel).
pure += '\nvar isScRadarActive = false;';
pure += '\nvar liveShowSupercells = false;';
pure += '\n' + extractFn('function scDataActive');
pure += '\n' + extractFn('function toggleLiveSupercells');
pure += '\n' + extractFn('function refreshLiveSupercells');
// startLivePanel per grep strutturali sullo stato WS (non eseguito qui)
const slp = extractFn('async function startLivePanel()');
// changeLightningSource / toggleLightning: grep strutturali sull'init gated
const cls = extractFn('function changeLightningSource');
const tlg = extractFn('function toggleLightning');

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
  const els = {
    'live-panel': livePanelNode,
    'status-msg': status,
    'sync-timeline-panel': { classList: { remove() {} } },
    'btn-live': { classList: { toggle() {} }, style: {} },
    // UI specifica dei fulmini dentro #live-panel: con flag OFF viene nascosta
    // (display:none) mentre il pannello RESTA nel DOM.
    'live-legend': { style: {} },
    'live-hint': { style: {} },
    'live-subtitle': { style: {} },
    // toggle interno "Supercelle radar" + id dedicati della vista LIVE (nessun
    // id duplicato del pannello #sc-radar-panel).
    'live-toggle-supercells': { classList: { add() {}, remove() {} }, style: {}, innerHTML: '' },
    // sub-toggle del pannello LIVE (spostati dal pannello SC): gating di visibilita'
    'live-toggle-trails': { style: {} },
    'live-toggle-forecast': { style: {} },
    'live-toggle-phenomena': { style: {} },
    'live-toggle-hail': { style: {} },
    'live-sc-summary': { style: {}, innerHTML: '' },
    'live-sc-list': { innerHTML: '', appendChild() {}, getAttribute() { return null; }, setAttribute() {}, addEventListener() {} },
    'live-sc-phenomena': { style: {}, innerHTML: '', getAttribute() { return null; }, addEventListener() {} },
  };
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
  blitzWsConnected: false,
  blitzWsPruneTimer: null,
  connectBlitzWs() {},
  setInterval() { return 1; },
};
vm.runInNewContext(pure, ctx);

const doc = ctx.document;

// ---------- 1. UI: #live-panel RESTA quando Blitzortung OFF (ospita supercelle) ----------
ok('UI: #live-panel presente nel DOM prima della guardia', !!doc.getElementById('live-panel'));
ctx.applyPublicEditionFeatureVisibility();
ok('UI: #live-panel NON rimosso dal DOM (lightningBlitzortung=false)', doc.getElementById('live-panel') !== null);
ok('UI: legenda fulmini #live-legend nascosta (display:none) con flag OFF',
  doc.els['live-legend'].style.display === 'none');
ok('UI: hint fulmini #live-hint nascosto (display:none) con flag OFF',
  doc.els['live-hint'].style.display === 'none');
let noThrow = true;
try { ctx.applyPublicEditionFeatureVisibility(); } catch (e) { noThrow = false; }
ok('UI: chiamata ripetuta non lancia (null-safe)', noThrow);
ok('UI: dopo la ripetizione il pannello è ancora nel DOM (idempotente)', doc.getElementById('live-panel') !== null);
// Con flag ON la UI specifica dei fulmini torna visibile; il pannello resta.
FLAGS.lightningBlitzortung = true;
ctx.applyPublicEditionFeatureVisibility();
ok('UI: con flag ON il pannello resta e legenda/hint NON sono nascosti',
  doc.getElementById('live-panel') !== null &&
  doc.els['live-legend'].style.display !== 'none' &&
  doc.els['live-hint'].style.display !== 'none');
FLAGS.lightningBlitzortung = false;
ctx.applyPublicEditionFeatureVisibility();

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
  ok('FETCH: connectBlitzWs() è chiamato SOLO dentro initBlitzortung (entry point flag-gated)',
    /function initBlitzortung\(\) \{[\s\S]*if \(!blitzWsConnected\) connectBlitzWs\(\);/.test(src) &&
    !/connectBlitzWs\(\);[\s\S]{0,120}function initBlitzortung/.test(src) &&
    /if \(isFeatureEnabled\('lightningBlitzortung'\)\) initBlitzortung\(\);/.test(sslp));
  ok('FETCH: reconnect timer (setTimeout connectBlitzWs) resta dentro connectBlitzWs, che ha il guard flag a inizio corpo',
    /function connectBlitzWs\(\) \{[\s\S]*if \(!isFeatureEnabled\('lightningBlitzortung'\)\) return;[\s\S]*setTimeout\(connectBlitzWs, 5000\)/.test(extractFn('function connectBlitzWs')));
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

// ---------- 6. INIT GATED: la flag controlla l'INIZIALIZZAZIONE, non solo l'UI ----------
{
  // 6a) Guardia di visibilità a PARSE-TIME, fuori dal gate Leaflet/boot:
  //     applicata appena lo script viene letto, PRIMA di attendere il CDN.
  ok('INIT: applyPublicEditionFeatureVisibility eseguita a parse-time prima di startApp',
    /applyPublicEditionFeatureVisibility\(\);\s*\n\s*startApp\(25\);/.test(src));
  // 6b) initBlitzortung() esiste, ha la guardia INTERNA alla flag ed è il punto
  //     unico di avvio WebSocket + timer potatura.
  ok('INIT: initBlitzortung guarda internamente la flag',
    /function initBlitzortung\(\) \{\s*\n\s*if \(!isFeatureEnabled\('lightningBlitzortung'\)\) return;/.test(src));
  // 6c) Comportamentale in vm: flag OFF -> NESSUN WS, NESSUN timer;
  //     flag ON -> connectBlitzWs + setInterval(prune).
  let connectCalls = 0, intervalCalls = 0;
  ctx.connectBlitzWs = () => { connectCalls++; };
  ctx.setInterval = () => { intervalCalls++; return 1; };
  ctx.blitzWsConnected = false; ctx.blitzWsPruneTimer = null;
  FLAGS.lightningBlitzortung = false;
  ctx.initBlitzortung();
  ok('INIT: flag OFF -> initBlitzortung NON apre WebSocket', connectCalls === 0);
  ok('INIT: flag OFF -> initBlitzortung NON avvia timer potatura', intervalCalls === 0);
  FLAGS.lightningBlitzortung = true;
  ctx.initBlitzortung();
  ok('INIT: flag ON -> initBlitzortung apre il WebSocket', connectCalls === 1);
  ok('INIT: flag ON -> initBlitzortung avvia il timer potatura', intervalCalls === 1);
  FLAGS.lightningBlitzortung = false;
  // 6d) Call site: changeLightningSource / toggleLightning / startLivePanel
  //     invocano initBlitzortung SOLO sotto isFeatureEnabled(...).
  ok('INIT: changeLightningSource usa initBlitzortung solo con flag ON',
    /lightningSource === 'blitzortung' && isFeatureEnabled\('lightningBlitzortung'\)[\s\S]{0,140}initBlitzortung\(\);/.test(cls));
  ok('INIT: toggleLightning usa initBlitzortung solo con flag ON',
    /lightningSource === 'blitzortung' && isFeatureEnabled\('lightningBlitzortung'\)[\s\S]{0,140}initBlitzortung\(\);/.test(tlg));
  ok('INIT: startLivePanel usa initBlitzortung solo con flag ON',
    /if \(isFeatureEnabled\('lightningBlitzortung'\)\) initBlitzortung\(\);/.test(slp));
  // 6e) Nessun timer di refresh tile fulmini a vuoto: con flag OFF startLivePanel
  //     NON avvia setInterval(refreshBlitzTile).
  ok('INIT: liveBlitzRefreshTimer avviato solo con flag ON',
    /if \(isFeatureEnabled\('lightningBlitzortung'\)\) \{\s*\n\s*liveBlitzRefreshTimer = setInterval\(refreshBlitzTile, LIVE_TILE_REFRESH_MS\);/.test(slp));
}

// ---------- 7. TOGGLE INTERNO "SUPERCELLE RADAR" del pannello LIVE ----------
{
  // 7a) UI: toggle dentro #live-panel + id dedicati, nessun id duplicato di #sc-*
  ok('LIVE-SC: toggle #live-toggle-supercells dentro #live-panel con onclick toggleLiveSupercells()',
    /id="live-panel"[\s\S]{0,1700}id="live-toggle-supercells"[\s\S]{0,600}onclick="toggleLiveSupercells\(\)"/.test(src));
  const liveIds = ['live-sc-summary', 'live-sc-list', 'live-sc-phenomena'];
  ok('LIVE-SC: id dedicati live-sc-summary/list/phenomena presenti una sola volta',
    liveIds.every(id => (src.match(new RegExp('id="' + id + '"', 'g')) || []).length === 1));
  ok('LIVE-SC: id del pannello SC restano unici (sc-summary/sc-list/sc-phenomena)',
    ['sc-summary', 'sc-list', 'sc-phenomena'].every(id => (src.match(new RegExp('id="' + id + '"', 'g')) || []).length === 1));

  // 7a-bis) Sub-toggle spostati nel pannello LIVE: id dedicati, handler condivisi,
  // dentro #live-panel (prima del pannello SC), nessun id duplicato.
  {
    const subIds = ['live-toggle-trails', 'live-toggle-forecast', 'live-toggle-phenomena', 'live-toggle-hail'];
    ok('LIVE-SC: sub-toggle live presenti una sola volta ciascuno',
      subIds.every(id => (src.match(new RegExp('id="' + id + '"', 'g')) || []).length === 1));
    ok('LIVE-SC: sub-toggle live agganciati agli handler esistenti',
      /id="live-toggle-trails"[^>]*onclick="toggleScTrails\(\)"/.test(src) &&
      /id="live-toggle-forecast"[^>]*onclick="toggleScForecast\(\)"/.test(src) &&
      /id="live-toggle-phenomena"[^>]*onclick="toggleScPhenomena\(\)"/.test(src) &&
      /id="live-toggle-hail"[^>]*onclick="toggleScHail\(\)"/.test(src));
    const iPanel = src.indexOf('id="live-panel"');
    const iMaster = src.indexOf('id="live-toggle-supercells"');
    const iScPanel = src.indexOf('id="sc-radar-panel"');
    ok('LIVE-SC: i sub-toggle stanno dentro #live-panel (prima di #sc-radar-panel)',
      iPanel >= 0 && iMaster > iPanel && iMaster < iScPanel &&
      subIds.every(id => { const i = src.indexOf('id="' + id + '"'); return i > iMaster && i < iScPanel; }));
    const allIds = ['live-toggle-supercells', ...subIds,
      'sc-toggle-trails', 'sc-toggle-forecast', 'sc-toggle-phenomena', 'sc-toggle-hail'];
    ok('LIVE-SC: nessun id toggle supercelle duplicato (live+sc)',
      allIds.every(id => (src.match(new RegExp('id="' + id + '"', 'g')) || []).length === 1));
    ok('LIVE-SC: scSetToggleBtn sincronizza sc-toggle-*/live-toggle-* (null-safe)',
      /function scSetToggleBtn\(id, on\)[\s\S]{0,400}sc-toggle-[\s\S]{0,160}live-toggle-/.test(src));
  }

  // 7b) guardia condivisa scDataActive() = isScRadarActive || liveShowSupercells
  ok('LIVE-SC: scDataActive() = isScRadarActive || liveShowSupercells',
    /function scDataActive\(\) \{\s*\n\s*return isScRadarActive \|\| liveShowSupercells;\s*\n\s*\}/.test(src));
  ctx.isScRadarActive = false; ctx.liveShowSupercells = false;
  ok('LIVE-SC: scDataActive false con entrambi OFF', ctx.scDataActive() === false);
  ctx.liveShowSupercells = true;
  ok('LIVE-SC: scDataActive true con toggle LIVE ON', ctx.scDataActive() === true);
  ctx.liveShowSupercells = false; ctx.isScRadarActive = true;
  ok('LIVE-SC: scDataActive true con pannello SC attivo', ctx.scDataActive() === true);
  ctx.isScRadarActive = false;

  // 7c) comportamento toggle: ON -> refresh supercelle (fetch condiviso);
  //     OFF -> liveClearSupercells e NESSUN fetch supercelle extra.
  let liveClearCalls = 0, refreshAllCalls = 0, liveViewCalls = 0;
  ctx.scSetToggleBtn = () => {};
  ctx.liveClearSupercells = () => { liveClearCalls++; };
  ctx.refreshScAll = () => { refreshAllCalls++; };
  ctx.liveRenderSupercellViews = () => { liveViewCalls++; };
  ctx.liveShowSupercells = false;
  ctx.toggleLiveSupercells();
  ok('LIVE-SC: toggle ON invoca refreshLiveSupercells -> refreshScAll (fetch unico)', refreshAllCalls === 1 && liveClearCalls === 0);
  ok('LIVE-SC: toggle ON mostra i sub-toggle del pannello LIVE',
    ['live-toggle-trails', 'live-toggle-forecast', 'live-toggle-phenomena', 'live-toggle-hail']
      .every(id => doc.els[id].style.display === ''));
  ctx.toggleLiveSupercells();
  ok('LIVE-SC: toggle OFF invoca liveClearSupercells e NON fetcha', liveClearCalls === 1 && refreshAllCalls === 1);
  ok('LIVE-SC: toggle OFF nasconde i sub-toggle del pannello LIVE',
    ['live-toggle-trails', 'live-toggle-forecast', 'live-toggle-phenomena', 'live-toggle-hail']
      .every(id => doc.els[id].style.display === 'none'));

  // 7d) con #sc-radar-panel attivo: nessun doppio fetch, solo riallineo view LIVE
  ctx.liveShowSupercells = true; ctx.isScRadarActive = true;
  refreshAllCalls = 0; liveViewCalls = 0;
  ctx.refreshLiveSupercells();
  ok('LIVE-SC: SC attivo -> nessun fetch supercelle doppio (solo view LIVE)', refreshAllCalls === 0 && liveViewCalls === 1);
  ctx.liveShowSupercells = false; ctx.isScRadarActive = false;

  // 7e) hook gated nel refresh live (SOLO con toggle ON) + guardie strutturali
  ok('LIVE-SC: refreshLivePanel aggancia supercelle SOLO con toggle ON',
    /if \(liveShowSupercells\) refreshLiveSupercells\(\);/.test(src));
  ok('LIVE-SC: refreshLiveSupercells early-return se toggle OFF',
    /function refreshLiveSupercells\(\) \{\s*\n\s*if \(!liveShowSupercells\) return;/.test(src));
  ok('LIVE-SC: liveClearSupercells non fa fetch (OFF = nessun fetch extra)',
    !/fetch\(/.test(extractFn('function liveClearSupercells')));
  ok('LIVE-SC: OFF rimuove i marker solo se #sc-radar-panel non attivo',
    /function liveClearSupercells\(\)[\s\S]{0,900}if \(!isScRadarActive\)/.test(extractFn('function liveClearSupercells')));

  // 7f) click handler condiviso anche per la lista LIVE (#live-sc-list):
  //     scEnsureListClickHandler accetta un listId; liveRenderScSummaryList lo
  //     aggancia su #live-sc-list; scSelectCandidate evidenzia entrambe le liste.
  ok('LIVE-SC: scEnsureListClickHandler accetta un listId (default #sc-list)',
    /function scEnsureListClickHandler\(listId\) \{[\s\S]{0,80}scRadarEl\(listId \|\| 'sc-list'\)/.test(src));
  ok('LIVE-SC: liveRenderScSummaryList aggancia la selezione su #live-sc-list',
    /function liveRenderScSummaryList\(cands, cards\)[\s\S]{0,1600}scEnsureListClickHandler\('live-sc-list'\)/.test(src));
  ok('LIVE-SC: scSelectCandidate evidenzia le righe in entrambe le liste (#sc-list + #live-sc-list)',
    /querySelectorAll\('#sc-list \.sc-item, #live-sc-list \.sc-item'\)/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
