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
const HTML = path.join(ROOT, 'mri-light-1.0.1.2.html');
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
  ok('day0 futuro (oggi prima del dataset): Oggi NON coperto → null (no clamp 0, no fabbricazione)',
    neg === null, 'idx=' + neg);
  const negD1 = ctx.datasetIndexForDayLabelAt(1, t12('2026-09-04'), '2026-09-05');
  ok('day0 futuro: Domani → primo giorno del dataset (indice 0 = giorno di calendario domani)',
    negD1 === 0, 'idx=' + negD1);
  const negD2 = ctx.datasetIndexForDayLabelAt(2, t12('2026-09-04'), '2026-09-05');
  ok('day0 futuro: Dopodomani → indice 1 (= giorno di calendario dopodomani)', negD2 === 1, 'idx=' + negD2);
}

// ---------- 3b. REGRESSIONE SCARTO +1 (oggi→domani, domani→dopodomani) ----------
{
  // Dataset che parte da DOMANI rispetto a oggi (day0 successivo): con il vecchio
  // clamp "Oggi" = primo giorno (domani) e "Domani" = secondo (dopodomani) = esattamente
  // lo scarto segnalato online. Ora: Oggi → null (messaggio disponibilità), e
  // Domani/Dopodomani ricadono ESATTAMENTE sui giorni di calendario richiesti.
  const oggi7 = t12('2026-09-07');
  const day0Domani = '2026-09-08';
  const o = ctx.datasetIndexForDayLabelAt(0, oggi7, day0Domani);
  ok('day0=08 now=07: Oggi → null (non il clamp "Oggi=domani")', o === null, 'idx=' + o);
  const dm = ctx.datasetIndexForDayLabelAt(1, oggi7, day0Domani);
  ok('day0=08 now=07: Domani → indice 0 = 2026-09-08 (domani vero)', dm === 0, 'idx=' + dm);
  const dp = ctx.datasetIndexForDayLabelAt(2, oggi7, day0Domani);
  ok('day0=08 now=07: Dopodomani → indice 1 = 2026-09-09 (dopodomani vero)', dp === 1, 'idx=' + dp);
  ok('nessun clamp: /if \(off < 0\) off = 0;/ ASSENTE nel sorgente', !/if \(off < 0\) off = 0;/.test(src));
  ok('currentDayOffset NON clampa negativi', !/off < 0 \? 0 : off;/.test(src));
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

// ---------- 5. ALLINEAMENTO METADATA↔PAYLOAD (scarto +1/slider da cache mista) ----------
// Le due fetch indipendenti potevano servire generazioni diverse di metadata.json (day0)
// e meteorisk-points.json (dati): con metadata di una generazione (day0 più vecchio) e
// payload di un'altra si riproduce "Oggi"→"Domani". Contratto: UNA generazione possiede
// tutti gli array → metadata sempre no-store (fonte di verità) + payload con URL
// cache-busted legato a generated_at; difese: bounds-check reale in updateUI e
// try/catch nello slider.
{
  ok('datasetFetchJson accetta opzioni di fetch',
    /async function datasetFetchJson\(url, opts\)/.test(src));
  ok('metadata.json caricato con cache no-store',
    /datasetFetchJson\(DATASET_PREFIX \+ 'metadata\.json', \{ cache: 'no-store' \}\)/ .test(src));
  ok('payload cache-bustato con ?v= generato da generated_at',
    /meteorisk-points\.json\?v=' \+ encodeURIComponent\(meta\.generated_at \|\| meta\.day0 \|\| ''\)/ .test(src));
  ok('updateUI: guardia corrente su currentDay (_dailyLen)',
    /if \(_dailyLen > 0 && \(currentDay < 0 \|\| currentDay >= _dailyLen\)\)/.test(src));
  ok('updateUI: guardia corrente sull ora (_hourlyLen)',
    /const _hourIdx = currentDay \* 24 \+ \(currentHour !== 'all' \? parseInt\(currentHour, 10\) : 23\);/.test(src) &&
    /if \(_hourlyLen > 0 && _hourIdx >= _hourlyLen\)/.test(src));
  ok('updateUI: messaggio onesto in status-msg quando non allineato',
    /'Giorno non disponibile nei dati caricati \(dataset non allineato\) — aggiorna la pagina\.'/.test(src) &&
    /'Ora non disponibile nei dati caricati \(dataset non allineato\) — aggiorna la pagina\.'/.test(src));
  ok('selectHourFromSlider protetto da try/catch con status-msg',
    /function selectHourFromSlider\(value\) \{/.test(src) &&
    /catch \(err\) \{/.test(src) &&
    /'Aggiornamento orario non riuscito: ' \+ err\.message/.test(src));
  // Scenario semantico: generazione mista vecchio meta + nuovo payload ⇒ indice sbagliato.
  // Con la nuova mappatura il clamo non c'è più; riproduciamo il caso "meta giovane + payload
  // vecchio di 1 ciclo" (oggi=07, meta.day0=08, payload ha ancora 08/09/10 → coerente per
  // costruzione grazie a ?v: il payload "vecchio" non può più essere riusato da un URL nuovo).
  {
    const oggi7 = t12('2026-09-07');
    const coerente = ctx.datasetIndexForDayLabelAt(0, oggi7, '2026-09-08');
    ok('base semantica: meta day0=08 + oggi 07 ⇒ Oggi null (niente fabbricazione)',
      coerente === null, 'idx=' + coerente);
  }
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
