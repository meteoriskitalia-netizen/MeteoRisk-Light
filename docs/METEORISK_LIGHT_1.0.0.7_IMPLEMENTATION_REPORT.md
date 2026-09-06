# METEORISK LIGHT 1.0.0.7 — IMPLEMENTATION REPORT

Data: 2026-09-06 · File app: `mri-light-1.0.0.7.html` · Release ZIP:
`MeteoRisk-Light-1.0.0.7-GitHub-Production-Hardening.zip`

## 1. Obiettivo della release

**AUDIT + REFACTOR di scheduling**: sostituzione del run driver ICON-2I
(`italia_meteo_arpae_icon_2i`) con **ECMWF IFS come driver unico del ciclo** e
**Best Match coordinato** (niente run_key proprio per best_match). Vincoli
rispettati: nessuna semplificazione delle protezioni esistenti, nessun fetch
continuo/pesante, budget mai ignorato, nessuna pubblicazione parziale, last known
good mai sovrascritto, nessuna modifica UI applicativa, struttura GitHub Pages
invariata (homepage = `_site/index.html`), rimozione di TUTTI i riferimenti
operativi ICON-2I (riferimenti storici/documentali ammessi).

Ordine seguito: **audit → modifica → test** (nessuna modifica cosmetica isolata).

## 2. Audit (PART 1) — cosa è stato letto

- `scripts/common.py` (config + stato), `check_model_runs.py`, `request_planner.py`,
  `fetch_source_data.py`, `build_meteorisk_dataset.py`, `validate_dataset.py`,
  `publish_dataset.py`, `workflow_gate.py`, `generate_points.py`;
- stato `data/state/last_model_run.json`, usage `data/state/api_usage.json`,
  metadata pubblicato `data/latest/metadata.json` (driver 1.0.0.6 = ICON-2I);
- grep trasversali: riferimenti `icon_2i|icon-2i|ICON-2I|arpae|ARPAE` (34 hit pre-audit),
  `best_match`/`ecmwf_ifs`/`run_key`, exit-code contract.

**Criterio di classificazione dei riferimenti ICON-2I**: *operativo* (detector,
stato, scheduling, metadata runtime, workflow decisionale) → RIMOSSO; *app/UI*
(voce modello, pannello Fonti & Metodologie, changelog) → MANTENUTO per vincolo
("nessuna modifica UI"); *storico/documentale* (report precedenti, audit
architetturale, licenze, README/header di migrazione) → MANTENUTO come registrazione.

## 3. Modifiche applicate

### Script (pipeline eseguita in ordine GitHub Action)
- `scripts/common.py` — rimosso `MODEL_RUN_DRIVER_MAP`; aggiunti
  `DRIVER_MODEL="ecmwf_ifs"`, `METADATA_MODELS_TRACKED=["ecmwf_ifs"]`; commento
  scheduling coordinato su `DUAL_MODELS="best_match,ecmwf_ifs"` (valore invariato:
  sono i leg coordinati scaricati insieme a ogni nuovo run ECMWF).
- `scripts/check_model_runs.py` — **riscritto**: query Metadata API SOLO su
  `ecmwf_ifs`; grace prudenziale dopo `available_time` (fallback
  `init + update_interval_seconds`, default 600 s); confronto con l'ultimo run
  processato (`last_processed_key`); `--provision-local` esclusivamente DEV
  (MAI in Action), `--dry-run`; exit contract invariato 0/10/1; nuovo schema stato.
- `scripts/request_planner.py` — header aggiornato (piano sui leg coordinati);
  logica invariata.
- `scripts/fetch_source_data.py` — header con ciclo coordinato; payload raw ora
  `"driver_model": common.DRIVER_MODEL` e `"models": common.DUAL_MODELS.split(",")`;
  **fix contabilità**: l'accounting del volume raw usava il default `requests=1`
  (gonfiava +1 per fetch); ora `requests=0` → 6 richieste reali registrate.
- `scripts/build_meteorisk_dataset.py` — `run_info["fetched_at"]` = `raw["fetched_at"]`
  reale; nuovo campo metadata `fetch_timestamps` (dataset_generation_timestamp,
  ecmwf_fetch_timestamp, best_match_fetch_timestamp, coordinated_cycle con la
  stessa `fetched_at` per entrambi i leg); guardia placeholder.
- `scripts/validate_dataset.py` — **+6 check live**: `driver_model==ecmwf_ifs`,
  presenza `run_key`/`run_init_ts`/`fetched_at`, presenza `fetch_timestamps`,
  uguaglianza `ecmwf_fetch_timestamp == best_match_fetch_timestamp` (coerenza
  temporale del ciclo). Totali reali: **3.625 check, 0 errori**.
- `scripts/workflow_gate.py` — docstring 1.0.0.7 (classificazione invariata).

### GitHub Action (`.github/workflows/update-weather-data.yml`)
- Header/commenti 1.0.0.7 (driver ECMWF, no polling pesante, best_match coordinato);
- step `Rileva nuovo run ECMWF IFS (Metadata API)`; step `Fetch coordinato
  ECMWF IFS + Best Match`; commit `chore(data): ... pipeline 1.0.0.7`;
