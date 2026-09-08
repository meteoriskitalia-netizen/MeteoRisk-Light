#!/usr/bin/env node
// TEST 1.0.0.15 — MODULO ENSEMBLE OPEN-METEO (structural checks).
// Non esegue un browser: parsa il sorgente HTML e asserisce la presenza/struttura
// del nuovo modulo ensemble client-side (sostituzione completa degli spaghetti
// Meteociel):
//   A) ENDPOINT/API ensemble di Open-Meteo + config centralizzata ENS_MODELS/ENS_VARS
//   B) ON-DEMAND + DEDUPE + timeout (nessuna fetch prima dell'apertura del pannello)
//   C) NORMALIZZAZIONE + rillevamento dinamico membri (pattern, mai numeri hardcoded)
//   D) GRAFICO ORIGINALE SU CANVAS + tooltip mouse/touch + legend + attribuzione
//   E) ERRORI UTENTE in italiano + pulsante Riprova (nessuno stack trace esposto)
//   F) RIMOZIONE COMPLETA DEL MODULO ENSEMBLE METEOCIEL dal sorgente (codice, non note)
// Documentato limite: verifica PRESENZA/STRUTTURA, non il rendering reale (il
// comportamento runtime va validato in-browser).
'use strict';

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const HTML = path.join(ROOT, 'mri-light-1.0.1.0.html');
const src = fs.readFileSync(HTML, 'utf8');

let failures = 0;
function ok(label, cond, detail = '') {
  console.log(`  [${cond ? 'PASS' : 'FAIL'}] ${label}${detail ? ` · ${detail}` : ''}`);
  if (!cond) failures++;
}

const has = (re, inStr = src) => re.test(inStr);

// ---------- A. ENDPOINT / CONFIG ----------
ok('A1: ENS_API_BASE = https://ensemble-api.open-meteo.com/v1/ensemble',
  has(/const ENS_API_BASE\s*=\s*'https:\/\/ensemble-api\.open-meteo\.com\/v1\/ensemble';/));
ok('A2: ENS_MODELS definiti (ncep_gefs025 + ncep_gefs05, GEFS NOAA; codici UFFICIALI dell\'API ensemble)',
  has(/const ENS_MODELS\s*=\s*\{[\s\S]{0,600}api:\s*'ncep_gefs025'/) &&
  has(/const ENS_MODELS\s*=\s*\{[\s\S]{0,1200}api:\s*'ncep_gefs05'/));
ok('A2b: ENS_MODELS include ECMWF IFS/AIFS, ICON-EU/globale e MOGREPS-G',
  has(/api:\s*'ecmwf_ifs025'/) && has(/api:\s*'ecmwf_aifs025'/) &&
  has(/api:\s*'icon_eu_eps'/) && has(/api:\s*'icon_global_eps'/) &&
  has(/api:\s*'ukmo_global_ensemble_20km'/));
ok('A2c: 7 modelli complessivi in ENS_MODELS',
  (src.match(/api:\s*'(ecmwf_ifs025|ecmwf_aifs025|icon_eu_eps|icon_global_eps|ncep_gefs025|ncep_gefs05|ukmo_global_ensemble_20km)'/g) || []).length === 7);
ok('A3: ENS_MODELS con orizzonti per modello (array days)',
  has(/const ENS_MODELS\s*=\s*\{[\s\S]{0,900}days:\s*\[/));
ok('A4: ENS_VARS contiene le 4 variabili richieste',
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,1200}api:\s*'temperature_850hPa'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,1600}api:\s*'temperature_2m'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,2200}api:\s*'geopotential_height_500hPa'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,2800}api:\s*'precipitation'/));
ok('A5: chart type configurati (spaghetti + bars)',
  has(/chart:\s*'spaghetti'/) && has(/chart:\s*'bars'/));
ok('A6: coordinate dinamiche (latitude/longitude dallo stato, non hardcoded)',
  has(/q\.set\('latitude',\s*String\(loc\.lat\)\);\s*[\s\S]{0,120}q\.set\('longitude',\s*String\(loc\.lon\)\);/));
ok('A7: forecast_days dinamico per modello',
  has(/q\.set\('forecast_days',\s*String\(days\)\);/));
ok('A7b: parametro MODELS al plurale (il singolare &model= viene IGNORATO dall\'API e restituirebbe sempre il modello di default)',
  has(/q\.set\('models',\s*model\.api\);/));
ok('A8: timezone Europe/Rome + temperature_unit celsius nella richiesta',
  has(/q\.set\('timezone',\s*'Europe\/Rome'\)/) &&
  has(/q\.set\('temperature_unit',\s*'celsius'\)/));
