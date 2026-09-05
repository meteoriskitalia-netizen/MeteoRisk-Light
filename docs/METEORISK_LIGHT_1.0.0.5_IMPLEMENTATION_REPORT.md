# METEORISK LIGHT 1.0.0.5 — IMPLEMENTATION REPORT
## Production Pipeline Validation & GitHub Readiness — MeteoRisk Light
Data: 2026-09-06 · File app: `mri-light-1.0.0.5.html` · Tipo dataset: `derived_meteorological_risk_data`

---

### 1. Obiettivo
Portare la pipeline centralizzata introdotta in 1.0.0.4 a livello **produzione validate**:
planner dedicato con dedup coordinate e batching, gestione esplicita del **budget API**
con riserva di sicurezza e telemetria persistente (`data/state/api_usage.json`), retry
limitati/selettivi, audit degli exit code nella GitHub Action (nessun errore mai silenziato),
deploy Pages **solo** su nuovo dataset valido, e generazione di **almeno un dataset REALE**
Open-Meteo (non fixture) pubblicato in `data/latest/`.

### 2. Audit dello stato (addendum §AUDIT)
- Il dataset `data/latest` di 1.0.0.4 è risultato **FIXTURE/demo**, non reale:
  `generated_at="2026-01-01T00:00:00Z"` (data di stampa fittizia), `run_info` tutto null,
  `day0=2026-09-05` inconsistente con `generated_at`, nessuna nota.
- Base punti reale: `data/_workdir/real_points.json` = **257 punti**; port campionamento
  v1=265 / v2=257 confermato dal golden test (invariato).
- Blocco rete locale: l'endpoint forecast Open-Meteo da IP condiviso rispondeva 429;
  **risolto** nel corso della validation (HTTP 200). Limite osservato: 429 "Minutely API
  request limit exceeded" con ~5 richieste/min da IP condiviso.

### 3. Architettura pipeline (cambi 1.0.0.5)
```
request_planner.py   → dedup coordinate + piano batch + PRE-FLIGHT budget (exit 0/2/1)
check_model_runs.py  → run driver ARPAE ICON-2I via Metadata API (exit 0/10/1)
fetch_source_data.py → fetch batch per leg modello, retry selettivo, usage (exit 0/2/3/1)
build_meteorisk_dataset.py → dataset derivato 257 pt / 107 province
validate_dataset.py  → 3.619 check (0 errori) — exit 1 su fallimento (publish SKIP)
publish_dataset.py   → swap atomico, status live, last known good preservato
GitHub Action        → audit exit code; deploy Pages SOLO su nuovo dataset valido
```

### 4. Dedup e batching (efficienza)
- `unique_coordinates()`: della lista punti sono state contate **257 coordinate uniche,
  0 osservazioni duplicate** → nessuna richiesta ridondante.
- Calibrazione misurata: 10 località → URL 1.4 KB / risp. 411 KB; 100 → 3.2 KB / 4.1 MB
  (sicuro); 257 in una sola richiesta → 429 (minutely). Soglia scelta:
  `BATCH_MAX_LOCATIONS=100`.
- **Scoperta chiave**: la risposta multi-location di Open-Meteo NON annida i modelli per
  elemento (array flat). Ricostruzione dual point-to-point con **2 leg per blocco**
  (una richiesta `models=best_match`, una `models=ecmwf_ifs`), forma verificata:
  48 variabili, 72 orari, 3 giornalieri per leg.
- Risultato piano (artefatto `data/_workdir/api_efficiency/request_plan.json`):
  naive **257** → ottimizzate **6** (batch 1×100, 2×100, 3×57 · n_model_legs=2),
  risparmio **251**, efficienza **+97.67%**.

### 5. API budget e telemetria (vedi `docs/API_BUDGET_MANAGEMENT.md`)
- `API_DAILY_LIMIT=10000`, `API_SAFETY_RESERVE_FRAC=0.1` → budget effettivo **9000 / gg**;
  `API_MIN_REQUEST_INTERVAL_S=30.0` (pacing), `RETRY_LIMIT=3`,
  `RETRY_BACKOFF_BASE_S=5.0` (backoff 5·2^i, cap). Altre sovrascrivibili via `METEO_RISK_*`.
- Stato persistente `data/state/api_usage.json` per giorno UTC (requests/failed/batches/
  locations/bytes). Rollover giornaliero su data.
- PRE-FLIGHT **bloccante** in planner (exit 2) e in fetch (exit 2): nessun download se
  disponibile < richieste pianificate.
- Telemetria efficienza per run: `data/_workdir/api_efficiency/fetch_<ts>.json`
  (naive/optimized/saved/efficiency, elapsed, esiti, usage_after).
