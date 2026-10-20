#!/usr/bin/env node
// TEST 1.2.4.11 — SINCRONIA PALETTE CLIENT <-> RENDERER (precipitazione, geopotenziale, pressure_msl, vento).
// Guardia contro la divergenza: le scale definite nel CLIENT (mri-light-1.2.4.11.html) devono
// essere IDENTICHE a quelle pubblicate dal renderer in scripts/forecast_v2/color_scales.json:
//   - precipitation / rain / showers (breakpoints + colori light/dark);
//   - precipitation_cum (cumulata dal riferimento: 21 bande 0..300 mm tarate sulla
//     distribuzione misurata, banda [0,1) trasparente, coda estesa al violetto scuro);
//   - geopotential_height_500hPa / 850hPa / 1000hPa (palette Meteociel, 31 bande, alpha 1.0);
//   - pressure_msl (breakpoints invariati; SOLO la variante light cambia all'indice 10);
//   - wind_speed_10m / wind_speed_850hPa (rampa SEQUENZIALE `wind`, 21 bande 0..40 m/s,
//     prima banda alpha 0) e wind_speed_300hPa (stessa rampa ri-spaziata 0..100 m/s);
//   - wind_direction_* NON ha scala (ne' client ne' renderer): la direzione e' pubblicata
//     solo per il reader, che per `_(?:speed|direction)_` pretende la coppia speed+direction
//     (senza la secondaria il reader LANCIA); il renderer la salta, nessun WebP;
//   - wind_u_component_* / wind_v_component_* NON sono piu' pubblicate (INPUT interni del
//     motore): non devono esistere ne' nel JSON ne' nel client;
//   - dew_point_2m / relative_humidity_700hPa: NON hanno override client, quindi il JSON
//     deve esserne la copia VERBATIM della scala che la libreria risolve per prefisso
//     (altrimenti mappa e legenda divergono).
// Tutte le scale che differiscono da un default della libreria devono essere INIETTATE nel
// registro colorScales (FM_COLOR_SCALE_OVERRIDES -> defaultOmProtocolSettings) e passate alle
// DUE chiamate getColorScale (raster/legenda + colore isolinee). Inoltre verifica le proprieta'
// delle scale (monotonia, cardinalita', alpha, duplicati) e che il passo delle isobare
// dell'overlay MSL sia 5. La sezione H2 e' la PROVA DI SENSIBILITA': mutando in memoria un
// breakpoint/colore/alpha il comparatore deve FALLIRE (altrimenti il test e' un falso verde).
// LIMITI: statico sul sorgente + sulle costanti; NON esegue il rendering in browser.
'use strict';

import fs from 'fs';
import path from 'path';
import vm from 'vm';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.2.4.11.html');
const JSON_PATH = path.join(ROOT, 'scripts', 'forecast_v2', 'color_scales.json');
const src = fs.readFileSync(HTML, 'utf8');
const scales = JSON.parse(fs.readFileSync(JSON_PATH, 'utf8')).scales;

let failures = 0;
let checks = 0;
function ok(label, cond, detail = '') {
  checks++;
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}
function failFast(label, cond) {
  if (!cond) { console.error(`FATAL: ${label}`); process.exit(1); }
}

// ---------- ESTRAZIONE COSTANTI JS DALL'HTML ----------
// I letterali terminano con `];` (le sottoliste interne con `],`), quindi il match
// non-greedy su `\];` individua l'intero array anche quando e' annidato.
function extractArrayLiteral(name) {
  const re = new RegExp('var\\s+' + name + '\\s*=\\s*(\\[[\\s\\S]*?\\]);');
  const m = src.match(re);
  if (!m) return null;
  try { return vm.runInNewContext('(' + m[1] + ')'); } catch (e) { return null; }
}
function extractUnit() {
  const m = src.match(/var\s+FM_PRECIP_COLOR_SCALE\s*=\s*\{[\s\S]*?\bunit:\s*'([^']*)'/);
  return m ? m[1] : null;
}

const jsBreakpoints = extractArrayLiteral('FM_PRECIP_BREAKPOINTS');
const jsColors = extractArrayLiteral('FM_PRECIP_COLORS');
const jsUnit = extractUnit();
failFast('FM_PRECIP_BREAKPOINTS non estratta dall\'HTML', Array.isArray(jsBreakpoints));
failFast('FM_PRECIP_COLORS non estratta dall\'HTML', Array.isArray(jsColors));
failFast('unita\' della scala JS non estratta', typeof jsUnit === 'string');

const NAMES = ['precipitation', 'rain', 'showers'];
const eq = (a, b) => JSON.stringify(a) === JSON.stringify(b);

// ---------- A. CONFRONTO CON IL JSON DEL RENDERER ----------
for (const name of NAMES) {
  const s = scales[name];
  ok(`A: "${name}" presente nel color_scales.json (light+dark)`, !!(s && s.light && s.dark));
  if (!s || !s.light || !s.dark) continue;

  ok(`A: "${name}" unit JSON == unit JS ("${jsUnit}")`, s.light.unit === jsUnit && s.dark.unit === jsUnit,
    `json light=${s.light.unit} / dark=${s.dark.unit}`);
  ok(`A: "${name}" breakpoints JS == JSON (light)`, eq(jsBreakpoints, s.light.breakpoints),
    `js=${jsBreakpoints ? jsBreakpoints.length : '?'} json=${s.light.breakpoints.length}`);
  ok(`A: "${name}" breakpoints JS == JSON (dark)`, eq(jsBreakpoints, s.dark.breakpoints));
  ok(`A: "${name}" colori JS == JSON (light)`, eq(jsColors, s.light.colors));
  ok(`A: "${name}" colori JS == JSON (dark)`, eq(jsColors, s.dark.colors));
  ok(`A: "${name}" light == dark (palette identica)`, eq(s.light.breakpoints, s.dark.breakpoints) && eq(s.light.colors, s.dark.colors));
}

