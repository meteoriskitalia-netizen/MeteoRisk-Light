#!/usr/bin/env node
// TEST 1.0.0.8 — PARTE E (MOBILE): checker STRUTTURALE responsivo.
// Non esegue un browser: parsa <style> + hook JS del sorgente e asserisce le
// regole critiche per ogni classe di viewport (320/375/430/600-768/desktop).
// Documentato limite: verifica PRESENZA/ORDINE delle regole CSS, non il
// rendering pixel-perfect.
'use strict';

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.0.8.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}

const style = src.slice(src.indexOf('<style'), src.indexOf('</style>'));
const js = src.slice(0, src.indexOf('</script>') === -1 ? src.length : src.length); // js intero sotto
const has = (re, inStr = style) => re.test(inStr);
const inBody = 'sorgente';

// ---------- 1. breakpoint centralizzati ----------
ok('BREAKPOINT: variabili --bp-sm/--bp-md/--bp-lg in :root',
  /--bp-sm:\s*360px/.test(style) && /--bp-md:\s*767px/.test(style) && /--bp-lg:\s*1024px/.test(style));
ok('BREAKPOINT: media <=359px (entry-level 320)', /@media\s*\(max-width:\s*359px\)/.test(style));
ok('BREAKPOINT: media <=767px (smartphone)', /@media\s*\(max-width:\s*767px\)/.test(style));
ok('BREAKPOINT: fascia 768-1024px (tablet)', /@media\s*\(min-width:\s*768px\)\s*and\s*\(max-width:\s*1024px\)/.test(style));
ok('BREAKPOINT: media <=1024px (laptop piccolo)', /@media\s*\(max-width:\s*1024px\)/.test(style));
ok('BREAKPOINT: media orientation landscape (phone)', /@media\s*\(orientation:\s*landscape\)/.test(style));
ok('CLASSI: agganci .app-mobile/.app-tablet/.app-desktop nel CSS',
  /\.app-mobile\s*,\s*\.app-tablet\s*,\s*\.app-desktop/.test(style));
ok('CLASSI: updateViewportClasses imposta app-* su <html> e <body>',
  /function updateViewportClasses\(\)[\s\S]{0,700}concat\(\[cls\]\)/.test(src) &&
  /d\.className\s*=/.test(js) && /b\.className\s*=/.test(js));