- **Risultato reale fetch**: 6 richieste per 257 punti, 0 fallimenti, 152.6 s,
  raw 8.848.830 byte in `data/_raw/source_raw.json` (MAI pubblicato);
  usato oggi **6** (di 9.000 effettivi) → disponibile 8.994.

### 6. Retry limitati e selettivi
- Errore non-HTTP/5xx → nda `{RETRY_LIMIT}` tentativi con backoff esponenziale;
  429/minutely → attesa 60 s (una volta sola per richiesta). **Mai rieseguita l'intera
  run**: al 1° passaggio solo i batch falliti vengono ritentati una volta (selettivo).
- Nessuna parzialità pubblicata: un fallimento residuo → exit 1, build/validate/publish SKIP.

### 7. Exit code audit (workflow, scenario A–F)
| Codice | Significato | Azione workflow |
|---|---|---|
| planner 0 | piano OK, pre-flight superato | prosegue |
| planner 2 | budget bloccante | fetch/build/… SKIP, summary avvisa |
| planner 1 | zero punti/errore | job error (mai silenziato) |
| check 0 | nuovo run usabile | prosegue verso fetch |
| check 10 | nessun nuovo run (grace / già processato) | fetch/build/… SKIP |
| check 1 | errore Metadata API | job error (mai silenziato) |
| fetch 0 | fetch completo, 0 fallimenti | prosegue verso build |
| fetch 2 | budget insufficiente | build SKIP, summary avvisa |
| fetch 3 | chiavi/coordinate/capoluoghi mancanti | build SKIP, nessuna parzialità |
| fetch 1 | fallimenti residui | job error |
| validate 1 | dataset non valido | publish SKIP → `data/latest` INTATTA |
| publish 0 | swap avvenuto | commit + (se nuovo dataset) deploy Pages |

- Scenario di riferimento A–F: (A) run già processato/graceless → 10, zero richieste;
  (B) budget corto → nessun download; (C) dati mancanti → nessuna pubblicazione parziale;
  (D) errori parziali → retry selettivo e mai parziale; (E) errore tecnico → job in errore
  visibile; (F) dataset invalido → last known good preservato.
- Il deploy Pages è eseguito **solo** quando `publish` indica un nuovo dataset valido
  oppure `force_deploy` (bootstrap). Step terminale "Summary pipeline" sempre eseguito.

### 8. Fix di robustezza applicati durante la validation
- **Bug dedup "già processato"**: `check_model_runs` leggeva `last_processed_key` dal dict
  nidificato dei metadati (dove non è mai salvato) → valeva sempre `null` e lo stesso run
  sarebbe stato rifetched ogni 15 min. Corretto leggendo il campo **top-level** dello
  state; `publish_dataset` ora registra `last_processed_key/last_processed_at`.
- **Bug conteggio richieste**: `record_api_usage` contava i batch (3) invece delle richieste
  di rete reali (batch × n_model_legs). Corretto a **6** per la run reale; il byte-volume
  raw viene ora registrato nello stato usage.

### 9. Run reale generata e pubblicata (non fixture)
- Run driver `italia_meteo_arpae_icon_2i`, **run_key 1788609600** = 2026-09-05 12:00Z,
  `run_available_ts 1788652800` (2026-09-06 00:00Z, update_interval 43200 s);
  `ecmwf_ifs` stesso init, update 21600 s.