// Le tre variabili devono essere la stessa scala (come richiesto).
ok('A: precipitation == rain == showers (JSON)', eq(scales.precipitation, scales.rain) && eq(scales.precipitation, scales.showers));

// ---------- B. PROPRIETA' DELLA SCALA ----------
ok('B: breakpoints strettamente crescenti', jsBreakpoints.every((v, i) => i === 0 || v > jsBreakpoints[i - 1]));
ok('B: len(colors) == len(breakpoints)', jsColors.length === jsBreakpoints.length,
  `colors=${jsColors.length} breakpoints=${jsBreakpoints.length}`);
ok('B: ogni colore e\' RGBA (len 4, canali 0..255, alpha 0..1)',
  jsColors.every((c) => Array.isArray(c) && c.length === 4 &&
    c.slice(0, 3).every((x) => Number.isInteger(x) && x >= 0 && x <= 255) &&
    typeof c[3] === 'number' && c[3] >= 0 && c[3] <= 1));
ok('B: alpha 0 SOLO sulla prima banda',
  jsColors.filter((c) => c[3] === 0).length === 1 && jsColors[0][3] === 0);
ok('B: nessun colore identico al precedente',
  jsColors.every((c, i) => i === 0 || !eq(c, jsColors[i - 1])));

// ---------- C. INIEZIONE NEL REGISTRO DELLA LIBRERIA (statico) ----------
ok('C: FM_COLOR_SCALE_OVERRIDES definita', /var\s+FM_COLOR_SCALE_OVERRIDES\s*=\s*\{/.test(src));
{
  // Il corpo dell'oggetto override (nessuna graffa annidata: i valori sono identificatori).
  // Si estrae e si verificano le singole chiavi, cosi' l'ordine delle chiavi non conta e
  // `precipitation_cum:` NON viene confuso con `precipitation:` (dopo "precipitation" in
  // "precipitation_cum" c'e' "_", non ":").
  const ovMatch = src.match(/var\s+FM_COLOR_SCALE_OVERRIDES\s*=\s*\{([^}]*)\}/);
  const ovBody = ovMatch ? ovMatch[1] : '';
  ok('B/A: override per precipitation, rain, showers presenti',
    /(^|[\s,{])precipitation:\s*FM_PRECIP_COLOR_SCALE/.test(ovBody) &&
    /(^|[\s,{])rain:\s*FM_PRECIP_COLOR_SCALE/.test(ovBody) &&
    /(^|[\s,{])showers:\s*FM_PRECIP_COLOR_SCALE/.test(ovBody), ovBody.trim().slice(0, 90));
}
ok('C: registro unificato = defaultOmProtocolSettings.colorScales + override',
  /Object\.assign\(\{\},\s*baseScales,\s*FM_COLOR_SCALE_OVERRIDES\)/.test(src) &&
  /defaultOmProtocolSettings\.colorScales\s*=\s*fmColorScales/.test(src));
ok('B: var fmColorScales dichiarata', /var\s+fmColorScales\s*=\s*null/.test(src));

// Le DUE chiamate getColorScale devono passare il terzo argomento (con fallback a 2).
const threeArg = (src.match(/getColorScale\([^)]*,\s*[^,)]*,\s*fmColorScales\s*\)/g) || []).length;
ok('B: entrambe le getColorScale passano fmColorScales (3 argomenti)', threeArg === 2, `trovate=${threeArg}`);
ok('B: fallback a 2 argomenti se fmColorScales non pronta',
  /fmColorScales\s*\?\s*fmModule\.getColorScale\([^)]*fmColorScales\s*\)\s*:\s*fmModule\.getColorScale\([^)]*\)/.test(src));

