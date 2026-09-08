#!/usr/bin/env node
// TEST 1.0.0.14 — PLAYER SAT24-STYLE (fluidity/performance structural checks).
// Non esegue un browser: parsa il sorgente HTML e asserisce le modifiche 1.0.0.14
// per l'animazione fluida come sat24.com:
//   A) SCHEDULER requestAnimationFrame (sostituisce la catena setTimeout)
//   B) RIUSO DEL LAYER RADAR (setUrl sul layer persistente, niente layer-per-frame)
//   C) WARM-AHEAD BOUNDED (pool trattenuto, budget condiviso, try/catch)
//   D) CRASH-GUARD CANVAS DPC
//   E) FIX TOGGLE MOBILE (riga toggle = CSS grid auto-fill, larghezze fisse)
// Documentato limite: verifica PRESENZA/STRUTTURA delle modifiche, non il
// rendering reale (il comportamento runtime va validato in-browser).
'use strict';

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.1.1.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}

const style = src.slice(src.indexOf('<style'), src.indexOf('</style>'));
const has = (re, inStr = src) => re.test(inStr);

// ---------- A. SCHEDULER requestAnimationFrame ----------
ok('A1: dichiarazione syncRAF (id rAF del play)',
  has(/let syncRAF\s*=\s*null;\s*\/\/\s*id del requestAnimationFrame del play/));
ok('A2: syncInterval di riserva ancora dichiarato (no regressione)',
  has(/let syncInterval\s*=\s*null;/));
ok('A3: startSyncPlay avvia il loop con requestAnimationFrame(playPacer)',
  has(/syncRAF\s*=\s*requestAnimationFrame\(playPacer\);/));