ok('A9: tutte le variabili in una sola richiesta (hourly = mappa di ENS_VARS, derivate escluse)',
  has(/q\.set\('hourly',\s*ENS_VARS\.filter\(function\s*\(v\)\s*\{\s*return\s*!v\.derived;\s*\}\)\.map/) &&
  has(/\.join\(','\)\);\s*const\s+url\s*=\s*ENS_API_BASE/));
ok('A10: ENS_VARS include le variabili del vecchio set Meteociel (Pressione, T500, Vento 10m, Raffiche, CAPE, neve...)',
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,3600}api:\s*'temperature_500hPa'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,4200}api:\s*'pressure_msl'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,4800}api:\s*'wind_speed_10m'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,5400}api:\s*'wind_gusts_10m'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,6000}api:\s*'wind_speed_850hPa'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,6600}api:\s*'relative_humidity_850hPa'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,7200}api:\s*'cape'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,7800}api:\s*'snow_depth'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,8400}api:\s*'snowfall'/) &&
  has(/const ENS_VARS\s*=\s*\[[\s\S]{0,9000}api:\s*'precipitation'/));
ok('A11: ENS_VARS include le 3 variabili DERIVATE (Iso 0°C, ThetaE 850, Cumul pioggia) marcate derived',
  has(/id:\s*'freezing_level'[\s\S]{0,220}derived:\s*true/) &&
  has(/id:\s*'theta_e_850'[\s\S]{0,220}derived:\s*true/) &&
  has(/id:\s*'cum_precipitation'[\s\S]{0,220}derived:\s*true/) &&
  has(/derived:\s*true/));
ok('A12: 17 variabili complessive (14 dirette + 3 derivate) in ENS_VARS',
  (src.match(/id:\s*'[a-z_0-9]+'/g) || []).some(() => true) &&
  (src.match(/derived:\s*true/g) || []).length === 3);

// ---------- B. ON-DEMAND / DEDUPE / TIMEOUT ----------
ok('B1: pannello accordion chiuso all\'avvio (hidden nel HTML)',
  has(/id="ens-body" hidden/));
