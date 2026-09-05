# METEORISK LIGHT — API BUDGET MANAGEMENT
## Telemetria e protezione del consumo Open-Meteo (pipeline 1.0.0.5 · 1.0.0.6 hardening)

Questo documento descrive il meccanismo di budget/usage della pipeline: nessuna richiesta
viene emessa verso l'API forecast Open-Meteo senza aver superato un **pre-flight** che legge
il consumo persistito nel giorno corrente.

---

### 1. Costanti e sovrascritture d'ambiente
Definite in `scripts/common.py`; ogni valore è sovrascrivibile con variabili `METEO_RISK_*`:

| Costante | Default | Ruolo |
|---|---|---|
| `API_DAILY_LIMIT` | 10000 | massimo richieste forecast consentite/giorno |
| `API_SAFETY_RESERVE_FRAC` | 0.10 | riserva di sicurezza sull'effettivo usabile |
| `BATCH_MAX_LOCATIONS` | 100 | località per richiesta forecast (calibrato: 100 → ~4.1 MB) |
| `API_MIN_REQUEST_INTERVAL_S` | 30.0 | pacing minimo tra richieste |
| `RETRY_LIMIT` | 3 | tentativi per richiesta fallita (errore / 5xx) |
| `RETRY_BACKOFF_BASE_S` | 5.0 | delay base del backoff esponenziale (5·2^i, cap) |
| `API_USAGE_JSON` | `data/state/api_usage.json` | file di stato persistente |
| `API_EFFICIENCY_DIR` | `data/_workdir/api_efficiency/` | report telemetria per run/plan |

Il limite effettivo usabile è `API_DAILY_LIMIT * (1 - API_SAFETY_RESERVE_FRAC)`
(es. 10000 → **9000**/giorno). La riserva copre fallback, retry e riaperture.

### 2. Stato persistente `data/state/api_usage.json`
```jsonc
{
  "schema_version": 1,
  "last_update": "2026-09-05T21:58:43Z",
  "days": {
    "2026-09-05": {
      "requests": 6, "failed": 0, "batches": 3,
      "locations": 257, "bytes": 8848830
    }
  }
}
```
- **requests** = richieste di rete forecast (batch × 2 leg modello); **failed** =
  richieste fallite; **batches** = blocchi emessi; **locations** = località totali;
  **bytes** = volume raw generato (informazione).
- Rollover giornaliero: la chiave giorno = data UTC corrente; il budget si "azzera"
  girando alla nuova chiave (i giorni precedenti restano come storico).
- I conteggi sono **corretti per leg** (una richiesta per `n_model_legs` per batch,
  senza contare i retry). Verificato sulla run reale: 3 batch × 2 = **6 requests**.

### 3. Pre-flight bloccante
1. `request_planner.py` calcola il piano (dedup + batch + leg) e il costo;
   legge `usage_today()`; **exit 2** se `piano > disponibile` (nessun download avviato).
2. `fetch_source_data.py` ripete il pre-flight (**exit 2**), quindi emette le richieste
   con pacing `API_MIN_REQUEST_INTERVAL_S` e al termine registra `record_api_usage`.
3. Il workflow valuta gli exit code: 2 ⇒ build/validate/publish SKIP con avviso nel
   summary (mai errori silenziati).

Esempio reale (2026-09-05):
```
limite=10000 riserva=10% effettivo=9000 · usato=6 · disponibile=8994
PRE-FLIGHT OK: 6 pianificate <= 8994
```

### 4. Telemetria efficienza (`data/_workdir/api_efficiency/`)
- `request_plan.json` — piano persistito dal planner (naive vs ottimizzate, batch).
- `fetch_<timestamp>.json` — report esecuzione: `naive_requests=257`,
  `optimized_requests=6`, `requests_saved=251`, `efficiency_gain_pct=97.67`,
  `batch_size=100`, `n_model_legs=2`, `elapsed_s=153.2`, `ok/failed points`, `raw_bytes`,
  `usage_after`.

### 5. Perché l'efficienza conta
- Naive 1.0.0.4: 257 richieste per run. Ottimizzato: **6**. Con cadenza aggiornamento
  driver di 12 h → ~12 richieste/giorno vs 9000 disponibili (margine enorme; la riserva
  resta per eventi anomali).
- Dedup coordinate garantisce zero richieste ridondanti anche se `real_points.json`
  contiene osservazioni ripetute.

### 6. Linee guida operative
- Abilitare sovrascritture solo per test mirati (es. `METEO_RISK_API_MIN_REQUEST_INTERVAL_S=5`
  su IP runner dedicato); in locale mantenere i default per non incappare nel limite
  minutely (~5 richieste/min da IP condiviso).
- `--skip-preflight` esiste SOLO per debug locale e **non** va usata nell'Action.
- Se `api_usage.json` viene cancellato, il giorno corrente riparte da zero (autodifesa).
- I report in `api_efficiency/` sono ignoti al VCS (`.gitignore`).