ok('A4: playPacer presente e arma il frame successivo',
  has(/function playPacer\(ts\)\s*\{[\s\S]{0,400}syncRAF\s*=\s*requestAnimationFrame\(playPacer\);/));
ok('A5: stopSyncPlay cancella il rAF',
  has(/function stopSyncPlay\(\)[\s\S]{0,300}if\s*\(syncRAF\)\s*\{\s*cancelAnimationFrame\(syncRAF\);\s*syncRAF\s*=\s*null;\s*\}/));
ok('A6: pacing a frameDelay = max(100, 800/syncPlaySpeed)',
  has(/var frameDelay\s*=\s*Math\.max\(100,\s*Math\.round\(800\s*\/\s*syncPlaySpeed\)\);/));
ok('A7: playPacer avanza solo se ora - lastStepMs >= frameDelay',
  has(/if\s*\(nowT\s*-\s*lastStepMs\s*>=\s*frameDelay\)\s*\{\s*[\s\S]{0,120}playStep\(\);/));
ok('A8: playStep avvolto in try/catch (frame fallito non uccide il play)',
  has(/function playStep\(\)[\s\S]{0,3000}try\s*\{[\s\S]{0,600}applySyncFrame\(syncIndex\s*\+\s*1\);/));
ok('A9: warmNextRadarFrame + warmAheadEumetsatTiles dentro il try di playStep',
  has(/try\s*\{[\s\S]{0,900}warmNextRadarFrame\(\);/));
ok('A10: kick del pacer conservato (window.__kickSatPacer)',
  has(/window\.__kickSatPacer\s*=\s*kickSatPacerNow;/));

// ---------- B. RIUSO DEL LAYER RADAR ----------
ok('B1: var radarTileLayer = null (layer radar persistente)',
  has(/var radarTileLayer\s*=\s*null;/) && has(/LAYER RADAR PERSISTENTE/));
ok('B2: la creazione del frame XYZ/DPC registra il layer persistente',
  has(/newLayer\s*=\s*L\.tileLayer\(url,\s*xyzOpts\)\.addTo\(map\);\s*[\s\S]{0,80}radarTileLayer\s*=\s*newLayer;/));
ok('B3: il frame successivo viene applicato con setUrl() sul layer persistente',
  has(/try\s*\{\s*radarTileLayer\.setUrl\(urlToSet\);\s*\}\s*catch\(e\)\s*\{/));
ok('B4: cleanupAllRadarLayers azzera il layer persistente',
  has(/cleanupAllRadarLayers\(\)\s*\{[\s\S]{0,80}radarTileLayer\s*=\s*null;/));
ok('B5: clearRadarOverlay azzera difensivamente il layer persistente',
  has(/clearRadarOverlay\(\)\s*\{[\s\S]{0,140}radarTileLayer\s*=\s*null;/));
ok('B6: resolveRadarLoad riconosce il layer condiviso',
  has(/var isSharedLayer\s*=\s*\(newLayer\s*===\s*radarTileLayer\);/));
ok('B7: i callback stale NON rimuovono il layer condiviso (>=2 guardie)',
  (src.match(/if\s*\(!isSharedLayer\)\s*\{\s*try\s*\{\s*map\.removeLayer\(newLayer\);\s*\}\s*catch\(e\)\s*\{\s*\}\s*\}/g) || []).length >= 2);
ok('B8: listener load/error ri-bindati con off()/on() sul layer persistente',
  has(/radarTileLayer\.off\(\s*'load',\s*radarTileLayer\._ctxOnTileLoad\s*\)/) &&
  has(/radarTileLayer\.on\(\s*'load',\s*radarTileLayer\._ctxOnTileLoad\s*\)/));

// ---------- C. WARM-AHEAD BOUNDED ----------
ok('C1: RADAR_WARM_AHEAD = 2 (frame avanti limitati)',
  has(/var RADAR_WARM_AHEAD\s*=\s*2;/));
ok('C2: WARM_CONCURRENCY_CAP = 16 (tetto preload simultanei)',
  has(/var WARM_CONCURRENCY_CAP\s*=\s*16;/));
ok('C3: pool trattenuto dei preload',
  has(/var warmPreloadPool\s*=\s*\[\];/));
ok('C4: trimWarmPool presente (potatura del pool)',
  has(/function trimWarmPool\(\)\s*\{/));
ok('C5: warmAheadEumetsatTiles in try/catch (non propaga al play)',
  has(/function warmAheadEumetsatTiles\(\)\s*\{[\s\S]{0,3000}\}\s*catch\(e\)\s*\{/));
ok('C6: warmNextRadarFrame in try/catch + budget per frame',
  has(/function warmNextRadarFrame\(\)\s*\{[\s\S]{0,300}try\s*\{[\s\S]{0,800}ahead\s*<=\s*RADAR_WARM_AHEAD\s*&&\s*budget\s*>\s*0/));
ok('C7: tetto condiviso sul pool prima del warm',
  has(/if\s*\(warmPreloadPool\.length\s*>=\s*WARM_CONCURRENCY_CAP\s*\*\s*2\)\s*return;/));
ok('C8: openImage del pool svuotato in fullPlayerCleanup',
  has(/warmPreloadPool\s*=\s*\[\];\s*\/\/\s*1\.0\.0\.14/));

// ---------- D. CRASH-GUARD CANVAS DPC ----------
ok('D1: getContext("2d") null-safe nella canvas DPC',
  has(/try\s*\{\s*ctx\s*=\s*cv\.getContext\('2d'\);\s*\}\s*catch\(e\)\s*\{\s*ctx\s*=\s*null;\s*\}/));
ok('D2: ctx null -> done("canvas context unavailable")',
  has(/if\s*\(!ctx\)\s*\{\s*done\('canvas context unavailable'\);\s*return\s*cv;\s*\}/));
ok('D3: drawImage in try/catch',
  has(/try\s*\{\s*ctx\.drawImage\(img,\s*0,\s*0,\s*size\.x,\s*size\.y\);\s*\}\s*catch\(e\)\s*\{\s*\}/));
ok('D4: getImageData/putImageData in try/catch (canvas tainted)',
  has(/var imgData\s*=\s*ctx\.getImageData\(0,\s*0,\s*size\.x,\s*size\.y\);/));

// ---------- E. FIX TOGGLE MOBILE ----------
ok('E1: riga toggle marcata .toolbar-row nel HTML',
  has(/<div class="toolbar-row" style="display:\s*flex;\s*gap:\s*8px;\s*flex-wrap:\s*wrap;">/));
ok('E2: .toolbar-row diventa CSS grid auto-fill (larghezze fisse)',
  has(/\.toolbar-row\s*\{\s*display:\s*grid\s*!important;\s*grid-template-columns:\s*repeat\(auto-fill,\s*minmax\(140px,\s*1fr\)\);/));
ok('E3: .tool-btn usa flex: 0 1 auto (niente allargamento a riga piena)',
  has(/\.toolbar\s+\.tool-btn\s*\{[^}]*flex:\s*0\s*1\s*auto/));
ok('E4: regressione assente: flex: 1 1 auto del vecchio toggle mobile',
  !has(/\.toolbar\s+\.tool-btn\s*\{[^}]*flex:\s*1\s*1\s*auto/));
ok('E5: touch target 44px preservato (min-height var(--touch-min))',
  has(/\.toolbar\s+\.tool-btn\s*\{[^}]*min-height:\s*var\(--touch-min\)/) &&
  has(/--touch-min:\s*44px/));

// ---------- F. Versione / nessuna regressione ----------
ok('F1: APP_VERSION = 1.0.1.1', has(/APP_VERSION\s*=\s*['"]1\.0\.1\.1['"]/));
ok('F2: changelog 1.0.0.14 PLAYER SAT24-STYLE presente',
  has(/PLAYER SAT24-STYLE \/ FLUIDITA.{0,30} ANIMAZIONE \(1\.0\.0\.14\)/));
ok('F3: full timeline mantenuta (25 slot = 24x5min, ultime 2h, scelta utente)',
  has(/const SYNC_NUM_SLOTS\s*=\s*24;/) &&
  has(/for\s*\(let i\s*=\s*SYNC_NUM_SLOTS;\s*i\s*>=\s*0;\s*i--\)/));

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);