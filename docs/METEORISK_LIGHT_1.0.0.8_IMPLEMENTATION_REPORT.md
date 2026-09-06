# METEORISK LIGHT 1.0.0.8 — IMPLEMENTATION REPORT

Data: 2026-09-06 · File app: `mri-light-1.0.0.8.html` · Release ZIP:
`MeteoRisk-Light-1.0.0.8-GitHub-Production-Hardening.zip`

## 1. Obiettivo della release

Quattro attività indipendenti su `MeteoRisk-Light-1.0.0.7`:

- **PARTE A — Fix UI "Previsioni"**: il select delle fasce (Mattina/Pomeriggio/
  Sera/Notte) era da ricostruire in modo robusto dopo il refactor dati.
- **PARTE B — Best Match indipendente**: canary dedicato (`check_best_match.py`)
  + decision engine `decide_cycle.py` che sceglie `cycle_mode` tra
  `coordinated` / `best_match_only` / `none`; fingerprint di contenuto
  (`best_match_fingerprint`) registrato in publish.
- **PARTE F — Version bump** 1.0.0.7 → 1.0.0.8 (app, VERSION, README, docs, workflow).
- **PARTE G — INITIAL DATASET BOOTSTRAP**: il rilascio **non contiene dataset
  live** (`data/latest/*.json` assenti); il primo dataset è generato dal primo
  run della GitHub Action (`bootstrap_pending`).
- **PARTE H — Robustezza rete / Metadata API**: retry controllati, timeout
  esplicito, backoff con jitter; un errore di rete NON viene mai interpretato
  come "nessun nuovo run".

Ordine seguito: **audit → modifica → test**.

## 2. Audit — cosa è stato letto

- `scripts/common.py`, `check_model_runs.py`, `request_planner.py`,
  `fetch_source_data.py`, `build_meteorisk_dataset.py`, `validate_dataset.py`,
  `publish_dataset.py`, `workflow_gate.py`, `generate_points.py`;
- stato `data/state/last_model_run.json`, usage `data/state/api_usage.json`,
  workflow `.github/workflows/update-weather-data.yml`, README + docs pipeline;
- UI: `getForecastSlotData`, `toggleForecast`, `applyStaticDataset`,
  `rebuildForecastSlotOptions`, select `#forecast-slot`.

## 3. PARTE A — Fix UI "Previsioni" (la fascia select)

**Causa radice**: `getForecastSlotData` era scritta assumendo l'array `time`
(dataset 1.0.0.3); dopo la distribuzione "piegata" a 24 ore/giorno senza
`time`, le fasce non erano più calcolabili in modo affidabile, quindi il select
veniva lasciato "vuoto/tutto il giorno" o popolato in modo incoerente.