// ---------- C2. LEGENDA: BANDE DI UGUALE LARGHEZZA + PASSO TEMPORALE ----------
ok('C2: legenda a bande di uguale larghezza (bandW = 100/n)',
  /var\s+bandW\s*=\s*100\s*\/\s*n/.test(src) && /left:\s*'\s*\+\s*leftPct/.test(src));
ok('C2: helper fmValidTimeStepHours presente (dai valid_times)',
  /function\s+fmValidTimeStepHours\s*\(/.test(src) && /fmMeta\.valid_times/.test(src));
ok('C2: passo temporale mostrato solo per precipitation/rain/showers',
  /fmVariable === 'precipitation' \|\| fmVariable === 'rain' \|\| fmVariable === 'showers'/.test(src));

// ---------- D. PALETTE GEOPOTENZIALE (Meteociel) — 3 scale, 31 bande ----------
// Baseline indipendente (scala Meteociel, PALETTE_TARGET 1.2.4.11): Z500 = 4920..6120 m
// passo 40 m; Z850 e Z1000 senza riferimento Meteociel -> stesso passo, range scelto sui
// dati misurati (700..1900 e -800..400). I 31 colori sono CONDIVISI dalle tre scale.
const jsGeopColors = extractArrayLiteral('FM_GEOP_COLORS');
const jsGeop500 = extractArrayLiteral('FM_GEOP_BREAKPOINTS_500');
const jsGeop850 = extractArrayLiteral('FM_GEOP_BREAKPOINTS_850');
const jsGeop1000 = extractArrayLiteral('FM_GEOP_BREAKPOINTS_1000');
failFast('FM_GEOP_COLORS non estratta dall\'HTML', Array.isArray(jsGeopColors));
failFast('FM_GEOP_BREAKPOINTS_500/850/1000 non estratte dall\'HTML',
  Array.isArray(jsGeop500) && Array.isArray(jsGeop850) && Array.isArray(jsGeop1000));

function extractUnitOfScale(scaleName) {
  const re = new RegExp('var\\s+' + scaleName + '\\s*=\\s*\\{[\\s\\S]*?\\bunit:\\s*\'([^\']*)\'');
  const m = src.match(re);
  return m ? m[1] : null;
}
const range = (start, step, n) => Array.from({ length: n }, (_, i) => start + step * i);

ok('D: Z500 breakpoints = 4920..6120 passo 40 (31 bande, scala Meteociel)',
  eq(jsGeop500, range(4920, 40, 31)));
ok('D: Z850 breakpoints = 700..1900 passo 40 (31 bande)',
  eq(jsGeop850, range(700, 40, 31)));
ok('D: Z1000 breakpoints = -800..400 passo 40 (31 bande)',
  eq(jsGeop1000, range(-800, 40, 31)));
ok('D: 31 colori geop', jsGeopColors.length === 31, `colors=${jsGeopColors.length}`);
ok('D: ogni colore geop e\' RGBA (canali 0..255, alpha == 1.0)',
  jsGeopColors.every((c) => Array.isArray(c) && c.length === 4 &&
    c.slice(0, 3).every((x) => Number.isInteger(x) && x >= 0 && x <= 255) && c[3] === 1));
ok('D: nessun colore geop identico al precedente',
  jsGeopColors.every((c, i) => i === 0 || !eq(c, jsGeopColors[i - 1])));
for (const [nm, bp] of [['500', jsGeop500], ['850', jsGeop850], ['1000', jsGeop1000]]) {
  ok(`D: Z${nm} breakpoints strettamente crescenti`,
    bp.every((v, i) => i === 0 || v > bp[i - 1]));
  ok(`D: Z${nm} len(colors) == len(breakpoints) == 31`,
    bp.length === jsGeopColors.length && jsGeopColors.length === 31,
    `colors=${jsGeopColors.length} breakpoints=${bp.length}`);
}
ok('D: le 3 scale geop usano GLI STESSI colori (light == dark == FM_GEOP_COLORS)',
  (src.match(/colors:\s*\{\s*light:\s*FM_GEOP_COLORS,\s*dark:\s*FM_GEOP_COLORS\s*\}/g) || []).length === 3);
ok('D: unita\' "m" per le 3 scale geop',
  extractUnitOfScale('FM_GEOP_500_COLOR_SCALE') === 'm' &&
  extractUnitOfScale('FM_GEOP_850_COLOR_SCALE') === 'm' &&
  extractUnitOfScale('FM_GEOP_1000_COLOR_SCALE') === 'm');
ok('D: FM_COLOR_SCALE_OVERRIDES contiene le 3 chiavi geop',
  /geopotential_height_500hPa:\s*FM_GEOP_500_COLOR_SCALE/.test(src) &&
  /geopotential_height_850hPa:\s*FM_GEOP_850_COLOR_SCALE/.test(src) &&
  /geopotential_height_1000hPa:\s*FM_GEOP_1000_COLOR_SCALE/.test(src));

// ---------- E. pressure_msl — SOLO variante light, banda [1010,1016) = indice 10 ----------
const jsMslBp = extractArrayLiteral('FM_MSL_BREAKPOINTS');
const jsMslLight = extractArrayLiteral('FM_MSL_COLORS_LIGHT');
const jsMslDark = extractArrayLiteral('FM_MSL_COLORS_DARK');
failFast('FM_MSL_BREAKPOINTS / FM_MSL_COLORS_LIGHT / FM_MSL_COLORS_DARK non estratte',
  Array.isArray(jsMslBp) && Array.isArray(jsMslLight) && Array.isArray(jsMslDark));
ok('E: pressure_msl breakpoints invariati (940..1060, 18 bande)',
  eq(jsMslBp, [940, 950, 960, 970, 980, 986, 990, 996, 1000, 1006, 1010, 1016, 1020, 1026, 1030, 1040, 1050, 1060]));
ok('E: light/dark hanno 18 colori ciascuna', jsMslLight.length === 18 && jsMslDark.length === 18,
  `light=${jsMslLight.length} dark=${jsMslDark.length}`);
ok('E: LIGHT indice 10 == [210,210,215,1] (il bianco sostituito)',
  eq(jsMslLight[10], [210, 210, 215, 1]), JSON.stringify(jsMslLight[10]));
ok('E: DARK indice 10 == bianco [255,255,255,1] (invariato)',
  eq(jsMslDark[10], [255, 255, 255, 1]), JSON.stringify(jsMslDark[10]));
ok('E: light e dark differiscono SOLO all\'indice 10 (tutti gli altri 17 invariati)',
  jsMslLight.every((c, i) => i === 10 || eq(c, jsMslDark[i])) &&
  jsMslLight.filter((c, i) => !eq(c, jsMslDark[i])).length === 1);
ok('E: FM_MSL_COLOR_SCALE risolve la variante tema via colors {light,dark}',
  /FM_MSL_COLOR_SCALE\s*=\s*\{[\s\S]*?breakpoints:\s*FM_MSL_BREAKPOINTS,\s*colors:\s*\{\s*light:\s*FM_MSL_COLORS_LIGHT,\s*dark:\s*FM_MSL_COLORS_DARK\s*\}/.test(src));
ok('E: FM_COLOR_SCALE_OVERRIDES contiene pressure_msl', /pressure_msl:\s*FM_MSL_COLOR_SCALE/.test(src));

// ---------- F. SINCRONIA CLIENT <-> RENDERER per geop + pressure_msl ----------
// (stessa forma del controllo precipitazione: costanti JS dell'HTML vs color_scales.json)
const jsonGeop500 = scales.geopotential_height_500hPa;
const jsonGeop850 = scales.geopotential_height_850hPa;
const jsonGeop1000 = scales.geopotential_height_1000hPa;
for (const [nm, jsc, jsbp, jscl] of [
  ['geopotential_height_500hPa', jsonGeop500, jsGeop500, jsGeopColors],
  ['geopotential_height_850hPa', jsonGeop850, jsGeop850, jsGeopColors],
  ['geopotential_height_1000hPa', jsonGeop1000, jsGeop1000, jsGeopColors],
]) {
  ok(`F: ${nm} presente in color_scales.json (light+dark)`,
    !!(jsc && jsc.light && jsc.dark));
  if (!jsc || !jsc.light || !jsc.dark) continue;
  ok(`F: ${nm} breakpoints JS == JSON (light)`, eq(jsbp, jsc.light.breakpoints),
    `js=${jsbp.length} json=${jsc.light.breakpoints.length}`);
  ok(`F: ${nm} breakpoints JS == JSON (dark)`, eq(jsbp, jsc.dark.breakpoints));
  ok(`F: ${nm} colori JS == JSON (light)`, eq(jscl, jsc.light.colors));
  ok(`F: ${nm} colori JS == JSON (dark)`, eq(jscl, jsc.dark.colors));
  ok(`F: ${nm} light == dark (palette identica)`,
    eq(jsc.light.breakpoints, jsc.dark.breakpoints) && eq(jsc.light.colors, jsc.dark.colors));
}
const jsonMsl = scales.pressure_msl;
ok('F: pressure_msl presente in color_scales.json (light+dark)', !!(jsonMsl && jsonMsl.light && jsonMsl.dark));
if (jsonMsl && jsonMsl.light && jsonMsl.dark) {
  ok('F: pressure_msl breakpoints JS == JSON (light)', eq(jsMslBp, jsonMsl.light.breakpoints));
  ok('F: pressure_msl breakpoints JS == JSON (dark)', eq(jsMslBp, jsonMsl.dark.breakpoints));
  ok('F: pressure_msl colori LIGHT JS == JSON', eq(jsMslLight, jsonMsl.light.colors));
  ok('F: pressure_msl colori DARK JS == JSON', eq(jsMslDark, jsonMsl.dark.colors));
}

// ---------- G. ISOBARE OVERLAY MSL: passo 5 hPa ----------
ok('G: fmVectorUrl dell\'overlay MSL forzato con passo 5',
  /fmVectorUrl\(\s*overlayBase\s*,\s*fmOverlayVariable\s*,\s*true\s*,\s*5\s*\)/.test(src));
ok('G: nessun residuo del passo 4 nell\'overlay MSL',
  !/fmVectorUrl\(\s*overlayBase\s*,\s*fmOverlayVariable\s*,\s*true\s*,\s*4\s*\)/.test(src));
ok('G: label di legenda "Isobare MSL (hPa) · passo 5 hPa"',
  /Isobare MSL \(hPa\)[\s\S]{0,4}passo 5 hPa/.test(src) && !/passo 4 hPa/.test(src));
ok('G: commento aggiornato a "intervallo sinottico 5 hPa"',
  /intervallo sinottico 5 hPa/.test(src) && !/intervallo sinottico 4 hPa/.test(src));
ok('G: fmContourInterval NON toccato (passo isolinee generiche resta base 2 per pressure_msl)',
  /else if \(\/pressure_msl\|surface_pressure\/\.test\(v\)\) base = 2;/.test(src));

// ---------- H. VENTO: TRE VELOCITA' PUBBLICATE, NESSUNA COMPONENTE, NESSUNA DIREZIONE ----------
// 1.2.4.11: il motore NON pubblica piu' wind_u_component_10m / wind_v_component_10m (restano
// INPUT interni, letti solo per derivare). Per 10 m, 850 hPa e 300 hPa pubblica la COPPIA
// speed+direction: la direzione serve al reader della libreria (per `_(?:speed|direction)_`
// getSourceVars('wind_speed_X') pretende la secondaria `wind_direction_X`, altrimenti LANCIA
// "Secondary variable ... not found"), NON al disegno -> nessuna scala `wind_direction_*`,
// quindi il renderer la salta e non produce WebP inutili.
// Le tre VELOCITA' hanno una scala: 10 m e 850 hPa usano la rampa SEQUENZIALE `wind` della
// libreria (21 bande 0..40 m/s, prima banda alpha 0) replicata in FM_WIND_COLOR_SCALE; il
// getto (300 hPa) usa la stessa rampa ri-spaziata su 0..100 m/s (FM_JET_COLOR_SCALE).
// Come per precipitazione e geopotenziale, le costanti del CLIENT devono coincidere con
// color_scales.json (breakpoints + colori light + dark) e le tre chiavi devono stare in
// FM_COLOR_SCALE_OVERRIDES, altrimenti mappa (renderer) e legenda (client) divergono.
const jsWindBp = extractArrayLiteral('FM_WIND_BREAKPOINTS');
const jsWindColors = extractArrayLiteral('FM_WIND_COLORS');
const jsJetBp = extractArrayLiteral('FM_JET_BREAKPOINTS');
const jsJetColors = extractArrayLiteral('FM_JET_COLORS');
failFast('FM_WIND_BREAKPOINTS non estratta dall\'HTML', Array.isArray(jsWindBp));
failFast('FM_WIND_COLORS non estratta dall\'HTML', Array.isArray(jsWindColors));
failFast('FM_JET_BREAKPOINTS non estratta dall\'HTML', Array.isArray(jsJetBp));
failFast('FM_JET_COLORS non estratta dall\'HTML', Array.isArray(jsJetColors));

// Comparatore RIUSABILE (stessa logica dei controlli A/F: costanti JS vs entry JSON).
// La prova di sensitivita' in H8 lo esercita su una copia MUTATA di ciascuna delle 3 scale.
function scaleMatchesJson(mutBp, mutColors, jsonEntry) {
  if (!jsonEntry || !jsonEntry.light || !jsonEntry.dark) return false;
  return eq(mutBp, jsonEntry.light.breakpoints) && eq(mutBp, jsonEntry.dark.breakpoints) &&
    eq(mutColors, jsonEntry.light.colors) && eq(mutColors, jsonEntry.dark.colors);
}

// H1. CLIENT <-> JSON: le TRE velocita'.
for (const [nm, jsc, jsbp, jscl] of [
  ['wind_speed_10m', scales.wind_speed_10m, jsWindBp, jsWindColors],
  ['wind_speed_850hPa', scales.wind_speed_850hPa, jsWindBp, jsWindColors],
  ['wind_speed_300hPa', scales.wind_speed_300hPa, jsJetBp, jsJetColors],
]) {
  ok(`H: ${nm} presente in color_scales.json (light+dark)`, !!(jsc && jsc.light && jsc.dark));
  ok(`H: ${nm} breakpoints + colori JS == JSON (light e dark)`,
    scaleMatchesJson(jsbp, jscl, jsc),
    `bp=${jsbp.length} colors=${jscl.length}`);
}

// H2. Proprieta' della scala delle velocita' a 10 m / 850 hPa: 21 bande 0..40 m/s.
ok('H: wind_speed_10m/850hPa = 21 bande, estremi 0..40 m/s (rampa `wind` della libreria)',
  jsWindBp.length === 21 && jsWindBp[0] === 0 && jsWindBp[20] === 40 && jsWindColors.length === 21,
  JSON.stringify(jsWindBp));
ok('H: wind_speed_10m/850hPa = breakpoints strettamente crescenti',
  jsWindBp.every((v, i) => i === 0 || v > jsWindBp[i - 1]));
ok('H: wind_speed_10m/850hPa = alpha 0 SOLO sulla prima banda (calma trasparente)',
  jsWindColors.filter((c) => c[3] === 0).length === 1 && jsWindColors[0][3] === 0);
ok('H: wind_speed_10m/850hPa = unita\' "m/s", light == dark',
  extractUnitOfScale('FM_WIND_COLOR_SCALE') === 'm/s' &&
  /FM_WIND_COLOR_SCALE\s*=\s*\{[\s\S]*?colors:\s*\{\s*light:\s*FM_WIND_COLORS,\s*dark:\s*FM_WIND_COLORS\s*\}/.test(src));

// H3. Proprieta' della scala del getto: stessa rampa, range esteso 0..100.
ok('H: wind_speed_300hPa = 21 bande, estremi 0..100 m/s',
  jsJetBp.length === 21 && jsJetBp[0] === 0 && jsJetBp[20] === 100, JSON.stringify(jsJetBp));
ok('H: wind_speed_300hPa = breakpoints strettamente crescenti',
  jsJetBp.every((v, i) => i === 0 || v > jsJetBp[i - 1]));
ok('H: wind_speed_300hPa riusa la rampa `wind` (stessi colori di wind_gusts_10m / `wind`)',
  eq(jsJetColors, scales.wind_gusts_10m.light.colors) && eq(jsJetColors, scales.wind_gusts_10m.dark.colors));
ok('H: wind_speed_300hPa = unita\' "m/s", light == dark',
  extractUnitOfScale('FM_JET_COLOR_SCALE') === 'm/s' && eq(scales.wind_speed_300hPa.light, scales.wind_speed_300hPa.dark));

// H4. Le scale rimosse NON devono tornare (componenti = INPUT, direzione senza scala).
ok('H: nessuna scala per le componenti u/v nel JSON (6 chiavi assenti)',
  ['wind_u_component_10m', 'wind_v_component_10m', 'wind_u_component_850hPa',
    'wind_v_component_850hPa', 'wind_u_component_300hPa', 'wind_v_component_300hPa']
    .every((k) => !(k in scales)));
ok('H: NESSUNA scala wind_direction_* (la direzione si legge, non si disegna)',
  !Object.keys(scales).some((k) => /^wind_direction_/.test(k)),
  Object.keys(scales).filter((k) => /^wind_direction_/.test(k)).join(','));
ok('H: client senza residui delle componenti (FM_WIND_COMP_* e "Vento U/V 10 m")',
  !/wind_[uv]_component_10m/.test(src) && !/FM_WIND_COMP_/.test(src) &&
  !/'Vento U 10 m'/.test(src) && !/'Vento V 10 m'/.test(src));

// H5. Registrazione nel registro unificato: le 3 velocita' si', componenti/direzioni no.
const ovBody = (src.match(/var\s+FM_COLOR_SCALE_OVERRIDES\s*=\s*\{([^}]*)\}/) || ['', ''])[1];
ok('H: FM_COLOR_SCALE_OVERRIDES registra le 3 velocita\' (stesso oggetto del JSON)',
  /(^|[\s,{])wind_speed_10m:\s*FM_WIND_COLOR_SCALE/.test(ovBody) &&
  /(^|[\s,{])wind_speed_850hPa:\s*FM_WIND_COLOR_SCALE/.test(ovBody) &&
  /(^|[\s,{])wind_speed_300hPa:\s*FM_JET_COLOR_SCALE/.test(ovBody), ovBody.trim().slice(-150));
ok('H: FM_COLOR_SCALE_OVERRIDES NON registra componenti u/v ne\' direzioni',
  !/wind_[uv]_component/.test(ovBody) && !/wind_direction_/.test(ovBody));

// H6. Gate di visibilita' (FM_CURATED_VARS / etichette).
{
  const m = src.match(/var\s+FM_CURATED_VARS\s*=\s*\[([^\]]*)\]/);
  const curated = m ? vm.runInNewContext('[' + m[1] + ']') : null;
  ok('H: FM_CURATED_VARS = 28 voci (30 meno le 2 componenti u/v rimosse)',
    Array.isArray(curated) && curated.length === 28, `n=${Array.isArray(curated) ? curated.length : '?'}`);
  ok('H: FM_CURATED_VARS contiene le 3 wind_speed_* e nessuna componente u/v',
    Array.isArray(curated) &&
    ['wind_speed_10m', 'wind_speed_850hPa', 'wind_speed_300hPa'].every((v) => curated.includes(v)) &&
    !curated.some((v) => /^wind_[uv]_component/.test(v)));
}
ok('H: etichette italiane delle 3 velocita\' presenti, quelle delle componenti rimosse',
  /wind_speed_10m:\s*'Vento 10 m'/.test(src) &&
  /wind_speed_850hPa:\s*'Vento 850 hPa'/.test(src) &&
  /wind_speed_300hPa:\s*'Jet Stream 300 hPa'/.test(src) &&
  !/'Vento U 10 m'/.test(src) && !/'Vento V 10 m'/.test(src));
ok('H: etichette italiane presenti per le altre variabili nuove',
  /dew_point_2m:\s*'Punto di rugiada 2 m'/.test(src) &&
  /relative_humidity_700hPa:\s*'Umidita relativa 700 hPa'/.test(src) &&
  /precipitation_cum:\s*'Cumulata precipitazione'/.test(src));
ok('H: le variabili nuove sono in FM_CURATED_VARS (gate di visibilita\')',
  ['dew_point_2m', 'relative_humidity_700hPa', 'wind_speed_10m', 'wind_speed_850hPa',
    'wind_speed_300hPa', 'precipitation_cum'].every((v) =>
      new RegExp("FM_CURATED_VARS = \\[[^\\]]*'" + v + "'").test(src)));

// H7. Il JSON resta coerente con la rampa `wind` della libreria per 10 m / 850 hPa.
ok('H: wind_speed_10m == wind_speed_850hPa == wind_gusts_10m (rampa `wind`, 0..40)',
  eq(scales.wind_speed_10m, scales.wind_gusts_10m) &&
  eq(scales.wind_speed_850hPa, scales.wind_gusts_10m));
ok('H: precipitation_cum ha una scala dedicata nel client (non risolta per prefisso)',
  /var\s+FM_PRECUM_COLOR_SCALE\s*=/.test(src) && /precipitation_cum:\s*FM_PRECUM_COLOR_SCALE/.test(src));

// H9. dew_point_2m / relative_humidity_700hPa: NESSUN override nel client, quindi la libreria
// li risolve per prefisso (`dew_point` / `relative_humidity`) e il JSON deve essere la copia
// verbatim di quelle scale. Qui si verifica la parte misurabile senza la tabella della
// libreria: l'assenza di override lato client (se comparisse un override, mappa e legenda
// userebbero due scale diverse).
ok('H9: nessun override client per dew_point_2m / relative_humidity_700hPa (risoluzione per prefisso)',
  !/(^|[\s,{])dew_point_2m:\s*FM_/.test(ovBody) &&
  !/(^|[\s,{])relative_humidity_700hPa:\s*FM_/.test(ovBody), ovBody.trim().slice(-150));
ok('H9: le due scale esistono nel JSON (light+dark); relative_humidity_700hPa usa la STESSA rampa di relative_humidity_2m (0..95 %, 15 bande)',
  !!(scales.dew_point_2m && scales.dew_point_2m.light && scales.dew_point_2m.dark) &&
  !!(scales.relative_humidity_700hPa && scales.relative_humidity_700hPa.light) &&
  eq(scales.relative_humidity_700hPa, scales.relative_humidity_2m) &&
  scales.relative_humidity_700hPa.light.breakpoints.length === 15 &&
  scales.relative_humidity_700hPa.light.breakpoints[0] === 0 &&
  scales.relative_humidity_700hPa.light.breakpoints[14] === 95);

// ---------- I. PRECIPITAZIONE CUMULATA (precipitation_cum) ----------
// 1.2.4.11 (dataset-driven): la cumulata dal riferimento della run ("Cumul depuis 0h")
// non esisteva nella libreria. Il client la risolveva per PREFISSO su `precipitation`
// (0-50 mm -> saturava a 50 mm) e il renderer la SALTava ("no color scale ... skip").
// Ora esiste una scala dedicata su ENTRAMBI i lati: 21 bande 0..300 mm tarate sulla
// distribuzione MISURATA (4 domini, 3.226.911 celle: p50 4,5 / p90 29,9 / p99 94,3 /
// p99.9 194,0 / max 1128,7 mm; >300 mm copre il 99,986% delle celle), banda [0,1)
// trasparente e coda estesa al violetto scuro. Costanti del CLIENT ed entry JSON devono
// coincidere (breakpoints + colori light + dark) e la chiave DEVE stare in
// FM_COLOR_SCALE_OVERRIDES: senza l'override esatto, `precipitation` la catturerebbe per
// prefisso e mappa/legenda divergerebbero in silenzio.
const EXPECTED_PRECUM_BP = [0, 1, 2.5, 5, 7.5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125, 150, 175, 200, 250, 300];
const jsPrecumBp = extractArrayLiteral('FM_PRECUM_BREAKPOINTS');
const jsPrecumColors = extractArrayLiteral('FM_PRECUM_COLORS');
failFast('FM_PRECUM_BREAKPOINTS non estratta dall\'HTML', Array.isArray(jsPrecumBp));
failFast('FM_PRECUM_COLORS non estratta dall\'HTML', Array.isArray(jsPrecumColors));

// Comparatore RIUSABILE (lato CLIENT <-> entry JSON), esercitato dalla sensitivita' in I2.
function scaleEq(bp, colors, entry) {
  return !!entry && !!entry.light && !!entry.dark &&
    eq(bp, entry.light.breakpoints) && eq(bp, entry.dark.breakpoints) &&
    eq(colors, entry.light.colors) && eq(colors, entry.dark.colors);
}
const jsonPrecum = scales.precipitation_cum;
ok('I: precipitation_cum presente nel color_scales.json (light+dark)',
  !!(jsonPrecum && jsonPrecum.light && jsonPrecum.dark));
ok('I: breakpoints attesi 0..300 mm (21 bande, [0,1) trasparente)',
  eq(jsPrecumBp, EXPECTED_PRECUM_BP), JSON.stringify(jsPrecumBp));
ok('I: 21 breakpoints e 21 colori', jsPrecumBp.length === 21 && jsPrecumColors.length === 21,
  `bp=${jsPrecumBp.length} colors=${jsPrecumColors.length}`);
ok('I: breakpoints strettamente crescenti',
  jsPrecumBp.every((v, i) => i === 0 || v > jsPrecumBp[i - 1]));
ok('I: ogni colore e\' RGBA (canali 0..255, alpha 0..1)',
  jsPrecumColors.every((c) => Array.isArray(c) && c.length === 4 &&
    c.slice(0, 3).every((x) => Number.isInteger(x) && x >= 0 && x <= 255) &&
    typeof c[3] === 'number' && c[3] >= 0 && c[3] <= 1));
ok('I: alpha 0 SOLO sulla prima banda ([0,1) = "non piove")',
  jsPrecumColors.filter((c) => c[3] === 0).length === 1 && jsPrecumColors[0][3] === 0 &&
  jsPrecumColors.slice(1).every((c) => c[3] === 1));
ok('I: nessun colore duplicato (la vecchia coda pioggia ne aveva 3 identici)',
  new Set(jsPrecumColors.map((c) => JSON.stringify(c))).size === jsPrecumColors.length,
  `distinti=${new Set(jsPrecumColors.map((c) => JSON.stringify(c))).size}/${jsPrecumColors.length}`);
ok('I: coda (indici 12..20) = violetto scuro monotono (R decrescente, B crescente)',
  jsPrecumColors.slice(13).every((c, i) => c[0] < jsPrecumColors[12 + i][0] && c[2] > jsPrecumColors[12 + i][2]) &&
  jsPrecumColors[20][2] > jsPrecumColors[20][0], JSON.stringify(jsPrecumColors.slice(12)));
ok('I: rampa piu\' fitta in basso (bp[1]=1, bp[2]=2.5) e piu\' larga in alto (bp[19]=250)',
  jsPrecumBp[1] === 1 && jsPrecumBp[2] === 2.5 && jsPrecumBp[20] === 300);
ok('I: unita\' "mm"', extractUnitOfScale('FM_PRECUM_COLOR_SCALE') === 'mm');
ok('I: FM_COLOR_SCALE_OVERRIDES registra precipitation_cum (anti-prefisso)',
  /precipitation_cum:\s*FM_PRECUM_COLOR_SCALE/.test(src));
ok('I: light == dark (palette identica)',
  /FM_PRECUM_COLOR_SCALE\s*=\s*\{[\s\S]*?colors:\s*\{\s*light:\s*FM_PRECUM_COLORS,\s*dark:\s*FM_PRECUM_COLORS\s*\}/.test(src));
ok('I: precipitation_cum CLIENT == JSON (breakpoints + colori light + dark)',
  scaleEq(jsPrecumBp, jsPrecumColors, jsonPrecum));

// ---------- I2. PROVA DI SENSIBILITA' DEL COMPARATORE (precipitation_cum) ----------
// Come H2, ma sulla nuova scala: mutare un breakpoint o un colore (JSON o CLIENT) deve
// far FALLIRE scaleEq, altrimenti la verifica di sincronia sarebbe un falso verde.
{
  const good = JSON.parse(JSON.stringify(jsonPrecum));
  ok('I2: baseline precipitation_cum supera il comparatore', scaleEq(jsPrecumBp, jsPrecumColors, good) === true);
  const badJsonBp = JSON.parse(JSON.stringify(good)); badJsonBp.light.breakpoints[5] = 11;
  ok('I2: sensitivita\' — breakpoint JSON mutato => FALLISCE', scaleEq(jsPrecumBp, jsPrecumColors, badJsonBp) === false);
  const badJsonCol = JSON.parse(JSON.stringify(good)); badJsonCol.dark.colors[20] = [1, 2, 3, 1];
  ok('I2: sensitivita\' — colore DARK JSON mutato => FALLISCE', scaleEq(jsPrecumBp, jsPrecumColors, badJsonCol) === false);
  const badClientBp = jsPrecumBp.slice(); badClientBp[0] = 0.5;
  ok('I2: sensitivita\' — breakpoint CLIENT mutato => FALLISCE', scaleEq(badClientBp, jsPrecumColors, good) === false);
  const badClientCol = jsPrecumColors.slice(); badClientCol[1] = [0, 0, 0, 1];
  ok('I2: sensitivita\' — colore CLIENT mutato => FALLISCE', scaleEq(jsPrecumBp, badClientCol, good) === false);
}

// ---------- H8. PROVA DI SENSIBILITA' DEL COMPARATORE (tutte e 3 le scale del vento) ----------
// Il comparatore deve FALLIRE quando una delle due parti diverge. Si muta in memoria una
// copia dell'entry JSON (breakpoint, colore, variante dark) di wind_speed_10m e di
// wind_speed_300hPa, e una copia delle costanti CLIENT; se restasse true il test sarebbe un
// falso verde.
for (const [nm, jsbp, jscl] of [
  ['wind_speed_10m', jsWindBp, jsWindColors],
  ['wind_speed_300hPa', jsJetBp, jsJetColors],
]) {
  const goodEntry = JSON.parse(JSON.stringify(scales[nm]));
  ok(`H8: baseline ${nm} - l'entry NON mutata supera il comparatore`,
    scaleMatchesJson(jsbp, jscl, goodEntry) === true);
  const badBp = JSON.parse(JSON.stringify(goodEntry));
  badBp.light.breakpoints[0] = 0.5;
  ok(`H8: sensitivita' ${nm} - breakpoint JSON mutato => FALLISCE`,
    scaleMatchesJson(jsbp, jscl, badBp) === false);
  const badCol = JSON.parse(JSON.stringify(goodEntry));
  badCol.light.colors[3] = [1, 2, 3, 1];
  ok(`H8: sensitivita' ${nm} - colore JSON mutato => FALLISCE`,
    scaleMatchesJson(jsbp, jscl, badCol) === false);
  const badDark = JSON.parse(JSON.stringify(goodEntry));
  badDark.dark.breakpoints[badDark.dark.breakpoints.length - 1] = 123;
  ok(`H8: sensitivita' ${nm} - variante dark mutata => FALLISCE`,
    scaleMatchesJson(jsbp, jscl, badDark) === false);
  const badClientBp = jsbp.slice();
  badClientBp[badClientBp.length - 1] = 41;
  ok(`H8: sensitivita' ${nm} - breakpoint CLIENT mutato => FALLISCE`,
    scaleMatchesJson(badClientBp, jscl, goodEntry) === false);
  const badClientCol = jscl.slice();
  badClientCol[0] = [0, 0, 0, 1];
  ok(`H8: sensitivita' ${nm} - colore CLIENT mutato => FALLISCE`,
    scaleMatchesJson(jsbp, badClientCol, goodEntry) === false);
}

// ---------- ESITO ----------
console.log(`\n${failures === 0 ? 'PASS' : 'FAIL'}: ${checks - failures}/${checks} controlli superati`);
process.exit(failures === 0 ? 0 : 1);
