# CENTRALIZED DATA PIPELINE — MeteoRisk Light 1.0.0.8 (Best Match Canary + Initial Bootstrap)

> **1.0.0.8 — Canary + Bootstrap**: si aggiungono alla pipeline coordinata 1.0.0.8
> il rilevatore **Best Match indipendente** (6 sentinelle, 1 richiesta multi-location
> per ciclo, fingerprint SHA-256 del solo contenuto registrata alla pubblicazione),
> il **decision engine 2×2 + bootstrap** in `decide_cycle.py` e l'**INITIAL DATASET
> BOOTSTRAP (Parte G)**: il rilascio non contiene dataset locali; il primo dataset
> sorge esclusivamente dal primo run GitHub Actions (`bootstrap_pending`, fallimenti
> del primo ciclo = workflow FAIL, G6). La pipeline di dataset derivati resta
> invariata (nessun refactoring).

La Centralized Data Pipeline genera un **DATASET DERIVATO MeteoRisk** da Open-Meteo
e lo pubblica come JSON statico in `data/latest/`, servito da GitHub Pages. L'app
si collega al dataset: per i modelli coperti **zero richieste browser verso l'API
Open-Meteo** (il fallback live resta come rete di sicurezza controllata).

Open-Meteo è **SOLO una fonte dati** (input meteorologico): i payload dell'API **non** vengono
ripubblicati, specchiati o incapsulati. Vengono pubblicati esclusivamente dati **derivati
aggregati** (`dataset_type=derived_meteorological_risk_data`), come da ADDENDUM OBBLIGATORIO.

## Architettura

```
Open-Meteo (fonte dati)  ──►  Pipeline Python (GitHub Action)  ──►  data/latest/*.json  ──►  GitHub Pages  ──►  mri-light-1.0.0.8.html (loader)
```

Pipeline (per run, si veda anche `docs/` e commenti in head di ogni script):

1. `scripts/common.py` — configurazione + port fedeli della logica dell'app
   (geometria province, sampling V1/V2, `collapse_day` (11 campi), `score_point_for_province`,
   client Open-Meteo, run state). Driver unico: `DRIVER_MODEL = "ecmwf_ifs"`;
   leg coordinati scaricati in ogni ciclo: `DUAL_MODELS = "best_match,ecmwf_ifs"`.
