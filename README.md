# MeteoRisk Light — edizione 1.0.0.9 (Default V2 vista continua + V3 restore + mapping "Oggi")

Pannello meteorologico statico per le province italiane. La 1.0.0.9 mantiene tutte le
novità della 1.0.0.8 (elencate sotto, pipeline coordinata driver ECMWF IFS) e aggiunge
lato applicazione:
- **Vista continua default V2** (uniforme per province: ancore virtuali al centroide +
  raggio adattivo + nearest + sfumatura a maschera). V3 resta selezionabile nel
  selettore "Sfumatura"; V1 resta il metodo storico bit-identico.
- **V3 ripristinata al concetto nato**: firma nativa `renderContinuousV3(points,
  colorReal)` di 10.52.27.0 (lattice virtuale ~0,25° solo raster, IDW dei COLORI dei
  punti campione, modulo modello corrente con merge dual incluso).
- **Mapping "Oggi" ↔ giorno-dataset**: l'etichetta "Oggi" è risolta sulla data ISO reale
  Europe/Rome e cercata nei giorni del dataset a partire da `dataset.day0` (offset, mai
  assunzione `dayIndex===0`); giorno non coperto → messaggio onesto, nessun dato finto.
- **Hotfix mapping (scarto +1)**: rimosso il clamp dell'offset negativo — se il dataset
  parte da un giorno futuro "Oggi" NON mostra più il primo giorno (domani); compare il
  messaggio di disponibilità e "Domani"/"Dopodomani" ricadono esattamente sui giorni.
- **Hotfix cache/alveo (scarto +1 persistente + slider)**: metadata.json è SEMPRE letto
  con `cache: 'no-store'` (fonte di verità per `day0`) e `meteorisk-points.json` è caricato
  con URL cache-busted legato a `generated_at` (`?v=...`) — le due risorse non possono più
  essere servite da generazioni diverse, che era la causa dello scarto +1 online.
  Difesa in profondità: `updateUI()` valida `currentDay`/ora sulla lunghezza REALE degli
  array caricati (messaggio esplicito, nessuna eccezione silenziosa) e lo slider orario è
  protetto da try/catch.

## Novità della 1.0.0.8 (mantenute)

1. **Best Match Canary indipendente** — 6 capoluoghi-sentinella (MI, VE, RM, PE,
   LE, PA), *stesse coordinate* dei punti pubblicati, controllati ad ogni ciclo
   con **una** richiesta forecast multi-location (weathercode+precipitation
   orarie). La **fingerprint SHA-256 del solo contenuto** (giorno0 + serie
   orarie delle sentinelle, nessun generation-time) è registrata alla
   **pubblicazione** (stato == dataset): atomicità tra check e dataset.
2. **Decision engine stateless + bootstrap** (`decide_cycle.py`): matrice a 3
   esiti (FIX PIPELINE: la modalità parziale `best_match_only` è rimossa, nessuna
   dipendenza dal raw di un ciclo precedente) — `coordinated` (fetch completo di
   entrambi i leg: ECMWF nuovo O Best Match cambiato) · `none` (clean exit, zero
   lavoro) · `bootstrap` (primo dataset reale). Priorità 1..4, **API USAGE
   GUARDRAILS** pre-flight: hard safety ceiling centralizzato
   (`data/state/api_usage.json`, forse sovrascritto da env), blocco SOLO oltre
   il tetto — nessun razionamento preventivo; osservabilità separata (checks
   Metadata, canary, fetch, retry, riuscite/fallite per giorno).
3. **Fix "Previsioni" fascia oraria** — il selettore delle fasce (mattina/
   pomeriggio/sera/notte) era legato all'array `time` assente nello schema
   derivato; riscritto con helper puri (`hourlyLength`/`fasciaIndices`) che usano
   solo le serie orarie del dataset. Preview "fascia" riaperta solo con ≥48 valori
   orari; mai valori inventati fuori range.
4. **PARTE G — INITIAL DATASET BOOTSTRAP** — questo rilascio **NON contiene un
   dataset meteorologico pre-generato**: `data/latest/` contiene solo `.gitkeep`.
   Il primo dataset è generato **esclusivamente** dal primo run della GitHub
   Action (check → canary → decide/plan → fetch → build → validate → publish →
   commit → Pages). Stato `bootstrap_pending`; un fallimento del primo ciclo fa
   **fallire il workflow** senza pubblicare file falsi o parziali (requisito G6).
5. **PARTE H — robustezza rete / Metadata API**: il check dei run ignora errori
   transienti di rete/TLS con retry automatici limitati (4 tentativi, mai
   infiniti), timeout esplicito connect/read (15 s) e exponential backoff +
   jitter su SSL/keepalive handshake timeout, TimeoutError, connection reset,
   HTTP 429 e 5xx. **NETWORK ERROR ≠ NO NEW RUN**: uno stato/fingerprint si
   aggiorna solo dopo un check riuscito; esausti i retry → workflow FAIL con
   report "Metadata API unavailable after retries · No data fetch performed ·
   Last dataset unchanged".