// ---------- 2. PRIORITÀ 1 — mappa dominante (45-65dvh) ----------
ok('P1: #leaflet-map mobile usa 56dvh (dentro 45-65dvh)',
  /#leaflet-map,\s*#radar-embed-container,\s*#pretemp-container\s*\{\s*height:\s*clamp\(45dvh,\s*56dvh,\s*66dvh\);[\s\S]{0,40}min-height:\s*300px/.test(style));
ok('P1: anche radar-embed e pretemp ricevono la stessa altezza mobile',
  /#leaflet-map,\s*#radar-embed-container,\s*#pretemp-container/.test(style));
ok('P1: tablet 768-1024 altezza mappa clamp(380px,60vh,640px)',
  /height:\s*clamp\(380px,\s*60vh,\s*640px\)/.test(style));
ok('P1: desktop: #leaflet-map resta height:560px (invariato)',
  /#leaflet-map\s*\{\s*width:\s*100%;\s*height:\s*560px/.test(style));

// ---------- 3. PRIORITÀ 2 — header ----------
ok('P2: .app-header a colonna su mobile',
  /@media\s*\(max-width:\s*767px\)[\s\S]{0,900}\.app-header,\s*\.toolbar/.test(style));
let m767 = style.match(/@media\s*\(max-width:\s*767px\)\{([\s\S]*?)\}@media\s*\(max-width:\s*359px\)/);
const mobileBlock = m767 ? m767[1] : style;
ok('P2: h1 mobile font 16px (scala proporzioni)', /\.app-title\s+h1\s*\{\s*font-size:\s*16px/.test(mobileBlock));

// ---------- 4. PRIORITÀ 4 — touch target >=44px ----------
ok('P4: --touch-min: 44px', /--touch-min:\s*44px/.test(style));
ok('P4: .tool-btn min-height 44 su mobile', /\.toolbar\s+\.tool-btn\s*\{[^}]*min-height:\s*var\(--touch-min\)/.test(style));
ok('P4: .day-btn min-height 44', /\.day-btn\s*\{[^}]*min-height:\s*var\(--touch-min\)/.test(mobileBlock));
ok('P4: .metric-tab min-height 44', /\.metric-tab\s*\{[^}]*min-height:\s*var\(--touch-min\)/.test(mobileBlock));
ok('P4: .mp-switch/.mp-select min-height 44', /\.mp-switch,\s*\.mp-select\s*\{[^}]*min-height:\s*var\(--touch-min\)/.test(mobileBlock));
ok('P4: input ricerca font-size>=16px (no zoom iOS)',
  /\.search-input\s*\{[^}]*font-size:\s*16px/.test(mobileBlock));

// ---------- 5. PRIORITÀ 5/6 — legende + selettore fascia ----------
ok('P5: .fl-section 100% su mobile', /\.forecast-legend-panel\s+\.fl-section,\s*\.sources-panel\s+\.fl-section\s*\{[^}]*100%/.test(mobileBlock));
ok('P5: badge legend 1 colonna su mobile', /\.badge-legend-bar\s*\{[^}]*grid-template-columns:\s*1fr/.test(mobileBlock));
ok('P6: forecast-slot dentro il viewport (left/right 8, transform none)',
  /#forecast-slot-container\s*\{[^}]*left:\s*8px;[\s\S]{0,120}transform:\s*none/.test(mobileBlock));
ok('P6: select fascia flex:1 (tappabile, no overflow)',
  /#forecast-slot\s*\{[^}]*flex:\s*1/.test(mobileBlock));

// ---------- 6. PRIORITÀ 3/7 — pannelli, LIVE, timeline ----------
ok('P7: .slider-timeline larghezza piena su mobile',
  /\.slider-timeline\s*\{[^}]*min-width:\s*100%/.test(mobileBlock));
ok('P7: #risk-summary padding-bottom safe-area',
  /#risk-summary\s*\{[^}]*padding-bottom:\s*calc\(10px\s*\+\s*var\(--safe-bottom\)\)/.test(mobileBlock));
ok('P3: .details-card padding ridotto su mobile', /\.details-card\s*\{[^}]*padding:\s*12px/.test(mobileBlock));
ok('P7: live-header va a capo su mobile', /\.live-header\s*\{[^}]*flex-wrap:\s*wrap/.test(mobileBlock));

// ---------- 7. PRIORITÀ 8 — popup/tooltip dentro il viewport ----------
ok('P8: .caletta-popup внутри viewport (left/right 12, transform none)',
  /\.caletta-popup\s*\{[^}]*left:\s*12px;[\s\S]{0,80}right:\s*12px;[\s\S]{0,60}transform:\s*none/.test(mobileBlock));
ok('P8: .leaflet-popup max-width calc(100vw - 24px)',
  /\.leaflet-popup,\s*\.leaflet-popup-content-wrapper,\s*\.leaflet-popup-content\s*\{[^}]*calc\(100vw\s*-\s*24px\)/.test(mobileBlock));
ok('P8: .bl-tip max-width adattivo', /\.bl-tip\s*\{[^}]*calc\(100vw\s*-\s*24px\)/.test(mobileBlock));

// ---------- 8. PRIORITÀ 9 — grafici/risoluzione ----------
ok('P9: .chart-box 150px su mobile (contenitore)', /\.chart-box\s*\{[^}]*height:\s*150px/.test(mobileBlock));
ok('P9: .metric-grid 1 colonna a 320px', /metric-grid\s*\{\s*grid-template-columns:\s*1fr/.test(style));

// ---------- 9. PRIORITÀ 10 — landscape ----------
const lsc = style.match(/@media\s*\(orientation:\s*landscape\)\s*(?:and\s*\(max-height:\s*520px\))?\s*\{?([\s\S]*?)\}?\s*@media?\s*(?:\(orientation|\})/);
ok('P10: landscape riduce mappa a 48dvh',
  /#leaflet-map,\s*#radar-embed-container,\s*#pretemp-container\s*\{[^}]*height:\s*48dvh/.test(style));
ok('P10: landscape nasconde il sottotitolo header (compatta)',
  /landscape[\s\S]{0,600}\.app-title\s+p\s*\{\s*display:\s*none/.test(style));

// ---------- 10. PRIORITÀ 11 — performance/adaptive rendering ----------
ok('P11: updateViewportClasses presente', /function updateViewportClasses\(\)/.test(src));
ok('P11: scheduleInvalidate presente', /function scheduleInvalidate\(\)/.test(src));
ok('P11: map.invalidateSize() chiamato (debounced)',
  /map\.invalidateSize\(\)/.test(src.slice(src.indexOf('function scheduleInvalidate'), src.indexOf('function scheduleInvalidate') + 600)));
ok('P11: listener resize + orientationchange',
  /addEventListener\('resize',\s*onViewportChanged\)/.test(src) &&
  /addEventListener\('orientationchange',\s*onViewportChanged\)/.test(src));
ok('P11: aggancio ai 5 toggle (radar, satellite, high-risk, forecast, live)',
  /function\s+toggle(?:RadarPlayer|Satellite|HighRiskFilter|Forecast)\(\)\s*\{\s*scheduleInvalidate\(\);/.test(src) &&
  /scheduleInvalidate\(\);/.test(src.slice(src.indexOf('async function toggleLivePanel()'), src.indexOf('async function toggleLivePanel()') + 2600)));

// ---------- 11. safe area ----------
ok('SAFE: variabili env(safe-area-inset-*) definite',
  /--safe-top:\s*env\(safe-area-inset-top/.test(style) && /--safe-bottom:\s*env\(safe-area-inset-bottom/.test(style));
ok('SAFE: body usa padding con safe-area',
  /body\s*\{\s*padding-left:\s*calc\(16px\s*\+\s*var\(--safe-left\)\)/.test(style));

// ---------- 12. nessuna regressione: root cause FIX1/FIX2/FIX3 ancora in posto ----------
ok('REGRESSIONE: contract metricValueContract ancora presente',
  /\/\/#pure# BEGIN metricValueContract/.test(src));
ok('REGRESSIONE: guardia Blitzortung ancora presente',
  /if \(!isFeatureEnabled\('lightningBlitzortung'\)\) return;/.test(src));

// ---------- 13. tabella di stato per viewport ----------
console.log(`\n=== STATO PER VIEWPORT (strutturale) ===`);
const viewports = [
  { w: 320, cls: 'app-mobile',  map: 'clamp(45-66dvh)+300px', touch: '44', grid: '1 colonna' },
  { w: 375, cls: 'app-mobile',  map: 'clamp(45-66dvh)+300px', touch: '44', grid: '2 colonne' },
  { w: 430, cls: 'app-mobile',  map: 'clamp(45-66dvh)+300px', touch: '44', grid: '2 colonne' },
  { w: 600, cls: 'app-mobile',  map: 'clamp(45-66dvh)+300px', touch: '44', grid: '2 colonne' },
  { w: 768, cls: 'app-tablet',  map: 'clamp(380px,60vh,640px)',  touch: 'base', grid: '2 colonne' },
  { w: 1024, cls: 'app-tablet', map: 'clamp(380px,60vh,640px)',  touch: 'base', grid: '2 colonne' },
  { w: 1280, cls: 'app-desktop', map: '560px',                   touch: 'base', grid: '2 colonne' },
];
for (const v of viewports) {
  let status = 'PASS';
  if (v.cls.includes('mobile')) {
    const okMap = /clamp\(45dvh,\s*56dvh,\s*66dvh\)/.test(style);
    const okTouch = (v.touch === '44' ? /var\(--touch-min\)/.test(style) : true);
    const okGrid = (v.grid === '1 colonna' ? /metric-grid\s*\{\s*grid-template-columns:\s*1fr/.test(style) : true);
    if (!(okMap && okTouch && okGrid)) status = 'FAIL';
  } else if (v.cls === 'app-tablet') {
    if (!/clamp\(380px,\s*60vh,\s*640px\)/.test(style)) status = 'FAIL';
  } else if (v.cls === 'app-desktop') {
    if (!/#leaflet-map\s*\{\s*width:\s*100%;\s*height:\s*560px/.test(style)) status = 'FAIL';
  }
  console.log(`  ${status}  ${String(v.w).padStart(4)}px  ${v.cls.padEnd(12)}  mappa=${v.map}  touch=${v.touch}  griglia=${v.grid}`);
}

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);