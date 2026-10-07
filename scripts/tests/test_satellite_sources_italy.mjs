#!/usr/bin/env node
// TEST 1.1.0.6 — SATELLITE ITALIA + DEFAULT IR + SPEED X1 + STATIC-FIRST
// (structural checks, no browser: parses the HTML source and asserts the
// 1.1.0.6 satellite-italy / default-IR / default-speed-x1 / static-first
// features):
//   1) <select id="sat-source-select"> = 6 opzioni, prima eumetsat_italy
//      selezionata, eumetsat_airmass_italy / eumetsat_geocolour_italy presenti
//   2) eumetsatLayerMap con le 3 chiavi italia, tutte su SATELLITE_ITALY_BOUNDS
//      e ITALY_FRAME_WIDTH
//   3) SATELLITE_ITALY_BOUNDS = L.latLngBounds([[36.5, 6.6], [47.2, 18.8]]) e
//      ITALY_FRAME_WIDTH / ITALY_LIVE_WIDTH = 1024
//   4) default: syncPlaySpeed = 1, option x1 selected, satSource eumetsat_italy
//   5) enabledSatSourceIds con eumetsat_italy come primo; toggleRadarPlayer
//      forza eumetsat_italy
//   6) static-first: loadSatelliteManifest / satStaticSlotName / satStaticUrl +
//      fetch('satellite/manifest.json')
//   7) APP_VERSION = 1.1.0.7
// Documentato limite: verifica PRESENZA/STRUTTURA delle modifiche, non il
// rendering reale (il comportamento runtime va validato in-browser).
'use strict';

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.1.0.7.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}

const has = (re, inStr = src) => re.test(inStr);