- App: `mri-light-1.0.0.13.html` (nessuna dipendenza runtime; solo static assets).
- Dati: dataset derivato in `data/latest/` — al primo deploy generato da GitHub
  Actions (zero dataset locali consegnati, Parte G).
- Fonti: Open-Meteo (unica fonte meteorologica, input — MAI ripubblicata come tale).
- Homepage GitHub Pages: `_site/index.html` (micro-fix mantenuto, `cp
  mri-light-1.0.0.13.html _site/index.html`).

## Scheduling 1.0.0.9 (coordinato + canary + bootstrap, dalla 1.0.0.8)
1. **Driver run unico = ECMWF IFS**: `check_model_runs.py` interroga SOLO
   `ecmwf_ifs` (Metadata API, non conteggiata nel budget forecast). Exit code:
   `0` = NEW ECMWF RUN · `10` = NO NEW ECMWF RUN · `1+` = TECHNICAL ERROR (FAIL).
2. **Canary Best Match**: `check_best_match.py`, 1 richiesta/ciclo (~1% del
   budget effettivo). `best` rc `0` = CAMBIATO · `10` = INVARIATO · `1+` = FAIL.
3. **Decision engine**: combina i due rilevatori e, in assenza di stato/dataset/
   fingerprint (`bootstrap`), forza il primo fetch reale coordinato.
4. **Budget vincolo assoluto**: pre-flight in decide_cycle e fetch. Steady state
   bloccato → safe skip (rc 2/rc 3, last known good preservato). **Bootstrap
   bloccato → workflow FAIL (rc 3/rc 4)**: non esiste un dataset da preservare.
5. **Ordine workflow**: punti (se necessari) → check ECMWF → canary Best Match →
   decide/plan → fetch (`--mode` dal piano) → build → validate → publish (con
   fingerprint del contenuto) → commit → deploy → summary.

## Struttura
```
.github/workflows/update-weather-data.yml   # pipeline 1.0.0.9 (exit code audit, deriva dalla 1.0.0.8)
scripts/                                    # pipeline + workflow_gate + test
data/latest/                                # SOLO .gitkeep finché la prima GitHub Action non genera il primo dataset (Parte G)
data/state/                                 # last_model_run.json (bootstrap_pending iniziale), api_usage.json (solo config)
data/geography/                             # boundaries richieste dal sito (statiche, non dataset meteorologico)
docs/                                       # report, budget API, pipeline, licenze
```

## Efficienza (invariata)
257 punti → **6 richieste** Open-Meteo per ciclo coordinato (+97.67%): dedup (0
duplicati), batch a 100 × 2 leg modello (best_match + ecmwf_ifs). Canary Best
Match: 1 richiesta/ciclo. Fetch SEMPRE completo (stateless): nessun refresh
parziale del solo best_match; il raw è scritto e consumato nello stesso run.

## Budget API e protezioni (invariate)
- Check dei run = Metadata API (non conteggiata); fetch forecast SOLO con
  pre-flight budget bloccante (limite 10000/giorno, riserva 10%,
  `data/state/api_usage.json`).
- Retry selettivi e limitati; mai ripetizioni integrali del piano.
- Last known good preservato: fetch/build/validation falliti non toccano
  `data/latest/`; publish atomico con backup/rollback. In bootstrap un fallimento
  = workflow FAIL (nessun file finto/parziale).

## Test
Test 1–8 (documentati in `docs/METEORISK_LIGHT_1.0.0.8_IMPLEMENTATION_REPORT.md`):
1) fix fascia oraria (helper puri + dropdown) · 2) fingerprint Best Match
deterministica e content-sensitive · 3) matrice decision engine stateless +
bootstrap (casi A/B/C) · 4) stato bootstrap (G3/G5) · 5) budget: rc 2 safe-skip /
rc 3+4 bootstrap FATAL · 6) gate best/plan/fetch (rc contract) · 7) fetch
stateless + budget-before-retry (casi D/E) ·
8) workflow bytecheck 1.0.0.8.

Regressioni 1.0.0.7: golden sampling (v1=265/v2=257) · negativi 3/3 · unit
planner/budget 9/9 · contract loader Node · `node --check` · workflow gate A–F.

Dettagli: `docs/METEORISK_LIGHT_1.0.0.8_IMPLEMENTATION_REPORT.md`,
`docs/API_BUDGET_MANAGEMENT.md`, `docs/CENTRALIZED_DATA_PIPELINE.md`.