2. `scripts/generate_points.py` — coordinate **reali** del campionamento (port bloccato dal
   golden test: v1=265, v2=257, ordine e coordinate identici all'app, seed coordIdx 0 = 107).
3. `scripts/check_model_runs.py` — rileva un NUOVO run **ECMWF IFS** tramite la **Metadata API**
   (non rate-limitated, check LEGGERO); grace period prudenziale (600 s dopo available_time);
   exit `0`=nuovo dataset da produrre, `10`=nessun nuovo run (clean success), `1`=errore rete.
   In stato **INITIAL BOOTSTRAP** il run corrente è SEMPRE considerato nuovo (mai "already
   processed"); se non esiste un run usabile → exit 1 (workflow FAIL, G6).
   **PARTE H — robustezza rete**: client Metadata API con timeout esplicito connect/read
   (15 s), retry automatici limitati (4 tentativi totali, mai infiniti) su errori transienti
   (SSL/keepalive handshake timeout, TimeoutError, connection reset, HTTP 429, HTTP 5xx)
   con exponential backoff + jitter. **NETWORK ERROR ≠ NO NEW RUN**: lo stato/fingerprint si
   aggiorna SOLO dopo un check riuscito; al termine dei retry → workflow FAIL con report
   esplicito ("Metadata API unavailable after retries", "No data fetch performed",
   "Last dataset unchanged").
4. `scripts/check_best_match.py` — **canary Best Match** (1.0.0.8): 6 capoluoghi-sentinella
   (MI, VE, RM, PE, LE, PA), coordinate identiche ai punti pubblicati, in UNA richiesta
   forecast multi-location (weathercode+precipitation orarie). Fingerprint SHA-256 del solo
   contenuto (giorno0 + sentinelle) confrontata con lo state: exit `0`=cambiato · `10`=invariato ·
   `1`=errore. In bootstrap non esegue richieste (il primo ciclo è sempre coordinated).
5. `scripts/decide_cycle.py` — **decision engine stateless + bootstrap** (1.0.0.8,
   FIX PIPELINE): combina `ecmwf_new` × `best_changed` e sceglie `coordinated` /
   `none` / `bootstrap` (la modalità parziale `best_match_only` è rimossa:
   qualunque cambiamento reale → fetch completo di entrambi i leg, nessuna
   dipendenza dal raw di un ciclo precedente); **pre-flight guardrails** (hard
   safety ceiling centralizzato in `data/state/api_usage.json`; blocco SOLO oltre
   il tetto, nessun razionamento preventivo; osservabilità separata: checks
   Metadata, canary, fetch, retry, riuscite/fallite per giorno).
   Steady-state oltre il tetto → exit 2 (safe skip); bootstrap oltre il tetto → exit 3 (FAIL:
   nessun dataset da preservare).
6. `scripts/fetch_source_data.py` — scarica il **raw temporaneo** in `data/_raw/` (MAI pubblicato)
   con i due leg `best_match` + `ecmwf_ifs` **SEMPRE completi** (`coordinated`, stessa fetched_at,
   STATELESS: il raw è scritto e consumato dentro lo stesso run, mai riletto da un ciclo successivo).
   Prima del retry selettivo dei batch falliti verifica il **budget residuo** (richiesta → errore
   retryable → budget → retry/stop; nessuna prenotazione preventiva). Se manca un capoluogo
   (coordIdx 0) → exit 3 (steady) / exit 4 (bootstrap FATAL); hard safety ceiling raggiunto →
   exit 2 (steady) / 4 (bootstrap FATAL).
7. `scripts/build_meteorisk_dataset.py` — METEO-RISK DATA ENGINE: collasso "day-wide" del
   giorno, riepilogo derivato giornaliero (20 campi), serie orarie spogliate dall'envelope API,
   worst-point per provincia (port di `scorePointForProvince`, tie-break primo max in ordine slot).
   Metadata: `models_covered=[best_match,ecmwf_ifs]`, `run_info` con driver `ecmwf_ifs`,
   `fetch_timestamps` per la coerenza temporale del ciclo, `update_strategy`.
8. `scripts/validate_dataset.py` — validazione **PRIMA** della pubblicazione (es. **3.619 check**):
   forma, contenuto, integrità territoriale, consistenza col port di coordinate, coerenza
   worst-point ricalcolato, driver ECMWF, coerenza temporale dei fetch (best_match ≥ ecmwf,
   `update_strategy` coerente con leg_timestamps) e `day0` presente. Su esito negativo
   `data/latest` NON viene toccato (last known good).
9. `scripts/publish_dataset.py` — pubblicazione **atomica**: swap staging→latest con backup e
   rollback; aggiorna `data/state/last_model_run.json` (status live) e **registra la fingerprint
   Best Match calcolata dal dataset VALIDATO pubblicato** (stato == dataset, 1.0.0.8).

## Contenuti pubblici di `data/latest` (esempio: fixture dataset, day0 = 2026-09-05)

| File | Dimensione (fixture) | Contenuto |
|---|---|---|
| `metadata.json` | 951 B | schema, dataset_type, modelli coperti, attribution, run_info, day0, point_count |
| `meteorisk-points.json` | 11.962.133 B | 257 punti reali: id, provinceIdx, sigla, coordIdx, lat/lon, elevation, `models` (best_match+ecmwf_ifs, 48 variabili orarie), `summary` giornaliera |
| `meteorisk-provinces.json` | 132.035 B | 107 province: `selected_point` (worst-point + score) e riepilogo per giorno |
| `validation.json` | 388.590 B | report completo della validazione (esiti per ognuno dei 3.619 check) |

Modelli coperti dal dataset: `best_match`, `ecmwf_ifs` (2 leg del dual). L'app tratta anche
`dual_best_ecmwf` come coperto (il merge dual avviene client-side con le stesse funzioni esistenti).

## Lato app (mri-light-1.0.0.8.html)

- `DATASET_PREFIX = 'data/latest/'`; `DATASET_COVERED_MODELS = ['dual_best_ecmwf','best_match','ecmwf_ifs']`.
- `initWeatherData('startup'|'manual')` → per i modelli coperti esegue `loadStaticDataset`
  (legge metadata + points, verifica `status='live'`, `point_count>0` e coerenza generazione);
  altrimenti **fallback controllato** sul fetch live esistente (`fetchWeatherData`).
- `applyStaticDataset` ricostruisce `provinceSamplePoints` (punti reali), `rawPointStores`,
  `modelStores`, densifica i punti virtuali (IDW, stesso modulo DENSIFY, ordine
  best_match → ecmwf_ifs → dual_best_ecmwf) e rifà il merge dual risk-preserving con
  `worstPointForProvince`/`assembleDualModelStores`. Nessuna funzione esistente rimossa.
- Pulsante "🔄 Aggiorna" → `requestWeatherData('manual')` (guardie anti doppie richieste).
- Attribuzione aggiunta nel footer: "Dati previsionali: © Open-Meteo → dataset derivato
  MeteoRisk (pipeline 1.0.0.8)".

## GitHub Action (`.github/workflows/update-weather-data.yml`)

- `schedule` ogni 10 minuti (cron `*/10 * * * *`) + `workflow_dispatch` opzionale con
  `force_update`. I check ogni 10 minuti rilevano SOLO cambiamenti reali (nuovo run ECMWF /
  Best Match cambiato); se nulla è cambiato il workflow esce pulito (clean success, zero lavoro).
- Il check (LEGGERO, Metadata API ECMWF IFS) precede ogni lavoro; exit 10 → clean
  success senza fetch; exit 1+ → workflow FAIL: in nessun caso un "nessun nuovo
  run" avvia fetch/build/commit/deploy.
- Interviene SOLO se la Metadata API segnala un nuovo run ECMWF (driver) oltre il
  grace period (10 min): da qui parte fetch coordinato (best_match + ecmwf_ifs) →
  build → validate → publish.
- Il commit avviene **solo** quando `publish_dataset.py` ritorna 0 (nuovo dataset valido).
- Nessun download senza run check; nessuna chiave/endpoint privati; solo stdlib Python.
- Deploy GitHub Pages SOLO su nuovo dataset valido (o `force_deploy`); la homepage
  è `_site/index.html` (micro-fix 1.0.0.6); `_site` contiene solo app + `data/latest`
  + `data/geography`.

## Come usare (deploy)

1. Copiare il contenuto di questa release nella root di un repository GitHub (il folder
   `MeteoRisk-Light-1.0.0.8-GitHub-Production-Hardening` **è** la root: contiene `.github/`, `data/`, `docs/`, `scripts/`, l'html e `VERSION`).
2. Abilitare GitHub Pages (deploy dal branch, workflow).
3. Il primo run con `workflow_dispatch` popolerà `data/latest` col dataset reale; fino ad
   allora l'app usa la rete al posto del dataset mancante (fallback controllato).

## Vincoli rispettati

- Non modifica 1.0.0.2 / 1.0.0.3 (baseline separate, immutate).
- Nessuna funzione applicativa rimossa; comportamento utente invariato per GFS/ARPAE e modi Sviluppo.
- Nessun backend, DB, API key, Vercel o proxy.
- Last known good: `data/latest` mai invalidato prima della validazione.