// Estrae un blocco dal sorgente con bilanciamento delle parentesi graffe
// (gestisce blocchi annidati senza interrompersi al primo `}`); ritorna '' se
// l'anchor non viene trovato.
function extractBalanced(anchor) {
  const a = src.indexOf(anchor);
  if (a === -1) return '';
  const start = src.indexOf('{', a);
  if (start === -1) return '';
  let depth = 0, inStr = null, i = start;
  for (; i < src.length; i++) {
    const c = src[i];
    if (inStr) {
      if (c === inStr && src[i - 1] !== '\\') inStr = null;
      continue;
    }
    if (c === '"' || c === "'" || c === '`') { inStr = c; continue; }
    if (c === '/') {
      if (src[i + 1] === '/') { while (i < src.length && src[i] !== '\n') i++; continue; }
      if (src[i + 1] === '*') { i += 2; while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) i++; i++; continue; }
    }
    if (c === '{') depth++;
    else if (c === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return src.slice(a, Math.min(i, src.length));
}

function extractFn(name) {
  const block = extractBalanced('function ' + name);
  return block || extractBalanced('async function ' + name);
}

// ---------- 1. SELECT SORGENTI ----------
const selStart = src.indexOf('<select id="sat-source-select"');
let selectHtml = '';
if (selStart !== -1) {
  const selEnd = src.indexOf('</select>', selStart);
  if (selEnd !== -1) selectHtml = src.slice(selStart, selEnd + '</select>'.length);
}
ok('1a: <select id="sat-source-select"> presente', selStart !== -1);
ok('1b: il selettore contiene 6 <option>',
  (selectHtml.match(/<option\b/g) || []).length === 6,
  `trovate ${((selectHtml.match(/<option\b/g) || [])).length}`);
ok('1c: prima option = value="eumetsat_italy" selected',
  /<option\b[^>]*value="eumetsat_italy"[^>]*selected/.test(selectHtml));
ok('1d: option value="eumetsat_airmass_italy" presente',
  /value="eumetsat_airmass_italy"/.test(selectHtml));
ok('1e: option value="eumetsat_geocolour_italy" presente',
  /value="eumetsat_geocolour_italy"/.test(selectHtml));

// ---------- 2. eumetsatLayerMap (chiavi italia) ----------
const mapBlock = extractBalanced('eumetsatLayerMap = {');
ok('2a: const eumetsatLayerMap definita', has(/eumetsatLayerMap\s*=\s*\{/));
ok('2b: chiave eumetsat_italy nella mappa',
  !!mapBlock && /['"]eumetsat_italy['"]\s*:/.test(mapBlock));
ok('2c: chiave eumetsat_airmass_italy nella mappa',
  !!mapBlock && /['"]eumetsat_airmass_italy['"]\s*:/.test(mapBlock));
ok('2d: chiave eumetsat_geocolour_italy nella mappa',
  !!mapBlock && /['"]eumetsat_geocolour_italy['"]\s*:/.test(mapBlock));
ok('2e: la mappa usa SATELLITE_ITALY_BOUNDS', !!mapBlock && has(/SATELLITE_ITALY_BOUNDS/, mapBlock));
ok('2f: la mappa usa ITALY_FRAME_WIDTH', !!mapBlock && has(/ITALY_FRAME_WIDTH/, mapBlock));

// ---------- 3. BOUNDS / WIDTH ITALIA ----------
ok('3a: SATELLITE_ITALY_BOUNDS = L.latLngBounds([[36.5, 6.6], [47.2, 18.8]])',
  has(/SATELLITE_ITALY_BOUNDS\s*=\s*L\.latLngBounds\(\[\[\s*36\.5\s*,\s*6\.6\s*\]\s*,\s*\[\s*47\.2\s*,\s*18\.8\s*\]\]\)/));
ok('3b: ITALY_FRAME_WIDTH = 1024', has(/ITALY_FRAME_WIDTH\s*=\s*1024/));
ok('3c: ITALY_LIVE_WIDTH = 1024', has(/ITALY_LIVE_WIDTH\s*=\s*1024/));

// ---------- 4. DEFAULT IR (ITALIA) + SPEED X1 ----------
ok('4a: default syncPlaySpeed = 1', has(/var\s+syncPlaySpeed\s*=\s*1;/));
ok('4b: opzione x1 selezionata di default',
  has(/<option\b[^>]*value="1"[^>]*selected[^>]*>x1<\/option>/));
ok('4c: satSource default = eumetsat_italy',
  has(/\b(?:let|var)\s+satSource\s*=\s*['"]eumetsat_italy['"]/));

// ---------- 5. ENABLED SOURCES / TOGGLE RADAR PLAYER ----------
function firstSatToken(body) {
  const m = /['"](eumetsat|[a-z_]*infoplaza)[a-z_]*['"]/.exec(body);
  return m ? m[0].slice(1, -1) : null;
}
const eidBody = extractFn('enabledSatSourceIds');
const trpBody = extractFn('toggleRadarPlayer');
ok('5a: enabledSatSourceIds include eumetsat_italy come primo',
  !!eidBody && firstSatToken(eidBody) === 'eumetsat_italy',
  !!eidBody ? `primo token: ${String(firstSatToken(eidBody))}` : 'body non estratto');
ok('5b: toggleRadarPlayer forza eumetsat_italy',
  !!trpBody && /['"]eumetsat_italy['"]/.test(trpBody));

// ---------- 6. STATIC-FIRST ----------
ok('6a: loadSatelliteManifest presente', has(/loadSatelliteManifest/));
ok('6b: satStaticSlotName presente', has(/satStaticSlotName/));
ok('6c: satStaticUrl presente', has(/satStaticUrl/));
ok('6d: fetch("satellite/manifest.json") presente (letterale, template o via satStaticUrl)',
  has(/fetch\(\s*(?:['"`][^'"`]*satellite[\/\\]manifest\.json['"`]|satStaticUrl\s*\))/) ||
  has(/fetch\(\s*['"`][^'"`]*manifest\.json['"`]/));

// ---------- 7. VERSIONE ----------
ok('7: APP_VERSION = 1.1.0.7', has(/APP_VERSION\s*=\s*['"]1\.1\.0\.7['"]/));

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);