**Fix** (helper puri estratti in un blocco `//#pure#`:
- `hourlyLength(h)` → lunghezza oraria dei dati (max tra i campi `hourly`);
- `fasciaIndices(slotKey, dayIndex, hLen)` → `{startIdx, count}` su finestre
  da 6 ore (Mattina 6, Pomeriggio 12, Sera 18, Notte 0+1 giorno); ritorna
  `null` se la fascia non è rappresentabile — **mai un valore inventato**;
- `rebuildForecastSlotOptions()` → ricostruisce il select: solo "Tutto il
  giorno" se i dati sono < 48h; altrimenti le 4 fasce. Stesso set di opzioni
  già presenti → nessuna riscrittura del DOM (idempotente).

## 4. PARTE B — Best Match indipendente (canary + decision engine)

### 4.1 Decisioni tecniche

- **6 sentinelle** `("MI","VE","RM","PE","LE","PA")` — capoluoghi pubblicati
  (`coordIdx == 0`), aree NW-Po / NE / Tirreno-centro / Adriatico / Sud / Isole.
- **Una sola richiesta multi-location per ciclo** — `BEST_MATCH_CHECK_HOURLY =
  "weathercode,precipitation"` (leggero; niente array grandi).
- **Fingerprint**: SHA-256 di `{day0, sentinels:{sigla:{weathercode,
  precipitation}}}`; **tutte le 6 sentinelle sempre presenti** (padding con
  array vuoti), contenuti canonici (float→int dove necessario),
  **nessun timestamp di generazione** → deterministico e sensibile al contenuto.
- **Quando si aggiorna**: solo in `publish_dataset.py` a dataset valido messo in
  `data/latest` (`state.last_best_match_changed` = `last_changed_at` solo quando
  il contenuto cambia).

### 4.2 Exit codes (contratto stabile, esteso)

| fase | 0 | 1 |
|---|---|---|
| `check_model_runs.py` | nessun nuovo run / fetch OK | failure (retry esauriti, H) |
| `check_best_match.py` | `changed` | `unchanged` (10) / error (1) |
| `decide_cycle.py` | ok | failure / `budget_blocked` (2) / bootstrap blocked (3) |
| `fetch_source_data.py` | ok | error / `budget_safe` (2) / capoluoghi safe (3) / bootstrap fatal (4) |
| build/validate/publish | ok | failure |

### 4.3 Matrice di decisione (`decide_cycle.py`)

| ECMWF nuovo | Best Match cambiato | ciclo |
|---|---|---|
| sì | qualunque | `coordinated` |
| no | sì | `best_match_only` (3 richieste; ECMWF preservato dal raw precedente) |
| no | no | `none` (clean exit) |
| **bootstrap** | — | override → `coordinated` |

## 5. PARTE G — INITIAL DATASET BOOTSTRAP

- **G1** → niente `data/latest/*.json` nel rilascio: la cartella contiene solo
  `.gitkeep`; lo stato è `bootstrap_pending = true`.
- **G3/G5** → `is_bootstrap_pending()` è vera se: state assente, dataset
  assente, o fingerprint Best Match assente. Uno stato bootstrap **non viene
  mai trattato** come "nessun cambiamento"/"già processato".
- **G6** → un primo ciclo fallito = workflow FAIL esplicito; nessun file
  finto/parziale al posto del dataset.

## 6. PARTE H — Robustezza rete / Metadata API

- **H1** retry controllati, `METADATA_API_RETRIES = 4` (3-4 totali, MAI infiniti)
  per soli errori transienti (SSL/keepalive handshake timeout, TimeoutError,
  URLError transitorio, connection reset, HTTP 429/5xx).
- **H2** timeout esplicito connect/read `OPENMETEO_METADATA_TIMEOUT_S = 15s`.
- **H3** exponential backoff `METADATA_RETRY_BASE_S = 2.0` + jitter
  `RETRY_JITTER_MAX_S = 1.5` (anche nel fetch forecast).
- **H4** `NETWORK ERROR ≠ NO NEW RUN`: lo state, il fingerprint e
  `last_processed_run_key` si aggiornano **solo dopo un check riuscito**.
- **H5** retry esauriti → CHECK FAILED con messaggio `METADATA API UNAVAILABLE
  AFTER RETRIES` e summary "Metadata API unavailable after retries / No data
  fetch performed / Last dataset unchanged".

## 7. File modificati

- **UI**: `mri-light-1.0.0.8.html` — helper puri `//#pure# fasciaIndices`,
  `rebuildForecastSlotOptions`, `getForecastSlotData` senza guard `h.time`,
  call sites in `toggleForecast`/`applyStaticDataset`, `APP_VERSION`,
  changelog, footer `pipeline 1.0.0.8`; file rinominato da `mri-light-1.0.0.7.html`.
- **Script**: `common.py` (sentinelle, fingerprint, bootstrap helpers,
  `fetch_best_match_check`, Parte H), `check_best_match.py` (nuovo canary),
  `decide_cycle.py` (nuovo; fix shadowing `fetch_mode` → `fetch_mode_for`),
  `check_model_runs.py` (bootstrap-aware + H5), `fetch_source_data.py`
  (`--mode`, `merge_best_match_only`, `leg_timestamps`, `cycle_mode`, rc 3/4),
  `build_meteorisk_dataset.py` (leg timestamps + `update_strategy`),
  `validate_dataset.py` (strategy/timestamps + day0), `publish_dataset.py`
  (fingerprint solo a dataset valido), `workflow_gate.py` (`classify_best`,
  plan rc 3, fetch rc 4).
- **Workflow**: `.github/workflows/update-weather-data.yml` — ordine
  points → check → canary (`best`, sempre) → decide/plan → fetch gated
  `plan_state == 'ok'` → build → validate → publish → commit
  ("... pipeline 1.0.0.8") → Pages → summary (bootstrap + H5).
- **Meta**: `README.md`, `VERSION` (BUILD=8), `docs/CENTRALIZED_DATA_PIPELINE.md`.
- **Test**: nuovi `test_best_match_canary.py`, `test_decide_cycle.py`,
  `test_bootstrap.py`, `test_metadata_retry.py`, `test_fascia_slots.mjs`;
  aggiornati `test_workflow_gate.py`, `test_negative_validation.py`,
  `contract_dataset_loader.mjs` (baseline offline da fixture in assenza di
  dataset live — Parte G).

## 8. Stato dati al rilascio (Parte G)

- `data/latest/` → solo `.gitkeep` (nessun dataset live).
- `data/state/last_model_run.json` → `bootstrap_pending: true`,
  `ecmwf/init/best_match fingerprint/dataset = null`.
- `data/state/api_usage.json` → solo configurazione (nessun consumo).
- Primo run della Action: `cycle_mode=coordinated` (bootstrap override),
  piano 6 richieste, publish del primo dataset.

## 9. Tabella test

| Suite | esito |
|---|---|
| `test_best_match_canary.py` (canary + fingerprint) | PASS |
| `test_decide_cycle.py` (matrice + pre-flight budget + rc) | PASS |
| `test_bootstrap.py` (G1/G3/G5/G6) | PASS |
| `test_metadata_retry.py` (H1-H5) | PASS |
| `test_workflow_gate.py` (A-F + best stage) | PASS |
| `test_sample_port.py` (golden 265/257, 107 capoluoghi) | PASS |
| `test_planner_budget.py` (9 test) | PASS |
| `test_negative_validation.py` (3 manomissioni + baseline intatto) | PASS |
| `contract_dataset_loader.mjs` (contratto loader sul baseline) | PASS |
| `test_fascia_slots.mjs` (helper puri + rebuilding DOM) | PASS |
| `node --check` sui 3 blocchi inline dell'HTML | PASS |
| `py_compile` su tutti gli script della pipeline | PASS |

## 10. Verifica smoke (non commitata)

- fingerprint: deterministico, content-sensitive, day0-sensitive;
- `is_bootstrap_pending`: matrice completa (state assente / dataset assente /
  fingerprint assente / tutti presenti);
- matrice decisionale `(ecmwf_new, best_changed)` → cycle_mode;
- contratto exit code gate (plan rc3 bootstrap, fetch rc4 fatale, best rc
  0/10/1);
- `fetch --dry-run`: coordinated BOOTSTRAP = 6 richieste; `best_match_only`
  in bootstrap → rifiutato (rc 1, incoerenza con Parte G);

## 11. Note operative

- I test sono eseguibili in locale con `py scripts/tests/<file>.py` e
  `node scripts/tests/<file>.mjs` (root della release); niente pytest.
- I test che richiedono un dataset (negativi, contratto loader) generano un
  **baseline offline** tramite il fixture sintetico in `data/_staging` quando
  `data/latest` è vuoto (Parte G).
- `data/_workdir`, `data/_staging`, `_raw` e le cartelle `__pycache__` sono
  escluse dal rilascio.