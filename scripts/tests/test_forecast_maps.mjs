#!/usr/bin/env node
// TEST 1.2.4.0 — SEZIONE "MAPPE PREVISIONALI" (Open-Meteo Weather Map Layer).
// La sezione sostituisce il vecchio modulo Meteociel (meteociel.fr, dismesso).
//   A) ASSERT STRUTTURALI sul sorgente HTML: feature flag attiva, assenza di
//      meteocielEmbed, nessun host meteociel.fr fuori dal changelog, definizioni
//      delle funzioni del modulo, markup, pane Leaflet, host dati + pin libreria.
//   B) ASSERT COMPORTAMENTALE: estrazione reale del blocco modulo
//      ("MAPPE PREVISIONALI (Open-Meteo Weather Map Layer)" -> riga
//      "Toggle Sviluppo: mostra/nasconde") ed esecuzione in vm di Node con stub
//      (DOM null-safe, L undefined, fetch che lancia). Verifica le funzioni pure
//      del file reale: fmRunDir / fmValidFile / fmCurrentOmUrl / fmVectorUrl.
// Non esegue un browser: verifica PRESENZA/STRUTTURA + logica pura, non il
// rendering Leaflet reale (validato in-browser).
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.2.4.2.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

const has = (re, inStr = src) => re.test(inStr);

// ---------- A. ASSERT STRUTTURALI ----------
ok('A1: feature flag forecastMaps attiva di default', has(/forecastMaps:\s*true/));
ok('A2: meteocielEmbed assente (0 occorrenze)', (src.match(/meteocielEmbed/g) || []).length === 0);

// A3: nessun host meteociel di CODICE fuori dal changelog storico.
// www.meteociel.fr: mai presente; modeles*.meteociel.fr: solo dentro APP_CHANGELOG.
const clStart = src.indexOf('const APP_CHANGELOG = [');
const clMatch = src.match(/const APP_CHANGELOG = \[[\s\S]*?\n    \];/);
failFast('blocco APP_CHANGELOG non trovato', clStart >= 0 && !!clMatch);
const clEnd = clStart + clMatch[0].length;
const outsideChangelog = src.slice(0, clStart) + src.slice(clEnd);
const hostRe = /modeles[0-9]*\.meteociel\.fr/g;
const allHosts = src.match(hostRe) || [];
const clHosts = clMatch[0].match(hostRe) || [];
ok('A3a: www.meteociel.fr assente nel file', (src.match(/www\.meteociel\.fr/g) || []).length === 0);
ok('A3b: nessun host modeles*.meteociel.fr fuori dal changelog',
  (outsideChangelog.match(hostRe) || []).length === 0);
ok('A3c: gli host meteociel storici esistono SOLO nel changelog (guardia non vacua)',
  allHosts.length > 0 && allHosts.length === clHosts.length, `${allHosts.length} totali / ${clHosts.length} nel changelog`);

