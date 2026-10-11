#!/usr/bin/env node
// TEST 1.2.3.1 — FORMATTAZIONE NUMERICA DEI VALORI METEO.
// La pipeline IDW continua (interpolateContinuousIDW, acc/wsum) e le fetch
// "current" restituiscono float lunghissimi (es. 19.34804234156339) che la UI
// stampava raw. Contratto: fmtNum(v,d) arrotonda a d decimali con precisione
// fisicamente sensata; null/non-finiti -> '--'.
// Decimali: temperatura 1, umidità 0, pressione 0, raffiche 1, prob 0,
// precipitazioni 1, dew point 1, cloud cover 0.
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

// ---------- estrazione blocco puro (//#pure# BEGIN/END numFormat) ----------
const cb = src.indexOf('//#pure# BEGIN numFormat');
const ce = src.indexOf('//#pure# END numFormat');
failFast('blocco numFormat non trovato', cb >= 0 && ce > cb);
const pure = src.slice(cb, ce + '//#pure# END numFormat'.length);
const ctx = {};
vm.runInNewContext(pure, ctx);
failFast('fmtNum non estratta', typeof ctx.fmtNum === 'function');

// ---------- 1. valori reali del bug (float IDW) ----------
{
  ok('fmtNum(19.34804234156339,1) === "19.3"', ctx.fmtNum(19.34804234156339, 1) === '19.3', ctx.fmtNum(19.34804234156339, 1));
  ok('fmtNum(5.834205126037622,1) === "5.8"', ctx.fmtNum(5.834205126037622, 1) === '5.8', ctx.fmtNum(5.834205126037622, 1));
  ok('fmtNum(70.2882407481653,0) === "70"', ctx.fmtNum(70.2882407481653, 0) === '70', ctx.fmtNum(70.2882407481653, 0));
  ok('fmtNum(20.4512180861319,1) === "20.5"', ctx.fmtNum(20.4512180861319, 1) === '20.5', ctx.fmtNum(20.4512180861319, 1));
}

// ---------- 2. fallback non-finiti/null ----------
{
  ok('fmtNum(null,1) === "--"', ctx.fmtNum(null, 1) === '--', ctx.fmtNum(null, 1));
  ok('fmtNum(undefined,1) === "--"', ctx.fmtNum(undefined, 1) === '--', ctx.fmtNum(undefined, 1));
  ok('fmtNum(NaN,1) === "--"', ctx.fmtNum(NaN, 1) === '--', ctx.fmtNum(NaN, 1));
  ok('fmtNum(Infinity,1) === "--"', ctx.fmtNum(Infinity, 1) === '--', ctx.fmtNum(Infinity, 1));
}

// ---------- 3. strutturale: updateUI usa fmtNum per temp/raffiche/prob/rain ----------
{
  ok('updateUI: val-temp usa fmtNum(...,1)',
    /document\.getElementById\('val-temp'\)\.innerText = .*fmtNum\(dMax,1\).*fmtNum\(dMin,1\)/.test(src) &&
    /document\.getElementById\('val-temp'\)\.innerText = .*fmtNum\(hourData\.temp,1\)/.test(src));
  ok('updateUI: val-wind (raffiche) usa fmtNum(...,1)',
    /document\.getElementById\('val-wind'\)\.innerText = .*fmtNum\(rafficaMax,1\)/.test(src));
  ok('updateUI: val-prob usa fmtNum(...,0)',
    /document\.getElementById\('val-prob'\)\.innerText = .*fmtNum\(prob,0\)/.test(src));
  ok('updateUI: val-rain usa fmtNum(...,1)',
    /document\.getElementById\('val-rain'\)\.innerText = .*fmtNum\(daily\.precipitation_sum\[currentDay\] \?\? 0,1\)/.test(src) &&
    /document\.getElementById\('val-rain'\)\.innerText = .*fmtNum\(hourData\.precip,1\)/.test(src));
}

// ---------- 4. strutturale: etichetta "Provincia di " SOLO per le province ----------
// Per le PROVINCE (selezione tile, !isCustomCity) la zona mostra "Provincia di <nome>";
// per le CITTA'/COMUNI (isCustomCity) resta il nome nudo.
{
  ok('updateUI: zone-title antepone "Provincia di " condizionato a !isCustomCity',
    /var zoneName = \(!isCustomCity && typeof subLabel === 'string'\) \? \('Provincia di ' \+ subLabel\) : subLabel;/.test(src) &&
    /document\.getElementById\('zone-title'\)\.innerText = zoneName \+/.test(src));
  ok('buildRiskTooltip: "<b>" + "Provincia di " + item.prov',
    /'<b>' \+ 'Provincia di ' \+ item\.prov \+ '<\/b>/.test(src));
  ok('showRiskSummary: rs-title con "Provincia di " + item.prov',
    /'<span class="rs-title">' \+ 'Provincia di ' \+ item\.prov \+ '<\/span>'/.test(src));
  ok('selectTile: status-msg usa " · Provincia di " + item.prov',
    /statusEl\.innerText = item\.region \+ ' · Provincia di ' \+ item\.prov;/.test(src));
  ok('chiusura toolbar: status-msg usa " · Provincia di " + provItem.prov',
    /provItem\.region \+ ' · Provincia di ' \+ provItem\.prov\)/.test(src));
  // Il ramo CITTA' non deve avere il prefisso: la selezione custom usa cityName nudo.
  ok('citta/comune: subLabel = cityName (nessun prefisso nel ramo isCustomCity)',
    /subLabel = customCityData\.cityName;/.test(src) && !/Provincia di ' \+ customCityData\.cityName/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