- Registrazione run: `check_model_runs.py --provision-local` (flag SOLO dev, fuori dalla
  cadenza dell'Action, nessun rischio per la produzione); `status=pending` → `live` dopo
  publish. Nota registrata in state: "PROVISION-LOCALE (dev…)".
- `metadata.json` reale: `run_info` popolato con `run_key`, `driver_model`,
  `run_init_ts`, `run_available_ts`, `fetched_at`; `day0=2026-09-05`; `generated_at`
  reale. `models_covered=[best_match, ecmwf_ifs]`, `point_count=257`,
  `province_count=107`.

### 10. Validazione reale → publish
- Build: **257 punti / 107 province**; validate: **3.619 check, 0 errori, outcome PASS**
  (esistenza file, id contigui, coordinate col port, 72h×3d, 107 capoluoghi,
  `selected_point` coerente — incluso worst-point ricalcolato, bounds lat/lon,
  modelli dual presenti, integrità/provenienza).
- Publish atomico con rollback + last known good. File in `data/latest/` (byte reali):
  - `metadata.json` **1.037 B**
  - `meteorisk-points.json` **9.130.209 B** (~9.1 MB, valori reali differenziati per punto)
  - `meteorisk-provinces.json` **124.031 B**
  - `validation.json` (3619 check)
- `data/state/last_model_run.json` → `status=live`, `last_processed_key=1788609600`.

### 11. Test (tutti PASS sul dataset reale)
- `test_sample_port.py` (golden): v1=265 / v2=257, 107 seed, primo VC / ultimo SS,
  non-capoluoghi dentro i poligoni (0 violazioni) — **PASS**.
- `test_negative_validation.py`: 3 scenari tamper rifiutati (exit 1) + `data/latest`
  intatta — **3/3 PASS**.
- `test_planner_budget.py` (unit): dedup, conteggi batch, pre-flight ok/blocked/boundary,
  roundtrip usage, rollover giornaliero — **9/9 PASS**.
- `contract_dataset_loader.mjs` (Node): metadata, 257 pt, 107 seed, 72h×3d,
  `selected_point` coerente, 48 variabili per modello — **PASS (0 errori)**.
- Sintassi main script HTML (1.565.865 B) via `node --check` — **PASS**.
  (Nota: l'ancoraggio `</script>` penultimo è errato quando il file ha più blocchi; il
  main script reale termina all'**ultimo** tag e va selezionato dal marker
  `APP VERSION SYSTEM`.)

### 12. Validazione HTTP (server locale, sezione app/browser)
Servendo la release con `python -m http.server 8799`:
- `/mri-light-1.0.0.5.html` → **200** (1.654.794 B), contiene `APP_VERSION='1.0.0.5'`
- `/data/latest/metadata.json` → **200** (1.037 B), `status=live`, `day0=2026-09-05`
- `/data/latest/meteorisk-points.json` → **200** (`Content-Length` 9.130.209)
- `/data/latest/meteorisk-provinces.json` → **200** (124.031 B)
- path inesistente → **404** (atteso)
Risultato: **PASS**. La verifica in HTTPS su GitHub Pages e in modalità browser online
restano un passo residuo post-push (vedi §15).

### 13. App 1.0.0.5
- Rinomata `mri-light-1.0.0.5.html`; `APP_VERSION='1.0.0.5'`; changelog aggiornato
  (produzione pipeline); attribuzione footer → "pipeline 1.0.0.5".
- Comportamento app invariato: lettura `data/latest/{metadata.json, meteorisk-points.json}`,
  fallback controllato su Internet se il dataset non è testabile, convergenza dual
  best_match + ecmwf_ifs (dual_best_ecmwf) preservata.

### 14. Struttura release 1.0.0.5
```
MeteoRisk-Light-1.0.0.5-Production-Pipeline-Validation-GitHub-Readiness/
├─ .github/workflows/update-weather-data.yml   → audit exit code, deploy gated
├─ .gitignore                                  → incl. data/_workdir/api_efficiency/
├─ README.md · VERSION
├─ mri-light-1.0.0.5.html
├─ data/
│  ├─ latest/          → dataset REALE pubblicato (run 1788609600)
│  ├─ geography/  state/  → geografie + last_model_run.json, api_usage.json
│  ├─ _raw/  _workdir/  _staging/  → artefatti (mai pubblicati)
├─ docs/  → report 1.0.0.5, API_BUDGET_MANAGEMENT, pipeline 1.0.0.4, audit, licence
└─ scripts/  → pipeline + common.py + tests (golden, negativi, planner, contract)
```

### 15. Limiti residui (da eseguire su GitHub)
- Run reale della GitHub Action e deploy GitHub Pages in produzione (serve repository+/token).
- Validazione HTTPS/browser online dell'app contro il dataset servito (browser-based,
  MIME, CORS statico) — qui solo la parte HTTP locale §12.
- `actionlint`/validazione YAML-runner-specifica (non disponibile in locale).

### 16. Checklist criteri di successo
- [x] Audit stato: dataset 1.0.0.4 = fixture (documentato), base reale 257 pt
- [x] Blocco rete risolto; calibrazione batch (100) documentata
- [x] Dedup coordinate (0 duplicati) e 2 leg: naive 257 → **6** richieste (+97.67%)
- [x] Budget: pre-flight bloccante, riserva 10%, `api_usage.json` con telemetria
- [x] Retry limitati/selettivi (mai replica intera; 429 → 60 s)
- [x] Exit code audit completo A–F; nessun errore silenziato; deploy Pages gated
- [x] Bug dedup "già processato" e conteggio usage corretti e ri-verificati
- [x] **Dataset reale** generato e pubblicato (run 1788609600, day0 2026-09-05)
- [x] Validate reale 3.619/0 PASS; publish atomico; test 4/4 + node --check PASS
- [x] Validazione HTTP locale PASS; app APP_VERSION 1.0.0.5 servita
- [x] Docs: report 1.0.0.5, API_BUDGET_MANAGEMENT, README; VERSION/.gitignore
- [~] Residuo: Actions reali + HTTPS/browser online (post-push, §15)
- [x] Baseline 1.0.0.2 / 1.0.0.3 / 1.0.0.4 NON modificate