ok('A4a: function toggleForecastMaps definita', has(/function toggleForecastMaps\s*\(/));
ok('A4b: function ensureWeatherMapLayer definita', has(/function ensureWeatherMapLayer\s*\(/));
ok('A4c: function fmCurrentOmUrl definita', has(/function fmCurrentOmUrl\s*\(/));
ok('A4d: function fmRunDir definita', has(/function fmRunDir\s*\(/));
ok('A4e: function fmValidFile definita', has(/function fmValidFile\s*\(/));
ok('A4f: function fmRebuild definita', has(/function fmRebuild\s*\(/));
ok('A4g: function fmApplyRegion definita', has(/function fmApplyRegion\s*\(/));
ok('A4h: function fmLoaderShow definita', has(/function fmLoaderShow\s*\(/));
ok('A4i: function fmEnsureMapLoader definita', has(/function fmEnsureMapLoader\s*\(/));
ok('A4j: array FM_REGIONS presente', has(/var FM_REGIONS\s*=\s*\[/));

ok('A5a: markup id="forecast-maps-panel"', has(/id="forecast-maps-panel"/));
ok('A5b: markup id="btn-forecast-maps"', has(/id="btn-forecast-maps"/));
ok('A5c: markup id="fm-domain-select"', has(/id="fm-domain-select"/));
ok('A5d: markup id="fm-variable-select"', has(/id="fm-variable-select"/));
ok('A5e: markup id="fm-slider"', has(/id="fm-slider"/));
ok('A5f: markup id="fm-play-btn"', has(/id="fm-play-btn"/));
ok('A5g: markup id="fm-region-select"', has(/id="fm-region-select"/));
ok('A5h: loader mappa id="fm-map-loader" (creato dinamicamente)', has(/d\.id\s*=\s*'fm-map-loader'/));

ok("A6: pane Leaflet createPane('forecastMapsPane')", has(/createPane\('forecastMapsPane'\)/));
ok('A7a: host dati Open-Meteo Weather Map Layer', has(/https:\/\/openmeteo\.s3\.amazonaws\.com\/data_spatial\//));
ok('A7b: pin libreria @openmeteo/weather-map-layer@0.2.2', has(/@openmeteo\/weather-map-layer@0\.2\.2\/dist\/index\.mjs/));

// A8: preload / warm (1.2.4.0)
ok('A8a: markup id="fm-preload-btn"', has(/id="fm-preload-btn"/));
ok('A8b: markup id="fm-preload-status"', has(/id="fm-preload-status"/));
ok('A8c: function fmStartPreload definita', has(/function fmStartPreload\s*\(/));
ok('A8d: function fmPreloadFrame definita', has(/function fmPreloadFrame\s*\(/));
ok('A8e: function fmWarmNext definita', has(/function fmWarmNext\s*\(/));
ok('A8f: function fmStopWarm definita', has(/function fmStopWarm\s*\(/));
ok('A8g: var fmPreloadGen presente', has(/var fmPreloadGen\s*=/));
ok('A8h: pane nascosto forecastMapsPanePre creato', has(/createPane\('forecastMapsPanePre'\)/));
const rebuildMatch = src.match(/function fmRebuild\(\)[\s\S]*?\n\}/);
ok('A8i: fmRebuild contiene fmWarmNext()',
  !!rebuildMatch && /fmWarmNext\(\)/.test(rebuildMatch[0]));

// ---------- B. ASSERT COMPORTAMENTALE (codice reale del file) ----------
const MARK_START = 'MAPPE PREVISIONALI (Open-Meteo Weather Map Layer)';
const MARK_END = 'Toggle Sviluppo: mostra/nasconde';
const bStart = src.indexOf(MARK_START);
const bEnd = src.indexOf(MARK_END);
failFast('marcatore iniziale "MAPPE PREVISIONALI" non trovato', bStart >= 0);
failFast('marcatore finale "Toggle Sviluppo" non trovato', bEnd > bStart);
// Estrai dalla RIGA che apre il modulo alla RIGA che apre il blocco Toggle Sviluppo
// (si parte dall'inizio riga per preservare il commento "// =====" iniziale).
const lineStart = src.lastIndexOf('\n', bStart) + 1;
const endLineStart = src.lastIndexOf('\n', bEnd) + 1;
const moduleSrc = src.slice(lineStart, endLineStart);
failFast('blocco modulo MAPPE PREVISIONALI vuoto', moduleSrc.length > 0);

const ctx = {
  document: { getElementById() { return null; } },
  window: {},
  L: undefined,
  isFeatureEnabled() { return true; },
  fetch() { throw new Error('fetch non consentito nei test'); },
  setInterval() { return 0; },
  clearInterval() {},
  AbortController: undefined,
  console,
};
try {
  vm.runInNewContext(moduleSrc, ctx, { filename: 'forecastMaps.js' });
} catch (e) {
  failFast('eval del blocco MAPPE PREVISIONALI fallito: ' + e.message + '\n' + (e.stack || ''), false);
}
for (const fn of ['fmRunDir', 'fmValidFile', 'fmCurrentOmUrl', 'fmVectorUrl']) {
  failFast(`${fn} non estratta dal blocco`, typeof ctx[fn] === 'function');
}

ok("B1: fmRunDir('2026-10-10T18:00:00Z') === '2026/10/10/1800Z'",
  ctx.fmRunDir('2026-10-10T18:00:00Z') === '2026/10/10/1800Z', ctx.fmRunDir('2026-10-10T18:00:00Z'));
ok("B2: fmValidFile('2026-10-10T23:00Z') === '2026-10-10T2300.om'",
  ctx.fmValidFile('2026-10-10T23:00Z') === '2026-10-10T2300.om', ctx.fmValidFile('2026-10-10T23:00Z'));

// Stato macchina coerente con il dataset Open-Meteo (run 18Z, valid 23Z).
ctx.fmDomain = 'dwd_icon_eu';
ctx.fmVariable = 'temperature_2m';
ctx.fmIdx = 0;
ctx.fmOpts = { contours: false, arrows: false, arrowStyle: 'arrow', grid: false, dark: false };
ctx.fmMeta = {
  reference_time: '2026-10-10T18:00:00Z',
  valid_times: ['2026-10-10T23:00Z'],
  variables: ['temperature_2m'],
};

const EXPECTED = 'https://openmeteo.s3.amazonaws.com/data_spatial/dwd_icon_eu/2026/10/10/1800Z/2026-10-10T2300.om'
  + '?variable=temperature_2m&tile_size=512&interpolation=linear';
const cur = ctx.fmCurrentOmUrl();
ok('B3: fmCurrentOmUrl().url = URL raster atteso', cur && cur.url === EXPECTED, cur && cur.url);
ok('B4: fmCurrentOmUrl().valid = valid_times[0]', cur && cur.valid === '2026-10-10T23:00Z');
ok('B5: fmVectorUrl(url) === null con tutte le opzioni off',
  ctx.fmVectorUrl(EXPECTED) === null, String(ctx.fmVectorUrl(EXPECTED)));

ctx.fmOpts.contours = true;
ok("B6: con contours=true fmVectorUrl ritorna l'URL con &contours=true",
  ctx.fmVectorUrl(EXPECTED) === EXPECTED + '&contours=true', ctx.fmVectorUrl(EXPECTED));

// ---- B7: legenda (fmUpdateLegend) — l'alpha della libreria e' in 0..1, non 0..255 ----
const legendEl = { innerHTML: '' };
ctx.document.getElementById = function (id) { return id === 'fm-legend' ? legendEl : null; };
ctx.fmModule = {
  getColorScale: function () {
    return { type: 'breakpoint', unit: '°C', breakpoints: [0, 10], colors: [[0, 0, 0, 1], [255, 0, 0, 1]] };
  },
};
ctx.fmVariable = 'temperature_2m';
ctx.fmUpdateLegend();
ok('B7a: legenda usa alpha intero rgba(0,0,0,1) (nessuna divisione per 255)',
  legendEl.innerHTML.indexOf('rgba(0,0,0,1)') >= 0, legendEl.innerHTML);
ok("B7b: legenda non contiene l'alpha errato 0.0039",
  legendEl.innerHTML.indexOf('0.0039') < 0, legendEl.innerHTML);

// ---- B8: il dominio dwd_icon_seamless (senza latest.json su S3) e' rimosso ----
const domains = ctx.FM_DOMAINS || [];
ok('B8: FM_DOMAINS non contiene piu\' dwd_icon_seamless',
  domains.length > 0 && domains.every(function (d) { return d.id !== 'dwd_icon_seamless'; }),
  `${domains.length} domini`);

// ---- B9: regioni geografiche (FM_REGIONS / fmRegionDef) ----
const regions = ctx.FM_REGIONS;
ok("B9a: fmRegionDef('inesistente') ritorna il default 'domain'",
  ctx.fmRegionDef('inesistente').id === 'domain');
ok('B9b: FM_REGIONS e\' un array con almeno 8 voci',
  Array.isArray(regions) && regions.length >= 8, `${regions && regions.length} regioni`);
ok('B9c: ogni voce di FM_REGIONS ha id e label',
  Array.isArray(regions) && regions.every(function (r) {
    return typeof r.id === 'string' && r.id && typeof r.label === 'string' && r.label;
  }),
  regions && regions.map(function (r) { return r.id + ':' + r.label; }).join(', '));

// ---------- C. ASSERT COMPORTAMENTALE PRELOAD / WARM (1.2.4.0) ----------
// Il preload usa layer nascosti sul pane forecastMapsPanePre (protocollo om://),
// NON new Image(). Simuliamo fmAdapter + map + DOM e verifichiamo la catena.
const createdLayers = [];
const statusSeq = [];
const btnMock = { textContent: '', disabled: false };
const statusMock = {};
Object.defineProperty(statusMock, 'textContent', {
  get() { return this._t === undefined ? '' : this._t; },
  set(v) { this._t = v; statusSeq.push(v); },
});
ctx.document.getElementById = function (id) {
  if (id === 'fm-preload-btn') return btnMock;
  if (id === 'fm-preload-status') return statusMock;
  return null;
};
ctx.setTimeout = setTimeout; // timer reali: la catena avanza sul loop di Node
ctx.fmAdapter = {
  createTileLayer: function (url, opts) {
    const layer = {
      _url: url, _opts: opts, _handlers: {},
      on(ev, fn) { this._handlers[ev] = fn; return this; },
      addTo(m) {
        createdLayers.push(this);
        if (m && m._layers) m._layers.add(this);
        const self = this;
        setTimeout(function () { if (self._handlers && self._handlers.load) self._handlers.load(); }, 0);
        return this;
      },
    };
    return layer;
  },
};
ctx.map = {
  _layers: new Set(),
  removed: [],
  removeLayer(l) { this._layers.delete(l); this.removed.push(l); },
  hasLayer(l) { return this._layers.has(l); },
  getPane() { return {}; },
  createPane() { return {}; },
};

function resetFmState() {
  createdLayers.length = 0;
  statusSeq.length = 0;
  btnMock.textContent = '';
  btnMock.disabled = false;
  ctx.fmMeta = {
    reference_time: '2026-10-11T00:00',
    valid_times: ['2026-10-11T00:00', '2026-10-11T01:00', '2026-10-11T02:00'],
  };
  ctx.fmDomain = 'dwd_icon_eu';
  ctx.fmVariable = 'temperature_2m';
  ctx.fmActive = true;
  ctx.fmIdx = 0;
  ctx.fmOpts = { contours: false, arrows: false, arrowStyle: 'arrow', grid: false, dark: false };
  ctx.fmPreloadActive = false;
  ctx.fmPreloadGen = 0;
  ctx.fmWarmLayer = null;
  ctx.fmWarmKey = null;
  ctx.fmIsPlaying = false;
  ctx.fmInterval = null;
  ctx.fmPendingPreloadLayer = null;
  ctx.fmLayerKey = null;
  ctx.fmRasterLayer = null;
  ctx.fmVectorLayer = null;
  if (ctx.map && ctx.map.removed) ctx.map.removed.length = 0;
  if (ctx.map && ctx.map._layers) ctx.map._layers.clear();
}
const tick = (ms) => new Promise((r) => setTimeout(r, ms));

// C1: fmWarmNext isolato -> 1 layer per il frame successivo sul pane pre.
resetFmState();
ctx.fmWarmNext();
ok('C1a: fmWarmNext crea esattamente 1 layer', createdLayers.length === 1, `${createdLayers.length}`);
ok("C1b: warm layer usa pane 'forecastMapsPanePre'",
  createdLayers[0] && createdLayers[0]._opts.pane === 'forecastMapsPanePre',
  createdLayers[0] && createdLayers[0]._opts.pane);
// NB: le valid_times della traccia sono senza suffisso Z -> new Date() le legge
// come ora locale. Per non dipendere dal fuso del runner confrontiamo con
// l'URL prodotto dal modulo stesso per l'indice atteso (fmIdx+1)%n.
ok('C1c: warm layer = frame successivo (fmIdx+1)%n',
  !!ctx.fmWarmKey && ctx.fmWarmKey === ctx.fmOmUrlForIndex(1), String(ctx.fmWarmKey));
ok('C1d-bis: warm layer NON e\' il frame corrente (fmIdx)',
  !!ctx.fmWarmKey && ctx.fmWarmKey !== ctx.fmOmUrlForIndex(0), String(ctx.fmWarmKey));
ok('C1d: warm layer inizia con protocollo om://',
  createdLayers[0] && createdLayers[0]._url.indexOf('om://') === 0, createdLayers[0] && createdLayers[0]._url);
ok('C1e: fmWarmLayer punta al layer creato', ctx.fmWarmLayer === createdLayers[0]);

// C2: fmStartPreload -> n layer (uno per frame) + warm-ahead finale.
resetFmState();
ctx.fmStartPreload();
ok('C2a: subito dopo fmStartPreload il bottone e\' disabled e mostra Preload...',
  btnMock.disabled === true && btnMock.textContent.indexOf('Preload') >= 0,
  `${btnMock.disabled} / ${btnMock.textContent}`);
await tick(80);
const preloadLayers = createdLayers.filter((l) => l !== ctx.fmWarmLayer);
ok('C2b: preload crea esattamente valid_times.length layer (3)',
  preloadLayers.length === 3, `${preloadLayers.length}`);
ok('C2c: tutti i layer (preload+warm) stanno su pane forecastMapsPanePre',
  createdLayers.length === 4 && createdLayers.every((l) => l._opts.pane === 'forecastMapsPanePre'),
  `${createdLayers.length} layer`);
ok('C2d: preload segue l\'ordine ciclico (fmIdx+1)%n, (fmIdx+2)%n, (fmIdx)%n',
  preloadLayers.length === 3 &&
  preloadLayers[0]._url === 'om://' + ctx.fmOmUrlForIndex(1) &&
  preloadLayers[1]._url === 'om://' + ctx.fmOmUrlForIndex(2) &&
  preloadLayers[2]._url === 'om://' + ctx.fmOmUrlForIndex(0),
  preloadLayers.map((l) => l._url).join(' | '));
ok("C2e: contatore passa da '0/3' a '3/3 ✓'",
  statusSeq[0] === '0/3' && statusSeq[statusSeq.length - 1] === '3/3 ✓',
  JSON.stringify(statusSeq));
ok("C2f: a fine corsa bottone riabilitato e testo '⏬ Preload'",
  btnMock.disabled === false && btnMock.textContent === '⏬ Preload',
  `${btnMock.disabled} / ${btnMock.textContent}`);
ok('C2g: fmPreloadActive tornato false a fine corsa', ctx.fmPreloadActive === false);

// C3: cancellazione — dopo fmStopPreload i callback in volo non creano altri layer.
resetFmState();
ctx.fmStartPreload();
const inflight = createdLayers.length;
ctx.fmStopPreload();
await tick(60);
ok('C3a: al momento dello stop era in volo 1 layer', inflight === 1, `${inflight}`);
ok('C3b: dopo fmStopPreload nessun altro layer creato', createdLayers.length === 1, `${createdLayers.length}`);
ok('C3c: fmStopPreload riabilita il bottone', btnMock.disabled === false);

// C4 (P0-C2): annullo del preload mentre un frame e' ANCORA IN VOLO.
// Regressione: la vecchia fmPreloadFrame tornava su gen!==fmPreloadGen PRIMA di
// rimuovere il layer gia' aggiunto alla mappa -> layer orfano (opacity 0) residuo.
resetFmState();
ctx.fmStartPreload();
const inFlightLayer = ctx.fmPendingPreloadLayer;
ok('C4a: dopo fmStartPreload esiste un layer preload in volo sulla mappa',
  !!inFlightLayer && ctx.map._layers.has(inFlightLayer));
ctx.fmStopPreload(); // invalida la generazione mentre il layer non e' ancora caricato
ok('C4b: fmStopPreload azzera fmPendingPreloadLayer', ctx.fmPendingPreloadLayer === null);
ok('C4c: il layer in volo e\' stato RIMOSSO dalla mappa (nessun orfano)',
  ctx.map.removed.indexOf(inFlightLayer) >= 0 && !ctx.map._layers.has(inFlightLayer));
await tick(50); // il load tardivo del layer annullato non deve duplicare la rimozione
ok('C4d: il load tardivo non rimuove due volte lo stesso layer',
  ctx.map.removed.filter((l) => l === inFlightLayer).length === 1);

// C5 (P0-C1): fmRebuild non ricrea i layer per lo STESSO frame (cache-by-url).
resetFmState();
ctx.document.getElementById = function () { return null; };
ctx.fmModule = { updateCurrentBounds() {}, getColorScale() { return null; } };
ctx.fmRebuild();
const firstRaster = ctx.fmRasterLayer;
const firstKey = ctx.fmLayerKey;
ok('C5a: primo fmRebuild crea il raster e popola fmLayerKey', !!firstRaster && !!firstKey);
ctx.map.removed.length = 0;
ctx.fmRebuild(); // stesso frame/dominio/variabile -> skip
ok('C5b: secondo fmRebuild riusa lo stesso raster (layer NON ricreato)',
  ctx.fmRasterLayer === firstRaster);
ok('C5c: il raster non e\' stato rimosso dal path di skip',
  ctx.map.removed.indexOf(firstRaster) === -1);
ctx.fmIdx = 1; // cambio frame -> la chiave cambia -> rebuild reale
ctx.fmRebuild();
ok('C5d: cambio frame ricrea il raster (fmLayerKey aggiornata)',
  ctx.fmRasterLayer !== firstRaster && ctx.fmLayerKey !== firstKey);

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