- **build sito**: riga esatta `cp mri-light-1.0.0.7.html _site/index.html`
  (micro-fix 1.0.0.6 mantenuto in forma esatta, nessun commento sulla riga);
- summary `MeteoRisk Light 1.0.0.7 — Pipeline status`; workforce `*/15 * * * *`
  invariata (check leggero; fetch costoso solo con nuovo run ECMWF).

### Version bump (coerenza totale)
- `mri-light-1.0.0.6.html` → **`mri-light-1.0.0.7.html`**: `APP_VERSION='1.0.0.7'`,
  voce `1.0.0.7` in testa a `APP_CHANGELOG`, attribuzione footer
  "pipeline 1.0.0.7"; nessuna modifica UI/funzionale.
- `VERSION` (BUILD=7, NOTE coordinato, `APP_FILE=mri-light-1.0.0.7.html`),
  `README.md` (sezione Coordinated scheduling), `docs/API_BUDGET_MANAGEMENT.md`
  (header).

### Stato e documentazione
- `data/state/last_model_run.json` — migrato allo schema 1.0.0.7: solo
  `last_model_runs.ecmwf_ifs`, `driver_model: ecmwf_ifs`, nessun run best_match/
  ICON-2I. Rigenerato dall'E2E reale (status live, `last_processed_key`);
- `docs/CENTRALIZED_DATA_PIPELINE.md` — aggiornato a 1.0.0.7 (banner
  Coordinated Scheduling; pipeline dataset derivati invariata);
- `docs/METEORISK_LIGHT_DATA_ARCHITECTURE_AUDIT.md` — banner **SUPERSEDED
  (1.0.0.7)**; contenuto storico conservato;
- report storici 1.0.0.2–1.0.0.6 e `docs/LICENSES.md` — invariati (storici/
  fattuali).

### File modificati (lista audit)
`scripts/common.py` · `scripts/check_model_runs.py` (riscritto) ·
`scripts/request_planner.py` · `scripts/fetch_source_data.py` ·
`scripts/build_meteorisk_dataset.py` · `scripts/validate_dataset.py` ·
`scripts/workflow_gate.py` · `scripts/tests/gen_fixture_raw.py` ·
`.github/workflows/update-weather-data.yml` · `mri-light-1.0.0.7.html`
(version bump) · `VERSION` · `README.md` · `docs/API_BUDGET_MANAGEMENT.md` ·
`docs/CENTRALIZED_DATA_PIPELINE.md` · `docs/METEORISK_LIGHT_DATA_ARCHITECTURE_AUDIT.md`
(banner) · `data/state/last_model_run.json` (migrazione).

### Riferimenti ICON-2I — bilanciamento (valori verificati)
| Categoria | Pre-audit | Dopo refactor | Post-E2E | Note |
|---|---|---|---|---|
| Operativi (code/state/workflow/dataset) | 4 (3 state + 1 metadata) | 1 (metadata, in attesa E2E) | **0** | state migrato, metadata rigenerato (driver `ecmwf_ifs`) |
| App/UI (html) | 10 | 10 | 10 | vincolo "nessuna modifica UI": voce modello, Fonti & Metodologie, changelog |
| Storici/documentali | 20 | 20 | 20 | report 1.0.0.4/1.0.0.5, audit (banner SUPERSEDED), licenze, README/workflow header, nota VERSION |
| **Totale** | 34 | 31 | **30** | operativi = 0 |

## 4. Tabella di audit — PRIMA → DOPO → STATO

| Area | Prima (≤1.0.0.6) | Dopo (1.0.0.7) | Stato |
|---|---|---|---|
| Modello driver del ciclo | ICON-2I (`italia_meteo_arpae_icon_2i`) | **ECMWF IFS** (`ecmwf_ifs`), driver unico in `common.DRIVER_MODEL` | OK |
| Rilevamento nuovi run | Metadata API driver ICON-2I | **Metadata API SOLO ecmwf_ifs** (check leggero, exit 0/10/1) | OK |
| Coordinate Best Match | best_match senza coordinamento (aggiornato solo quando il driver cambiava) | **best_match aggiornato COORDINATO** con il run ECMWF nello stesso ciclo di fetch (`fetch_timestamps`, stessa `fetched_at`) | OK |
| Stato run | `last_model_runs` con 2 modelli (best_match + icon) | **solo `ecmwf_ifs`**, `run_key`/`last_processed_key` sul run ECMWF; niente run_key artificiale per best_match | OK |
| Budget API | pre-flight bloccante (limite, riserva 10%, usage persistito) | **invariato** (planner PLAN VALID / BUDGET BLOCKED rc2 / error rc1+); +fix over-count `requests=1` nel accounting bytes | OK |
| Grace period | 600 s dopo available_time | **invariato/prudenziale** (fallback avail = init + interval) | OK |
| Fetch | 2 leg best_match+ecmwf_ifs, batching 100, retry selettivi | **invariato**; raw header con `driver_model=ecmwf_ifs`, `models=[best_match,ecmwf_ifs]` | OK |
| Validazione | 3.619 check | **3.625 check** (+6: driver ECMWF, run_info chiavi, fetch_timestamps, coerenza temporale) | OK |
| Pubblicazione | swap atomico staging→latest, backup/rollback, last known good | **invariato**; publish rifiuta dataset non valido (`data/latest` mai toccato) | OK |
| GitHub Actions | check→planner→fetch→build→validate→publish→commit→deploy con exit audit | **invariato** con driver ECMWF e step/testi 1.0.0.7 | OK |
| GitHub Pages | homepage `_site/index.html` con micro-fix (`cp ... _site/index.html`) | **invariato**: riga esatta `cp mri-light-1.0.0.7.html _site/index.html` (nessun commento sulla riga) | OK |
| Riferimenti operativi ICON-2I | presenti (state, metadata, docs operativi) | **0** | OK |

