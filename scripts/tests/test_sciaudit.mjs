#!/usr/bin/env node
// TEST 1.0.0.12-SCIAUDIT — applicazione dei fix dell'AUDIT SCIENTIFICO degli indici
// convettivi (2 CRITICI + 8 IMPORTANTI):
//  - C1: geoToMetres()/geoAGL() + spessore AGL reale dei top 700/500hPa (z03agl/z06agl)
//  - C2: ML-SHIP con cap mediterranei (MR [7,13.6], DLS [7,30]) e lapse rate geometrico
//  - stagionalita' seasonInfo() (checklist Liguria) + convezione elevated in detectVorticosi
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.1.0.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}
// Estrae una funzione dal sorgente HTML con bilanciamento delle parentesi graffe
// (gestisce funzioni annidate senza interrompersi al primo `}` a indentazione bassa).
function extractFn(name) {
  const m0 = src.match(new RegExp('function ' + name + '\\s*\\([^{]{0,200}\\{'));
  failFast('funzione ' + name + ' non estratta', !!m0 && m0.index >= 0);
  const start = src.indexOf('{', m0.index);
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
  failFast('estrazione ' + name + ' non completata', i <= src.length);
  return src.slice(m0.index, i);
}

// ---------- estrazione funzioni pure ----------
const ctx = {};
ctx.console = console;
function run(js) { vm.runInNewContext(js, ctx); }
run(extractFn('geoToMetres'));
run(extractFn('geoAGL'));
run(extractFn('seasonInfo'));
run(extractFn('meanWind'));
run(extractFn('meanWindStrata'));
run(extractFn('levelsAGLTable'));

failFast('geoToMetres non definita', typeof ctx.geoToMetres === 'function');
failFast('geoAGL non definita', typeof ctx.geoAGL === 'function');
failFast('seasonInfo non definita', typeof ctx.seasonInfo === 'function');
failFast('meanWind non definita', typeof ctx.meanWind === 'function');
failFast('meanWindStrata non definita', typeof ctx.meanWindStrata === 'function');
failFast('levelsAGLTable non definita', typeof ctx.levelsAGLTable === 'function');

// ============ C1: geometria delle quote ============
{
  // 500hPa ~5570m -> geometrico ASL (leggermente minore del geopotenziale)
  const g500 = ctx.geoToMetres(5570);
  ok('C1: geoToMetres(5570) ~5560-5575 (geometrico < geopotenziale)', g500 > 5500 && g500 < 5590, g500.toFixed(0));
  // AGL su Alpi (2000m) ben sotto i 6km nominali -> il bias 0-6km viene corretto
  const aglAlpi500 = ctx.geoAGL(5570, 2000);
  ok('C1: geoAGL(500hPa, elev 2000) ~3.5-3.7km (non 6km → bias corretto)', aglAlpi500 > 3000 && aglAlpi500 < 4000, aglAlpi500.toFixed(0));
  // Pianura quasi 6km
  const aglFlat500 = ctx.geoAGL(5570, 100);
  ok('C1: geoAGL(500hPa, elev 100) ~5.4-5.5km', aglFlat500 > 5300 && aglFlat500 < 5600, aglFlat500.toFixed(0));
  ok('C1: computeSevereIndices accetta elevazione (3^ arg) nel sorgente', /function computeSevereIndices\(hourly, idx, terrainElevation\)/.test(src));
  ok('C1: profilo espone z03agl/z06agl annotati', /z06agl: z06agl, z03agl: z03agl/.test(src));
  ok('C1: call site con elevazione in computeDayMaxForStore', /computeSevereIndices\(hourly, ix, elev\)/.test(src));
}

