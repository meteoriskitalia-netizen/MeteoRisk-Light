# METEORISK LIGHT 1.0.0.4 — IMPLEMENTATION REPORT
## Centralized Data Pipeline — MeteoRisk Light
Data: 2026-09-05 · File app: `mri-light-1.0.0.4.html` · Tipo dataset: `derived_meteorological_risk_data`

---

### 1. Obiettivo
Eliminare le richieste browser verso l'API di Open-Meteo per i modelli serviti da un
**dataset derivato centralizzato**: la pipeline genera, valida e pubblica su GitHub Pages
i dati aggregati MeteoRisk; l'app li legge come JSON statico in `data/latest/`. Open-Meteo
resta l'unica fonte meteorologica (input), MAI ripubblicata come tale.

### 2. Contesto e vincoli
- Baseline immutate: 1.0.0.2 (stable) e 1.0.0.3 (audit) non modificate.
- Nessuna funzione applicativa rimossa; comportamento invariato per GFS/ARPAE ICON-2I e Modi Sviluppo.
- Nessun backend, DB, API key, Vercel o proxy. Solo file statici + GitHub Actions (Python stdlib).
- Endpoint Open-Meteo pubblici; logica del dataset = port fedele della logica client esistente.

### 3. Architettura dati
```
export: data/latest/{metadata.json, meteorisk-points.json, meteorisk-provinces.json, validation.json}
dataset_type: derived_meteorological_risk_data
modelli coperti: best_match, ecmwf_ifs (dual_best_ecmwf in app = merge client-side)
forecast: 3 giorni, timezone Europe/Rome, 48 variabili orarie (nomi esatti dell'app)
```

### 4. Port del campionamento (fedeltà verificata)
- v1 = **265** punti, v2 = **257** punti (order/coordinate IDENTICI all'app).
- Seed deterministico (coordIdx 0) = **107**; primo punto v2 = **VC** (vc_idx 101,
  45.555383 / 8.346284), ultimo = **SS** (87).
- `km2 = area_max * 9250`; `frac = base * factor * scale`, `scale = budget / max(1, wSum)`
  (budget 265, wSum 328.4, diff −3); ordinamento stabile per `(frac−target)`.
- Griglia gx,gy ∈ {1,3,5,7,9,11}; OROGRAPHY_CLASS last-wins (MB→L, RM→L, NU→M2);
  OROGRAPHY_FACTOR {M2:1.3, H:1.6, L:0.7}; spaziatura {H:18, M2:20, M:26, L:34}.
- Verifica: `scripts/tests/test_sample_port.py` (golden `golden_samples.json`) — PASS.

### 5. Pipeline (9 fasi)
1. `common.py` (config + port) 2. `generate_points.py` 3. `check_model_runs.py`
4. `fetch_source_data.py` 5. `build_meteorisk_dataset.py` 6. `validate_dataset.py`
7. `publish_dataset.py` 8. `data/state/last_model_run.json` 9. GitHub Action (commit/deploy).
- Nessun download senza run check (Metadata API, non rate-limitated; driver ARPAE ICON-2I,
  grace 10 min; exit 0/10/1).
- Raw in `data/_raw/` (MAI pubblicato); staging validato; publish atomico con rollback.

### 6. Validazione (risultati reali su fixture)
- **3.619 check eseguiti, 0 errori, outcome OK** (`validation.json`).
- Include: esistenza file, id contigui/ordinati, coerenza coordinate col port (257 seed),
  lunghezza array orari (72) e giornalieri (3), 107 capoluoghi, coerenza `selected_point`
  (id reale + score ricalcolato), metadata `models_covered`, checks territoriali, integrità.
- `validate_dataset.py` supporta `--staging` (default latest) e `--dir` (test negativi).

### 7. Pubblicazione
- Swap atomico staging→latest; backup in `data/_workdir/_latest_backup`; rollback su errore.
- Firma: `validation.json` riporta sha256 per points/provinces e byte reali:
  - `meteorisk-points.json` **11.962.133 byte** (sha256 `81114c3e…4744`)
  - `meteorisk-provinces.json` **132.035 byte** (sha256 `f2dee527…1269`)
- `data/state/last_model_run.json` → status `live`.
- Last known good: `data/latest` mai toccata prima di un esito valido.

### 8. Dataset pubblicato (fixture, day0 = 2026-09-05)
- `metadata.json` (951 B): status live, point_count **257**, province_count **107**,
  models_covered [best_match, ecmwf_ifs], forecast_days 3, timezone Europe/Rome.
- `meteorisk-points.json`: 257 punti reali con `models` (48 var orarie per modello) + `summary` (giorni).
- `meteorisk-provinces.json`: 107 province con `selected_point` e giorni.

### 9. Test negativi
`scripts/tests/test_negative_validation.py`: 3 scenari che devono FALLIRE (exit 1):
1) array orario 72→70; 2) tamper coordinate (points + provinces selected_point);
3) valore non numerico. Risultato: **3/3 PASS**; `data/latest` **intatta** (last known good).