## 5. Test A–G

- **TEST A (nessun nuovo ECMWF)**: reale → check **EXIT 10**, dedup, zero lavoro.
  Post-publish rieseguito → **10** di nuovo (run già processato). **PASS**
- **TEST B (catena completa reale)**: `check --force-update` (run disponibile, modo
  locale dev) → **0**; planner **0** (257 punti · 0 duplicati · 6 richieste,
  efficienza +97,67%, pre-flight 8988 disponibili); fetch **0** (257/257 ok, 6
  richieste, 194 s, best_match **stessa `fetched_at`** di ecmwf_ifs); build **0**;
  validate **0** (3.625 check); publish **0** (status=live, driver `ecmwf_ifs`,
  `fetch_timestamps` presenti). **PASS**
- **TEST C (grace period)**: harness con metadata sintetico (init futuro,
  available futuro) → **EXIT 10**, 1 sola Metadata API, stato non scritto. **PASS**
- **TEST D (budget)**: `METEO_RISK_API_DAILY_LIMIT=5` → planner **rc2 BUDGET
  BLOCKED**, safe success (today usage reading 6). **PASS**
- **TEST E (fetch failure)**: `workflow_gate fetch 1` → **exit 1** ([ERROR],
  workflow FAIL, mai silenziato); `fetch 2` → **exit 0** (safe skip, last known
  good preservato). **PASS**
- **TEST F (Best Match mancante)**: staging copia senza `best_match` su punto 0 →
  validate **rc 1** ([FAIL] modello best_match presente), nessuna pubblicazione. **PASS**
- **TEST G (validazione fallita)**: staging non valido → `publish_dataset` **rc 1**
  "Dataset NON valido … data/latest NON viene toccato"; hash SHA-256 del contenuto
  pubblicato **invariato**; + `test_negative_validation.py` (3/3, last known good). **PASS**

### Regressioni
`test_sample_port.py` (golden v1=265/v2=257) PASS · `test_planner_budget.py` 9/9 PASS ·
`test_negative_validation.py` 3/3 PASS · `test_workflow_gate.py` TEST A–F PASS ·
`contract_dataset_loader.mjs` (Node) PASS · `node --check` main script 1.0.0.7 PASS.

## 6. Consumo API reale (oggi, 2026-09-06)

`requests=6 · failed=0 · batches=3 · locations=257 · bytes=8.866.133` (3 batch × 2
leg coordinati). Uso storico preservato: 2026-09-05 → 12 richieste. Budget 10.000
giorno con riserva 10% (effettivo 9.000) → **disponibile 8.994**.

## 7. Note operative e residui

- `--provision-local`/`--force-update` restano SOLO strumenti dev/locali; mai
  nel workflow (documentato in `check_model_runs.py`).
- **Non verificabile in locale**: esecuzione reale della GitHub Action e validazione
  online HTTPS post-deploy GitHub Pages (i contratti sono coperti da
  `workflow_gate.py` + unit test; la struttura `_site/index.html` è quella già
  collaudata in 1.0.0.6 con la riga esatta `cp mri-light-1.0.0.7.html _site/index.html`).
- Il dataset pubblicato in `data/latest` è reale (run ECMWF 1788609600, fetch
  2026-09-06T00:00:50Z), driver `ecmwf_ifs`; nessun timestamp falsificato.

## 8. Checklist finale

- [x] Audit completato prima delle modifiche
- [x] Driver ECMWF IFS unico; best_match coordinato (nessun run_key artificiale)
- [x] Protezioni invariato: budget, grace, retry, last known good, exit-code contract, no-partial-publish
- [x] Version bump coerente (app, VERSION, README, workflow, docs, stato)
- [x] Micro-fix Pages `_site/index.html` con riga esatta `cp mri-light-1.0.0.7.html _site/index.html`
- [x] Riferimenti ICON-2I operativi = 0; storici/UI documentati e lasciati per vincolo
- [x] TEST A–G PASS + regressioni PASS
- [x] Repository pulito (raw/staging/workdir solo `.gitkeep`), dati reali in `data/latest`
- [x] ZIP finale costruito da copia pulita e verificato (niente payload/percorsi vietati)