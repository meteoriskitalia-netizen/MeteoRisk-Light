# MeteoRisk Light — edizione 1.0.0.6 (GitHub Production Hardening)

Pannello meteorologico statico per le province italiane. La 1.0.0.6 è una release
conservativa e focalizzata sul passaggio a **produzione GitHub**: distinzione
obbligatoria fra nuovo run / nessun nuovo run / errore tecnico, workflow riordinato,
repository ripulito. La pipeline e l'app della 1.0.0.5 NON sono state riscritte.

- App: `mri-light-1.0.0.6.html` (nessuna dipendenza runtime; solo static assets).
- Dati: dataset derivato in `data/latest/` generato offline da `scripts/*.py`.
- Fonti: Open-Meteo (unica fonte meteorologica, input — MAI ripubblicata come tale).

## FIX di questa release
1. **Error handling obbligatorio**: `check` 0 = nuovo run / **10 = clean success
   (zero lavoro)** / **1+ = workflow FAIL** (mai silenziato, mai confuso con "no new
   run"). Classificazione centralizzata e testata in `scripts/workflow_gate.py`.
2. **Ordine workflow**: punti (se necessari) → **check run** → request planner →
   fetch → build → validate → publish → commit → deploy. Nessuna pianificazione
   senza un nuovo run.
3. **Planner**: PLAN VALID · BUDGET BLOCKED (safe success, last known good
   preservato) · TECHNICAL ERROR (fail).
4. **Repo pulito**: `data/_raw`, `data/_staging`, `data/_workdir` fuori da Git,
   Pages e pacchetto finale (solo `.gitkeep`); artefatto Pages = app + dataset
   derivati + geografie richieste.
5. **No deploy su no-op**: commit e deploy solo con dataset nuovo valido.

## Struttura
```
.github/workflows/update-weather-data.yml   # pipeline hardening (exit code audit)
scripts/                                    # pipeline + workflow_gate + test
data/latest/                                # dataset pubblicato (status live)
data/state/                                 # last_model_run.json, api_usage.json
data/geography/                             # boundaries richieste dal sito
docs/                                       # report, budget API, pipeline, licenze
```

## Efficienza (invariata)
257 punti → **6 richieste** Open-Meteo (+97.67%): dedup (0 duplicati), batch a 100
× 2 leg modello, pre-flight budget (limite 10000/giorno, riserva 10%,
`data/state/api_usage.json`).

## Test
golden sampling (v1=265/v2=257) · negativi 3/3 · unit planner/budget 9/9 ·
contract loader Node · `node --check` main script · **workflow gate A–F**
(`scripts/tests/test_workflow_gate.py`) · E2E reale.

Dettagli: `docs/METEORISK_LIGHT_1.0.0.6_IMPLEMENTATION_REPORT.md`,
`docs/METEORISK_LIGHT_1.0.0.5_IMPLEMENTATION_REPORT.md`,
`docs/API_BUDGET_MANAGEMENT.md`.