### 10. Contract test del loader
`scripts/tests/contract_dataset_loader.mjs`: verifica il contratto consumato dall'app
(metadata, day0, id, 107 seed, 72h×3d, selected_point coerente, 48 variabili orarie,
`models_covered` anziché `models`). Risultato: **PASS (0 errori)**.

### 11. Migrazione app (mri-light-1.0.0.4.html)
- `APP_VERSION` → `'1.0.0.4'`; changelog aggiornato (5 voci ADD/REPL/DOC).
- Nuovi simboli: `DATASET_PREFIX`, `DATASET_COVERED_MODELS`, `datasetState`, `datasetLoading`,
  `datasetFetchJson`, `applyStaticDataset`, `loadStaticDataset`, `initWeatherData`, `requestWeatherData`.
- 2 call site modificati: pulsante "🔄 Aggiorna" → `requestWeatherData('manual')`;
  startup → `initWeatherData('startup')`. Il change-model handler resta invariato
  (cache-hit via `modelCacheUsable` copre gli swap; miss/null → fallback live corretto).
- Densifica IDW nello stesso ordine del fetch dual live: best_match → ecmwf_ifs → dual_best_ecmwf.
- Merge dual risk-preserving: `worstPointForProvince` + `assembleDualModelStores`
  (fallback single-store se un leg è vuoto). Zero richieste rete per i modelli coperti.
- Attribuzione: "Dati previsionali: © Open-Meteo → dataset derivato MeteoRisk (pipeline 1.0.0.4)".

### 12. Verifica sintattica
- Main `<script>` estratto (offset 88.362 → 1.650.111, 1.561.741 byte) e `node --check`: **PASS**.
- Script Python: compilazione verificata in E2E (validate/build/publish/test negativi).
- Fix durante la verifica: ripristinata la graffa di chiusura della funzione contenitore
  (inserimento loader) — il main script ora è bilanciato.

### 13. GitHub Action (`.github/workflows/update-weather-data.yml`)
- `schedule */15` + `workflow_dispatch` (`force_update` bypassa SOLO il "già processato").
- Permissions minime (`contents: write`, `pages: write`, `id-token: write`), concurrency pages.
- Commit esclusivamente quando `publish_dataset.py` ritorna 0 (nuovo dataset valido).
- Deploy Pages del sito statico (html + data/latest) ad ogni run; `_raw`/`_workdir`/`_staging` esclusi.

### 14. Copertura rete in app
- Prima (live): 1 richiesta per punto reale (257) + aggiornamenti per giorno/modello
  (ordine 10²–10³ richieste per avvio). Con dataset: **1–3 richieste** (2 JSON statici +
  eventuale fallback). Local è coperto da cache HTTP (`cache: 'default'`).

### 15. Limiti e note operative
- Da IP condiviso l'API forecast risponde 429: il dataset reale nasce sul primo run GitHub
  (IP runner dedicato). Finché `metadata.status='empty'` l'app usa il **fallback controllato**.
- "run già processato": i run non vengono mai copiati/ripubblicati (sono fonte, non prodotto).
- `generated_at` del placeholder fixture è una data di stampa fittizia (2026-01-01T00:00:00Z);
  il r-timestamp reale viene scritto dalla pipeline di produzione.

### 16. Struttura della release
```
MeteoRisk-Light-1.0.0.4-Centralized-Data-Pipeline/
├─ .github/workflows/update-weather-data.yml
├─ .gitignore
├─ VERSION
├─ mri-light-1.0.0.4.html
├─ data/
│  ├─ latest/            → dataset pubblicato (fixture valida)
│  ├─ geography/         → province_italiane.geojson, regions_data.json
│  ├─ state/             → last_model_run.json
│  ├─ _raw/ _workdir/ _staging/  → artefatti di lavoro (mai pubblicati)
├─ docs/                 → AUDIT, REPAIR_REPORT (1.0.0.2), LICENSES, + questi documenti
└─ scripts/              → pipeline + tests (golden, negativi, contract)
```

### 17. Verifica finale (checklist)
- [x] Port sampling fedele (v1=265 / v2=257) — golden test PASS
- [x] E2E fixture: build → validate (3.619 PASS) → publish (sizes reali sopra)
- [x] Test negativi 3/3 PASS + last known good preservato
- [x] Contract test Node PASS
- [x] Migrazione app: APP_VERSION e changelog aggiornati; loader presente; 2 call site
- [x] Sintassi main script HTML: node --check PASS
- [x] GitHub Action (schedule + dispatch, commit solo su dataset valido)
- [x] VERSION, .gitignore, docs (pipeline + report)
- [x] Baseline 1.0.0.2 / 1.0.0.3 NON modificate