// ============ C2: ML-SHIP con cap mediterranei ============
{
  ok('C2: firma computeHailFromParams accetta terrainElevation', /function computeHailFromParams\([^)]*terrainElevation\)/.test(src));
  ok('C2: cap MR minimo abbassato a 7 (non 11)', /Math\.max\(7\.0, Math\.min\(mr, 13\.6\)\)/.test(src));
  ok('C2: cap DLS alzato a 30 (non 27)', /Math\.max\(7, Math\.min\(dls, 30\)\)/.test(src));
  ok('C2: lapse rate 700-500 GEOMETRICO AGL (geoToMetres)', /geoToMetres\(z700\), h500g = geoToMetres\(z500\)/.test(src));
  ok('C2: indice documentato come ML-SHIP', /ML-SHIP/.test(src));
  ok('C2: cap US rimossi (MR 11, DLS 27 assenti)', !/Math\.max\(11\.0, Math\.min\(mr/.test(src) && !/Math\.min\(dls, 27\)/.test(src));
}

// ============ stagionalita' + convezione elevated ============
{
  // seasonInfo: inverno gennaio -> factor 1.0 (rilassamento pieno)
  const jan = ctx.seasonInfo({ time: ['2026-01-15T12:00'] }, 0);
  ok('stag: gennaio = inverno hslcFactor 1.0', jan.winter && jan.hslcFactor === 1.0, JSON.stringify(jan));
  // estate luglio -> factor 0 (soglie standard)
  const jul = ctx.seasonInfo({ time: ['2026-07-15T12:00'] }, 0);
  ok('stag: luglio = estate hslcFactor 0', !jul.winter && jul.hslcFactor === 0, JSON.stringify(jul));
  // spalle ottobre -> factor 0.5
  const oct = ctx.seasonInfo({ time: ['2026-10-05T12:00'] }, 0);
  ok('stag: ottobre = spalle hslcFactor 0.5', oct.shoulder && oct.hslcFactor === 0.5, JSON.stringify(oct));
  // nessun dato -> default neutro (estate)
  const nod = ctx.seasonInfo({}, 0);
  ok('stag: nessun dato = default neutro (hslcFactor 0)', nod.hslcFactor === 0, JSON.stringify(nod));

  ok('stag: detectVorticosi usa seasonInfo (gate LCL ammorbidito in freddo)', /seasonInfo\(hourly, idx\)/.test(src));
  ok('stag: LCL gate 2500 in stagione fredda / 1500 in estate', /lcl <= \(cold \? 2500 : 1500\)/.test(src));
  ok('stag: CAPE threshold HSLC rilassato in freddo', /cape >= \(cold \? 400 : 500\)/.test(src));
  ok('stag: getRiskProfileNew abbassa il floor CAPE a 150 in freddo', /coldCapFloor = \(seasonInfo\(hourly, idx\)\.hslcFactor > 0\) \? 150 : 200;/.test(src));
}

// ============ MIGLIORAMENTO (1.0.0.13, i 14 🟡) ============
{
  // mean-wind Bunkers INTEGRALE per strato (§4.4/§8.5): pesato per spessore geometrico,
  // non media aritmetica -> un livello alto (spessore maggiore) conta di più del centroide.
  // livelli uniformi (3 livelli equidistanti) => media integrale == media aritmetica.
  const flat = ctx.meanWindStrata([[0,0],[10,0],[20,0]], [0,1000,2000]);
  ok('migl: meanWindStrata uniforme ~ media aritmetica (10)', Math.abs(flat[0]-10) < 0.01, flat[0].toFixed(3));
  // strato alto molto più spesso -> il centroide integrale spinge verso l'alto
  const thick = ctx.meanWindStrata([[0,0],[10,0],[200,0]], [0,100,9000]);
  ok('migl: meanWindStrata pesa lo strato spesso in alto (media integrale > aritmetica)', thick[0] > 10 + 5, thick[0].toFixed(1));
  // fallback a media aritmetica quando mancano le quote (z null)
  const fb = ctx.meanWindStrata([[0,0],[10,0],[20,0]], [0,null,null]);
  ok('migl: meanWindStrata fallback a media aritmetica quando mancano le quote', Math.abs(fb[0]-10) < 0.01, fb[0].toFixed(3));

  // levelsAGLTable: interpola in log-p tra gli anchor 850/700/500 e la superficie.
  const tab = ctx.levelsAGLTable(1500, 3020, 5570, 200); // elevazione 200m
  ok('migl: levelsAGLTable 700hPa AGL ~ 2.8-3.0km', tab[700] > 2800 && tab[700] < 3100, 'z700='+tab[700].toFixed(0));
  ok('migl: levelsAGLTable 500hPa AGL ~ 5.3-5.5km', tab[500] > 5200 && tab[500] < 5600, 'z500='+tab[500].toFixed(0));
  ok('migl: levelsAGLTable superficie (1000hPa) a 0', tab[1000] === 0);
  ok('migl: levelsAGLTable 850hPa AGL ~ 1.2-1.4km', tab[850] > 1200 && tab[850] < 1450, 'z850='+tab[850].toFixed(0));
  // 600hPa è a quota PIÙ BASSA di 500hPa ma SOPRA 700hPa
  ok('migl: levelsAGLTable 600hPa tra 700 e 500', tab[600] != null && tab[600] > tab[700] && tab[600] < tab[500], 'z600='+tab[600].toFixed(0));
  // 925hPa è a quota PIÙ BASSA dell'850 (vicino al suolo)
  ok('migl: levelsAGLTable 925hPa tra superficie e 850', tab[925] != null && tab[925] > 0 && tab[925] < tab[850], 'z925='+tab[925].toFixed(0));
  // ordine altimetrico monotono rispettato
  ok('migl: levelsAGLTable ordine altimetrico monotono (925<850<700<500)', tab[925] < tab[850] && tab[850] < tab[700] && tab[700] < tab[500]);

  // Shear 0-1km e SRH 0-1km esposti dal profilo (§5.6/§8.6)
  ok('migl: computeSevereIndices espone shear01 e srh1', /shear01: shear01, srh1: srh1/.test(src));
  ok('migl: detectVorticosi legge shear01/srh1 dal profilo', /var shear01 = si\.shear01 \?\? 0;/.test(src));
  ok('migl: canale low-level 0-1km in detectVorticosi', /lowLevelStrong = \(shear01 >= 10 && srh1 >= 90\)/.test(src));

  // SRH 0-3km allineato alle basi liguri (§6)
  ok('migl: SRH Liguria bands in escalazione vorticosi', /srh3 >= \(cold \? 250 : 300\)/.test(src));
  ok('migl: Rilascio SRH>=150 come livello 2', /srh3 >= 150\) out\.level = 2/.test(src));

  // documentazione: downburst Mediterraneo-sperimentale + melting gate vs Po Valley 2023
  ok('migl: downburst documentato Mediterranean/experimental (§4.9)', /"sperimentale\/Mediterraneo"/.test(src));
  ok('migl: melting gate documentato vs Po Valley 2023 (§6/§8.7)', /classe large\/very-large resta intatta/.test(src));
}

console.log(failures === 0 ? '\nRESULT: PASS' : `\nRESULT: FAIL (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);
