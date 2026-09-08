#!/usr/bin/env node
// TEST 1.0.0.9-HOTFIX3 — RENDERING TEMPORALE DELLE MAPPE (dualAggCache hour-aware).
// Problema: spostando lo slider orario la mappa poligonale restava colorata con i
// colori della PRIMA ora della giornata e la sfumatura restava parzialmente congelata,
// perché getDualMergedAggForZone() chiavava dualAggCache SOLO per provincia+giorno,
// mentre computeAggregateForStore() dipende da currentDay*24+currentHour.
// Contratto imposto: dualAggCache[provinceIdx] = { day, hour, agg, ... }; al cambio di
// currentDay OPPURE currentHour l'aggregate DUAL viene ricalcolato; la stessa coppia
// giorno+ora riusa la cache (no ricalcolo); la chiave della vista 'all' è distinta da
// '0'..'23' (niente collisione max-giorno vs ora reale).
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.1.1.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- estrazione blocco puro (//#pure# BEGIN/END dualCache) ----------
const cb = src.indexOf('//#pure# BEGIN dualCache');
const ce = src.indexOf('//#pure# END dualCache');
failFast('blocco dualCache non trovato', cb >= 0 && ce > cb);
const pure = src.slice(cb, ce + '//#pure# END dualCache'.length);

// Context di simulazione:
//  - currentDay/currentHour sono le variabili che la pagina aggiorna dallo slider;
//  - computeAggregateForStore = aggregato dipendente da (day, hour) come il reale:
//    legge l'array hourly all'indice currentDay*24+currentHour.
//  - mergeModelAnalyses = merge risk-preserving minimale (dominante + disagreement).
const ctx = {};
ctx.console = console;
ctx.computeAggregateForStore = (store) => {
  const idx = (ctx.currentHour === 'all')
    ? ctx.currentDay * 24
    : ctx.currentDay * 24 + parseInt(ctx.currentHour, 10);
  return {
    hval: store.hourly.cape[idx] != null ? store.hourly.cape[idx] : null,
    hour: ctx.currentHour === 'all' ? 'all' : String(ctx.currentHour)
  };
};
ctx.mergeModelAnalyses = (a, b) => {
  if (!b) return Object.assign({}, a, { _dualDominant: 'a', _dualDisagreement: {} });
  const va = (a && a.hval != null) ? a.hval : -1;
  const vb = (b && b.hval != null) ? b.hval : -1;
  return Object.assign({}, a, {
    hval: Math.max(va, vb),
    _dualDominant: va >= vb ? 'a' : 'b',
    _dualDisagreement: {}
  });
};
vm.runInNewContext(pure, ctx);
failFast('dualHourKey non estratta', typeof ctx.dualHourKey === 'function');
failFast('getDualMergedAggForZone non estratta', typeof ctx.getDualMergedAggForZone === 'function');

// ---------- helper: store macchina con hourly di lunghezza 72 ----------
// best_match tag 'a': cape[i] = 100 + i ; ecmwf tag 'b': cape[i] = 600 + i.
// La merge prende il max, quindi l'aggregato DUAL a (day,hour) ha hval = 600 + day*24 + hour.
function storeWith(tag) {
  const cape = [];
  for (let i = 0; i < 72; i++) cape.push(100 + i + (tag === 'b' ? 500 : 0));
  return { _tag: tag, hourly: { cape }, daily: {} };
}
function setTime(day, hour) { ctx.currentDay = day; ctx.currentHour = hour; }

// ---------- 1. TEST POLIGONALE: stessa giornata, ore 00/06/12/18/23 ----------
// Con la vecchia cache (giorno-only) tutte queste chiamate restituivano l'aggregato
// della PRIMA ora → la mappa non cambiava colore. Ora ogni ora (e ogni provincia) ha
// il suo aggregato DUAL.
{
  ctx.dualAggCache = {};                  // simula assembleDualModelStores che resetta
  ctx.dualStoresA = { 0: storeWith('a'), 1: storeWith('a') };
  ctx.dualStoresB = { 0: storeWith('b'), 1: storeWith('b') };
  setTime(1, '0');
  const h0 = ctx.getDualMergedAggForZone(0);
  setTime(1, '6');
  const h6 = ctx.getDualMergedAggForZone(0);
  setTime(1, '12');
  const h12 = ctx.getDualMergedAggForZone(0);
  setTime(1, '18');
  const h18 = ctx.getDualMergedAggForZone(0);
  setTime(1, '23');
  const h23 = ctx.getDualMergedAggForZone(0);
  setTime(1, '0');
  const h0bis = ctx.getDualMergedAggForZone(0);
  setTime(1, '6');
  const z6 = ctx.getDualMergedAggForZone(1);

  ok('ore 00/06/12/18/23 → hval monotoni e indipendenti dall ora di partenza',
    h0.hval === 624 && h6.hval === 630 && h12.hval === 636 && h18.hval === 642 && h23.hval === 647,
    [h0.hval, h6.hval, h12.hval, h18.hval, h23.hval].join('/'));
  ok('hval = 600 + day*24 + hour (coerenza indice/hr)', h6.hval === 600 + 1 * 24 + 6);
  ok('provincia diversa stessa ora → oggetto distinto', z6 !== h6);
  // La cache è UNA entry per provincia che conserva l ultima coppia (day,hour): un
  // ritorno su un ora già vista dopo averne visitate altre RICALCOLA un valore FRESCO
  // (mai il valore della prima ora = bug eliminato). Il valore è sempre coerente.
  ok('ritorno all ora 00 dopo 23 → valore FRESCO di 00 (niente valore della prima visita)',
    h0bis.hval === 624, 'h0bis=' + h0bis.hval);
  ok('cache zona0 taggata con l ULTIMA coppia visitata (day1, hour0 = ritorno)',
    ctx.dualAggCache[0].day === 1 && ctx.dualAggCache[0].hour === '0'
    && ctx.dualAggCache[0].agg === h0bis);
}

// ---------- 2. SFUMATURA (punti virtuali / ancore di provincia) ----------
// Le ancore chiamano getAggregateForZone → dual getDualMergedAggForZone: devono seguire
// l ora corrente come i punti reali (che usano computeAggregateForStore). Con cache
// giorno-only un ancora restava all ora della prima visita.
{
  ctx.dualStoresA[2] = storeWith('a');
  ctx.dualStoresB[2] = storeWith('b');
  setTime(2, '3');
  const aV = ctx.getDualMergedAggForZone(2);
  const aV2 = ctx.getDualMergedAggForZone(2);
  setTime(2, '17');
  const bV = ctx.getDualMergedAggForZone(2);
  ok('ancora segue l ora (03 → 17): hval cambia di +14', bV.hval === aV.hval + 14,
    aV.hval + ' -> ' + bV.hval);
  ok('stessa coppia consecutiva → cache riusata (stessa referenza)', aV2 === aV);
  setTime(2, '3');
  const back3 = ctx.getDualMergedAggForZone(2);
  ok('ancora, ritorno all ora 03 (dopo 17) → valore FRESCO di 03 (non congelato, mai old hour)',
    back3.hval === aV.hval, 'back=' + back3.hval);
}

// ---------- 3. CAMBIO GIORNO 0→1→2→0 (test 3 obbligatorio) ----------
// Il mock ha 72 valori orari (3 giorni), come il dataset reale: giorni validi 0..2.
{
  const rec = {};
  for (let i = 0; i < 3; i++) {
    setTime(i, '9');
    rec[i] = ctx.getDualMergedAggForZone(0);
  }
  ok('giorni diversi → aggregati diversi (cache per giorno)', rec[0].hval !== rec[1].hval
    && rec[1].hval !== rec[2].hval,
    [rec[0].hval, rec[1].hval, rec[2].hval].join('/'));
  setTime(1, '9');
  const back1 = ctx.getDualMergedAggForZone(0);
  ok('giorno 1 rientrato → valore FRESCO del giorno 1 ora 9 (nessuna collisione col giorno 2)',
    back1.hval === rec[1].hval, 'back=' + back1.hval);
  ok('entry zona0 taggata con l ULTIMA coppia visitata (day1, hour9)',
    ctx.dualAggCache[0].day === 1 && ctx.dualAggCache[0].hour === '9');
}

// ---------- 4. Modalità 'all' con chiave distinta da 0..23 ----------
{
  setTime(2, 'all');
  const allAgg = ctx.getDualMergedAggForZone(0);
  const allAgg2 = ctx.getDualMergedAggForZone(0);
  setTime(2, '0');
  const zeroAgg = ctx.getDualMergedAggForZone(0);
  ok('modalita all (hour key distinta) → stessa referenza per la stessa all',
    allAgg2 === allAgg && allAgg.hour === 'all');
  ok('all non collide con ora 00: oggetti e chiavi distinte',
    zeroAgg.hour === '0' && zeroAgg !== allAgg);
  setTime(2, 'all');
  ok('ritorno su all → valore FRESCO di all (hour tag all, mai l ora 00)',
    ctx.getDualMergedAggForZone(0).hour === 'all');
}

// ---------- 5. Dual model: merge su B-only, sole ore, e provincia senza dati ----------
{
  setTime(1, '7');
  ctx.dualAggCache = {};
  const nul = ctx.getDualMergedAggForZone(99);
  ok('provincia senza store → null (niente fabbricazione)', nul === null, 'r=' + nul);
  ctx.dualStoresA[99] = null;
  ctx.dualStoresB[99] = storeWith('b');
  const onlyB = ctx.getDualMergedAggForZone(99);
  ok('solo ecmwf (B) → merge usa B, dominant=b', onlyB._dualDominant === 'b', 'dom=' + onlyB._dualDominant);
  let mono = true, prev = -1;
  for (let h = 0; h < 24; h++) {
    setTime(1, String(h));
    const v = ctx.getDualMergedAggForZone(99).hval;
    if (v <= prev) mono = false;
    prev = v;
  }
  ok('B-only in tutte le 24 ore → hval monotoni (0..23)', mono, 'last=' + prev);
}

// ---------- 6. Struttura sorgente: la cache ha ORA + GIORNO ----------
{
  ok('getDualMergedAggForZone testa GIORNO + ORA (hourKey)',
    /dualAggCache\[i\] && dualAggCache\[i\]\.day === currentDay && dualAggCache\[i\]\.hour === hourKey/.test(src));
  ok('entry cache scrive hour: { day, hour, agg, dominant, disagreement }',
    /dualAggCache\[i\] = \{ day: currentDay, hour: hourKey, agg: merged, dominant: merged\._dualDominant, disagreement: merged\._dualDisagreement \};/.test(src));
  ok('assembleDualModelStores inizializza la cache con hour (dualHourKey)',
    /dualAggCache\[p\] = \{ day: currentDay, hour: dualHourKey\(\)/.test(src));
  ok('dualHourKey definito come helper esplicito', /function dualHourKey\(\)/.test(src));
  ok('guardia "solo giorno" ELIMINATA (sei curioso: non deve riapparire una cache giorno-only)',
    !/day === currentDay\) return dualAggCache\[i\]\.agg;/.test(src));
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);