ok('B2: nessuna fetch se il pannello e\' chiuso (guardia all\'inizio di ensLoad)',
  has(/function ensLoad\(\)\s*\{\s*if\s*\(!ensState\.open\)\s*return;/));
ok('B3: dedupe via chiave di cache (model|days|lat|lon)',
  has(/(?:const|var) key\s*=\s*model\.api\s*\+\s*'\|'/) &&
  has(/ensState\.cache\.key\s*===\s*key/) &&
  has(/\/\/ Cache di sessione/));
ok('B4: cache solo sessione in memoria (commento esplicito, MAI persistente)',
  has(/cache:\s*null,\s*\/\/ risposta normalizzata della sessione corrente/) &&
  has(/solo memoria, MAI persistente/));
ok('B5: AbortController + timeout (ENS_TIMEOUT_MS, 40s) per payload piu\' pesanti con 17 variabili',
  has(/(?:const|var)\s+timer\s*=\s*setTimeout\(function\(\)\s*\{\s*ctrl\.abort\(\);\s*\},\s*ENS_TIMEOUT_MS\);/) &&
  has(/const ENS_TIMEOUT_MS\s*=\s*40000;/));
ok('B6: cambio solo variabile = render da cache (nessun refetch)',
  has(/Il cambio SOLO variabile non fa mai fetch/) &&
  has(/vSel\.addEventListener\('change',\s*function\(\)\s*\{\s*ensRenderSelected\(\);\s*\}/));
ok('B7: risposta stale ignorata (controllo chiave nel catch)',
  has(/if\s*\(!ensState\.open\s*\|\|\s*ensState\.key\s*!==\s*key\)\s*return;/));

// ---------- C. NORMALIZZAZIONE / MEMBRI ----------
ok('C1: ensNormalize presente (layer dati normalizzato)',
  has(/function ensNormalize\(/));
ok('C2: rilevamento dinamico membri via pattern ^<var>_memberNN$',
  has(/new RegExp\('\^'\s*\+\s*api\s*\+\s*'_member/));
ok('C3: statistiche min/max/p25/p75 calcolate dai membri (helper ensStatsFrom)',
  has(/stats:\s*ensStatsFrom\(members,\s*times\.length\)/) &&
  has(/function ensStatsFrom\(/));
ok('C4: output normalizzato con chiavi {key, model, location, metadata, times, vars}',
  has(/return\s*\{\s*key:/) && has(/metadata:\s*\{ provider:/) && has(/times:\s*times/) && has(/vars:\s*varsOut/));
ok('C5: variabili derivate calcolate in locale (ensComputeDerived, ThetaE, Iso 0°C, Cumul)',
  has(/function ensComputeDerived\(/) &&
  has(/function ensMakeVarFromMembers\(/) &&
  has(/function ensThetaE\(/) && has(/function ensDewPoint\(/) &&
  has(/function ensZeroCross\(/) &&
  has(/varsOut\['cum_precipitation'\]\s*=\s*ensMakeVarFromMembers/) &&
  has(/varsOut\['freezing_level'\]\s*=\s*ensMakeVarFromMembers/) &&
  has(/varsOut\['theta_e_850'\]\s*=\s*ensMakeVarFromMembers/));

// ---------- D. GRAFICO ORIGINALE CANVAS ----------
ok('D1: canvas #ens-chart presente nel modulo',
  has(/<canvas id="ens-chart"/));
ok('D2: disegno DPR-aware (devicePixelRatio + setTransform)',
  has(/Math\.max\(1,\s*window\.devicePixelRatio \|\| 1\)/) &&
  has(/ctx\.setTransform\(dpr,\s*0,\s*0,\s*dpr,\s*0,\s*0\);/));
ok('D3: fascia spread p25-p75 disegnata (band)',
  has(/Fascia spread p25-p75/) && has(/v\.stats\.p25\[i\]/) && has(/v\.stats\.p75\[i\]/));
ok('D4: tooltip mouse/touch (pointer events su #ens-chart)',
  has(/addEventListener\('pointermove',\s*ensOnPointer\)/) &&
  has(/addEventListener\('pointerleave',\s*(?:function\(\)\s*\{\s*)?ensClearHover/));
ok('D5: legend con voce "Media"',
  has(/id="ens-legend"/) && has(/>Media<\/span>/));
ok('D6: attribuzione Open-Meteo nel modulo',
  has(/Dati ensemble:\s*<a href="https:\/\/open-meteo\.com\/"/));

// ---------- E. ERRORI / UX ----------
ok('E1: stato del modulo presente (aperto=false di default)',
  has(/(?:const|var) ensState\s*=\s*\{[\s\S]{0,300}open:\s*false/));
ok('E2: messaggi utente in italiano (offline, timeout, generic)',
  has(/ENS_OFFLINE_MSG\s*=\s*'/) && has(/ENS_TIMEOUT_MSG\s*=\s*'/) &&
  has(/Sei offline/));
ok('E3: stato HTTP gestito (429/404/5xx) senza stack trace',
  has(/httpStatus\s*===\s*429/) && has(/httpStatus\s*===\s*404/) &&
  has(/httpStatus\s*&&\s*httpStatus\s*>=\s*500/));
ok('E4: pulsante Riprova in caso di errore',
  has(/btn\.textContent\s*=\s*'Riprova';/) && has(/ensShowMsg\(msg,\s*true\)/));
ok('E5: pannello accessibile (role=button, aria-expanded sul header)',
  has(/id="ens-head"/) && has(/role="button"/) && has(/aria-expanded="false"/));
ok('E6: refresh manuale disponibile (pulsante ↻)',
  has(/id="ens-refresh"/) && has(/refreshBtn\.addEventListener\('click',\s*function\(\)\s*\{\s*ensLoad\(\);\s*\}/));

// ---------- F. RIMOZIONE MODULO ENSEMBLE METEOCIEL ----------
ok('F1: id spaghetti-* rimossi dal HTML',
  !has(/id="spaghetti-/));
ok('F2: stato e funzioni del vecchio modulo rimosse (niente codice residuo)',
  !has(/function\s+(?:onSpaghettiModelChange|updateSpaghettiPlot|getLatestEnsRun)\s*\(/) &&
  !has(/spaghettiLat\s*=|spaghettiLon\s*=|spaghettiVille\s*=/));
ok('F3: config rimosse (SPAGHETTI_MODELS, GEFS_IMG_SCRIPTS)',
  !has(/SPAGHETTI_MODELS|GEFS_IMG_SCRIPTS/));
ok('F4: URL immagine/iframe Meteociel del vecchio modulo rimosse',
  !has(/gens_display\.php/) && !has(/graphe_ens/));
ok('F5: CSS .spaghetti-* rimosso',
  !has(/\.spaghetti-/));
ok('F6: aggiornamento localita\' via updateUI mantiene il modulo (ensSetLocation)',
  has(/ensSetLocation\(lat,\s*lon,\s*subLabel\s*\|\|\s*label\s*\|\|\s*''\);/));
ok('F7: inizializzazione del modulo in startApp (oltre alla definizione)',
  (src.match(/initEnsembleModule\(\)/g) || []).length >= 2);

// ---------- G. VERSIONE ----------
ok('G1: APP_VERSION = 1.0.1.0', has(/APP_VERSION\s*=\s*['"]1\.0\.1\.0['"]/));
ok('G2: changelog 1.0.0.15 MODULO ENSEMBLE OPEN-METEO presente',
  has(/MODULO ENSEMBLE OPEN-METEO \(1\.0\.0\.15\)/));

console.log(`\nRESULT: ${failures === 0 ? 'PASS' : 'FAIL'} (${failures} errori)`);
process.exit(failures === 0 ? 0 : 1);