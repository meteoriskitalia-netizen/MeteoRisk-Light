# METEORISK LIGHT — API USAGE GUARDRAILS

## Osservabilità + protezioni, non razionamento (1.0.0.8 production hardening)

Questo documento descrive le **API usage guardrails** della pipeline: protezione
contro loop, retry infiniti, fetch duplicati, richieste massive accidentali,
scheduler runaway ed errori di configurazione — con un **unico hard safety
ceiling** centralizzato. Sostituisce la vecchia logica di "budget/riserva
personale" del 1.0.0.5–1.0.0.7: **mai** un blocco preventivo per risparmiare la
riserva; il fetch reale (nuovo run ECMWF / Best Match cambiato / bootstrap)
parte normalmente finché il tetto di sicurezza non è effettivamente raggiunto.

---

### 1. Configurazione centralizzata (un unico luogo)

Definita in `scripts/common.py` (`API_GUARDRAILS_DEFAULT`, sovrascrivibile con
variabili `METEO_RISK_API_DAILY_LIMIT` e `METEO_RISK_SAFETY_RESERVE_FRAC`) e
persistita, come runtime, in `data/state/api_usage.json`:

```jsonc
{
  "api_usage_guardrails": {
    "enabled": true,
    "daily_safety_ceiling": 10000,   // TETTO DURO: mai oltre questo numero di richieste/g
    "warn_threshold_fraction": 0.8,  // soglia OSSERVABILITA': warning, MAI blocco
    "hard_stop_enabled": true        // beyond-ceiling => safe skip / FAIL bootstrap
  },
  "days": { ... },
  "last_update": null
}
```

Nessun limite è hardcodato negli script: tutti leggono `common.guardrails()`.
Un eventuale `api_usage.json` del vecchio schema (`daily_limit`) viene migrato
automaticamente a `api_usage_guardrails.daily_safety_ceiling` al primo load.

### 2. Cosa viene CONTEGGIATO (osservabilità giornaliera, chiave = data UTC)

| Contatore | Chi lo alimenta | Significato |
|---|---|---|
| `checks` | `check_model_runs.py` | Metadata API (leggero, MAI usato per bloccare) |
| `canary_requests` | `check_best_match.py` | 1 canary sentinel (weathercode+prec) |
| `forecast_requests` | `fetch_source_data.py` | richieste forecast reali (batch × leg) |
| `retries` | `get_model_metadata` (tentativi-1) | retry automatici su errori transienti |
| `successful` / `failed` | fetch | esito delle richieste forecast |
| `requests` | tutti i precedenti | totale richieste (base del ceiling) |
| `batches` / `locations` / `bytes` | fetch | volume (informazione) |

### 3. Cosa PUÒ bloccare (e cosa NO)

- **PUÒ bloccare** — `guard_planned_requests(planned)` (pre-flight in
  `decide_cycle.py`, `request_planner.py`, `fetch_source_data.py`):
  - `hard_stop_enabled=true` **e** `usate_today + pianificate > daily_safety_ceiling`
    → il fetch NON parte: `exit 2` (steady, safe skip, last known good preservato)
    oppure `exit 3` (piano) / `exit 4` (fetch) in **bootstrap** = workflow FAIL.
  - Canary sentinelle: stessa guardia (`available_today() < 1` → `exit 10`, skip).
- **NON blocca MAI** (solo warning in log + summary):
  - consumi alti **sotto** il tetto, anche oltre `warn_threshold_fraction`
    (`[guardrails] OSSERVAZIONE (nessun blocco): ...`);
  - contatori di osservabilità (`checks`, `canary_requests`, `forecast_requests`,
    `retries`, `successful`, `failed`): registrano e basta;
  - la **Metadata API** (run detection): non è conteggiata nel ceiling.

### 4. Protezione da runaway / loop / retry infiniti

- Retry limitati (mai infiniti) in un unico client (`fetch_source_batch`,
  `get_model_metadata`): 429/5xx/Timeout/SSL con `RETRY_LIMIT=3` +
  exponential backoff + jitter (Parte H). Sotto il tetto i retry contano nel
  ceiling previsto (`worst = planned × RETRY_LIMIT`, informativo).
- Pacing minimo tra richieste (`API_MIN_REQUEST_INTERVAL_S`) anti minutely-limit.
- Pipeline **idempotente**: senza un nuovo run ECMWF / Best Match cambiato →
  **clean exit, zero fetch** (cron `*/10 * * * *` rialzato per rilevare SOLO
  cambi reali).
- Nessun fetch "totale" se più del tetto verrebbe superato; nessuna ripetizione
  integrale del piano su errori (retry **selettivi** sui soli batch falliti).

### 5. Telemetria separata dai blocchi

- `data/state/api_usage.json` — contatori giornalieri (sezione 2), rollover alla
  data UTC; giorni precedenti conservati come storico.
- `data/_workdir/api_efficiency/` — `request_plan.json`, `fetch_<ts>.json`
  (naive vs ottimizzate, batch, elasticità), entrambi esclusi dal VCS e dal
  publish.
- Il **blocco** è deciso dal solo pre-flight guardrail (sezione 3); l'
  **osservabilità** registra senza mai influenzare la decisione.

### 6. Riferimenti

- `scripts/common.py` → `API_GUARDRAILS_DEFAULT`, `guardrails()`,
  `guard_planned_requests()`, `record_api_usage()`, `usage_today()`.
- `scripts/decide_cycle.py` (exit 2/3), `scripts/fetch_source_data.py` (2/4),
  `scripts/check_best_match.py` (10 = skip), `scripts/request_planner.py` (2).
- `scripts/workflow_gate.py` — classificazione: chiavi macchina stabili
  (`budget_blocked`, `bootstrap_budget_blocked`), messaggi guardrails.