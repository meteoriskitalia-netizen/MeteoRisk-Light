#!/usr/bin/env node
// TEST 1.0.0.8 — MAPPATURA "OGGI" ↔ INDICE DATASET.
// Dopo un reload a mezzanotte "Oggi" mostrava ancora i dati del giorno precedente:
// la UI associava "Oggi" a dayIndex 0 invece di cercare la data ISO reale (Europe/Rome)
// nei giorni del dataset. Contratto imposto:
//   selectedDateKey (oggi Europe/Rome)  →  offset = giorni(dataset.day0 → oggi)  →  indice
// MAI assumere dayIndex === 0 come sinonimo di "Oggi".
// Tabella obbligatoria: data locale | dataset day0 | "Oggi" deve usare indice
//   2026-09-05 | 2026-09-05 | 0
//   2026-09-06 | 2026-09-05 | 1
//   2026-09-07 | 2026-09-05 | 2
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.0.9.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- estrazione blocco puro (//#pure# BEGIN/END dayMapping) ----------
const cb = src.indexOf('//#pure# BEGIN dayMapping');
const ce = src.indexOf('//#pure# END dayMapping');
failFast('blocco dayMapping non trovato', cb >= 0 && ce > cb);
const pure = src.slice(cb, ce + '//#pure# END dayMapping'.length);
const ctx = {};
vm.runInNewContext(pure, ctx);
failFast('dateKeyInTimeZone non estratta', typeof ctx.dateKeyInTimeZone === 'function');
failFast('daysBetweenIso non estratta', typeof ctx.daysBetweenIso === 'function');
failFast('datasetIndexForDayLabelAt non estratta', typeof ctx.datasetIndexForDayLabelAt === 'function');

// ---------- 1. helper di base ----------
{
  ok('dateKeyInTimeZone(Europe/Rome) a chiaro di mezzogiorno = stesso giorno',
    ctx.dateKeyInTimeZone('Europe/Rome', new Date('2026-09-06T12:00:00Z')) === '2026-09-06',
    ctx.dateKeyInTimeZone('Europe/Rome', new Date('2026-09-06T12:00:00Z')));
  ok('daysBetweenIso(2026-09-05 → 2026-09-06) = 1', ctx.daysBetweenIso('2026-09-05', '2026-09-06') === 1);
  ok('daysBetweenIso(2026-09-05 → 2026-09-07) = 2', ctx.daysBetweenIso('2026-09-05', '2026-09-07') === 2);
  ok('daysBetweenIso data uguale = 0', ctx.daysBetweenIso('2026-09-05', '2026-09-05') === 0);
}

// ---------- 2. TABELLA OBBLIGATORIA (data locale | day0 | indice "Oggi") ----------
const t12 = (iso) => new Date(iso + 'T12:00:00Z'); // mezzogiorno UTC → fermo su Europe/Rome
{
  let r1 = ctx.datasetIndexForDayLabelAt(0, t12('2026-09-05'), '2026-09-05');
  ok('5 sett | day0 5 sett | Oggi → indice 0', r1 === 0, 'idx=' + r1);
  let r2 = ctx.datasetIndexForDayLabelAt(0, t12('2026-09-06'), '2026-09-05');
  ok('6 sett | day0 5 sett | Oggi → indice 1', r2 === 1, 'idx=' + r2);
  let r3 = ctx.datasetIndexForDayLabelAt(0, t12('2026-09-07'), '2026-09-05');
  ok('7 sett | day0 5 sett | Oggi → indice 2', r3 === 2, 'idx=' + r3);
  let r4 = ctx.datasetIndexForDayLabelAt(1, t12('2026-09-06'), '2026-09-05');
  ok('6 sett | day0 5 sett | Domani → indice 2', r4 === 2, 'idx=' + r4);
}

// ---------- 3. giorno non coperto dal dataset (non falsificare) ----------
{
  const stale = ctx.datasetIndexForDayLabelAt(0, t12('2026-09-08'), '2026-09-05');
  ok('8 sett | day0 5 sett | Oggi non coperto → null (NO indice fabbricato)', stale === null, 'idx=' + stale);
  const d3 = ctx.datasetIndexForDayLabelAt(2, t12('2026-09-08'), '2026-09-05');
  ok('dataset scaduto: Domani/Dopodomani fuori range → null', d3 === null, 'idx=' + d3);
  const neg = ctx.datasetIndexForDayLabelAt(0, t12('2026-09-04'), '2026-09-05');
  ok('day0 futuro (oggi prima del dataset): clamp al primo giorno → 0', neg === 0, 'idx=' + neg);
}

// ---------- 4. regressioni strutturali: selectDay e init usano la mappatura ----------
{
  ok('selectDay mappa etichetta → datasetIndexForDayLabel (currentDay = dsIdx)',
    /var dsIdx = datasetIndexForDayLabel\(uiIdx\);/.test(src) && /currentDay = dsIdx;/.test(src));
  ok('selectDay NON assegna più currentDay = dayIdx grezzo', !/currentDay = dayIdx;/.test(src));
  ok('init dataset: currentDay = datasetIndexForDayLabel(0) con fallback giorno0',
    /var initIdx = datasetIndexForDayLabel\(0\);/.test(src) &&
    /currentDay = \(initIdx === null \|\| initIdx === undefined\) \? 0 : initIdx;/.test(src));
  ok('caletta auto-slot "Oggi" confronta con currentDayOffset()',
    /if \(currentDay === currentDayOffset\(\)\)/.test(src));
  ok('DPC usa l indice calendario (currentDay - currentDayOffset())',
    /var calDay = currentDay - currentDayOffset\(\);/.test(src) &&
    /fetchDpcAlerts\(Math\.max\(0, currentDay - currentDayOffset\(\)\)\)/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
